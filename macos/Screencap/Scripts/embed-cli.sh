#!/usr/bin/env bash
# Xcode build phase: embeds the PyInstaller `dist/ScreencapDaemon.app` helper
# bundle into Contents/Library/LoginItems/ and writes the daemon launcher so the
# LaunchAgent execs the helper bundle's nested binary.
#
# SCR-196: the daemon now runs from a proper helper .app (bundle id
# com.screencap.daemon, exec at Contents/MacOS/screencap, its own Info.plist)
# rather than a bare nested Mach-O. Running from a binary that carries an
# Info.plist is what makes macOS treat the daemon as a first-class TCC subject
# (auto-listed, tccutil-targetable by bundle id) for Screen Recording /
# Accessibility / Input Monitoring. The launcher indirection is preserved (it
# carries the Debug dev-source fast-path); after the launcher `exec`s the nested
# binary the process image IS that bundled binary, so TCC attributes to
# com.screencap.daemon regardless of the launcher hop.
#
# Tolerant of missing dist/ in dev: emits a warning so the .app still launches
# (CLIClient falls back to SCREENCAP_CLI_PATH or SCREENCAP_DEV_REPO_ROOT).

set -euo pipefail

# Resolve repo root: macos/ is one level below the repo root.
REPO_ROOT="${SRCROOT}/.."
# The helper .app produced by `pyinstaller pyinstaller/screencap.spec` (BUNDLE step).
SOURCE_APP="${SCREENCAP_CLI_APP_DIR:-${REPO_ROOT}/dist/ScreencapDaemon.app}"
CONTENTS_DIR="${BUILT_PRODUCTS_DIR}/${CONTENTS_FOLDER_PATH}"
# Apple-conventional home for an embedded SMAppService helper bundle.
DEST_APP="${CONTENTS_DIR}/Library/LoginItems/ScreencapDaemon.app"
# The launched daemon executable inside the embedded helper bundle.
HELPER_BIN="${DEST_APP}/Contents/MacOS/screencap"
LAUNCHER="${BUILT_PRODUCTS_DIR}/${UNLOCALIZED_RESOURCES_FOLDER_PATH}/screencap-daemon-launcher"
# Deliberately OUTSIDE the .app: the bundle's code seal must not depend on a
# file that changes with the developer's build mode. Writing it into
# Contents/Resources broke `codesign --verify` on incremental builds (Xcode
# skips re-signing when no tracked inputs changed, so the new file stayed
# unsealed). The Debug launcher reads it at a fixed path relative to the
# bundle (../../.. of Contents/Resources = the products dir).
DEV_ENV_FILE="${BUILT_PRODUCTS_DIR}/screencap-dev-env"
# Stale copy from the earlier in-bundle placement; remove so old bundles
# converge back to a valid seal on rebuild.
rm -f "${BUILT_PRODUCTS_DIR}/${UNLOCALIZED_RESOURCES_FOLDER_PATH}/screencap-dev-env"

write_daemon_launcher() {
    mkdir -p "$(dirname "${LAUNCHER}")"
    # The launcher lives in Contents/Resources/ and reaches the embedded helper
    # binary at a fixed path relative to itself
    # (../Library/LoginItems/ScreencapDaemon.app/Contents/MacOS/screencap), so the
    # heredocs stay fully literal (<<'EOF') — no build-time interpolation needed.
    #
    # The dev-source branch lets the LaunchAgent exec the in-repo `screencap`
    # module directly, bypassing the PyInstaller bundle for fast iteration.
    # We only emit it for Debug builds — release builds get a launcher that
    # only execs the bundled helper binary, so SCREENCAP_DAEMON_USE_DEV_SOURCE is
    # an inert env var in shipped clients.
    if [ "${CONFIGURATION:-}" = "Debug" ]; then
        cat >"${LAUNCHER}" <<'EOF'
#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# On-device segmentation helper (SCR-239). The daemon runs either from a nested
# ScreencapDaemon.app (whose Contents/MacOS carries no helper, so the Python
# OnDeviceProvider._find_bundled_helper() walk-up cannot see the OUTER app's
# copy) or, in dev-source mode, from the repo with no app-bundle ancestry at
# all. This launcher lives in the OUTER app's Contents/Resources, so it knows
# the helper's exact path (../MacOS/IntelligenceHelper) — export it so it rides
# through every exec below and the daemon actually names tasks on-device instead
# of degrading to the idle-gap heuristic. Respect a caller's override; only set
# when the helper is present + runnable.
if [ -z "${SCREENCAP_ONDEVICE_HELPER:-}" ] && [ -x "${SCRIPT_DIR}/../MacOS/IntelligenceHelper" ]; then
    SCREENCAP_ONDEVICE_HELPER="${SCRIPT_DIR}/../MacOS/IntelligenceHelper"
    export SCREENCAP_ONDEVICE_HELPER
fi

# Debug-only stderr capture: the LaunchAgent plist has no StandardErrorPath,
# so without this redirect daemon stderr (engine events forwarded by
# _stderr_pump, supervisor logger output, fatal tracebacks) goes to
# /dev/null and a daemon-side bug is effectively unobservable. The file
# is append-only and grows unbounded — Release launchers do NOT do this.
# Mirrors `auto-serve.log` for the CLI auto-spawn path. SCR-69.
SCREENCAP_LOG_DIR="${HOME}/.screencap/run"
umask 077
mkdir -p "${SCREENCAP_LOG_DIR}" 2>/dev/null || true
if [ -d "${SCREENCAP_LOG_DIR}" ]; then
    exec 2>>"${SCREENCAP_LOG_DIR}/serve.log"
fi

# Dev-source config written at build time next to the built .app
# (embed-cli.sh `write_dev_env_file`, driven by script/build_and_run.sh). A
# products-dir file — not launchd environment — because (a) `launchctl
# setenv` is global to the GUI session and leaks dev paths into every app,
# and (b) per-job plist EnvironmentVariables are snapshotted at bootstrap,
# so propagating a change would need an SMAppService re-registration only
# the app's onboarding UI performs. This way `launchctl kickstart -k` alone
# picks up env changes. It lives OUTSIDE the bundle so the code seal never
# depends on the developer's build mode. When present, the file is
# authoritative for the mode this build requested (its absence in a
# bundled-mode rebuild is what switches dev-source off). Values are
# extracted literally — the file is never sourced/eval'd.
DEV_ENV_FILE="${SCRIPT_DIR}/../../../screencap-dev-env"
if [ -f "${DEV_ENV_FILE}" ]; then
    SCREENCAP_DAEMON_USE_DEV_SOURCE="$(sed -n 's/^SCREENCAP_DAEMON_USE_DEV_SOURCE=//p' "${DEV_ENV_FILE}")"
    SCREENCAP_DEV_REPO_ROOT="$(sed -n 's/^SCREENCAP_DEV_REPO_ROOT=//p' "${DEV_ENV_FILE}")"
    SCREENCAP_DEV_PYTHON="$(sed -n 's/^SCREENCAP_DEV_PYTHON=//p' "${DEV_ENV_FILE}")"
    export SCREENCAP_DAEMON_USE_DEV_SOURCE SCREENCAP_DEV_REPO_ROOT SCREENCAP_DEV_PYTHON
fi

if [ "${SCREENCAP_DAEMON_USE_DEV_SOURCE:-0}" = "1" ] && [ -n "${SCREENCAP_DEV_REPO_ROOT:-}" ] && [ -d "${SCREENCAP_DEV_REPO_ROOT}/src/screencap" ]; then
    PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"
    export PATH

    if [ -n "${PYTHONPATH:-}" ]; then
        export PYTHONPATH="${SCREENCAP_DEV_REPO_ROOT}/src:${PYTHONPATH}"
    else
        export PYTHONPATH="${SCREENCAP_DEV_REPO_ROOT}/src"
    fi

    if [ -n "${SCREENCAP_DEV_PYTHON:-}" ] && [ -x "${SCREENCAP_DEV_PYTHON}" ]; then
        exec "${SCREENCAP_DEV_PYTHON}" -m screencap "$@"
    fi

    # `command -v` returning empty under `set -eu` would abort the script and
    # cause launchd to immediately respawn us, creating a tight restart loop.
    # Fall through to the bundled binary path instead when python3 is absent
    # or sys.executable resolution fails.
    PYTHON="$(command -v python3 || true)"
    if [ -n "${PYTHON}" ]; then
        RESOLVED="$("${PYTHON}" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
        if [ -n "${RESOLVED}" ] && [ -x "${RESOLVED}" ]; then
            exec "${RESOLVED}" -m screencap "$@"
        fi
    fi
    # Dev-source path requested but unusable — fall through to bundled exec.
fi

exec "${SCRIPT_DIR}/../Library/LoginItems/ScreencapDaemon.app/Contents/MacOS/screencap" "$@"
EOF
    else
        cat >"${LAUNCHER}" <<'EOF'
#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# On-device segmentation helper (SCR-239). The daemon runs from the nested
# ScreencapDaemon.app, whose Contents/MacOS has no helper; this launcher lives
# in the OUTER app's Contents/Resources and knows the helper's exact path, so
# export it explicitly rather than rely on the Python bundle walk-up. Respect a
# caller's override; only set when present + runnable.
if [ -z "${SCREENCAP_ONDEVICE_HELPER:-}" ] && [ -x "${SCRIPT_DIR}/../MacOS/IntelligenceHelper" ]; then
    SCREENCAP_ONDEVICE_HELPER="${SCRIPT_DIR}/../MacOS/IntelligenceHelper"
    export SCREENCAP_ONDEVICE_HELPER
fi

exec "${SCRIPT_DIR}/../Library/LoginItems/ScreencapDaemon.app/Contents/MacOS/screencap" "$@"
EOF
    fi
    chmod 0755 "${LAUNCHER}"
}

write_dev_env_file() {
    # Bakes the dev-source config next to the built .app so the daemon job's
    # env is scoped to this build — never published to the launchd session.
    # Debug + dev-source builds write it; every other build REMOVES it, so a
    # bundled-mode rebuild switches a previously-dev build back cleanly.
    if [ "${CONFIGURATION:-}" = "Debug" ] \
        && [ "${SCREENCAP_DAEMON_USE_DEV_SOURCE:-0}" = "1" ] \
        && [ -n "${SCREENCAP_DEV_REPO_ROOT:-}" ]; then
        {
            printf 'SCREENCAP_DAEMON_USE_DEV_SOURCE=1\n'
            printf 'SCREENCAP_DEV_REPO_ROOT=%s\n' "${SCREENCAP_DEV_REPO_ROOT}"
            if [ -n "${SCREENCAP_DEV_PYTHON:-}" ]; then
                printf 'SCREENCAP_DEV_PYTHON=%s\n' "${SCREENCAP_DEV_PYTHON}"
            fi
        } >"${DEV_ENV_FILE}"
        echo "Wrote dev-source daemon env -> ${DEV_ENV_FILE}"
    else
        rm -f "${DEV_ENV_FILE}"
    fi
}

write_daemon_launcher
write_dev_env_file

if [ ! -d "${SOURCE_APP}" ]; then
    echo "warning: dist/ScreencapDaemon.app not found at ${SOURCE_APP}"
    echo "warning: ScreenCap.app will NOT have a bundled daemon helper."
    echo "warning: Set SCREENCAP_CLI_APP_DIR or run \`pyinstaller pyinstaller/screencap.spec\` from the repo root."
    exit 0
fi

if [ ! -x "${SOURCE_APP}/Contents/MacOS/screencap" ]; then
    echo "warning: ${SOURCE_APP}/Contents/MacOS/screencap is not executable. Skipping embed."
    exit 0
fi

# Refuse to embed a binary that doesn't actually launch. The executable-bit
# check above passes even for a broken PyInstaller bundle (e.g. a bootloader/
# loader version mix that dies with "Bootloader did not set sys._pyinstaller_pyz!"),
# which would otherwise ship a .app whose every CLI call exits 1. `--version`
# is offline and exits immediately, but still exercises the full PyInstaller
# bootstrap that was broken.
if ! "${SOURCE_APP}/Contents/MacOS/screencap" --version >/dev/null 2>&1; then
    echo "error: ${SOURCE_APP}/Contents/MacOS/screencap does not launch — refusing to embed a broken bundle." >&2
    echo "error: likely a PyInstaller bootloader/loader version mismatch; rebuild with a clean cache (rm -rf build dist)." >&2
    "${SOURCE_APP}/Contents/MacOS/screencap" --version || true
    exit 1
fi

mkdir -p "$(dirname "${DEST_APP}")"
rm -rf "${DEST_APP}"
ditto "${SOURCE_APP}" "${DEST_APP}"

echo "Embedded screencap daemon helper from ${SOURCE_APP} -> ${DEST_APP}"

cli_version_from_binary() {
    # parse must match daemon.info.daemon_version; keep in sync with the sibling
    # script's copy (script/build_and_run.sh `cli_version_from_binary`).
    "$1" --version 2>/dev/null | awk 'NF {print $NF}'
}

stamp_bundled_cli_version() {
    # Record the bundled CLI's version in Contents/Resources/ so
    # DaemonInstallController can tell, at install time, whether the daemon
    # answering api.sock is actually the version this app bundles — vs a
    # stale/foreign daemon squatting the socket (SCR-121). `screencap --version`
    # prints "screencap, version X.Y.Z"; we keep the last whitespace-delimited
    # field. The daemon reports the same value as daemon.info.daemon_version, so
    # the two compare directly.
    local version_file version
    version_file="${BUILT_PRODUCTS_DIR}/${UNLOCALIZED_RESOURCES_FOLDER_PATH}/screencap-cli-version"
    # `|| true`: under `set -euo pipefail` a non-zero `screencap --version`
    # (broken bundle) would otherwise abort the build at this assignment instead
    # of reaching the safe-fallback `else` below.
    version="$(cli_version_from_binary "${HELPER_BIN}" || true)"
    if [ -n "${version}" ]; then
        printf '%s' "${version}" >"${version_file}"
        echo "Stamped bundled CLI version ${version} -> ${version_file}"
    else
        # No readable version — remove any stale stamp so the app's gate fails
        # safe (treats the version as unknown) rather than trusting an old value.
        rm -f "${version_file}"
        echo "warning: could not read bundled CLI version; daemon version gate disabled."
    fi
}

stamp_bundled_cli_version

sign_embedded_helper() {
    # PyInstaller emits an ad-hoc signature, so the embedded daemon binary has no
    # stable code identity. macOS TCC then keys the daemon's Screen Recording /
    # Accessibility / Input Monitoring grants on the binary's cdhash, which changes
    # on every rebuild — the "TCC treadmill" that forces the user to re-grant after
    # each build. Xcode signs the outer .app wrapper + main executable but does NOT
    # recurse into Contents/Library/LoginItems/, so without this step the embedded
    # helper keeps its ad-hoc signature. Re-signing inside-out (nested Mach-O ->
    # helper exec with entitlements -> the helper .app) with the app's team
    # identity gives the daemon a stable Designated Requirement
    # (com.screencap.daemon + Team ID), so grants persist across rebuilds and
    # macOS lists it as a real TCC subject. Hardened runtime +
    # screencap-cli.entitlements keep it notarization-ready.
    # See docs/research/2026-06-05-daemon-tcc-registration-spike.md (SCR-196).
    local identity="${EXPANDED_CODE_SIGN_IDENTITY:-}"
    if [ -z "${identity}" ] || [ "${identity}" = "-" ]; then
        echo "note: no team signing identity (EXPANDED_CODE_SIGN_IDENTITY unset/ad-hoc);"
        echo "note: leaving the embedded helper ad-hoc — TCC grants will reset on rebuild."
        echo "note: set DEVELOPMENT_TEAM (see macos/README.md) to make grants persist."
        return
    fi

    local entitlements="${SRCROOT}/ScreenCap/Scripts/screencap-cli.entitlements"
    # --timestamp needs the network and is required for notarized release builds;
    # skip it for Debug so local rebuilds stay fast and work offline.
    local timestamp_flag="--timestamp"
    [ "${CONFIGURATION:-}" = "Debug" ] && timestamp_flag="--timestamp=none"

    echo "Signing embedded daemon helper (${EXPANDED_CODE_SIGN_IDENTITY_NAME:-${identity}}, hardened runtime)..."
    # Inside-out: nested Mach-O first, then the helper exe (with entitlements),
    # then the helper .app wrapper last. No `--deep` (deprecated for signing).
    find "${DEST_APP}" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 \
        | xargs -0 codesign --force --options runtime "${timestamp_flag}" --sign "${identity}"
    codesign --force --options runtime "${timestamp_flag}" \
        --entitlements "${entitlements}" --sign "${identity}" "${HELPER_BIN}"
    codesign --force --options runtime "${timestamp_flag}" \
        --sign "${identity}" "${DEST_APP}"

    # Fail loud if signing broke the bundle (entitlements/hardened-runtime mismatch).
    if ! "${HELPER_BIN}" --version >/dev/null 2>&1; then
        echo "error: embedded daemon helper fails to launch after signing." >&2
        echo "error: check screencap-cli.entitlements against hardened-runtime needs." >&2
        exit 1
    fi
    # Verify the helper carries the expected bundle identity — a wrong/missing
    # Designated Requirement here means macOS will not attribute TCC grants to
    # com.screencap.daemon (the whole point of SCR-196).
    if ! codesign -d -r- "${DEST_APP}" 2>&1 | grep -q "com.screencap.daemon"; then
        echo "error: signed helper Designated Requirement does not name com.screencap.daemon." >&2
        codesign -d -r- "${DEST_APP}" >&2 || true
        exit 1
    fi
    # SCR-241/SCR-242: keychain-access-groups is a RESTRICTED entitlement that only
    # survives — and is only authorizable — on the Developer ID re-sign WITH an
    # embedded provisioning profile. This Xcode build phase runs under Apple
    # Development (which strips restricted entitlements) and does not embed the
    # profile; script/sign_app.sh owns embedding it and hard-asserting the
    # entitlement + profile at release time, and re-signs every nested Mach-O from
    # scratch — so this phase's entitlement state is not what ships. Asserting the
    # group here fails EVERY build under Apple Development (SCR-242 secondary bug #1),
    # so defer rather than assert.
    echo "note: keychain-access-groups authorization (entitlement + embedded provisioning"
    echo "note: profile) is applied and asserted by the Developer ID re-sign (script/sign_app.sh)."
    echo "Signed embedded daemon helper with team identity (DR names com.screencap.daemon; grants persist)."
}

sign_embedded_helper
