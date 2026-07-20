---
title: "Fix binary install failure on macOS earlier than 14 (Intel Big Sur)"
date: 2026-03-16
problem_type: build-and-packaging
component: release-workflow, install-script, pyinstaller-spec
symptoms:
  - "Installation verification failed with no error output on macOS Big Sur 11.7.5"
  - "Binary silently killed by kernel (SIGKILL) before any code executes"
  - "Install script swallows all diagnostic output via >/dev/null 2>&1"
root_causes:
  - "Bundled dylibs from macos-14 CI runner carry minos 14.0 — kernel SIGKILLs process on macOS < 14"
  - "fast-gliner's ONNX Runtime 1.20.0 requires macOS 13.0 minimum, hidden behind wheel tag macosx_10_12"
  - "x86_64 binary built under Rosetta 2 on ARM64 runner inherits ARM-native Homebrew library versions"
tags:
  - macos
  - minos
  - pyinstaller
  - intel
  - rosetta
  - onnxruntime
  - install-script
  - pip-fallback
  - binary-compatibility
severity: critical
time_to_resolve: large
---

# Fix binary install failure on macOS earlier than 14

## Context

Screencap is a macOS screen recording CLI. It's distributed as a pre-built PyInstaller binary via `curl -sSfL https://get.screencap.sh | sh`, which downloads an architecture-specific tarball (ARM64 or x86_64) from GCS, verifies the checksum, and extracts it to `~/.screencap/bin/`. The binary bundles Python, all dependencies, and native libraries into a single `--onedir` directory.

## Problem

A user on a 2017 MacBook Air (Intel, macOS Big Sur 11.7.5) ran `curl -sSfL https://get.screencap.sh | sh` and got:

```
Installing screencap 0.11.0 for x86_64...
screencap-0.11.0-x86_64.tar.gz: OK
ERROR: Installation verification failed.
```

No further diagnostic information. The binary was downloaded, checksum verified, and extracted successfully — but it could not execute.

## Root Cause Analysis

Three compounding issues:

### 1. Mach-O `minos` deployment target mismatch (the killer)

Both ARM64 and x86_64 binaries were built on the same `macos-14` (Sonoma, ARM64) GitHub Actions runner. The x86_64 build used Rosetta 2 translation via `actions/setup-python` with `architecture: x64`.

PyInstaller bundles every native library (`.dylib`, `.so`) from the build system. On the `macos-14` runner, Homebrew-installed libraries carry `minos 14.0` in their `LC_BUILD_VERSION` Mach-O load command. The effective minimum macOS for the bundle is the **maximum `minos` across all collected binaries**.

On Big Sur 11.7.5, the kernel sees `minos 14.0`, terminates the process with **SIGKILL before any code executes**, and produces zero output. The binary appears to run and return nothing.

### 2. fast-gliner ONNX Runtime requires macOS 13+

`fast-gliner` wraps the `gline-rs` Rust crate, which pins `ort = "=2.0.0-rc.9"` bundling ONNX Runtime 1.20.0. This requires macOS 13.0 (Ventura) minimum — but the PyPI wheel is tagged `macosx_10_12_x86_64`, hiding the runtime requirement. Even fixing the Rosetta/minos issue, this library blocks Big Sur.

Python-level fallback in the privacy module catches `RuntimeError`/`OSError` and falls back to spaCy, but in the frozen binary the process is killed at dylib load time before Python starts.

### 3. Silent error reporting

The install script ran verification as:
```sh
if "$INSTALL_DIR/screencap/screencap" --version >/dev/null 2>&1; then
```
Both stdout and stderr redirected to `/dev/null`. SIGKILL produces no output anyway, so the user got zero diagnostic information.

## Minimum macOS Version Matrix

| Component | Minimum macOS | Source |
|---|---|---|
| Python 3.12 (python.org x86_64) | 10.13 | python.org universal2 |
| PyInstaller bootloader | 10.13 | Pre-compiled wheel |
| pyobjc >=9.0 | 10.9 | PyPI wheel |
| pydantic-core | 10.12 | PyPI wheel |
| av/PyAV wheels | 11.0 | pip source install |
| fast-gliner ONNX Runtime (dynamic) | **13.0** | ort crate, onnxruntime 1.20.0 |
| Homebrew libs on macos-14 runner | **14.0** | Compiled for host OS |
| numpy/X11 PyPI wheels | **14.0** | PyPI wheel platform tags |

**Effective minimum for pre-built binary**: macOS 14.0 (numpy/X11 wheels)
**Effective minimum for pip fallback**: macOS 11.0 (Big Sur)

## Solutions

### Solution 1: Native Intel runner + deployment target

Changed x86_64 matrix entry from `macos-14` (Rosetta) to `macos-15-intel` (native Intel runner). Set `MACOSX_DEPLOYMENT_TARGET: "11.0"` as env var.

Why the old approach (Rosetta + deployment target) was unsalvageable:
- `MACOSX_DEPLOYMENT_TARGET` only affects source compilations, not pre-compiled wheels
- Would require a post-build `vtool` sweep + re-codesigning of every pre-compiled binary

### Solution 2: Post-build minos verification

Added CI step that scans every bundled binary with `otool -l`, extracts `minos` from `LC_BUILD_VERSION` (or `LC_VERSION_MIN_MACOSX` for older binaries), and fails the build if any exceeds the target:

```bash
TARGET_MINOS="${{ matrix.minos-target }}"
while IFS= read -r f; do
  minos=$(otool -l "$f" 2>/dev/null | grep -A3 LC_BUILD_VERSION | grep minos | awk '{print $2}')
  if [ "$(printf '%s\n' "$TARGET_MINOS" "$minos" | sort -V | tail -1)" != "$TARGET_MINOS" ]; then
    echo "FAIL: $f has minos $minos (target: $TARGET_MINOS)"
    FAIL=1
  fi
done < <(find dist/screencap -type f \( -name "*.dylib" -o -name "*.so" -o -name "screencap" \))
```

### Solution 3: fast-gliner ONNX Runtime isolation

Bundle `fast-gliner` (which statically links ONNX Runtime with `minos 11.0`) but exclude the standalone `onnxruntime` package (which has `minos 14.0`). The `_smoke-test` includes a negative check that `import onnxruntime` fails.

### Solution 4: Reactive pip fallback in install.sh

Redesigned install script with binary-first, pip-fallback architecture:

1. Downloads and verifies binary as before
2. Runs `screencap --version` — captures output instead of swallowing it
3. If binary fails, automatically falls back to pip install:
   - Checks macOS >= 11 (av/PyAV minimum)
   - Checks for Xcode Command Line Tools
   - Finds Python 3.10+ (skipping Xcode CLT shim that pops GUI dialog)
   - If no Python, downloads python.org `.pkg` with SHA-256 + code signature verification
   - Creates venv at `~/.screencap/venv`, installs `screencap[record]`
   - Creates wrapper script (not symlink) at same PATH location
4. Always attempts binary on re-install; falls back to pip only when binary download or verify fails (no persistent install-method marker)

Override controls: `SCREENCAP_INSTALL_METHOD=pip` forces pip, `=binary` forces binary-only (CI).

### Solution 5: Improved error reporting

Verification now captures actual output. For the SIGKILL case (no output):
```
The binary exited without output. This usually means your macOS
version is older than what the binary was built for.
```

## Prevention Strategies

### 1. CI minos verification on every build

The `Verify deployment target (minos)` step runs after PyInstaller on every release and PR binary build. It catches:
- Homebrew library contamination from runner upgrades
- Dependency upgrades that raise their minimum macOS
- Build environment changes

### 2. Smoke tests on frozen binary

Two-layer smoke testing:
- `screencap --help` — basic binary launch
- `screencap _smoke-test` — validates 9 critical subsystems load correctly

### 3. Post-release install verification

`verify-install` job runs install.sh on both architectures after GCS upload, then runs `--version` + `_smoke-test`. Catches install script bugs, GCS upload issues, and checksum mismatches.

### 4. Binary-first install on every run

`install.sh` always attempts the binary first, falling back to pip only on download or verify failure. An earlier version persisted the install method to `~/.screencap/.install_method` and skipped the binary on subsequent runs if the marker was `pip`; that trap stranded users whose first install hit a broken binary. Users who want pip unconditionally can set `SCREENCAP_INSTALL_METHOD=pip`.

### 5. Key insight: `minos` is contagious

One library with `minos 14.0` makes the entire bundle require macOS 14. When adding any new native dependency:
1. Check its PyPI wheel platform tag
2. Run `otool -l` on its bundled `.dylib`/`.so` files
3. Check the `minos` field in `LC_BUILD_VERSION`
4. The CI minos check will catch this, but catching it before merge saves a release cycle

## Related Documentation

- `docs/solutions/build-errors/pyinstaller-frozen-binary-ci-failures.md` — related: smoke test failures from Presidio/spaCy bundling in frozen binaries
- `scripts/install.sh` — the install script with binary verification, pip fallback, and Rosetta detection
- `.github/workflows/release.yml` — release workflow with minos verification and verify-install job
- `pyinstaller/screencap.spec` — PyInstaller spec with `collect_all`, hidden imports, and `onnxruntime` exclusion
- Key commits: `b59bb17` (native Intel runner), `214c384` (pip fallback), `1379dd0` (raise minos to 14.0)
