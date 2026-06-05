---
title: "Fix PyInstaller bootloader/loader version mismatch from a stale localpycs cache"
date: 2026-06-05
problem_type: build-and-packaging
component: build-and-run-script, embed-cli-script, pyinstaller-bundle
symptoms:
  - "Bundled screencap CLI aborts at launch: 'RuntimeError: Bootloader did not set sys._pyinstaller_pyz!'"
  - "macOS app shows 'Couldn't load recordings — screencap exited with code 1'"
  - "Traceback frame: File pyimod02_importers.py, line 646, in install"
  - "dist/screencap/screencap --version fails identically when run directly from the terminal (rules out the Swift app)"
root_causes:
  - "build_and_run.sh built the bundle with the pyenv-global PyInstaller (6.9.0) instead of the repo .venv (6.20.0)"
  - "PyInstaller validates its cached bootstrap loaders (build/<name>/localpycs/*.pyc) by source mtime, not by PyInstaller version, so the 6.9.0 build silently reused a 6.20.0-seeded loader cache"
  - "The 6.9.0 bootloader never sets sys._pyinstaller_pyz, but the cached 6.20.0 loader requires it and raises at line 646"
tags:
  - pyinstaller
  - frozen-binary
  - bootloader
  - localpycs
  - build-cache
  - venv
  - pyenv
  - macos-app-shell
severity: high
time_to_resolve: medium
---

# Fix PyInstaller bootloader/loader version mismatch from a stale localpycs cache

## Context

ScreenCap ships its CLI as a PyInstaller `--onedir` frozen bundle (`dist/screencap/`) that the macOS app shell embeds and execs. After a routine rebuild, the bundled CLI stopped launching: the app's Recordings/Calendar views showed "Couldn't load recordings" and every CLI command exited 1. The failure was **in the bundle, not the app** — running `dist/screencap/screencap --version` straight from the terminal reproduced it identically.

## Symptoms

```
Traceback (most recent call last):
  File "PyInstaller/loader/pyiboot01_bootstrap.py", line 20, in <module>
  File "pyimod02_importers.py", line 646, in install
RuntimeError: Bootloader did not set sys._pyinstaller_pyz!
[xxxxx] Failed to execute script 'pyiboot01_bootstrap' due to unhandled exception!
```

- The macOS app surfaces this as `screencap exited with code 1` via `CLIClient`.
- `file dist/screencap/screencap` reports a valid arm64 Mach-O, and the executable bit is set — so naive "is it built / is it executable" checks all pass.

## What Didn't Work / Investigation

- **Blaming the Swift app.** `CLIClient.swift` execs the bundled binary directly; running the binary by hand failed the same way, so the app was exonerated immediately.
- **Assuming a corrupt or truncated binary.** It was a well-formed 35–45 MB Mach-O with an intact `_internal/` directory. The bundle layout was fine.

The decisive evidence came from treating the traceback line number as ground truth and comparing the two PyInstaller installs on the machine:

```bash
# Two PyInstaller versions coexisted:
.venv/bin/python -m PyInstaller --version          # 6.20.0  (repo virtualenv)
~/.pyenv/shims/python3 -m PyInstaller --version    # 6.9.0   (pyenv global)

# The 6.20.0 bootloader sets the attribute; the 6.9.0 one does not:
strings -a .venv/.../PyInstaller/bootloader/Darwin-64bit/run  | grep _pyinstaller_pyz   # match
strings -a ~/.pyenv/.../PyInstaller/bootloader/Darwin-64bit/run | grep _pyinstaller_pyz # no match

# Only the 6.20.0 loader SOURCE raises at line 646; 6.9.0 has no such check:
grep -n "Bootloader did not set" .venv/.../PyInstaller/loader/pyimod02_importers.py     # line 646
grep -n "Bootloader did not set" ~/.pyenv/.../PyInstaller/loader/pyimod02_importers.py  # nothing

# The cached loader was older than the rebuild and never regenerated:
ls -la build/screencap/localpycs/*.pyc   # compiled by the 6.20.0 build, NOT rebuilt
```

The traceback frame `pyimod02_importers.py:646` matched the **6.20.0** source verbatim, proving the running loader was 6.20.0 — while the bootloader (which never set `sys._pyinstaller_pyz`) was 6.9.0.

## Solution

The fix has two parts: build with one consistent PyInstaller, and never silently ship a non-launching bundle.

`script/build_and_run.sh`:

```bash
# resolve_dev_python(): prefer the repo's pinned PyInstaller over the pyenv global
if [[ -z "$python_cmd" && -x "$ROOT_DIR/.venv/bin/python3" ]]; then
  python_cmd="$ROOT_DIR/.venv/bin/python3"
fi
# ...pyenv shim is now only a fallback when no .venv exists.

# build_cli_if_needed(): wipe the cross-version-poisonable loader cache, then verify launch
(
  cd "$ROOT_DIR"
  rm -rf "$ROOT_DIR/build/screencap/localpycs"
  "$SCREENCAP_DEV_PYTHON" -m PyInstaller --noconfirm "$ROOT_DIR/pyinstaller/screencap.spec"
)
if ! "$CLI_BINARY" --version >/dev/null 2>&1; then
  echo "error: freshly built screencap bundle fails to launch ..." >&2
  exit 1
fi
```

`macos/ScreenCap/Scripts/embed-cli.sh` — refuse to embed a binary that doesn't run (the old check only tested the executable bit):

```bash
if ! "${SOURCE_DIR}/screencap" --version >/dev/null 2>&1; then
    echo "error: ${SOURCE_DIR}/screencap does not launch — refusing to embed a broken bundle." >&2
    exit 1
fi
```

Immediate unblock for an already-broken bundle: `rm -rf build dist && .venv/bin/python -m PyInstaller --noconfirm pyinstaller/screencap.spec` (a single consistent PyInstaller + cleared cache), then re-embed into the `.app`.

## Why This Works

`sys._pyinstaller_pyz` is set by the **compiled C bootloader** (the `run` executable PyInstaller copies and renames). The **Python loader** (`pyimod02_importers.install()`) reads it. Both ship inside one PyInstaller package, so within a single version they always agree.

The mix arose because PyInstaller caches its compiled bootstrap loaders in `build/<name>/localpycs/` and validates that cache by **source mtime**, not by PyInstaller version. The pyenv 6.9.0 loader sources were *older* than the cache the earlier 6.20.0 build had written, so 6.9.0 judged the cache "fresh" and reused 6.20.0's loader. Pairing a 6.9.0 bootloader (no `sys._pyinstaller_pyz`) with a 6.20.0 loader (requires it) is the abort. Building with one pinned PyInstaller makes bootloader, loader, and cache the same version; clearing `localpycs` forces the active PyInstaller to recompile loaders that match its own bootloader.

## Prevention

- **Build frozen bundles with one pinned PyInstaller**, not whatever a global shim resolves to. Prefer the project `.venv`.
- **Never trust PyInstaller's `localpycs` cache across versions** — it is mtime-keyed, not version-keyed. Clear `build/<name>/localpycs` (or use `pyinstaller --clean`) whenever the PyInstaller version may have changed.
- **Smoke-run a freshly built bundle before shipping or embedding it** (`screencap --version` exercises the full bootstrap and exits offline). An executable-bit check is not enough — a broken bundle is still executable.
- When a frozen binary fails with a bootstrap error, **trust the traceback's file:line against the installed loader source** to identify which PyInstaller version is actually running.

## Related Documentation

- [`pyinstaller-frozen-binary-ci-failures.md`](pyinstaller-frozen-binary-ci-failures.md) — sibling PyInstaller-bundling failure, but a data-collection problem (Presidio/spaCy model not collected) rather than a bootloader/loader version mismatch.
- [`macos-pre14-binary-install-failure.md`](macos-pre14-binary-install-failure.md) — the other "frozen binary won't launch" mode (kernel SIGKILL from `minos`/ONNX Runtime), useful for disambiguating a Python-level bootstrap `RuntimeError` from a pre-exec kill.
