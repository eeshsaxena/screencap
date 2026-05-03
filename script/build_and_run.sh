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
RUN_LOG_DIR="$ROOT_DIR/.build/run"
STDOUT_LOG="$RUN_LOG_DIR/$APP_NAME.stdout.log"
STDERR_LOG="$RUN_LOG_DIR/$APP_NAME.stderr.log"

usage() {
  echo "usage: $0 [run|--debug|--logs|--telemetry|--verify]" >&2
  exit 2
}

prepend_path_if_dir() {
  local dir="$1"
  if [[ -d "$dir" && ":$PATH:" != *":$dir:"* ]]; then
    PATH="$dir:$PATH"
  fi
}

prepare_launch_env() {
  PATH="${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
  prepend_path_if_dir "$HOME/.pyenv/shims"
  prepend_path_if_dir "/opt/homebrew/bin"
  prepend_path_if_dir "/usr/local/bin"

  export PATH
  export SCREENCAP_DEV_REPO_ROOT="${SCREENCAP_DEV_REPO_ROOT:-$ROOT_DIR}"
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
  /bin/launchctl setenv PATH "$PATH"
  /bin/launchctl setenv SCREENCAP_DEV_REPO_ROOT "$SCREENCAP_DEV_REPO_ROOT"
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

  # Normalize PATH before any tool discovery so Homebrew-installed helpers
  # like xcodegen are found even when the script is launched from a minimal
  # GUI environment.
  prepare_launch_env
  generate_project_if_needed
  warn_if_ad_hoc_signing
  kill_existing_app
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
