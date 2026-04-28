---
name: local-release
description: Bump version, build, and release screencap locally. Mirrors the /release flow (version bump, changelog, commit, tag, push) but builds locally instead of triggering GH Actions. Supports arm64 (native on Apple Silicon), x86_64 (requires python.org universal2 Python 3.12 + Rosetta), and both. Use when CI is unavailable (billing limits, runner outages). Triggers on "local release", "release locally", "build and release", or when CI release fails.
---

# Local Release

Bump version, update changelog, commit, tag, push, then build a PyInstaller binary locally and publish to GCS + GitHub Releases. Mirrors the `/release` flow but builds on the local Mac instead of triggering GitHub Actions.

## Handling Args

| Usage | Effect |
|---|---|
| `/local-release` | prompt for version and arch, then bump + build + release |
| `/local-release 0.18.0` | bump to that version, then build + release (prompts for arch) |
| `/local-release 0.18.0 --arch arm64` | arm64 only (fast path on Apple Silicon) |
| `/local-release 0.18.0 --arch x86_64` | x86_64 only (needs python.org universal2 Python; see §x86_64 Prereqs) |
| `/local-release 0.18.0 --arch both` | sequential arm64 + x86_64 build, single release with both tarballs |
| `/local-release --build-only [--arch ...]` | build and smoke test without uploading (still bumps version if one is provided) |
| `/local-release --release-only [--arch ...]` | skip build, upload existing `dist/` artifacts (for re-runs after a failed upload) |

Default arch if unspecified: prompt the user (`arm64` / `x86_64` / `both`).

## Prerequisites

Always:
1. `gcloud` CLI installed and authenticated with write access to `gs://screencap-releases`
2. `gh` CLI authenticated with repo write access
3. Working tree clean (`git status --short` shows nothing)
4. On macOS (arm64 Apple Silicon OR Intel)

For `--arch arm64`:
- Running on an arm64 Mac (Apple Silicon) with Python 3.12+
- Or: running on an Intel Mac under Rosetta (works but slower)

### x86_64 Prereqs

Building x86_64 on Apple Silicon requires either a native Intel Mac OR python.org's universal2 Python (NOT Homebrew Python, which is single-arch). The installer at https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg ships Python with both x86_64 and arm64 slices.

Before running `--arch x86_64` or `--arch both` on Apple Silicon, verify:

```bash
# Find a universal2 Python executable
PY_UNIVERSAL=$(find /Library/Frameworks/Python.framework/Versions -name python3.12 -maxdepth 3 2>/dev/null | head -1)
test -x "$PY_UNIVERSAL" || { echo "python.org universal2 Python 3.12 not found; install from https://www.python.org/downloads/macos/"; exit 1; }

# Confirm it contains an x86_64 slice
file "$PY_UNIVERSAL" | grep -q "Mach-O universal binary" || { echo "$PY_UNIVERSAL is not universal2"; exit 1; }
file "$PY_UNIVERSAL" | grep -q "x86_64" || { echo "$PY_UNIVERSAL lacks x86_64 slice"; exit 1; }
```

If this fails, stop and tell the user to install the python.org universal2 Python 3.12 installer. Do NOT attempt an x86_64 build with Homebrew Python — it is arm64-only and will silently produce an arm64 binary labelled x86_64.

## Instructions

### Step 1: Determine New Version and Arch

Read the current version from `pyproject.toml`.

If the user did NOT provide a version arg:
- Show commits since the last tag: `git log $(git describe --tags --abbrev=0)..HEAD --oneline --no-merges`
- Suggest a version (patch for fixes, minor for features) and ask the user to confirm

If the user did NOT provide `--arch`, ask via AskUserQuestion: arm64 / x86_64 / both.

Report: "Releasing screencap v{VERSION} for {arch(es)}."

### Step 2: Bump Version and Update CHANGELOG

1. Update `pyproject.toml` version field
2. Add a new section to `CHANGELOG.md` for this version with today's date, categorizing commits since the last tag into Added/Changed/Fixed sections (skip chores/tests/docs)
3. Commit both files: `git commit -m "bump version to {VERSION}"` (match recent commit style)
4. Create tag: `git tag v{VERSION}`
5. Push commit and tag: `git push origin main && git push origin v{VERSION}`

The tag push will trigger GH Actions `release.yml` in parallel. If CI succeeds, it will produce its own tarballs and (for `both`) may collide with the local build on GCS. That's OK: same bits either way. For stable tags, the `promote-latest` CI job updates `latest.txt` only after verify-install passes.

### Step 3: Build (skip if `--release-only`)

Run once per requested arch. For `--arch both`, run arm64 first, then x86_64.

Use a dedicated venv per arch so they don't poison each other. Default venv path: `.venv` (arm64 on Apple Silicon, x86_64 on Intel Mac). For a second-arch build on Apple Silicon, create `.venv-x86_64` under Rosetta.

#### arm64 path (native on Apple Silicon)

```bash
# Use existing .venv if already arm64-aligned; otherwise create
if [ ! -d .venv ]; then python3.12 -m venv .venv; fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[record]"
.venv/bin/pip install pyinstaller
.venv/bin/python -m spacy download en_core_web_sm
.venv/bin/pyinstaller pyinstaller/screencap.spec -y
```

#### x86_64 path (Rosetta on Apple Silicon, or native on Intel)

```bash
# Create venv from the x86_64 slice of the python.org universal2 installer
PY_UNIVERSAL=$(find /Library/Frameworks/Python.framework/Versions/3.12 -name python3.12 -maxdepth 3 | head -1)
arch -x86_64 "$PY_UNIVERSAL" -m venv .venv-x86_64
arch -x86_64 .venv-x86_64/bin/pip install --upgrade pip

# Apply the x86_64 constraint file — same as CI — so pip selects wheels with minos <= 11.0.
# MACOSX_DEPLOYMENT_TARGET=11.0 mirrors the workflow-level setting in
# .github/workflows/release.yml for any build-time tooling that reads it
# (PyInstaller bootloader build, C extensions compiled from source, etc.).
# Note: it does NOT shift pip's wheel-tag preference — packaging.tags reads
# platform.mac_ver() at runtime, so on a Tahoe (macOS 26.x) host pip still
# prefers macosx_26_0 → macosx_14_0 → ... wheels regardless of this env.
# Wheel selection is enforced by the version pins in constraints-x86_64.txt.
arch -x86_64 env MACOSX_DEPLOYMENT_TARGET=11.0 PIP_CONSTRAINT="$PWD/pyinstaller/constraints-x86_64.txt" \
  .venv-x86_64/bin/pip install -e ".[record]"
arch -x86_64 .venv-x86_64/bin/pip install pyinstaller
arch -x86_64 .venv-x86_64/bin/python -m spacy download en_core_web_sm

# Clean dist/ and build/ first so the arm64 build output isn't reused
rm -rf dist build

arch -x86_64 .venv-x86_64/bin/pyinstaller pyinstaller/screencap.spec -y
```

### Step 4: Minos Verify (x86_64 only)

CI has a minos verification step that catches bundled binaries whose Mach-O `minos` exceeds the target. The local x86_64 build bypasses that gate, so replicate it manually. Target: `11.0` for x86_64 (matches `.github/workflows/release.yml` matrix).

```bash
TARGET_MINOS="11.0"
FAIL=0
while IFS= read -r f; do
  minos=$(otool -l "$f" 2>/dev/null | grep -A3 LC_BUILD_VERSION | grep minos | awk '{print $2}')
  if [ -z "$minos" ]; then
    minos=$(otool -l "$f" 2>/dev/null | grep -A3 LC_VERSION_MIN_MACOSX | grep version | head -1 | awk '{print $2}')
  fi
  if [ -n "$minos" ]; then
    if [ "$(printf '%s\n' "$TARGET_MINOS" "$minos" | sort -V | tail -1)" != "$TARGET_MINOS" ]; then
      echo "FAIL: $f has minos $minos (target: $TARGET_MINOS)"
      FAIL=1
    fi
  fi
done < <(find dist/screencap -type f \( -name "*.dylib" -o -name "*.so" -o -name "screencap" \))
[ "$FAIL" -eq 0 ] || { echo "::error::x86_64 build has bundled binaries exceeding minos 11.0 — STOP, do not publish"; exit 1; }
```

For `--arch arm64`, skip this step (target is 14.0 and pip resolution naturally lands there on Apple Silicon).

### Step 5: Smoke Test (skip if `--release-only`)

```bash
./dist/screencap/screencap --help
./dist/screencap/screencap _smoke-test
```

Both must exit 0. If `_smoke-test` fails, stop and report the error.

### Step 6: Package

Use a per-arch checksum file matching the CI convention (release workflow writes `checksums-${arch}.sha256` and the release job merges them into a combined `checksums.sha256`).

```bash
VERSION=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
ARCH=arm64   # or x86_64

tar czf "screencap-${VERSION}-${ARCH}.tar.gz" -C dist screencap/
shasum -a 256 "screencap-${VERSION}-${ARCH}.tar.gz" > "checksums-${ARCH}.sha256"
```

Display the checksum for the user to verify.

**Stop here if `--build-only`** — report the tarball path and checksum.

### Step 7: Upload Tarball to GCS

Upload only the tarball and per-arch checksum file. Do NOT overwrite `install.sh` or `latest.txt` — those are shared state controlled by CI's `release` and `promote-latest` jobs.

```bash
VERSION=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
ARCH=arm64   # or x86_64

gcloud storage cp "screencap-${VERSION}-${ARCH}.tar.gz"    "gs://screencap-releases/releases/v${VERSION}/"
gcloud storage cp "checksums-${ARCH}.sha256"               "gs://screencap-releases/releases/v${VERSION}/"
```

Verify upload:
```bash
gcloud storage ls "gs://screencap-releases/releases/v${VERSION}/"
```

### Step 8: Merge Checksums and Upload Combined File (last arch only)

After the final arch has been uploaded (for `both`, this is x86_64; for single-arch, this is that arch), rebuild the combined `checksums.sha256` that the installer reads.

```bash
VERSION=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")

mkdir -p /tmp/screencap-release-merge
gcloud storage cp "gs://screencap-releases/releases/v${VERSION}/checksums-*.sha256" /tmp/screencap-release-merge/
cat /tmp/screencap-release-merge/checksums-*.sha256 > /tmp/screencap-release-merge/checksums.sha256
gcloud storage cp /tmp/screencap-release-merge/checksums.sha256 "gs://screencap-releases/releases/v${VERSION}/"
```

This keeps `install.sh`'s `shasum -c checksums.sha256 --ignore-missing` working for both arches. For single-arch releases, the combined file contains just the one line — `--ignore-missing` handles the absent arch gracefully.

### Step 9: Create or Update GitHub Release

Extract release notes from CHANGELOG.md:

```bash
VERSION=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
awk "/^## \[${VERSION}\]/{found=1; next} /^## \[/{if(found) exit} found" CHANGELOG.md > /tmp/release_notes.txt
if [ ! -s /tmp/release_notes.txt ]; then
    echo "See [CHANGELOG.md](CHANGELOG.md) for details." > /tmp/release_notes.txt
fi
```

Create or update the release with all artifacts present locally:

```bash
# List all locally-built tarballs for this version (one or both arches)
ASSETS=$(ls screencap-${VERSION}-*.tar.gz /tmp/screencap-release-merge/checksums.sha256 2>/dev/null)

if ! gh release create "v${VERSION}" \
    --title "v${VERSION}" \
    --notes-file /tmp/release_notes.txt \
    $ASSETS; then
    # Release already exists (e.g., CI got there first) — update notes and upsert assets.
    gh release edit "v${VERSION}" --notes-file /tmp/release_notes.txt
    gh release upload "v${VERSION}" $ASSETS --clobber
fi
```

### Step 10: Verify Install

Run the install script against the just-published release. For `--arch both`, test both arches if you have hardware for each.

```bash
SCREENCAP_VERSION="${VERSION}" SCREENCAP_NO_MODIFY_PATH="1" SCREENCAP_INSTALL_METHOD="binary" \
  bash scripts/install.sh
xattr -dr com.apple.quarantine ~/.screencap/bin/
~/.screencap/bin/screencap/screencap --version
~/.screencap/bin/screencap/screencap _smoke-test
```

### Step 11: Cleanup

```bash
rm -f screencap-*.tar.gz checksums-*.sha256 /tmp/release_notes.txt
rm -rf /tmp/screencap-release-merge
```

Do NOT `rm -rf dist/` or `rm -rf build/` — they are reusable for subsequent builds. Do NOT remove `.venv` or `.venv-x86_64` — they are reusable too.

### Step 12: Report

```
## Local Release: v{VERSION}

- Arches built: {arm64, x86_64, or both}
- GCS: gs://screencap-releases/releases/v{VERSION}/
- GitHub: https://github.com/proteus-computer-use/screencap/releases/tag/v{VERSION}
- Install verified: {yes/no per arch}
- latest.txt: unchanged (CI's promote-latest job owns this)

{If arm64 only:} Note: Intel users will fall back to pip install via install.sh until
an x86_64 tarball exists at gs://screencap-releases/releases/v{VERSION}/.
```

## Error Handling

- **PyInstaller fails**: Check `.[record]` extras installed correctly (especially PyObjC frameworks).
- **Smoke test fails**: Read the error — usually a missing hidden import in `screencap.spec`.
- **Minos verify fails on x86_64**: A bundled dylib exceeds 11.0. The `pyinstaller/constraints-x86_64.txt` pins should prevent this; if they don't, inspect which wheel slipped through and add a tighter pin.
- **GCS upload fails**: Verify `gcloud auth list` shows an account with Storage Object Admin on `screencap-releases`.
- **GitHub release exists**: The skill already handles this via `gh release edit` + `gh release upload --clobber`.
- **Install verification fails**: Check `xattr -dr` was run. If the binary crashes, the build is bad — do not publish.
- **x86_64 venv build fails at first `pip install`**: Verify the venv is actually x86_64 via `file .venv-x86_64/bin/python | grep x86_64`. Rosetta must be installed (`softwareupdate --install-rosetta --agree-to-license`).

## Important Notes

- The tag push also triggers GH Actions `release.yml`. If CI is working, it'll upload its own tarballs + checksums to the same GCS path. The objects have identical names per-arch — last write wins. Local and CI builds should be bit-identical at the tarball level modulo timestamp bytes; checksums will differ.
- Do NOT update `gs://screencap-releases/releases/latest.txt` from this skill. That promotion is owned by CI's `promote-latest` job (gated on `verify-install`). Promoting locally would bypass verify-install on the matching arch's runner.
- Do NOT overwrite `gs://screencap-releases/releases/install.sh` from this skill. That file is updated by CI's `release` job at release time.
- If CI fails after the tag is pushed, this skill fills the gap by publishing the missing tarball(s) to the versioned GCS path and the matching GH release assets. Users who pin `SCREENCAP_VERSION=X.Y.Z` get the local build. Users who run the default `curl install.sh` still get whatever `latest.txt` points to.
- For `--arch both` on an Apple Silicon Mac under Rosetta: the x86_64 build uses `MACOSX_DEPLOYMENT_TARGET=11.0` + `PIP_CONSTRAINT=pyinstaller/constraints-x86_64.txt` (forces numpy<2.0, av<14, onnxruntime<=1.19.2, etc.) so the resulting minos stays ≤ 11.0 even though Homebrew libraries on the host are arm64-native. The minos-verify step (Step 4) is the authoritative gate.
