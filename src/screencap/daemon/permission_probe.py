"""Fresh-subprocess TCC permission probe for the long-lived daemon.

The daemon is a long-lived process, and macOS caches TCC answers *per process*
at the moment of the first query (see
``docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md``).
So an in-process ``CGPreflight*`` read from the daemon returns whatever state
was live when the daemon first asked — never a grant the user made afterwards
(R9). To read *live* state the daemon must genuinely spawn a fresh process whose
first in-process check reads the current answer — the same property
``screencap.session.run_recording_worker`` relies on for capture to pick up new
grants without a daemon restart.

The ``sys.executable -c "<code>"`` trick used by
``screencap.recorder._check_permission_fresh`` is rejected by the bundled Click
entry point in the frozen binary (SCR-69, documented in ``session.py``), so the
probe shells out to a *real* hidden subcommand (``screencap _permission-probe``)
instead. One spawn checks all three permissions and prints a structured tri-state
result, avoiding the fork-bomb of three spawns per refresh.

Tri-state per permission: ``granted`` / ``denied`` / ``indeterminate``. Any
process-level failure (timeout, non-zero exit, unparseable output, or a
non-darwin host) FAILS OPEN to ``indeterminate`` for every permission — never
``denied`` — so a transient Quartz/PyObjC hiccup can never block a fully-granted
user or be mistaken for a real revocation. This mirrors
``_check_permission_fresh``'s tri-state ``None`` semantics.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Literal

GrantState = Literal["granted", "denied", "indeterminate"]

# Canonical permission keys. These exact strings are the cross-language contract
# shared with the Swift app (``PrivacyPane.from(permissionString:)``) and the
# worker preflight — keep them in sync.
PERMISSION_SCREEN_RECORDING = "screen_recording"
PERMISSION_ACCESSIBILITY = "accessibility"
PERMISSION_INPUT_MONITORING = "input_monitoring"

PERMISSION_KEYS: tuple[str, ...] = (
    PERMISSION_SCREEN_RECORDING,
    PERMISSION_ACCESSIBILITY,
    PERMISSION_INPUT_MONITORING,
)

_VALID_STATES: frozenset[str] = frozenset(("granted", "denied", "indeterminate"))

# The hidden Click subcommand the probe shells out to. Registered in
# ``screencap.cli``. NEVER invoked via ``-c`` (SCR-69).
PROBE_SUBCOMMAND = "_permission-probe"

# Bounded like the engine probe (``_check_permission_fresh`` uses 5s).
_PROBE_TIMEOUT_SECONDS = 5.0


def indeterminate_result() -> dict[str, GrantState]:
    """Tri-state map with every permission ``indeterminate`` (the fail-open)."""
    return {key: "indeterminate" for key in PERMISSION_KEYS}


def probe_command() -> list[str]:
    """Build the argv that runs the probe subcommand as a fresh process.

    Mirrors ``supervisor._default_engine_command``'s frozen-vs-dev split.
    ``--no-update-check`` is passed at the group level so the CLI's startup
    update check neither makes a network call nor pollutes stdout. The argv
    contains **no ``-c``** — the frozen binary's Click entry point rejects it
    (SCR-69), and a test asserts this invariant.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--no-update-check", PROBE_SUBCOMMAND]
    return [sys.executable, "-m", "screencap", "--no-update-check", PROBE_SUBCOMMAND]


def probe_permissions(*, timeout: float = _PROBE_TIMEOUT_SECONDS) -> dict[str, GrantState]:
    """Read *live* TCC state for all three permissions via a fresh subprocess.

    Returns a tri-state map keyed by :data:`PERMISSION_KEYS`. Fails open to
    ``indeterminate`` (never ``denied``) on any process-level failure.
    """
    try:
        result = subprocess.run(
            probe_command(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        # Timeout, OSError spawning the binary, etc. — couldn't determine.
        return indeterminate_result()
    if result.returncode != 0:
        return indeterminate_result()
    return _parse_probe_output(result.stdout)


def _parse_probe_output(stdout: str) -> dict[str, GrantState]:
    """Parse the subcommand's JSON line into a complete tri-state map.

    Scans from the last non-empty line so any incidental stdout before the
    result line can't break parsing. Missing or out-of-range values for any
    permission decode to ``indeterminate``.
    """
    raw: dict | None = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(candidate, dict):
            raw = candidate
            break
    if raw is None:
        return indeterminate_result()

    out: dict[str, GrantState] = {}
    for key in PERMISSION_KEYS:
        value = raw.get(key)
        out[key] = value if value in _VALID_STATES else "indeterminate"
    return out


def run_probe_checks() -> dict[str, GrantState]:
    """In-process check of all three permissions — the subcommand body.

    This is intended to run *inside* the freshly spawned ``_permission-probe``
    subprocess. Because that host process has made no prior TCC call, each
    in-process ``CGPreflight*`` reads LIVE state (the property session.py's
    worker relies on). The three permissions map to three distinct TCC services
    (``kTCCServiceScreenCapture`` / ``ListenEvent`` / ``Accessibility``), and the
    per-process cache is keyed per service, so all three read live within this
    one process — confirmed by the single-spawn end-to-end test.

    Reuses ``DarwinPlatform.is_*_enabled`` (no new check logic). Each check is
    wrapped so an unexpected raise yields ``indeterminate`` for that one
    permission only, never a false ``denied``. Non-darwin → all indeterminate.
    """
    if sys.platform != "darwin":
        return indeterminate_result()

    try:
        from screencap.engine.platform.darwin import DarwinPlatform
    except Exception:
        return indeterminate_result()

    checks = (
        (PERMISSION_SCREEN_RECORDING, DarwinPlatform.is_screen_recording_enabled),
        (PERMISSION_ACCESSIBILITY, DarwinPlatform.is_accessibility_enabled),
        (PERMISSION_INPUT_MONITORING, DarwinPlatform.is_input_monitoring_enabled),
    )
    out: dict[str, GrantState] = {}
    for key, check in checks:
        try:
            out[key] = "granted" if check() else "denied"
        except Exception:
            out[key] = "indeterminate"
    return out
