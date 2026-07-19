"""Chat generation dispatch (U4) — consent + egress scoping + delegation.

Turns a stripped :class:`~screencap.recall.orchestrator.EvidenceBundle` (from U3)
into a grounded :class:`ChatAnswer`. This is where the trust model becomes
behavioral (KTD3): the evidence BOUND is already structural (U3 emits an ALLOW-only
bundle), and here we enforce faithful USE of that bound —

1. **Per-turn consent (KTD1/KTD6).** Resolve the ``RECALL_ANSWER`` execution target
   from :class:`~screencap.segmentation.consent.ConsentPolicy` on THIS call, for
   REPORTING + egress gating. On-device *generation* is not available in this tree,
   so the dispatch resolves with ``on_device_available=False`` — the same
   cloud-fallback resolution :func:`~screencap.segmentation.recall.answer_recall`
   uses internally. The target can flip on-device→cloud between turns as consent
   changes, so it is recomputed every turn and never reused.

2. **Bundle-only evidence text (KTD3).** The evidence text is built STRICTLY from
   the bundle: the rendered snippet lines (``[i] <text>``, newlines neutralized) plus
   the figure lines. That text is the ONLY captured content that egresses besides the
   question + prompt scaffold — a snippet that tries to inject instructions ("ignore
   prior instructions …") is inert data here; the grounding framing that mitigates
   semantic injection is owned by main's prompt builder, not this module.

3. **Generation via main's seam (SCR-243).** The model call is DELEGATED to
   :func:`screencap.segmentation.recall.answer_recall` — the single recall entry
   point, which runs main's fail-closed stripped gate, the on-device chain, and the
   consented-cloud fallback. This module does NOT own the prompt: it mints an
   :class:`~screencap.segmentation.generation.Evidence` (``stripped=True``; the
   dispatch is the consumer that is allowed to set that marker) and hands it to
   ``answer_recall``. An empty/insufficient bundle refuses BEFORE the delegated call
   — the dispatch never lets a provider fabricate over no evidence. A two-state
   return (``str`` answer | ``PROVIDER_UNAVAILABLE``) maps a non-``str`` to a refusal.

4. **Answer-side attribution (KTD3).** Run the model's prose through
   :func:`~screencap.recall.attribution.validate_attribution`; a failing verdict
   BLANKS the answer to a canonical refusal. The surviving answer carries the
   attribution verdict's source pointers.

5. **Whole-payload egress guard (defense-in-depth).** BEFORE the delegated call,
   :func:`assert_cloud_payload_bounded` asserts the EXACT payload main will build
   (:func:`~screencap.segmentation.generation_finish.build_answer_prompt`) is ⊆ (the
   fixed grounding scaffold + the bundle's snippet/figure text + the user's own
   question) — no frame bytes, no un-retrieved history, no client-supplied prose. It
   now runs on EVERY turn (not only cloud) as a belt-and-suspenders check on the
   content that could egress; on a breach the dispatch refuses. It is a REAL function
   the tests call, not a comment.

Architecture: like ``orchestrator``, this module MUST NOT import
``screencap.daemon.app`` (U5 imports this). It imports only the sibling recall /
segmentation modules (all cloud-free at import time) and reaches ``answer_recall``
lazily.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from screencap.recall.attribution import validate_attribution
from screencap.content_index import IndexState
from screencap.recall.orchestrator import (
    CoverageDescriptor,
    EvidenceBundle,
    EvidencePointer,
    QuestionKind,
    Stream,
)
from screencap.segmentation.consent import (
    ConsentPolicy,
    ExecutionTarget,
    TaskKind,
    frames_may_attach,
)
from screencap.segmentation.generation import (
    Evidence,
    MaskedFrame,
    ProviderUnavailable,
    verify_masked_frames,
)
from screencap.segmentation.generation_finish import (
    _GROUNDING_INSTRUCTIONS,
    build_answer_prompt,
)

logger = logging.getLogger(__name__)


# The canonical refusal the dispatch surfaces when there is no evidence, no
# execution target, the egress guard trips, or the attribution validator rejects the
# model's answer. Its phrasing is a refusal marker (see attribution.is_refusal) so a
# re-validation of the refusal itself passes.
REFUSAL_TEXT = "I don't have that in your recorded history."

# The refusal surfaced when evidence WAS found but no answer could be produced (an
# attribution rejection on the strict path, a model self-refusal over a non-empty
# bundle, or no backend with evidence present). Distinct from REFUSAL_TEXT so the
# user is never told their history is missing when activity was actually found
# (KTD3/R5). Its phrasing is a refusal marker (attribution._REFUSAL_MARKERS carries
# "put together an answer") so a re-validation of it passes.
REFUSAL_TEXT_FOUND_NO_ANSWER = (
    "I found activity from that time but couldn't put together an answer for it."
)

# Refusal reasons (U2) — a distinct code per refusal BRANCH so the client can render
# the honest state instead of collapsing every refusal into "not in your history":
#   * no_backend  — the provider chain (on-device, then consented cloud) was
#                   unavailable; nothing could generate an answer.
#   * no_evidence — the bundle was empty (no evidence, no figures), or the model
#                   itself refused over the retrieved evidence.
#   * unsupported — the answer-side attribution validator rejected the model's prose.
#   * blocked     — the whole-payload egress guard tripped (a safety refusal).
# ``reason`` is ``None`` on a successful answer.
REASON_NO_BACKEND = "no_backend"
REASON_NO_EVIDENCE = "no_evidence"
REASON_UNSUPPORTED = "unsupported"
REASON_BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Result type (U5 serializes this)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatAnswer:
    """The typed result of one chat turn (U5 serializes it into the daemon envelope).

    * ``answer`` — the grounded prose, or :data:`REFUSAL_TEXT` on a refusal.
    * ``sources`` — the evidence pointers backing the answer (the sources panel).
      Pointer-only (recording + timestamp_ms + stream); no bytes.
    * ``coverage`` — the bundle's honest coverage descriptor (R12), passed through.
    * ``target`` — the execution target this turn RESOLVED to (on-device / cloud /
      none), recomputed per turn.
    * ``refusal`` — whether the answer is a refusal (empty bundle, no target, or an
      attribution rejection). U5/U8 render a refusal distinctly.
    * ``question_kind`` — point vs aggregate (carried through from the bundle).
    * ``reason`` — WHY a refusal happened (``no_backend`` / ``no_evidence`` /
      ``unsupported`` / ``blocked``), so the client renders the honest state rather
      than collapsing every refusal into one message. ``None`` on a real answer.
    """

    answer: str
    sources: list[EvidencePointer]
    coverage: CoverageDescriptor
    target: ExecutionTarget
    refusal: bool
    question_kind: QuestionKind = QuestionKind.POINT
    reason: str | None = None


# ---------------------------------------------------------------------------
# Evidence-text rendering — snippets + figures, bundle-derived ONLY
# ---------------------------------------------------------------------------


def _fmt_minutes(ms: float) -> str:
    """Whole-minute rendering for the model ("50 minutes", "under 1 minute").

    Rounded ints, never decimals — a model echoes these numbers verbatim, and
    "5.7 minutes" reads like telemetry, not an answer about someone's day.
    ``attribution._figure_numbers`` mirrors this rendering so an echoed figure
    counts as backed; keep the two in lockstep.
    """
    minutes = round(ms / 60_000.0)
    if minutes < 1:
        return "under 1 minute"
    if minutes == 1:
        return "1 minute"
    return f"{minutes} minutes"


def _figure_lines(figures: object | None) -> list[str]:
    """Render a computed :class:`WindowAggregate` (duck-typed) into human figure
    lines for the evidence text. Never a hard import of U2.

    Focus time (wall-clock in front of the app, within recorded coverage) is the
    headline per-app figure; the corroborated-active figure rides alongside as
    the interaction qualifier. The longest-focused window titles give the model
    something concrete to say about WHAT was happening, not just where.
    """
    if figures is None:
        return []
    lines: list[str] = []
    recorded_ms = getattr(figures, "recorded_ms", None)
    covered_ms = getattr(figures, "covered_active_ms", None)
    uncovered_ms = getattr(figures, "uncovered_ms", None)
    if isinstance(recorded_ms, (int, float)) and recorded_ms > 0:
        lines.append(f"screen time recorded in this period: {_fmt_minutes(recorded_ms)}")
    if isinstance(covered_ms, (int, float)):
        lines.append(
            f"computed active time (over covered spans): {_fmt_minutes(covered_ms)}"
        )
    if isinstance(uncovered_ms, (int, float)) and uncovered_ms > 0:
        lines.append(
            f"of the recorded time, {_fmt_minutes(uncovered_ms)} had no captured "
            "interaction (reading / watching / idle)"
        )
    for app in getattr(figures, "apps", []) or []:
        name = getattr(app, "app", None)
        covered = getattr(app, "covered_active_ms", None)
        count = getattr(app, "event_count", None)
        if not isinstance(name, str) or not isinstance(covered, (int, float)):
            continue
        display = getattr(app, "display_name", "") or name
        focus = getattr(app, "focus_ms", None)
        extra = f", {count} input events" if isinstance(count, int) else ""
        if isinstance(focus, (int, float)) and focus > 0:
            line = (
                f"{display}: {_fmt_minutes(focus)} in focus "
                f"({_fmt_minutes(covered)} actively interacting{extra})"
            )
        else:
            line = f"{name}: {_fmt_minutes(covered)} active{extra}"
        titles = [t for t in (getattr(app, "top_titles", None) or []) if isinstance(t, str) and t]
        if titles:
            # Quoted so a title containing a separator can't read as two titles.
            line += "; windows: " + ", ".join(f'"{t}"' for t in titles)
        lines.append(line)
    return lines


def _evidence_text(bundle: EvidenceBundle) -> str:
    """Render the bundle's evidence into the text handed to the generation seam.

    The snippet lines (``[i] <text>`` with newlines neutralized so a snippet cannot
    fake structure) followed by the rendered figure lines, joined with newlines. This
    is the ONLY captured content that reaches the model (besides the question and the
    fixed grounding scaffold main prepends), so it is bundle-derived only (KTD3)."""
    lines: list[str] = []
    if bundle.evidence:
        for i, item in enumerate(bundle.evidence, start=1):
            # One snippet per line; newlines within a snippet are neutralized.
            text = " ".join((item.text or "").splitlines())
            lines.append(f"[{i}] {text}")
    lines.extend(_figure_lines(bundle.figures))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Whole-payload egress guard
# ---------------------------------------------------------------------------


class EgressViolation(Exception):
    """Raised when the outbound payload carries content NOT derived from the
    bundle — un-retrieved history, client-supplied prose, or frame bytes. Fail
    closed: the dispatch catches it and refuses rather than egressing (R8/R10)."""


# Byte markers that must NEVER appear in a text-only prompt (a frame leaking in as
# raw bytes). JPEG SOI / EXIF, PNG signature, GIF headers.
_FRAME_BYTE_MARKERS = ("\xff\xd8\xff", "\x89PNG", "GIF87a", "GIF89a")

# Tokenizer for the containment check — the same substantive-token notion the
# attribution validator uses, so "only bundle-derived text" is checked at word
# granularity (punctuation / whitespace differences don't cause false positives).
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9#'/.-]*")

# The structural snippet-index markers the evidence renderer emits ("[1] ", "[2] ").
# Stripped before the containment check so they don't leak a bare digit token.
_INDEX_MARKER_RE = re.compile(r"\[\d+\]\s?")

# The label main's build_answer_prompt prefixes the user's question with, so the
# egress guard can recover the question from a finished prompt (the question is the
# user's own input, legitimately exempt from the bundle-only bound).
_QUESTION_LABEL = "QUESTION:"


def _extract_question_line(payload: str) -> str:
    """Recover the user's question from a finished answer prompt's ``QUESTION:``
    block. Main emits the question on the line(s) AFTER the ``QUESTION:`` label and
    before the ``ANSWER:`` label; returns "" when absent."""
    lines = payload.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == _QUESTION_LABEL:
            out: list[str] = []
            for follow in lines[i + 1 :]:
                if follow.strip() == "ANSWER:":
                    break
                out.append(follow)
            return "\n".join(out).strip()
    return ""


# Tokens that are part of the FIXED grounding scaffold main emits (its
# instructions + the EVIDENCE/QUESTION/ANSWER labels), always allowed in the payload
# regardless of the bundle. Derived from main's ``_GROUNDING_INSTRUCTIONS`` so the
# allowed set can't drift from the prompt text — if it did, main's grounding words
# would be flagged as leaked and every answer would refuse.
_SCAFFOLD_TOKENS = frozenset(
    _TOKEN_RE.findall(_GROUNDING_INSTRUCTIONS.lower())
) | frozenset(
    # main's structural labels + the structural words the figure renderer emits.
    ["evidence", "question", "answer", "computed", "minutes", "events",
     "active", "covered", "uncovered", "idle", "away", "captured", "spans",
     "span", "not", "screen", "time", "recorded", "period", "focus",
     "actively", "interacting", "input", "windows", "under", "reading",
     "watching", "interaction", "had"]
)


def _bundle_tokens(bundle: EvidenceBundle) -> set[str]:
    """Every substantive token the bundle can legitimately contribute to a payload:
    snippet text + rendered figure lines. This is the allowed egress vocabulary."""
    text = " ".join(item.text for item in bundle.evidence)
    text += " " + " ".join(_figure_lines(bundle.figures))
    # The question is the user's own input, not captured history; but we bound to
    # BUNDLE-derived content only, so the question's novel tokens are checked too —
    # a caller that wants the question exempt passes it via assert_cloud_payload_bounded.
    return set(_TOKEN_RE.findall(text.lower()))


def assert_cloud_payload_bounded(
    payload: str,
    bundle: EvidenceBundle,
    *,
    question: str = "",
    masked_frames: "tuple[MaskedFrame, ...]" = (),
) -> None:
    """Assert the WHOLE payload is bounded to the re-derived bundle (R8/R10).

    Checks, in order:

    * **No frame bytes in the TEXT channel** — a text prompt must never carry image
      magic bytes (a frame smuggled in as raw text).
    * **Vetted frame channel (SCR-272)** — every attached :class:`MaskedFrame` must
      carry the masked-verified provenance marker: the frames ride to the provider
      through the typed multimodal channel, so the whole-payload bound now covers
      them too, not only the text. A frame that is not a ``MaskedFrame(masked=True)``
      (the U1 producer's blessed mint) → :class:`EgressViolation`.
    * **Whole-payload containment** — every substantive token in ``payload`` must be
      either (a) part of the fixed grounding scaffold / labels, (b) a token of the
      bundle's snippet/figure text, or (c) a token of the user's own ``question``. A
      token outside all three is un-retrieved history / client prose / injected
      content → :class:`EgressViolation`.

    Raises :class:`EgressViolation` on any breach. The dispatch calls this on every
    turn BEFORE the delegated model call; the tests call it directly. This bounds the
    ENTIRE outbound payload — the text string AND the frame channel — not just the
    server-resolved snippet list.
    """
    for marker in _FRAME_BYTE_MARKERS:
        if marker in payload:
            raise EgressViolation("cloud payload carries frame/image bytes")

    # Vet the typed multimodal channel: the same fail-closed checkpoint both
    # provider paths run, hoisted here so recall's whole-payload bound rejects an
    # unmarked/raw frame BEFORE the delegated model call. A TypeError from the
    # verifier is a bound breach → refuse (fail-closed), never egress.
    try:
        verify_masked_frames(tuple(masked_frames))
    except TypeError as exc:
        raise EgressViolation(
            f"cloud payload carries an unverified frame ({exc})"
        ) from exc

    # The user's own question is legitimate payload content (the operator's words,
    # not captured history). It reaches the guard either explicitly (``question``) or
    # embedded by build_answer_prompt in the QUESTION: block — accept both so the
    # guard can be called on the finished prompt with no separate question arg.
    question_text = question or _extract_question_line(payload)
    allowed = _SCAFFOLD_TOKENS | _bundle_tokens(bundle) | set(
        _TOKEN_RE.findall(question_text.lower())
    )
    # Strip the structural snippet-index markers ("[1] ", "[2] ") the evidence
    # renderer emits before tokenizing — they are scaffold, not content, and would
    # otherwise leak a bare digit token. Only the fenced-list markers are removed.
    scrubbed = _INDEX_MARKER_RE.sub("", payload)
    payload_tokens = set(_TOKEN_RE.findall(scrubbed.lower()))
    leaked = payload_tokens - allowed
    if leaked:
        # Content-free diagnostic: COUNT of leaked tokens only, never their text
        # (they may be un-retrieved captured content).
        raise EgressViolation(
            f"cloud payload carries {len(leaked)} token(s) not derived from the "
            "bundle, the instructions, or the question (un-retrieved history / "
            "client prose)"
        )


# ---------------------------------------------------------------------------
# The dispatch
# ---------------------------------------------------------------------------

# The delegated model call: ``(question, evidence) -> str | PROVIDER_UNAVAILABLE``.
# Default is main's recall entry point (answer_recall); tests inject a fake.
AnswerFn = Callable[[str, Evidence], "str | ProviderUnavailable"]


def _default_answer_fn(question: str, evidence: Evidence) -> str | ProviderUnavailable:
    """Delegate to main's single recall entry point, imported lazily so this
    module stays import-light and cloud-free at load (and never pulls
    ``screencap.daemon.app``)."""
    from screencap.segmentation.recall import answer_recall

    return answer_recall(question, evidence)


def _refusal(
    bundle: EvidenceBundle, target: ExecutionTarget, *, reason: str
) -> ChatAnswer:
    """A canonical refusal ChatAnswer — no sources, no leak, tagged with WHY.

    The message is chosen on whether evidence was actually found, NOT on ``reason``
    (KTD3/R5). ``REASON_NO_EVIDENCE`` is overloaded — it fires both for a genuinely
    empty bundle (step 2) and for a model self-refusal over a NON-empty bundle
    (step 8) — so keying the empty-history string on the reason code alone would
    wrongly tell the user "not in your recorded history" after activity WAS found.
    The empty-history string is reserved for a genuinely empty bundle; every other
    refusal surfaces the found-but-unanswerable string.
    """
    bundle_empty = not bundle.evidence and bundle.figures is None
    answer = REFUSAL_TEXT if bundle_empty else REFUSAL_TEXT_FOUND_NO_ANSWER
    return ChatAnswer(
        answer=answer,
        sources=[],
        coverage=bundle.coverage,
        target=target,
        refusal=True,
        question_kind=bundle.question_kind,
        reason=reason,
    )


# The IndexState per-stream values that mean a stream ACTUALLY served results (ran a
# search and either matched or honestly returned nothing) — as opposed to
# not_indexed / store_unavailable / index_degraded, which mean the stream could not
# be served. Load-bearing for the recap predicate below (content_index defaults OFF).
_STREAM_SERVED = frozenset({IndexState.OK.value, IndexState.NO_MATCH.value})


def _is_recap_bundle(bundle: EvidenceBundle) -> bool:
    """True when ``bundle`` is a day-recap that gets the SOFTENED attribution gate
    (KTD1). Two shapes qualify:

    * an AGGREGATE bundle carrying no content/transcript evidence items (its figures
      are a timeline summary), or
    * a non-empty bundle whose every evidence item is ``Stream.TIMELINE`` AND whose
      content AND transcript streams were actually searched (coverage ``ok`` /
      ``no_match``), not merely un-indexed.

    The coverage guard is load-bearing: ``content_index_enabled`` defaults OFF, so a
    timeline-only bundle usually means "content was never searched", not "this is a
    recap". Without the guard the softened gate would swallow ordinary point lookups
    and silently disable the strict validator (R2/AE2). Genuine recaps whose content
    stream is un-indexed are still routed here by the AGGREGATE classifier arm.
    """
    evidence = bundle.evidence
    has_non_timeline = any(item.stream is not Stream.TIMELINE for item in evidence)
    if bundle.question_kind is QuestionKind.AGGREGATE:
        return not has_non_timeline
    if not evidence or has_non_timeline:
        return False
    per_stream = bundle.coverage.per_stream
    return all(
        per_stream.get(stream) in _STREAM_SERVED
        for stream in (Stream.CONTENT.value, Stream.TRANSCRIPT.value)
    )


def _answered_target(reporting_target: ExecutionTarget) -> ExecutionTarget:
    """The target to REPORT for a successful answer — never ``NONE`` (KTD5).

    The dispatch resolves ``reporting_target`` with ``on_device_available=False``,
    which is ``NONE`` on a machine with no consented cloud. But a produced answer
    means the provider chain succeeded: ``answer_recall`` tries on-device first and
    only falls back to consented cloud, so a ``str`` result with no cloud target
    must have come from the on-device chain. Report ``CLOUD`` when cloud was the
    consented target, else ``ON_DEVICE`` — never the misleading ``NONE`` that would
    tag a real answer as "no backend".

    Known imprecision (deliberate, privacy-conservative): when cloud IS consented,
    ``answer_recall`` still tries on-device FIRST, so a locally-produced answer is
    reported as ``CLOUD``. That over-claims egress — the SAFE direction: it never
    falsely tells the user their data "stayed on this Mac". Reporting the exact
    producing stage would require ``answer_recall`` to surface it (its contract is
    ``str | PROVIDER_UNAVAILABLE`` today); deferred. U8's honest-state selector keys
    off ``refusal``/``reason``, not ``target``, so this label has no honest-state
    impact.
    """
    if reporting_target is ExecutionTarget.CLOUD:
        return ExecutionTarget.CLOUD
    return ExecutionTarget.ON_DEVICE


# ---------------------------------------------------------------------------
# Masked-frame egress (SCR-272 U5) — attach frames ONLY at the cloud-bound point
# ---------------------------------------------------------------------------


def _load_corpus_key() -> "bytes | None":
    """The recording corpus key when stills are stored encrypted, else ``None``.

    Mirrors the chunk-processor / index pass: only when ``corpus_encrypted`` is on
    are stills ``*.jpg.enc`` and a key needed; plaintext stills read fine with
    ``None`` (:func:`produce_egress_frames` globs ``*.jpg.enc`` only when a key is
    supplied). Best-effort — a missing key returns ``None`` (the encrypted stills
    are then simply not globbed, so no frame leaves unreadable)."""
    from screencap import config

    if not config.get_corpus_encrypted():
        return None
    from screencap import corpus_crypto

    return corpus_crypto.load_corpus_key()


def _cloud_provider_supports_frames(policy: ConsentPolicy) -> bool:
    """Whether the policy's configured cloud provider is vision-capable (U4).

    Resolves the provider by its configured name and reads the ``supports_frames``
    capability (only Gemini is ``True`` today). Fail-closed: an unset / unknown
    provider, or any resolution error, means "no frames" (``False``)."""
    name = policy.cloud_provider
    if not name:
        return False
    try:
        from screencap.segmentation.provider import get_provider

        provider = get_provider(name)
    except Exception:
        return False
    return bool(getattr(provider, "supports_frames", False))


def _build_recall_frames(
    bundle: EvidenceBundle, recordings_dir: "Path | None"
) -> "tuple[MaskedFrame, ...]":
    """Build U1's masked frames for the retrieved snippets' INDIVIDUAL timestamps.

    Each surviving evidence item carries a ``(recording, timestamp_ms)`` pointer.
    We group by recording and hand :func:`produce_egress_frames` the exact snippet
    timestamps via ``only_timestamps`` (NOT the ``[min, max]`` span), so a moment
    inside the span that was never retrieved never ships. The ALLOW-only /
    fail-closed structural gate inside the producer is untouched. Returns the
    provenance-marked frames (masked=True) across all recordings."""
    from screencap.recall.orchestrator import _resolve_recording_dir
    from screencap.segmentation.frame_egress import (
        _EGRESS_ONLY_TS_TOLERANCE_S,
        produce_egress_frames,
    )

    by_recording: dict[str, list[float]] = {}
    for item in bundle.evidence:
        by_recording.setdefault(item.recording, []).append(item.timestamp_ms / 1000.0)

    corpus_key = _load_corpus_key()
    tol = _EGRESS_ONLY_TS_TOLERANCE_S
    frames: list[MaskedFrame] = []
    for recording, tss in by_recording.items():
        rec_dir = _resolve_recording_dir(recording, recordings_dir)
        if rec_dir is None:
            # Unresolvable recording name (traversal / gone) → no frames for it.
            continue
        # A tight window bounds the still glob to the retrieved timestamps ± the
        # match tolerance; ``only_timestamps`` then keeps only the near-target
        # stills. ``end`` is exclusive, so pad it past the furthest target+tol.
        start = min(tss) - tol
        end = max(tss) + tol + 1e-3
        frames.extend(
            produce_egress_frames(
                rec_dir,
                start,
                end,
                corpus_key=corpus_key,
                only_timestamps=tss,
            )
        )
    return tuple(frames)


def _maybe_build_recall_frames(
    bundle: EvidenceBundle,
    policy: ConsentPolicy,
    target: ExecutionTarget,
    recordings_dir: "Path | None",
) -> "tuple[MaskedFrame, ...]":
    """Masked frames for this turn, or ``()`` when frames must not attach.

    Frames ride ONLY when ALL hold (SCR-272 U5): the task resolved to
    :attr:`ExecutionTarget.CLOUD`, the independent frames opt-in is on and a cloud
    provider is configured (:func:`frames_may_attach`), and that provider is
    vision-capable (:func:`_cloud_provider_supports_frames`). Any false → ``()`` and
    the turn stays text-only, unchanged. Frame-building is fail-open: an egress
    error degrades to text-only rather than failing the answer (the structural
    ALLOW gate inside the producer is the privacy guarantee, not this build)."""
    if not frames_may_attach(policy, target):
        return ()
    if not _cloud_provider_supports_frames(policy):
        return ()
    try:
        return _build_recall_frames(bundle, recordings_dir)
    except Exception:
        logger.warning(
            "recall dispatch: masked-frame egress failed; sending text-only",
            exc_info=True,
        )
        return ()


def answer_from_bundle(
    bundle: EvidenceBundle,
    *,
    question: str = "",
    policy: ConsentPolicy | None = None,
    answer_fn: AnswerFn | None = None,
    recordings_dir: "Path | None" = None,
) -> ChatAnswer:
    """Turn a stripped :class:`EvidenceBundle` into a grounded :class:`ChatAnswer`.

    Args:
        bundle: the ALLOW-only evidence bundle from U3.
        question: the operator's question, forwarded to the delegated model call and
            used to exempt the user's own words from the egress bound.
        policy: the consent policy for THIS turn. Defaults to
            :meth:`ConsentPolicy.from_config` — resolved per call, never cached
            across turns (KTD6: the target can flip on-device→cloud between turns).
            Used to REPORT the resolved target and gate egress; the actual on-device
            vs cloud routing lives inside :func:`answer_recall`.
        answer_fn: the delegated ``(question, evidence) -> str | PROVIDER_UNAVAILABLE``
            model call. Defaults to :func:`screencap.segmentation.recall.answer_recall`
            (the single recall entry point); tests inject a fake so no real model runs.
        recordings_dir: recordings root for the masked-frame egress (SCR-272 U5);
            defaults to the configured dir. Passed by tests over a ``tmp_path``
            library so no real recording tree is touched.

    Returns a :class:`ChatAnswer`. Never raises for an ordinary model/API error or an
    egress breach — it degrades to a refusal (fail-closed, R8).
    """
    policy = policy or ConsentPolicy.from_config()

    # --- 1. Resolve the reporting target (egress reporting only). We NO LONGER
    # refuse on it up-front (KTD2): the target resolves with on_device_available=
    # False, which is NONE on a machine with no consented cloud — but answer_recall
    # DOES try the on-device chain first and only falls back to consented cloud, so
    # an early gate here would refuse a viable on-device answer. answer_recall
    # returns PROVIDER_UNAVAILABLE when the whole chain cannot answer; the egress
    # guard below still runs on every turn before any provider call. --------------
    reporting_target = policy.resolve(TaskKind.RECALL_ANSWER, on_device_available=False)

    # --- 2. Empty/insufficient bundle → refuse BEFORE any model call. -----------
    # An empty bundle must never reach a provider that could fabricate over it.
    if not bundle.evidence and bundle.figures is None:
        return _refusal(bundle, reporting_target, reason=REASON_NO_EVIDENCE)

    # --- 3. Build masked frames ONLY at the cloud-bound point (SCR-272 U5). ------
    # When the task resolved CLOUD, the frames opt-in is on, and the cloud provider
    # is vision-capable, attach U1's masked frames for the retrieved snippets'
    # INDIVIDUAL timestamps (never their span). Otherwise this is () and the turn is
    # text-only, unchanged. The frames ride inside Evidence.masked_frames — the
    # recall carrier answer_recall passes straight through to the provider.
    masked_frames = _maybe_build_recall_frames(
        bundle, policy, reporting_target, recordings_dir
    )

    # --- 4. Build the evidence text (bundle-derived only) + the stripped Evidence.
    # The dispatch is the CONSUMER (outside segmentation/), so it is allowed to mint
    # stripped=True — the terminal ALLOW-only strip already ran in U3. Evidence's
    # __post_init__ re-verifies the frame provenance markers (fail-closed).
    evidence = Evidence(
        text=_evidence_text(bundle), stripped=True, masked_frames=masked_frames
    )

    # --- 5. Whole-payload egress guard (defense-in-depth, every turn). ----------
    # Compute the EXACT payload main will build and bound it to the bundle + question
    # AND vet the frame channel (every attached frame must carry the masked marker).
    payload = build_answer_prompt(question, evidence)
    try:
        assert_cloud_payload_bounded(
            payload, bundle, question=question, masked_frames=masked_frames
        )
    except EgressViolation:
        logger.warning("recall dispatch: payload failed the egress guard; refusing")
        return _refusal(bundle, reporting_target, reason=REASON_BLOCKED)

    # --- 6. Delegate the model call to answer_recall (on-device, then cloud). ----
    try:
        result = (answer_fn or _default_answer_fn)(question, evidence)
    except Exception:
        logger.warning("recall dispatch: answer_fn raised; treating as unavailable")
        result = None
    if not isinstance(result, str):
        # PROVIDER_UNAVAILABLE (or a raised error mapped above) — the whole provider
        # chain could not answer. This is the honest "no backend" refusal.
        return _refusal(bundle, reporting_target, reason=REASON_NO_BACKEND)

    model_answer = result

    # --- 7. Answer-side attribution: a failing verdict is a safe refusal on the
    # strict (content-lookup) path. On a recap bundle the gate is SOFTENED (KTD2):
    # the model's grounded-but-paraphrased narrative is surfaced even when the v1
    # heuristics (verbatim-number, token-overlap) reject it — those heuristics are
    # unsatisfiable for a day-recap over sparse timeline titles. verdict.sources
    # still backs the answer, and the model-refusal passthrough below still fires.
    verdict = validate_attribution(model_answer, bundle, question=question)
    if not verdict.ok and not _is_recap_bundle(bundle):
        logger.info("recall dispatch: attribution rejected the answer (%s); refusing",
                    verdict.reason)
        return _refusal(bundle, reporting_target, reason=REASON_UNSUPPORTED)

    # A model that emitted a refusal over real evidence → honest no-evidence state.
    if _is_refusal_text(model_answer):
        return _refusal(bundle, reporting_target, reason=REASON_NO_EVIDENCE)

    # --- 8. Answered: report a target that reflects success, never NONE (KTD5). --
    return ChatAnswer(
        answer=model_answer,
        sources=list(verdict.sources),
        coverage=bundle.coverage,
        target=_answered_target(reporting_target),
        refusal=False,
        question_kind=bundle.question_kind,
        reason=None,
    )


def _is_refusal_text(answer: str) -> bool:
    from screencap.recall.attribution import is_refusal

    return is_refusal(answer)
