"""CLI-delegation backend for the provider interface (BYO cloud, U4).

Delegates a cloud-eligible task to the vendor's **own installed CLI** —
``codex`` (OpenAI), ``claude`` (Anthropic), ``gemini`` (Google) — which the user
has already signed into. ScreenCap holds no OAuth/subscription token: it shells
out to the real binary and reads only stdout, so subscription reuse rides each
vendor's sanctioned non-interactive surface (KTD1). Anthropic explicitly
sanctions subprocess use of the real ``claude`` binary; Codex documents the same
boundary. The daemon runs as the user (same EUID), so each CLI resolves its own
credentials.

Vendor invocations (KTD1, KTD5)
-------------------------------
- ``openai-cli`` → ``codex exec -m <model> <prompt>`` — plain ``codex exec``
  prints only the final agent message to stdout. NOT ``--json`` (that is a JSONL
  *event* stream, silently ignored when tools are active).
- ``anthropic-cli`` → ``claude -p --output-format json --model <model>`` with the
  prompt on stdin; the answer is the ``result`` field. NOT ``--bare`` (that
  forces API-key mode and skips subscription auth).
- ``gemini-cli`` → ``gemini -p --output-format json -m <model>`` with the prompt
  on stdin; the answer is the ``response`` field.

Privacy + hardening (KTD4, KTD5)
--------------------------------
- The backend receives ONLY text — the ALLOW-only ``activity_summary`` dict
  (``segment``) or ``Evidence.text`` (``answer``). It is never handed, and cannot
  request, frame bytes (KTD4).
- The subprocess is spawned with a **scrubbed env** (:func:`ondevice._scrubbed_env`
  strips ``*_KEY`` / ``*_TOKEN`` / ``*_SECRET`` / ``*_PASSWORD`` / ``*_CREDENTIAL``)
  so an arbitrary third-party binary never inherits the daemon's Firebase auth
  context or the BYO API keys.
- The binary is resolved via a **configured path then a PATH probe** — never a
  bare name assumed on PATH, because the LaunchAgent daemon runs with a
  restricted launchd PATH.
- A wall-clock timeout is enforced, stdout is drained to EOF, and **stderr is
  never logged verbatim** (truncate + class-name only, mirroring ``downloaded.py``
  — stderr can echo recording-derived text or a leaked secret).

Return contract (see :mod:`screencap.segmentation.provider`)
------------------------------------------------------------
- a validated **tasks dict** — the CLI ran and its output validated (``segment``).
- ``None`` — the CLI ran but produced no usable tasks (``segment`` only).
- :data:`~screencap.segmentation.provider.PROVIDER_UNAVAILABLE` — could not run at
  all: binary missing, non-zero exit, timeout, or unparseable stdout. Degradation
  routes on this sentinel (→ on-device / heuristic), never a hang or crash.

Availability (KTD1)
-------------------
:meth:`CliDelegateProvider.available` is an **existence/stat-only** check —
binary resolvable + a vendor auth artifact present on disk. It NEVER opens or
parses the vendor's auth files: ScreenCap never reads the vendor's stored
credentials, only observes that the CLI is set up.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from screencap.segmentation.generation import Evidence
from screencap.segmentation.generation_finish import (
    build_answer_prompt,
    evidence_gate_ok,
    sanitize_answer,
)
from screencap.segmentation.local_finish import build_local_prompt, finalize_local_result
from screencap.segmentation.provider import PROVIDER_UNAVAILABLE, ProviderUnavailable
from screencap.segmentation.providers.ondevice import _scrubbed_env

log = logging.getLogger(__name__)

# Wall-clock ceiling for one CLI invocation. A vendor agent CLI can be slow
# (model + tool loop), but a hang past this maps to unavailable so terminal_stage
# never blocks on segmentation. Overridable via env for the (fast) fake-CLI tests.
_DEFAULT_TIMEOUT_S = 180.0
_TIMEOUT_ENV = "SCREENCAP_CLI_DELEGATE_TIMEOUT"

# Reject a stdout larger than this rather than parsing it (DoS / OOM guard).
_MAX_STDOUT_BYTES = 4 * 1024 * 1024


class _VendorSpec:
    """Per-vendor invocation + availability facts for one ``*-cli`` id.

    ``binary`` is the CLI name resolved on PATH (or via the configured override).
    ``model`` is the default model flag value. ``auth_artifacts`` is the set of
    candidate on-disk paths whose *existence* (never contents, KTD1) evidences the
    CLI is signed in. ``config_key`` names the optional ``[intelligence]`` binary
    path override read from config.
    """

    __slots__ = ("provider_id", "binary", "model", "config_key", "auth_artifacts")

    def __init__(
        self,
        provider_id: str,
        binary: str,
        model: str,
        config_key: str,
        auth_artifacts: tuple[Path, ...],
    ) -> None:
        self.provider_id = provider_id
        self.binary = binary
        self.model = model
        self.config_key = config_key
        self.auth_artifacts = auth_artifacts


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _vendor_specs() -> dict[str, _VendorSpec]:
    """Build the per-vendor specs (home is resolved lazily for testability)."""
    home = _home()
    return {
        "openai-cli": _VendorSpec(
            provider_id="openai-cli",
            binary="codex",
            model="gpt-5-codex",
            config_key="openai_cli_path",
            # Codex "sign in with ChatGPT" writes ~/.codex/auth.json (mode 0600).
            auth_artifacts=(home / ".codex" / "auth.json",),
        ),
        "anthropic-cli": _VendorSpec(
            provider_id="anthropic-cli",
            binary="claude",
            model="claude-sonnet-4-5",
            config_key="anthropic_cli_path",
            # Claude Code stores auth in the macOS Keychain (not statable cheaply)
            # OR ~/.claude/.credentials.json; ~/.claude.json is its main state file
            # (present on Keychain-auth macOS setups). Any → "the CLI is set up".
            auth_artifacts=(
                home / ".claude" / ".credentials.json",
                home / ".claude.json",
            ),
        ),
        "gemini-cli": _VendorSpec(
            provider_id="gemini-cli",
            binary="gemini",
            model="gemini-2.5-pro",
            config_key="gemini_cli_path",
            # The Gemini CLI writes OAuth creds to ~/.gemini/oauth_creds.json;
            # an API-key / settings setup leaves ~/.gemini/settings.json.
            auth_artifacts=(
                home / ".gemini" / "oauth_creds.json",
                home / ".gemini" / "settings.json",
            ),
        ),
    }


VENDOR_IDS = ("openai-cli", "anthropic-cli", "gemini-cli")


def _timeout_s() -> float:
    raw = os.environ.get(_TIMEOUT_ENV)
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return _DEFAULT_TIMEOUT_S


def _configured_binary_path(config_key: str) -> str | None:
    """Return the ``[intelligence].<config_key>`` binary override, or ``None``.

    A dedicated getter isn't warranted per vendor; read the ``[intelligence]``
    section directly (env ``SCREENCAP_<CONFIG_KEY>`` > toml > ``None``), mirroring
    the existing getter precedence without adding three near-identical functions.
    """
    env_name = "SCREENCAP_" + config_key.upper()
    env = os.environ.get(env_name)
    if env is not None:
        env = env.strip()
        return env or None
    try:
        from screencap import config

        section = config._load_toml().get("intelligence", {})
    except Exception:  # noqa: BLE001 — config unreadable → no override
        return None
    if isinstance(section, dict):
        val = section.get(config_key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _resolve_binary(spec: _VendorSpec) -> str | None:
    """Resolve the CLI binary: configured path first, then a PATH probe.

    Never a bare name assumed on PATH (KTD5 — the daemon's launchd PATH is
    restricted). Returns an absolute, executable path or ``None`` when the binary
    can't be found / isn't executable.
    """
    configured = _configured_binary_path(spec.config_key)
    if configured:
        p = Path(configured)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        log.info(
            "%s: configured binary path is not an executable file", spec.provider_id
        )
        return None
    found = shutil.which(spec.binary)
    return found or None


def _auth_artifact_present(spec: _VendorSpec) -> bool:
    """Return whether any vendor auth artifact exists — stat only, never read (KTD1).

    ScreenCap never opens or parses the vendor's credential files; it only
    observes (via :meth:`Path.exists`, a stat) that the CLI has been set up.
    """
    return any(p.exists() for p in spec.auth_artifacts)


class CliDelegateProvider:
    """Delegate a task to a vendor CLI subprocess. See module docs.

    ``vendor`` is one of :data:`VENDOR_IDS`. ``run_cli`` is an injectable seam
    (defaults to the real subprocess call) so tests exercise the parse/validate
    flow against a fake CLI without spawning a real binary. Stateless otherwise;
    safe to construct per call.
    """

    def __init__(self, vendor: str, run_cli=None) -> None:
        specs = _vendor_specs()
        if vendor not in specs:
            raise ValueError(
                f"Unknown CLI-delegation vendor: {vendor!r}. "
                f"Known: {', '.join(VENDOR_IDS)}."
            )
        self.vendor = vendor
        self._spec = specs[vendor]
        self._run_cli = run_cli if run_cli is not None else self._default_run_cli

    # -- Availability (existence/stat only, KTD1) --------------------------

    def available(self) -> bool:
        """Return whether this delegation option is usable right now.

        True only when BOTH the binary resolves (configured path or PATH probe)
        AND a vendor auth artifact exists on disk. Existence/stat only — it never
        opens or parses the vendor's auth files (KTD1). Cheap by design (no
        subprocess probe) so the settings UI can call it per render.
        """
        if _resolve_binary(self._spec) is None:
            return False
        return _auth_artifact_present(self._spec)

    # -- Segmentation ------------------------------------------------------

    def segment(self, activity_summary: dict) -> dict | None | ProviderUnavailable:
        """Segment the session by delegating to the vendor CLI.

        See :mod:`screencap.segmentation.provider` for the tri-state return.
        Never raises for an ordinary CLI failure.
        """
        # Fail-closed privacy gate (R7/R8): refuse anything not explicitly marked
        # privacy-stripped. Do NOT spawn the CLI on unmarked input.
        if activity_summary.get("stripped") is not True:
            log.warning(
                "CliDelegateProvider(%s) refused an activity summary not marked "
                "stripped=True (fail-closed); unavailable.",
                self.vendor,
            )
            return PROVIDER_UNAVAILABLE

        binary = _resolve_binary(self._spec)
        if binary is None:
            log.info("%s: CLI binary not found; unavailable", self.vendor)
            return PROVIDER_UNAVAILABLE

        try:
            prompt = build_local_prompt(activity_summary["summary"])
        except Exception:  # pragma: no cover - defensive
            log.warning("%s: failed to build the segmentation prompt", self.vendor,
                        exc_info=True)
            return PROVIDER_UNAVAILABLE

        stdout = self._run_cli(binary, prompt)
        if stdout is PROVIDER_UNAVAILABLE:
            return PROVIDER_UNAVAILABLE

        raw = self._parse_tasks(stdout)  # type: ignore[arg-type]
        if raw is PROVIDER_UNAVAILABLE:
            return PROVIDER_UNAVAILABLE

        # Validate → sanitize (KTD12) → confidence-gate (KTD9), shared with the
        # downloaded / local-server backends (a BYO CLI's names reach the same sink).
        return finalize_local_result(raw, activity_summary)  # type: ignore[arg-type]

    # -- Free-form generation ----------------------------------------------

    def answer(self, prompt: str, evidence: Evidence) -> str | ProviderUnavailable:
        """Answer ``prompt`` grounded in ``evidence`` by delegating to the vendor CLI.

        See :mod:`screencap.segmentation.generation` for the two-state return.
        Never raises for an ordinary CLI failure.
        """
        # Single fail-closed gate: stripped marker (R7), str text/prompt (R7),
        # within the size caps (KTD10). No CLI spawn on refusal.
        if not evidence_gate_ok(prompt, evidence):
            log.warning("CliDelegateProvider(%s).answer refused (gate); unavailable",
                        self.vendor)
            return PROVIDER_UNAVAILABLE

        binary = _resolve_binary(self._spec)
        if binary is None:
            log.info("%s: CLI binary not found; answer unavailable", self.vendor)
            return PROVIDER_UNAVAILABLE

        stdout = self._run_cli(binary, build_answer_prompt(prompt, evidence))
        if stdout is PROVIDER_UNAVAILABLE:
            return PROVIDER_UNAVAILABLE

        text = self._parse_answer(stdout)  # type: ignore[arg-type]
        if text is PROVIDER_UNAVAILABLE:
            return PROVIDER_UNAVAILABLE

        cleaned = sanitize_answer(text)  # type: ignore[arg-type]
        if not cleaned.strip():
            return PROVIDER_UNAVAILABLE
        return cleaned

    # -- Subprocess ---------------------------------------------------------

    def _argv(self, binary: str) -> list[str]:
        """Build the vendor-specific non-interactive argv (KTD1).

        The prompt is passed on **stdin** for claude/gemini (kept off argv, which
        is world-readable via ``ps``). ``codex exec`` reads its prompt from a
        positional arg, so for OpenAI the prompt IS an argv element — that's the
        CLI's documented non-interactive contract, and the input is already the
        ALLOW-only stripped text (never a secret).
        """
        if self.vendor == "openai-cli":
            # Plain ``codex exec`` (NOT --json): prints only the final agent
            # message to stdout. Prompt appended by the caller as the positional.
            return [binary, "exec", "-m", self._spec.model]
        if self.vendor == "anthropic-cli":
            # ``claude -p`` non-interactive; JSON envelope with a ``result`` field.
            # NOT --bare (that forces API-key mode, skipping subscription auth).
            return [binary, "-p", "--output-format", "json", "--model", self._spec.model]
        # gemini-cli
        return [binary, "-p", "--output-format", "json", "-m", self._spec.model]

    def _default_run_cli(self, binary: str, prompt: str):
        """Spawn the vendor CLI, drain stdout to EOF, and return it (or the sentinel).

        Prompt is delivered on stdin for claude/gemini and as a positional arg for
        codex (its documented contract). Scrubbed env (KTD5), wall-clock timeout,
        stderr never logged verbatim. Returns the raw stdout ``str`` on success or
        :data:`PROVIDER_UNAVAILABLE` on non-zero exit / timeout / spawn failure /
        oversized output.
        """
        argv = self._argv(binary)
        # codex takes the prompt as a positional; claude/gemini read it on stdin.
        stdin_input: str | None
        if self.vendor == "openai-cli":
            argv = argv + [prompt]
            stdin_input = None
        else:
            stdin_input = prompt

        try:
            proc = subprocess.run(
                argv,
                input=stdin_input,
                capture_output=True,
                text=True,
                timeout=_timeout_s(),
                env=_scrubbed_env(),
            )
        except subprocess.TimeoutExpired:
            log.warning("%s: CLI timed out; unavailable", self.vendor)
            return PROVIDER_UNAVAILABLE
        except OSError:
            log.warning("%s: CLI could not be spawned; unavailable", self.vendor,
                        exc_info=True)
            return PROVIDER_UNAVAILABLE

        if proc.returncode != 0:
            # Do NOT log stderr verbatim — it can carry recording-derived text or a
            # leaked secret. Only the class of failure (exit code) is logged.
            log.warning("%s: CLI exited %d; unavailable", self.vendor, proc.returncode)
            return PROVIDER_UNAVAILABLE

        if len(proc.stdout or "") > _MAX_STDOUT_BYTES:
            log.warning("%s: CLI output exceeds the stdout cap; unavailable", self.vendor)
            return PROVIDER_UNAVAILABLE

        return proc.stdout or ""

    # -- Output parsing -----------------------------------------------------

    def _extract_text(self, stdout: str):
        """Extract the vendor's final answer text from raw stdout.

        - openai-cli: plain ``codex exec`` prints the final message as plain text
          → the whole stdout is the answer.
        - anthropic-cli: ``{"result": "..."}`` JSON envelope → the ``result`` field.
        - gemini-cli: ``{"response": "..."}`` JSON envelope → the ``response`` field.

        Returns the text ``str`` or :data:`PROVIDER_UNAVAILABLE` when the expected
        shape isn't present.
        """
        text = (stdout or "").strip()
        if not text:
            log.warning("%s: CLI produced empty stdout; unavailable", self.vendor)
            return PROVIDER_UNAVAILABLE

        if self.vendor == "openai-cli":
            return text

        # claude / gemini emit a JSON envelope.
        try:
            envelope = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            log.warning("%s: CLI stdout was not JSON; unavailable", self.vendor)
            return PROVIDER_UNAVAILABLE
        if not isinstance(envelope, dict):
            log.warning("%s: CLI envelope was not an object; unavailable", self.vendor)
            return PROVIDER_UNAVAILABLE

        field = "result" if self.vendor == "anthropic-cli" else "response"
        value = envelope.get(field)
        if isinstance(value, str) and value.strip():
            return value
        log.warning("%s: CLI envelope had no %r string; unavailable", self.vendor, field)
        return PROVIDER_UNAVAILABLE

    def _parse_tasks(self, stdout: str):
        """Parse the CLI's final text into the raw tasks dict (pre-validation).

        The model is instructed (via the shared segmentation prompt) to return a
        JSON tasks object; its final message is that JSON, possibly fenced in a
        ```` ```json ```` block. Returns the raw dict or
        :data:`PROVIDER_UNAVAILABLE` when it isn't a parseable JSON object.
        """
        text = self._extract_text(stdout)
        if text is PROVIDER_UNAVAILABLE:
            return PROVIDER_UNAVAILABLE
        payload = _strip_code_fence(text)  # type: ignore[arg-type]
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            log.warning("%s: CLI task output was not JSON; unavailable", self.vendor)
            return PROVIDER_UNAVAILABLE
        if not isinstance(parsed, dict):
            log.warning("%s: CLI task output was not a JSON object; unavailable",
                        self.vendor)
            return PROVIDER_UNAVAILABLE
        return parsed

    def _parse_answer(self, stdout: str):
        """Parse the CLI's final text for the free-form ``answer`` path."""
        return self._extract_text(stdout)


def _strip_code_fence(text: str) -> str:
    """Return ``text`` with a leading/trailing Markdown code fence removed.

    An agent CLI often wraps a JSON reply in a ```` ```json … ``` ```` block. Strip
    a single outer fence so the JSON parses; a plain (unfenced) reply is returned
    unchanged.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    # Drop the opening fence line (```` ``` ```` or ```` ```json ````) and a
    # trailing fence line if present.
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()
