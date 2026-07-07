"""Configuration: ~/.screencap/config.toml + env vars."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

if TYPE_CHECKING:
    import tomlkit

_DEFAULT_BASE = Path.home() / ".screencap"
_DEFAULT_RECORDINGS = _DEFAULT_BASE / "recordings"
_DEFAULT_DOWNLOADS = _DEFAULT_BASE / "downloads"
_CONFIG_PATH = _DEFAULT_BASE / "config.toml"

_config_cache: dict | None = None

_BOOL_TRUE = ("1", "true", "yes")


def _load_toml() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if _CONFIG_PATH.exists():
        _config_cache = tomllib.loads(_CONFIG_PATH.read_text())
    else:
        _config_cache = {}
    return _config_cache


def invalidate_config_cache() -> None:
    """Reset the config cache so the next read re-loads from disk."""
    global _config_cache
    _config_cache = None


def _parse_bool_env(env_name: str, cfg_key: str, default: bool) -> bool:
    """Env var (truthy → bool) > config.toml > default."""
    env = os.environ.get(env_name)
    if env is not None:
        return env.lower() in _BOOL_TRUE
    return _load_toml().get(cfg_key, default)


def _parse_nonneg_int_env(env_name: str, cfg_key: str, default: int) -> int:
    """Non-negative integer: env var > config.toml > default.

    Exits with a message if the env var is non-integer or negative, or if
    the config.toml value is not an int.
    """
    env = os.environ.get(env_name)
    if env is not None:
        env = env.strip()
        try:
            val = int(env)
        except ValueError:
            raise SystemExit(
                f"Error: {env_name} must be an integer, got: {env!r}"
            )
        if val < 0:
            raise SystemExit(
                f"Error: {env_name} cannot be negative, got: {val}"
            )
        return val
    val = _load_toml().get(cfg_key, default)
    if not isinstance(val, int):
        raise SystemExit(
            f"Error: {cfg_key} in config.toml must be an integer, got: {val!r}"
        )
    return val


def save_config_atomic(
    config_path: Path, doc: "tomlkit.TOMLDocument",
) -> None:
    """Write a tomlkit document atomically (tempfile + os.rename).

    Single source of truth for atomic config writes — used by both the
    setup wizard and the runtime privacy persistence layer. Preserves
    comments and formatting via tomlkit.
    """
    import tomlkit  # local import — keeps `screencap --help` fast

    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(config_path.parent),
        suffix=".toml.tmp",
    )
    closed = False
    try:
        os.write(fd, tomlkit.dumps(doc).encode())
        os.close(fd)
        closed = True
        os.rename(tmp_path, str(config_path))
    except Exception:
        if not closed:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def get_recordings_dir() -> Path:
    """Return recordings directory, creating it if needed."""
    env = os.environ.get("SCREENCAP_RECORDINGS_DIR")
    if env:
        p = Path(env)
    else:
        cfg = _load_toml()
        p = Path(cfg.get("recordings_dir", str(_DEFAULT_RECORDINGS)))
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_audio_default() -> bool:
    """Return default audio setting (True = on)."""
    return _parse_bool_env("SCREENCAP_AUDIO_DEFAULT", "audio_default", True)


def set_audio_default(value: bool) -> None:
    """Persist the default audio setting to ``config.toml``.

    Uses tomlkit via the setup-wizard loader/saver pair so comments and
    formatting are preserved. Mirrors the path taken by
    ``screencap settings --set audio_default=…`` (cli.py:1911).

    Invalidates the in-process config cache so subsequent reads in the
    same process observe the new value. Cross-process invalidation is
    not required — each ``screencap start`` is a fresh Python process
    with an empty cache.
    """
    from screencap.setup_wizard import _load_config_toml, _save_config_atomic

    doc = _load_config_toml(_CONFIG_PATH)
    doc["audio_default"] = value
    _save_config_atomic(_CONFIG_PATH, doc)
    invalidate_config_cache()


def get_wifi_metrics() -> bool:
    """Return whether WiFi metrics collection is enabled (True = on)."""
    return _parse_bool_env("SCREENCAP_WIFI_METRICS", "wifi_metrics", True)


def get_app_versions() -> bool:
    """Return whether running-application version capture is enabled (True = on)."""
    return _parse_bool_env("SCREENCAP_APP_VERSIONS", "app_versions", True)


def get_auto_name() -> bool:
    """Return whether LLM auto-naming is enabled after recording (True = on)."""
    return _parse_bool_env("SCREENCAP_AUTO_NAME", "auto_name", True)


def get_auto_update() -> bool:
    """Return whether auto-update checking is enabled (True = on)."""
    return _parse_bool_env("SCREENCAP_AUTO_UPDATE", "auto_update", True)


def get_auto_name_local_only() -> bool:
    """Return whether LLM auto-naming is restricted to local providers only."""
    return _parse_bool_env(
        "SCREENCAP_AUTO_NAME_LOCAL_ONLY", "auto_name_local_only", False,
    )


def get_content_index_enabled() -> bool:
    """Return whether the SCR-118 on-screen content index is enabled.

    Opt-in (default off): when on, chunk processing OCRs the recording's local
    screenshots (skipping secure-field / EXCLUDE frames) into the global
    ``content_index.db`` so an MCP agent can search on-screen text. The index is
    local-only (never uploaded) and purged on retroactive disable. Requires
    scrubbing to be enabled — the secure-field skip depends on the scrub context.
    """
    return _parse_bool_env("SCREENCAP_CONTENT_INDEX", "content_index_enabled", False)


def get_cloud_e2ee_enabled() -> bool:
    """Return whether cloud uploads are end-to-end encrypted on-device (E2EE slice).

    Default OFF — the slice ships dark until the crypto path is proven. When on,
    a cloud recording's artifacts are encrypted with the device-held cloud key
    before upload, so the object store holds only ciphertext; when off, uploads
    are plaintext exactly as before. This flag is the single runtime signal that
    gates encryption, and the onboarding "we can't watch" copy is bound to it
    (surfaced via ``settings --json``) so a flag-off build never claims E2EE.
    """
    return _parse_bool_env("SCREENCAP_CLOUD_E2EE", "cloud_e2ee_enabled", False)


def get_content_index_consent_declined() -> bool:
    """Return whether the user declined the one-time on-screen-text indexing
    consent prompt (SCR-174 U7).

    Persisted as a settings bool distinct from ``content_index_enabled`` so that
    "declined" never re-prompts and is never conflated with "feature off"
    (never-asked = flag off and not declined; consented = flag on; declined =
    flag off and this set). The in-app Search consent prompt is a UI-honesty
    gate, not a security boundary — enabling indexing directly via
    ``screencap settings --set content_index_enabled=true`` is an accepted
    same-EUID path (consistent with SECURITY.md).
    """
    return _parse_bool_env(
        "SCREENCAP_CONTENT_INDEX_CONSENT_DECLINED",
        "content_index_consent_declined",
        False,
    )


def get_content_index_backfill_declined() -> bool:
    """Return whether the user declined the one-time "index existing recordings"
    backfill offer (SCR-178 U8).

    Persisted as a settings bool, distinct from ``content_index_consent_declined``
    (the indexing-consent decline) and ``content_index_enabled``: the backfill
    offer is shown right after the user enables indexing, and once they enable it
    the consent banner no longer renders — so this flag is what keeps the offer
    from re-appearing on a later launch when an explicit re-entry point exists.
    Same UI-honesty (not security) posture as the consent flag.
    """
    return _parse_bool_env(
        "SCREENCAP_CONTENT_INDEX_BACKFILL_DECLINED",
        "content_index_backfill_declined",
        False,
    )


def get_downloads_dir() -> Path:
    """Return downloads directory, creating it if needed."""
    env = os.environ.get("SCREENCAP_DOWNLOADS_DIR")
    if env:
        p = Path(env)
    else:
        cfg = _load_toml()
        p = Path(cfg.get("downloads_dir", str(_DEFAULT_DOWNLOADS)))
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_base_dir() -> Path:
    """Return ~/.screencap/, creating it if needed."""
    _DEFAULT_BASE.mkdir(parents=True, exist_ok=True)
    return _DEFAULT_BASE


def get_disk_warn_mb() -> int:
    """Minimum free MB to start recording / show warning. Default 2000."""
    return _parse_nonneg_int_env("SCREENCAP_DISK_WARN_MB", "disk_warn_mb", 2000)


def get_disk_stop_mb() -> int:
    """Free MB threshold to auto-stop recording. Default 500."""
    return _parse_nonneg_int_env("SCREENCAP_DISK_STOP_MB", "disk_stop_mb", 500)


def get_chunk_duration() -> float:
    """Return auto-cut chunk duration in seconds. Default 900 (15 min). 0 = legacy."""
    env = os.environ.get("SCREENCAP_CHUNK_DURATION")
    if env is not None:
        return float(env)
    cfg = _load_toml()
    return float(cfg.get("chunk_duration", 900.0))


def get_auto_delete_after_upload() -> bool:
    """Return whether to auto-delete chunks after confirmed upload. Default False.

    The retention default is now ``keep_forever`` (R11), so with no ``[retention]``
    block, no legacy ``auto_delete_after_upload``, and no env override this returns
    False; it is True only when the resolved policy is ``delete_after_upload``
    (legacy ``auto_delete_after_upload = true`` still maps to that).

    .. deprecated::
        Backward-compat shim. The retention policy is now expressed as a
        first-class ``{retention_policy, params}`` block via
        :func:`get_retention_policy` (the U3 monetization seam). This lone
        bool only answers "is the configured retention policy
        ``delete_after_upload``?" so existing callers
        (``cli`` settings display, ``engine/collaborators``) keep working
        unchanged. New code should resolve a frozen
        :class:`screencap.pipeline_policy.ResolvedPolicy` instead.
    """
    # Read through the retention block so the legacy bool stays consistent
    # with the new config surface: a config that sets
    # ``retention_policy = "delete_after_upload"`` (or the legacy
    # ``auto_delete_after_upload = true``) both answer True here.
    policy, _params = get_retention_policy()
    return policy == "delete_after_upload"


# Valid retention policy names (mirror screencap.pipeline_policy.RetentionPolicy
# values — kept as bare strings here to avoid importing the heavier module on
# every ``screencap --help``).
_RETENTION_POLICIES = (
    "keep_forever",
    "delete_after_upload",
    "delete_after_days",
    "size_cap",
)


def get_retention_policy() -> tuple[str, dict]:
    """Return the configured default ``(retention_policy, params)``.

    This is the retention config block that replaces the lone
    ``auto_delete_after_upload`` bool. It is the *default* the U3 resolver
    (:func:`screencap.pipeline_policy.resolve_policy`) reads when no
    per-account / plan-tier override applies; the resolved value is then
    frozen per recording.

    Precedence: env var > ``[retention]`` config block > legacy
    ``auto_delete_after_upload`` bool > default ``keep_forever``.

    - ``SCREENCAP_RETENTION_POLICY`` (env) names the policy directly; its
      params come from the env params vars below.
    - ``[retention]`` config block: ``policy`` plus optional ``days`` /
      ``size_cap_mb``.
    - **Backward-compat:** a legacy top-level ``auto_delete_after_upload =
      true`` (or ``SCREENCAP_AUTO_DELETE`` truthy) with no explicit
      ``[retention]`` block maps to ``delete_after_upload``; ``false`` /
      absent maps to ``keep_forever`` (the default — existing local
      behavior unchanged unless a cap is set, per R11).

    Params carried: ``delete_after_days`` -> ``{"days": int}``;
    ``size_cap`` -> ``{"size_cap_mb": int}``; others -> ``{}``.
    """
    # 1) Explicit env override of the policy name wins outright.
    env_policy = os.environ.get("SCREENCAP_RETENTION_POLICY")
    if env_policy is not None:
        policy = env_policy.strip().lower()
        if policy not in _RETENTION_POLICIES:
            raise SystemExit(
                f"Error: SCREENCAP_RETENTION_POLICY must be one of "
                f"{_RETENTION_POLICIES}, got: {env_policy!r}"
            )
        return policy, _retention_params(policy)

    cfg = _load_toml()
    section = cfg.get("retention", {})
    if isinstance(section, dict) and "policy" in section:
        policy = section.get("policy")
        if not isinstance(policy, str) or policy.lower() not in _RETENTION_POLICIES:
            raise SystemExit(
                f"Error: [retention].policy must be one of "
                f"{_RETENTION_POLICIES}, got: {policy!r}"
            )
        return policy.lower(), _retention_params(policy.lower(), section)

    # 2) No explicit [retention] block — fall back to the legacy bool so old
    #    configs keep meaning the same thing.
    legacy = _parse_bool_env(
        "SCREENCAP_AUTO_DELETE", "auto_delete_after_upload", False,
    )
    if legacy:
        return "delete_after_upload", {}
    return "keep_forever", {}


def _retention_params(policy: str, section: dict | None = None) -> dict:
    """Resolve params for ``policy`` from env vars then the config section."""
    if policy == "delete_after_days":
        env = os.environ.get("SCREENCAP_RETENTION_DAYS")
        if env is not None:
            return {"days": _coerce_pos_int("SCREENCAP_RETENTION_DAYS", env)}
        if section is not None and "days" in section:
            days = section["days"]
            if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
                raise SystemExit(
                    f"Error: [retention].days must be a positive integer, got: {days!r}"
                )
            return {"days": days}
        return {}
    if policy == "size_cap":
        env = os.environ.get("SCREENCAP_RETENTION_SIZE_CAP_MB")
        if env is not None:
            return {"size_cap_mb": _coerce_pos_int("SCREENCAP_RETENTION_SIZE_CAP_MB", env)}
        if section is not None and "size_cap_mb" in section:
            cap = section["size_cap_mb"]
            if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
                raise SystemExit(
                    f"Error: [retention].size_cap_mb must be a positive integer, got: {cap!r}"
                )
            return {"size_cap_mb": cap}
        return {}
    return {}


def _coerce_pos_int(env_name: str, raw: str) -> int:
    raw = raw.strip()
    try:
        val = int(raw)
    except ValueError:
        raise SystemExit(f"Error: {env_name} must be an integer, got: {raw!r}")
    if val <= 0:
        raise SystemExit(f"Error: {env_name} must be positive, got: {val}")
    return val


def get_rest_threshold() -> float:
    """Return rest threshold in seconds for task segmentation. Default 120."""
    env = os.environ.get("SCREENCAP_REST_THRESHOLD")
    if env is not None:
        return float(env)
    cfg = _load_toml()
    return float(cfg.get("rest_threshold", 120.0))


def get_segmentation_mode() -> str:
    """Return segmentation mode: 'idle' or 'llm'. Default 'llm'.

    Reads from config.toml key ``segmentation_mode``.
    CLI flag ``--segmentation-mode`` takes priority (passed directly, not via this function).

    Controls manifest format:
    - 'idle': old manifest with tasks (format_version absent)
    - 'llm': simplified manifest with chunk metadata only (format_version: 2)
    """
    valid = ("idle", "llm")
    cfg = _load_toml()
    val = cfg.get("segmentation_mode", "llm")
    if not isinstance(val, str) or val not in valid:
        raise SystemExit(
            f"Error: segmentation_mode must be one of {valid}, got: {val!r}"
        )
    return val


def get_llm_provider() -> str:
    """Return the active LLM segmentation provider. Default 'on-device'.

    Selects the backend behind ``screencap.segmentation.provider.LLMProvider``
    (env ``SCREENCAP_LLM_PROVIDER`` > config.toml ``llm_provider`` > default),
    mirroring :func:`get_segmentation_mode` / :func:`get_rest_threshold`.

    Default ``'on-device'`` keeps the "nothing leaves the Mac" promise for
    local recordings (the on-device backend lands in U5). The cloud processor
    does not read this — it pins ``'gemini'`` explicitly. The value is only
    resolved here; ``provider.get_provider`` maps it to a backend (and raises a
    clear ``NotImplementedError`` for on-device until U5).
    """
    env = os.environ.get("SCREENCAP_LLM_PROVIDER")
    if env is not None:
        return env.strip()
    cfg = _load_toml()
    return str(cfg.get("llm_provider", "on-device"))


def _parse_intelligence_bool(env_name: str, cfg_key: str, default: bool) -> bool:
    """Env var (truthy → bool) > ``[intelligence].<cfg_key>`` > default.

    A section-scoped twin of :func:`_parse_bool_env` for the per-task cloud
    consent rows (U6). Consent defaults **OFF** — cloud never runs a task
    unless its row is explicitly enabled.
    """
    env = os.environ.get(env_name)
    if env is not None:
        return env.lower() in _BOOL_TRUE
    section = _load_toml().get("intelligence", {})
    if isinstance(section, dict):
        val = section.get(cfg_key, default)
        if isinstance(val, bool):
            return val
    return default


def get_llm_cloud_provider() -> str | None:
    """Return the configured *cloud* segmentation provider, or ``None``.

    The consent policy (``screencap.segmentation.consent``) may route a
    consented, on-device-unavailable summary/title task to this backend. It is
    distinct from :func:`get_llm_provider` (the *active/preferred* provider,
    default ``on-device``): this getter names which cloud backend a consented
    fallback is allowed to use, and returns ``None`` when no cloud provider is
    configured (the zero-config default — nothing leaves the Mac).

    Resolution mirrors the other getters: env ``SCREENCAP_LLM_CLOUD_PROVIDER`` >
    ``[intelligence].cloud_provider`` > default ``None``.
    """
    env = os.environ.get("SCREENCAP_LLM_CLOUD_PROVIDER")
    if env is not None:
        env = env.strip()
        return env or None
    section = _load_toml().get("intelligence", {})
    if isinstance(section, dict):
        val = section.get("cloud_provider")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def get_summary_cloud_consent() -> bool:
    """Return whether the summary/title cloud-consent row is enabled. Default **False**.

    When on (R8), an on-demand summary/title may fall back to the configured
    cloud provider — but only when on-device is unavailable (the row grants
    fallback permission, not always-cloud; the policy in
    ``screencap.segmentation.consent`` enforces the preference order).

    Env ``SCREENCAP_SUMMARY_CLOUD_CONSENT`` > ``[intelligence].summary_cloud_consent``
    > default ``False``.
    """
    return _parse_intelligence_bool(
        "SCREENCAP_SUMMARY_CLOUD_CONSENT", "summary_cloud_consent", False,
    )


def get_recall_cloud_consent() -> bool:
    """Return whether the recall-answer cloud-consent row is enabled. Default **False**.

    Recall-answering runs on-device by default and becomes cloud-eligible only
    when this opt-in row is added (R10). Even when on, the policy in
    ``screencap.segmentation.consent`` prefers on-device whenever available.

    Env ``SCREENCAP_RECALL_CLOUD_CONSENT`` > ``[intelligence].recall_cloud_consent``
    > default ``False``.
    """
    return _parse_intelligence_bool(
        "SCREENCAP_RECALL_CLOUD_CONSENT", "recall_cloud_consent", False,
    )


# --- Intelligence settings: write surface (U8) -----------------------------
#
# The ``screencap settings intelligence`` CLI verb (U8) is the write side of
# the getters above. The value vocabularies live here — next to the getters —
# so the CLI validation and the config schema cannot drift.

#: The active/preferred provider (``[intelligence].llm_provider``). Only the
#: backends :func:`screencap.segmentation.provider.get_provider` can actually
#: construct are accepted; the CLI rejects anything else.
_VALID_LLM_PROVIDERS = ("on-device", "gemini")

#: The cloud backend a consented fallback may use
#: (``[intelligence].cloud_provider``). Only Gemini ships as a cloud backend in
#: this plan; the interface accommodates more, but the CLI refuses to persist a
#: cloud provider the daemon cannot run.
_VALID_CLOUD_PROVIDERS = ("gemini",)

#: The cloud-consent rows, keyed by their CLI/`[intelligence]` name → the
#: getter that reads them back. Only summary/title and recall-answer may be
#: consented to cloud (R8/R10); day-split/label and frames rows are **never**
#: cloud-settable (R7/R9) and are deliberately absent here — the CLI rejects
#: them with a clear message rather than persisting a forbidden row.
_CLOUD_CONSENT_ROWS = ("summary_cloud_consent", "recall_cloud_consent")


def set_intelligence_provider(value: str) -> None:
    """Persist the active provider so :func:`get_llm_provider` reads it back.

    Writes the **top-level** ``llm_provider`` key — the exact key
    :func:`get_llm_provider` reads (``cfg.get("llm_provider", ...)``), which is
    top-level, unlike the ``[intelligence]``-scoped cloud provider / consent
    rows. Persisting the provider anywhere else would silently no-op the
    read-back, so the write mirrors the getter's key rather than the section.

    Writes through the shared advisory-flock config writer (the same
    read → flock → mutate → atomic-save → invalidate-cache path used by
    ``settings privacy``), so a concurrent settings write cannot lose this
    update. Preserves comments and key order via tomlkit.

    The caller is responsible for validating ``value`` against
    :data:`_VALID_LLM_PROVIDERS` first; this helper only persists.
    """
    from screencap.privacy_settings import _privacy_config_writer

    with _privacy_config_writer() as doc:
        doc["llm_provider"] = value


def set_intelligence_cloud_provider(value: str | None) -> None:
    """Persist (or clear) ``[intelligence].cloud_provider``.

    ``None`` removes the key (the zero-config default — no cloud backend
    configured). Otherwise the string is written verbatim; the caller validates
    against :data:`_VALID_CLOUD_PROVIDERS` first.
    """
    _write_intelligence_key("cloud_provider", value)


def set_intelligence_consent(row: str, value: bool) -> None:
    """Persist a cloud-consent row to ``[intelligence].<row>``.

    ``row`` must be one of :data:`_CLOUD_CONSENT_ROWS`; the caller enforces that
    (the day-split/label and frames rows can never be cloud-consented, R7/R9).
    """
    _write_intelligence_key(row, bool(value))


def _write_intelligence_key(key: str, value: object) -> None:
    """Write a single ``[intelligence]`` key via the shared flock config writer.

    A ``None`` value removes the key (used to clear ``cloud_provider``).
    Reuses :func:`screencap.privacy_settings._privacy_config_writer` — despite
    the name it is the generic config read-modify-write helper (it yields the
    whole tomlkit doc under the advisory flock), so intelligence writes
    serialize against privacy writes on the same lock instead of racing.
    """
    import tomlkit

    from screencap.privacy_settings import _privacy_config_writer

    with _privacy_config_writer() as doc:
        if "intelligence" not in doc:
            doc.add("intelligence", tomlkit.table())
        if value is None:
            if key in doc["intelligence"]:
                del doc["intelligence"][key]
        else:
            doc["intelligence"][key] = value


def get_show_on_website() -> bool:
    """Return whether recordings should be visible on the website. Default True."""
    return _parse_bool_env("SCREENCAP_SHOW_ON_WEBSITE", "show_on_website", True)


def get_masked_video_upload_enabled() -> bool:
    """Return whether masked video is uploaded to the cloud. Default **False**.

    The single switch enforcing the project's conservative privacy posture
    (U6 gated OFF; U4 lands only after U6+U7 are proven). Env var
    ``SCREENCAP_MASKED_VIDEO_UPLOAD`` > config ``masked_video_upload`` >
    default ``False``. Precedence matches the neighboring booleans
    (e.g. :func:`get_show_on_website`): a truthy env var (``1`` / ``true`` /
    ``yes``) overrides the TOML value.

    **OFF (default)** — the current behavior holds: capture-time blocking +
    PUBLIC-forcing for cloud-intent recordings stays active, the cloud video
    copy is the capture-blocked chunks as today, and U6's post-hoc video
    masker (built later) is NOT in the upload path.

    **ON (future)** — capture goes rich for all destinations, U6 masks the
    video post-hoc, and the terminal stage uploads the masked video copy.
    This flag MUST NOT flip to ON until U6+U7 fail-closed masking is proven
    AND the native-redaction-review "what uploads" surface is reconciled
    (it currently labels video "local-only, not uploaded").
    """
    return _parse_bool_env(
        "SCREENCAP_MASKED_VIDEO_UPLOAD", "masked_video_upload", False,
    )


def get_upload_default() -> str:
    """Return default recording destination: 'local', 'cloud', 'both', or 'ask'.

    Priority: SCREENCAP_UPLOAD_DEFAULT env var > privacy.upload_default config > 'ask'.
    """
    valid = ("local", "cloud", "both", "ask")
    env = os.environ.get("SCREENCAP_UPLOAD_DEFAULT")
    if env is not None:
        val = env.strip().lower()
        if val not in valid:
            raise SystemExit(
                f"Error: SCREENCAP_UPLOAD_DEFAULT must be one of {valid}, got: {env!r}"
            )
        return val
    cfg = _load_toml()
    section = cfg.get("privacy", {})
    if isinstance(section, dict):
        val = section.get("upload_default", "ask")
        if not isinstance(val, str):
            raise SystemExit(
                f"Error: privacy.upload_default must be a string, got: {type(val).__name__}"
            )
        val = val.lower()
        if val not in valid:
            raise SystemExit(
                f"Error: privacy.upload_default must be one of {valid}, got: {val!r}"
            )
        return val
    return "ask"


def get_privacy_config():
    """Return a PrivacyConfig parsed from [privacy] in config.toml.

    Deferred import to avoid circular deps and keep CLI startup fast.
    """
    from screencap.privacy.policy import parse_privacy_config

    return parse_privacy_config(_load_toml())


def get_network_config():
    """Return a NetworkConfig parsed from [network] in config.toml.

    V1 fields only — see :class:`screencap.network.config.NetworkConfig`.
    Deferred import to avoid circular deps and keep CLI startup fast.
    """
    from screencap.network.config import parse_network_config

    return parse_network_config(_load_toml().get("network", {}))


def get_first_seen_prompt_enabled() -> bool:
    """Return whether the first-seen privacy prompt is enabled. Default True.

    Reads ``[menubar].first_seen_prompt`` from config.toml.
    Env var ``SCREENCAP_FIRST_SEEN_PROMPT`` overrides the config.
    """
    env = os.environ.get("SCREENCAP_FIRST_SEEN_PROMPT")
    if env is not None:
        return env.lower() in ("1", "true", "yes")
    cfg = _load_toml()
    section = cfg.get("menubar", {})
    if isinstance(section, dict):
        val = section.get("first_seen_prompt", True)
        if isinstance(val, bool):
            return val
    return True


def resolve_recording_dir(name: str) -> Path:
    """Resolve a recording name to a directory path, with traversal protection.

    Raises ValueError if the resolved path escapes the recordings directory.
    """
    recordings_dir = get_recordings_dir()
    recording_dir = (recordings_dir / name).resolve()
    if not recording_dir.is_relative_to(recordings_dir.resolve()):
        raise ValueError(f"Invalid recording name: {name}")
    return recording_dir
