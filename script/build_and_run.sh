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
  if [[ -x "$CLI_BINARY" ]] && "$CLI_BINARY" serve --help >/dev/null 2>&1; then
    return
  fi

  if [[ -z "${SCREENCAP_DEV_PYTHON:-}" ]]; then
    echo "error: unable to resolve python3 for PyInstaller CLI build." >&2
    exit 1
  fi

  echo "Building screencap CLI bundle for helper..."
  # Subshell-cd so PyInstaller writes dist/ and build/ relative to the repo
  # root regardless of where this script was invoked from. The outer cwd
  # stays unchanged.
  (
    cd "$ROOT_DIR"
    "$SCREENCAP_DEV_PYTHON" -m PyInstaller --noconfirm "$ROOT_DIR/pyinstaller/screencap.spec"
  )
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

  # Pick up local-only config (DEVELOPMENT_TEAM etc.) before tool discovery so
  # xcodegen and xcodebuild see the right signing identity.
  load_local_env

  # Normalize PATH before any tool discovery so Homebrew-installed helpers
  # like xcodegen are found even when the script is launched from a minimal
  # GUI environment.
  prepare_launch_env
  generate_project_if_needed
  warn_if_ad_hoc_signing
  kill_existing_app
  build_cli_if_needed
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
