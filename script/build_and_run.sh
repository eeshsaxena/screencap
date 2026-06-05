#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-run}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MACOS_DIR="$ROOT_DIR/macos"
PROJECT_FILE="$MACOS_DIR/ScreenCap.xcodeproj"
PROJECT_YML="$MACOS_DIR/project.yml"
SCHEME="ScreenCap"
CONFIGURATION="Debug"
APP_NAME="ScreenCap"
BUNDLE_ID="com.screencap.macos"
DERIVED_DATA="$ROOT_DIR/.build/ScreenCapDerivedData"
APP_BUNDLE="$DERIVED_DATA/Build/Products/$CONFIGURATION/$APP_NAME.app"
APP_BINARY="$APP_BUNDLE/Contents/MacOS/$APP_NAME"
CLI_BUNDLE="$ROOT_DIR/dist/screencap"
CLI_BINARY="$CLI_BUNDLE/screencap"
RUN_LOG_DIR="$ROOT_DIR/.build/run"
STDOUT_LOG="$RUN_LOG_DIR/$APP_NAME.stdout.log"
STDERR_LOG="$RUN_LOG_DIR/$APP_NAME.stderr.log"

usage() {
  echo "usage: $0 [run|--debug|--logs|--telemetry|--verify]" >&2
  exit 2
}

load_local_env() {
  # Parse repo-root .env if present so local-only config (e.g. DEVELOPMENT_TEAM)
  # reaches xcodegen and xcodebuild without a manual `source` step.
  # The file is gitignored; see macos/README.md for what belongs in it.
  #
  # We deliberately don't `source` it: `source` would evaluate backticks,
  # $(…), and trailing `;` payloads as shell, turning a misplaced .env
  # into arbitrary code execution. Parse KEY=VALUE lines literally instead.
  local env_file="$ROOT_DIR/.env"
  if [[ ! -f "$env_file" ]]; then
    return
  fi

  local key value
  while IFS='=' read -r key value || [[ -n "$key" ]]; do
    # Skip blank lines and comments.
    if [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]]; then
      continue
    fi
    # Trim whitespace around the key; reject anything that isn't a
    # plausible identifier so malformed lines don't smuggle in syntax.
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      continue
    fi
    # Strip optional surrounding single or double quotes from the value.
    value="${value%$'\r'}"
    if [[ "$value" =~ ^\".*\"$ ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" =~ ^\'.*\'$ ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < "$env_file"
}

prepend_path_if_dir() {
  local dir="$1"
  local entry
  local new_path="$dir"
  local path_entries=()

  if [[ ! -d "$dir" ]]; then
    return
  fi

  IFS=":" read -r -a path_entries <<<"${PATH:-}"
  for entry in "${path_entries[@]}"; do
    [[ -z "$entry" || "$entry" == "$dir" ]] && continue
    new_path="$new_path:$entry"
  done

  PATH="$new_path"
}

prepare_launch_env() {
  PATH="${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
  prepend_path_if_dir "$HOME/.pyenv/shims"
  prepend_path_if_dir "/opt/homebrew/bin"
  prepend_path_if_dir "/usr/local/bin"

  export PATH
  export SCREENCAP_DEV_REPO_ROOT="${SCREENCAP_DEV_REPO_ROOT:-$ROOT_DIR}"
  resolve_dev_python
}

resolve_dev_python() {
  local python_cmd="${SCREENCAP_DEV_PYTHON:-}"
  local resolved_python

  # Prefer the repo's own virtualenv: it carries the project-pinned PyInstaller.
  # Building with a *different* PyInstaller than the one that seeded build/'s
  # cached bootstrap loaders produces a bootloader/loader version mix that fails
  # at runtime ("Bootloader did not set sys._pyinstaller_pyz!"). The pyenv shim
  # below is an unpinned global and is only a fallback when no .venv exists.
  if [[ -z "$python_cmd" && -x "$ROOT_DIR/.venv/bin/python3" ]]; then
    python_cmd="$ROOT_DIR/.venv/bin/python3"
  fi
  if [[ -z "$python_cmd" && -x "$HOME/.pyenv/shims/python3" ]]; then
    python_cmd="$HOME/.pyenv/shims/python3"
  fi
  if [[ -z "$python_cmd" ]]; then
    python_cmd="$(command -v python3 || true)"
  fi
  if [[ -z "$python_cmd" ]]; then
    return
  fi

  if resolved_python="$("$python_cmd" -c 'import sys; print(sys.executable)' 2>/dev/null)" && [[ -x "$resolved_python" ]]; then
    export SCREENCAP_DEV_PYTHON="$resolved_python"
  fi
}

warn_if_ad_hoc_signing() {
  local effective_team="${DEVELOPMENT_TEAM:-}"
  if [[ -z "$effective_team" && -f "$PROJECT_FILE/project.pbxproj" ]]; then
    effective_team="$(sed -n 's/.*DEVELOPMENT_TEAM = \([^;]*\);.*/\1/p' "$PROJECT_FILE/project.pbxproj" | tr -d ' "' | head -n 1)"
  fi

  if [[ -z "$effective_team" ]]; then
    echo "warning: DEVELOPMENT_TEAM is not set." >&2
    echo "warning: Xcode will use ad-hoc signing, and macOS TCC permissions may reset on every rebuild." >&2
  fi
}

publish_launch_env() {
  # NOTE: `launchctl setenv` here publishes vars to the entire GUI session,
  # so every LaunchAgent-spawned process (not just our daemon) inherits
  # SCREENCAP_DAEMON_USE_DEV_SOURCE, SCREENCAP_DEV_REPO_ROOT, etc. That's
  # the trade-off that lets the daemon helper see them without per-process
  # plumbing. If you ever care about scoping these to just the daemon,
  # switch to `launchctl bootout` + a custom plist with EnvironmentVariables.
  /bin/launchctl setenv PATH "$PATH"
  /bin/launchctl setenv SCREENCAP_DEV_REPO_ROOT "$SCREENCAP_DEV_REPO_ROOT"
  if [[ -n "${SCREENCAP_DEV_PYTHON:-}" ]]; then
    /bin/launchctl setenv SCREENCAP_DEV_PYTHON "$SCREENCAP_DEV_PYTHON"
  else
    /bin/launchctl unsetenv SCREENCAP_DEV_PYTHON
  fi
  if [[ "${SCREENCAP_DAEMON_USE_DEV_SOURCE:-0}" == "1" ]]; then
    /bin/launchctl setenv SCREENCAP_DAEMON_USE_DEV_SOURCE "1"
  else
    /bin/launchctl unsetenv SCREENCAP_DAEMON_USE_DEV_SOURCE
  fi
}

generate_project_if_needed() {
  local should_generate=0

  if [[ ! -d "$PROJECT_FILE" ]]; then
    should_generate=1
  elif [[ "$PROJECT_YML" -nt "$PROJECT_FILE/project.pbxproj" ]]; then
    should_generate=1
  elif [[ -n "$(find "$MACOS_DIR/ScreenCap" "$MACOS_DIR/ScreenCapTests" -type d -newer "$PROJECT_FILE/project.pbxproj" -print -quit 2>/dev/null)" ]]; then
    # xcodegen globs sources at generation time, so adding/removing/renaming a
    # source file changes the project's file list without touching project.yml.
    # Those operations bump the containing directory's mtime (plain content
    # edits do not), so a source dir newer than the generated project means the
    # baked-in file list is stale. Without this, a newly added .swift file is
    # silently left out of the build and every reference to it fails with
    # "cannot find ... in scope" against the stale .pbxproj.
    should_generate=1
  elif [[ -n "${DEVELOPMENT_TEAM:-}" ]]; then
    # `project.yml` reads DEVELOPMENT_TEAM at xcodegen time, not build time.
    should_generate=1
  fi

  if [[ "$should_generate" -eq 0 ]]; then
    return
  fi

  if ! command -v xcodegen >/dev/null 2>&1; then
    echo "error: xcodegen is required to generate $PROJECT_FILE." >&2
    echo "error: install it with 'brew install xcodegen'." >&2
    exit 1
  fi

  echo "Generating Xcode project..."
  (
    cd "$MACOS_DIR"
    xcodegen generate
  )
}

build_cli_if_needed() {
  # `--no-update-check` is critical: the `cli` group callback runs
  # `maybe_check_for_update()` even on subcommand `--help`, and when the
  # bundled CLI is older than the GCS-published version it calls
  # `click.confirm()`. The prompt goes to the redirected stdout, but stdin
  # is still the user's terminal, so the binary blocks on input() forever
  # and the script appears stuck at this phase.

  # Dev-source mode bypasses the bundled binary entirely (the launcher
  # execs the in-repo source via $SCREENCAP_DEV_PYTHON). Skip the
  # staleness check + rebuild so iteration stays fast in that mode.
  if [[ "${SCREENCAP_DAEMON_USE_DEV_SOURCE:-0}" == "1" ]]; then
    return
  fi

  local needs_rebuild=0
  local rebuild_reason=""

  if [[ ! -x "$CLI_BINARY" ]] || ! "$CLI_BINARY" --no-update-check serve --help >/dev/null 2>&1; then
    needs_rebuild=1
    rebuild_reason="bundle missing or broken"
  elif [[ -n "$(find "$ROOT_DIR/src/screencap" -name '*.py' -newer "$CLI_BINARY" -print -quit 2>/dev/null)" ]]; then
    # Any .py file in src/screencap is newer than the bundled binary —
    # without this check the daemon would silently run stale code, and
    # the developer would think their fix didn't work when it just
    # wasn't running. PyInstaller rebuild is slow (minutes); use
    # SCREENCAP_DAEMON_USE_DEV_SOURCE=1 to bypass it for fast iteration.
    needs_rebuild=1
    rebuild_reason="src/screencap/*.py is newer than the bundled binary"
  fi

  if [[ "$needs_rebuild" -eq 0 ]]; then
    return
  fi

  if [[ -z "${SCREENCAP_DEV_PYTHON:-}" ]]; then
    echo "error: unable to resolve python3 for PyInstaller CLI build." >&2
    exit 1
  fi

  echo "Building screencap CLI bundle for helper ($rebuild_reason)..."
  # Subshell-cd so PyInstaller writes dist/ and build/ relative to the repo
  # root regardless of where this script was invoked from. The outer cwd
  # stays unchanged.
  (
    cd "$ROOT_DIR"
    # Drop the cached bootstrap loaders before rebuilding. PyInstaller validates
    # this cache by source mtime, not by PyInstaller version, so a cache left by
    # a different PyInstaller than the one now providing the bootloader gets
    # silently reused — yielding a bootloader/loader version mix that crashes at
    # runtime with "Bootloader did not set sys._pyinstaller_pyz!". Wiping it
    # forces the active PyInstaller to recompile loaders matching its bootloader.
    rm -rf "$ROOT_DIR/build/screencap/localpycs"
    "$SCREENCAP_DEV_PYTHON" -m PyInstaller --noconfirm "$ROOT_DIR/pyinstaller/screencap.spec"
  )

  # Fail loud if the freshly built bundle can't even start. embed-cli.sh only
  # checks the executable bit, so without this a broken binary is embedded and
  # the app surfaces a confusing "screencap exited with code 1" at runtime.
  if ! "$CLI_BINARY" --version >/dev/null 2>&1; then
    echo "error: freshly built screencap bundle fails to launch ($CLI_BINARY --version)." >&2
    echo "error: typically a PyInstaller bootloader/loader version mismatch — try a clean rebuild (rm -rf build dist)." >&2
    "$CLI_BINARY" --version || true
    exit 1
  fi
}

restart_daemon_if_loaded() {
  # launchd's KeepAlive keeps the daemon process alive across app and CLI
  # rebuilds. Without an explicit kickstart, a freshly-built CLI bundle or
  # an updated launchctl setenv (e.g. SCREENCAP_DAEMON_USE_DEV_SOURCE)
  # never reaches the running daemon — the developer tests fresh source
  # against stale execution. Kickstart -k forces the LaunchAgent to
  # terminate and respawn from the current plist / env / binary.
  local uid
  uid="$(id -u)"
  if ! /bin/launchctl print "gui/$uid/com.screencap.daemon" >/dev/null 2>&1; then
    # Daemon not loaded yet (e.g. first run, or after `screencap serve
    # --uninstall`). Nothing to restart — the SwiftUI app will install it
    # on launch via SMAppService.
    return
  fi
  /bin/launchctl kickstart -k "gui/$uid/com.screencap.daemon" >/dev/null 2>&1 || true
}

kill_existing_app() {
  pkill -x "$APP_NAME" >/dev/null 2>&1 || true

  for _ in $(seq 1 30); do
    if ! pgrep -x "$APP_NAME" >/dev/null 2>&1; then
      return
    fi
    sleep 0.2
  done
}

build_app() {
  mkdir -p "$RUN_LOG_DIR" "$DERIVED_DATA"

  xcodebuild \
    -project "$PROJECT_FILE" \
    -scheme "$SCHEME" \
    -configuration "$CONFIGURATION" \
    -derivedDataPath "$DERIVED_DATA" \
    build

  if [[ ! -x "$APP_BINARY" ]]; then
    echo "error: built app binary not found at $APP_BINARY" >&2
    exit 1
  fi
}

launch_app() {
  prepare_launch_env
  publish_launch_env
  # Kickstart the daemon AFTER publish_launch_env so the respawned helper
  # inherits the freshly-set launchctl env (PATH, SCREENCAP_DEV_PYTHON,
  # SCREENCAP_DAEMON_USE_DEV_SOURCE). Without this, env changes published
  # by this script never reach the long-lived daemon process.
  echo "==> Restarting daemon helper (if loaded)"
  restart_daemon_if_loaded

  : >"$STDOUT_LOG"
  : >"$STDERR_LOG"

  /usr/bin/open -n "$APP_BUNDLE"

  echo "Launched $APP_NAME through LaunchServices."
  echo "Logs: ./script/build_and_run.sh --logs"
}

launch_debugger() {
  prepare_launch_env
  env \
    PATH="$PATH" \
    SCREENCAP_DEV_REPO_ROOT="$SCREENCAP_DEV_REPO_ROOT" \
    SCREENCAP_DEV_PYTHON="${SCREENCAP_DEV_PYTHON:-}" \
    lldb -- "$APP_BINARY"
}

verify_launch() {
  launch_app

  for _ in $(seq 1 20); do
    if pgrep -x "$APP_NAME" >/dev/null 2>&1; then
      sleep 2
      if ! pgrep -x "$APP_NAME" >/dev/null 2>&1; then
        echo "error: $APP_NAME exited shortly after launch." >&2
        exit 1
      fi
      echo "$APP_NAME is running."
      return
    fi
    sleep 0.25
  done

  echo "error: $APP_NAME did not stay running." >&2
  echo "error: run ./script/build_and_run.sh --logs for unified logs." >&2
  exit 1
}

stream_logs() {
  launch_app
  /usr/bin/log stream --info --style compact --predicate "process == \"$APP_NAME\""
}

stream_telemetry() {
  launch_app
  /usr/bin/log stream --info --style compact --predicate "subsystem == \"$BUNDLE_ID\""
}

main() {
  case "$MODE" in
    run|--debug|debug|--logs|logs|--telemetry|telemetry|--verify|verify)
      ;;
    *)
      usage
      ;;
  esac

  # Progress echoes for each phase: without them the script is silent for
  # ~5-15s before xcodebuild produces its first line of output (env parse +
  # PATH munging + pkill/pgrep wait + CLI cache check + xcodebuild's own
  # dependency-graph prelude), which reads as a hang.

  # Pick up local-only config (DEVELOPMENT_TEAM etc.) before tool discovery so
  # xcodegen and xcodebuild see the right signing identity.
  echo "==> Loading .env"
  load_local_env

  # Normalize PATH before any tool discovery so Homebrew-installed helpers
  # like xcodegen are found even when the script is launched from a minimal
  # GUI environment.
  echo "==> Preparing launch environment"
  prepare_launch_env
  echo "==> Checking Xcode project"
  generate_project_if_needed
  warn_if_ad_hoc_signing
  echo "==> Stopping any running $APP_NAME"
  kill_existing_app
  echo "==> Checking CLI bundle"
  build_cli_if_needed
  echo "==> Building app (xcodebuild)"
  build_app

  case "$MODE" in
    run)
      launch_app
      ;;
    --debug|debug)
      launch_debugger
      ;;
    --logs|logs)
      stream_logs
      ;;
    --telemetry|telemetry)
      stream_telemetry
      ;;
    --verify|verify)
      verify_launch
      ;;
  esac
}

main
