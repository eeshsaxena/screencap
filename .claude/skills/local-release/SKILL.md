---
name: local-release
description: Build and release screencap locally for ARM (arm64). Use when CI is unavailable (billing limits, runner outages) or you want to release an arm64-only build. Intel users auto-fallback to pip install. Triggers on "local release", "release locally", "build and release", "arm release", "release arm64", or when CI release fails.
---

# Local Release (ARM64)

Build a PyInstaller binary on the local Mac (arm64) and publish it to GCS + GitHub Releases. Intel (x86_64) users will automatically fall back to pip-based install via `install.sh`.

## Handling Args

- `/local-release` — build + release the version already in `pyproject.toml`
- `/local-release 0.13.0` — bump to specified version first, then build + release
- `/local-release --build-only` — build and smoke test without uploading/releasing
- `/local-release --release-only` — skip build, upload existing `dist/` artifacts (for re-runs after a failed upload)

## Prerequisites

Before starting, verify:
1. `gcloud` CLI is installed and authenticated with write access to `gs://screencap-releases`
2. `gh` CLI is authenticated with repo write access
3. On an Apple Silicon Mac (arm64)

## Instructions

### Step 1: Resolve Version

Read the current version from `pyproject.toml` (line 7: `version = "X.Y.Z"`).

If the user provided a version arg:
- Update `pyproject.toml` version field
- Check CHANGELOG.md has a section for this version; warn if missing

Report: "Building screencap v{VERSION} for arm64."

### Step 2: Build (skip if `--release-only`)

Run each command sequentially, stopping on failure:

```bash
# Install deps (use record extras for the full binary)
pip install -e ".[record]" && pip install pyinstaller

# Download spaCy model (needed for privacy pipeline in frozen binary)
python -m spacy download en_core_web_sm

# Build the frozen binary
pyinstaller pyinstaller/screencap.spec
```

### Step 3: Smoke Test (skip if `--release-only`)

```bash
./dist/screencap/screencap --help
./dist/screencap/screencap _smoke-test
```

Both must exit 0. If `_smoke-test` fails, stop and report the error.

### Step 4: Package

```bash
VERSION=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
tar czf "screencap-${VERSION}-arm64.tar.gz" -C dist screencap/
shasum -a 256 "screencap-${VERSION}-arm64.tar.gz" > checksums.sha256
```

Display the checksum for the user to verify.

**Stop here if `--build-only`** — report the tarball path and checksum.

### Step 5: Upload to GCS

```bash
VERSION=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
gcloud storage cp "screencap-${VERSION}-arm64.tar.gz" "gs://screencap-releases/releases/v${VERSION}/"
gcloud storage cp checksums.sha256 "gs://screencap-releases/releases/v${VERSION}/"
gcloud storage cp scripts/install.sh gs://screencap-releases/releases/install.sh
echo "$VERSION" | gcloud storage cp - "gs://screencap-releases/releases/latest.txt"
```

Verify upload:
```bash
gcloud storage ls "gs://screencap-releases/releases/v${VERSION}/"
```

### Step 6: Create GitHub Release

Extract release notes from CHANGELOG.md, then create the release:

```bash
VERSION=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")

# Extract release notes
awk "/^## \[${VERSION}\]/{found=1; next} /^## \[/{if(found) exit} found" CHANGELOG.md > /tmp/release_notes.txt

# Fallback if no changelog section
if [ ! -s /tmp/release_notes.txt ]; then
    echo "See [CHANGELOG.md](CHANGELOG.md) for details." > /tmp/release_notes.txt
fi

gh release create "v${VERSION}" \
    --title "v${VERSION}" \
    --notes-file /tmp/release_notes.txt \
    "screencap-${VERSION}-arm64.tar.gz" \
    checksums.sha256
```

If the release tag already exists (e.g., from a previous failed CI attempt), use `gh release edit` to update it and upload the assets:
```bash
gh release edit "v${VERSION}" --notes-file /tmp/release_notes.txt
gh release upload "v${VERSION}" "screencap-${VERSION}-arm64.tar.gz" checksums.sha256 --clobber
```

### Step 7: Verify Install

Run the install script against the just-published release:
```bash
SCREENCAP_VERSION="${VERSION}" SCREENCAP_NO_MODIFY_PATH="1" SCREENCAP_INSTALL_METHOD="binary" bash scripts/install.sh
xattr -dr com.apple.quarantine ~/.screencap/bin/
~/.screencap/bin/screencap/screencap --version
```

### Step 8: Cleanup

Remove local build artifacts:
```bash
rm -f screencap-*.tar.gz checksums.sha256 /tmp/release_notes.txt
```

Do NOT `rm -rf dist/` or `rm -rf build/` — those are reusable for subsequent builds.

### Step 9: Report

```
## Local Release: v{VERSION}

- Build: arm64 (local PyInstaller)
- GCS: gs://screencap-releases/releases/v{VERSION}/
- GitHub: https://github.com/proteus-computer-use/screencap/releases/tag/v{VERSION}
- Install verified: yes/no
- Intel (x86_64): fallback to pip install via install.sh

Note: This is an arm64-only release. Intel users will automatically
install from source via pip when running install.sh.
```

## Error Handling

- **PyInstaller fails**: Check that `.[record]` extras installed correctly. Common issue: missing PyObjC frameworks.
- **Smoke test fails**: Read the error — usually a missing hidden import in `screencap.spec`.
- **GCS upload fails**: Verify `gcloud auth list` shows an account with Storage Object Admin on `screencap-releases`.
- **GitHub release exists**: Use `gh release edit` + `gh release upload --clobber` instead of `gh release create`.
- **Install verification fails**: Check `xattr -dr` was run. If binary crashes, the build is bad — do not publish.

## Important Notes

- This skill produces an **arm64-only release**. x86_64 binaries require an Intel Mac or CI.
- The `install.sh` script will fall back to pip install for Intel users when no x86_64 tarball is available.
- If CI billing is restored later, you can re-run the CI workflow to add the x86_64 binary to the same release.
- The existing `/release` skill handles the normal CI-based flow (version bump + tag push). Use `/local-release` only when CI is unavailable.
