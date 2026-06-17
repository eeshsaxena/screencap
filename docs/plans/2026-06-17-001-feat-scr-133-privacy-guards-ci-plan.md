---
title: "feat: Run @pytest.mark.privacy guards in CI so privacy regressions can't ship"
type: feat
status: active
date: 2026-06-17
---

# feat: Run @pytest.mark.privacy guards in CI so privacy regressions can't ship

## Summary

Add a GitHub Actions CI workflow that runs the `@pytest.mark.privacy` test suite on every push to `main` and every pull request, so a privacy/security regression (e.g. background-window masking leaking) fails a check instead of shipping silently. The primary lane runs on a macOS runner with pyobjc-Vision installed (highest fidelity — exercises the real OCR/masking path); a second, cheap Vision-free lane runs the already-Vision-free background-masking guards on every PR as defense-in-depth.

---

## Problem Frame

Privacy regression guards marked `@pytest.mark.privacy` were intended as the safety net for the scrubber's masking behavior. SCR-110 (sensitive background windows leaking when the foreground OCR pass ran) regressed undetected across the SCR-30 scrubber refactor precisely because **nothing ran those guards automatically**. The guards existed, were parked as `xfail`, and CI stayed green throughout.

Investigation of the current repo state refines the issue's stated premise (see Open Questions → Resolved):

- The repo has **no CI job that runs pytest at all**. The only workflows are `.github/workflows/release.yml` (tag-triggered, smoke tests only) and `.github/workflows/binary-test.yml` (disabled via `if: false`, smoke tests only).
- The `privacy` marker **is registered** (`pyproject.toml` `[tool.pytest.ini_options].markers`) but **is not deselected anywhere** — there is no `addopts`, no `-m "not privacy"`. So the marker isn't being filtered out; pytest simply never runs in CI.
- The acceptance-named guard, `tests/test_selective_masking.py::TestBackgroundWindowMasking`, **already asserts the invariant without Vision** (mock geometry DB + PIL). The whole file carries `pytestmark = pytest.mark.privacy` (line 41).

The gap is therefore narrow and well-bounded: **wire the existing privacy guards into an automated check.** No new test logic or predicate extraction is required.

---

## Requirements

- R1. A green-on-`main` CI check runs the `@pytest.mark.privacy` guards and fails if background-window masking (or another privacy guard) regresses.
- R2. `tests/test_selective_masking.py::TestBackgroundWindowMasking` (the Vision-free assertion) participates in that check.
- R3. The check runs automatically on every change to `main` (push) and on pull requests — not gated behind an environment CI skips.
- R4. The background-masking invariant is exercised through at least one path that does **not** depend on an optional native dep (pyobjc-Vision) being present, per the institutional learning doc's rule #1.

---

## Scope Boundaries

- **Not** extracting a new pure predicate function (`should_apply_background_masking` or similar). The existing `TestBackgroundWindowMasking` tests already assert the full invariant Vision-free, end-to-end. A separate predicate would be redundant, lower-fidelity, and unnecessary new abstraction.
- **Not** adding a full-suite CI lane. This plan runs only the `privacy` marker scope, matching the ticket and avoiding unrelated flakiness (e.g. the known-flaky daemon-reconnect macOS test). A broader test-CI lane is a separate concern.
- **Not** changing any scrubber/masking behavior — SCR-110 is already fixed in PR #228. This is a CI/test-harness change only.
- **Not** re-enabling or modifying `.github/workflows/binary-test.yml` or `release.yml` beyond what's needed to mirror their setup steps.

### Deferred to Follow-Up Work

- Full-suite CI lane (run all of `pytest`, not just `-m privacy`): a future iteration, tracked separately if desired.
- Path-filtering the macOS lane to only privacy-relevant paths as a macOS-minutes cost optimization: deferred; default is to run on all PRs + main pushes for safety.

---

## Context & Research

### Relevant Code and Patterns

- `.github/workflows/release.yml` — existing macOS workflow. Uses `macos-14` (arm64) and `macos-15-intel` (x86_64) runners and installs `pip install -e ".[record]"`. The new workflow mirrors its runner labels and Python/pip setup steps.
- `.github/workflows/binary-test.yml` — disabled (`if: false`); same runner/setup shape. Reference only.
- `pyproject.toml` `[tool.pytest.ini_options]` (≈ lines 108-113) — registers the `privacy` marker; **no `addopts`, no marker deselection**.
- `pyproject.toml` `[project.optional-dependencies]` (≈ lines 48-94) — `record` extra carries `pyobjc-framework-Vision>=9.0; sys_platform == 'darwin'`; `dev` extra includes `screencap[record]`, `screencap[build]`, and pytest tooling (`pytest`, `pytest-cov`, `pytest-asyncio`, `pytest-timeout`). On Linux, the `sys_platform == 'darwin'` markers cause pyobjc deps to be skipped automatically.
- `tests/test_selective_masking.py` — module-level `pytestmark = pytest.mark.privacy` (line 41). `TestBackgroundWindowMasking` (≈ lines 474-650): 5 tests asserting sensitive background windows are masked for foreground `ALLOW`/`TEXT_REDACT` while preserving foreground content — all Vision-free (mock geometry DB + PIL via `mask_screenshots()`), all run on any platform.
- `tests/privacy/test_ocr.py` — `pytestmark = [pytest.mark.privacy, pytest.mark.skipif(sys.platform != "darwin", ...)]`. Runs the real Vision OCR path; executes on the macOS lane, skips cleanly elsewhere.
- Other `@pytest.mark.privacy` guards covered by the marker: `tests/test_cli_review_data.py` (3), `tests/test_upload.py` (1).
- `src/screencap/scrubber.py:mask_screenshots` (≈ lines 1665-2051) — the masking function. Background-masking gate post-SCR-110-fix (≈ lines 1982-1986) runs independently of the OCR pass; `respect_z_order=True` protects foreground content. No code change here; listed for reviewer orientation.

### Institutional Learnings

- `docs/solutions/workflow-issues/privacy-guards-deselected-on-ci-silently-rot-2026-06-10.md` — the originating learning. Core rule #1: *"Either run the marker in at least one CI lane that installs the dep, or add a parallel test that exercises the same invariant through a path that runs everywhere."* This plan does the former as primary (macOS+Vision lane) and the latter as defense-in-depth (Vision-free lane). The doc's closing note ("the deeper fix to the blind spot … remains open") is the open item this plan closes.

### External References

- None required. This is a CI-wiring change against well-understood GitHub Actions + existing repo patterns; local patterns (`release.yml`) are sufficient.

---

## Key Technical Decisions

- **Primary lane = macOS runner with Vision (`.[dev]`), not Linux-only.** Rationale: the macOS+Vision lane satisfies *both* acceptance bullets and additionally runs `tests/privacy/test_ocr.py` (the real OCR path) which a Linux lane cannot. It reuses the proven `release.yml` runner setup. Privacy is the highest-cost regression class in this codebase, so the higher-fidelity lane is the right default.
- **Scope CI to `pytest -m privacy`, not the full suite.** Matches the ticket exactly and avoids importing unrelated flakiness into the gate (notably the known-flaky daemon-reconnect macOS test, which is not privacy-marked and so won't be collected as a privacy test). On macOS with `.[dev]` installed, collection imports succeed for all test modules.
- **Keep the existing tests as the Vision-free assertion; do not extract a new predicate.** The `TestBackgroundWindowMasking` integration-style tests already prove the invariant without Vision. Extracting a pure predicate would duplicate coverage at lower fidelity (YAGNI).
- **Add a cheap Vision-free fast lane (ubuntu) as defense-in-depth.** Gives sub-minute per-PR feedback on the most important invariant and de-risks macOS-runner availability/queueing. Explicitly droppable without failing acceptance if Linux import portability proves troublesome (see U2 execution note).
- **Trigger on `push: branches: [main]` and `pull_request`.** Directly satisfies the "green-on-`main` check" acceptance while also catching regressions pre-merge.
- **Python version: target the project's supported range (`requires-python >= 3.10`), pin to 3.12** to match the dev/build toolchain. Exact pin confirmable at implementation time.

---

## Open Questions

### Resolved During Planning

- *Does CI currently deselect the `privacy` marker?* No. There is no `addopts`/`-m "not privacy"` anywhere, and no CI job runs pytest. The issue's "CI deselects the marker" framing predates verification; the actual gap is "no pytest in CI." The fix is to **add** a test lane.
- *Do we need to write a new Vision-free test or extract a predicate to satisfy R2/R4?* No. `tests/test_selective_masking.py::TestBackgroundWindowMasking` is already Vision-free and privacy-marked. We only need to run it in CI.
- *Will `pytest -m privacy` collect cleanly on the macOS runner?* Yes — `.[dev]` installs all pyobjc frameworks on darwin, so every test module imports during collection.

### Deferred to Implementation

- **Does `tests/test_selective_masking.py` import cleanly on `ubuntu-latest`** (no transitive top-level pyobjc/Quartz import at module load)? This determines whether the U2 Linux lane can run that file directly. If it doesn't import, the fallback is to scope the lane more tightly or drop it — U1 alone satisfies acceptance. (See U2 execution note.)
- Exact macOS runner label for the primary lane (`macos-14` arm64 is the default choice, matching `release.yml`'s primary).
- Exact Python version pin and whether to use `actions/setup-python` caching.
- Whether to constrain `concurrency`/`fail-fast` across the two jobs (minor ergonomics).

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
.github/workflows/ci.yml  (new)

on:
  push:        { branches: [main] }
  pull_request:

jobs:
  privacy-guards-macos:        # U1 — highest fidelity, satisfies R1+R2 alone
    runs-on: macos-14          # mirrors release.yml primary runner
    steps:
      - checkout
      - setup-python 3.12
      - pip install -e ".[dev]"   # pulls .[record] -> pyobjc-Vision on darwin
      - pytest -m privacy         # runs test_selective_masking.py (Vision-free)
                                  #   + tests/privacy/test_ocr.py (Vision-backed)
                                  #   + other @pytest.mark.privacy guards

  privacy-guards-visionfree:   # U2 — cheap defense-in-depth, runs everywhere
    runs-on: ubuntu-latest
    steps:
      - checkout
      - setup-python 3.12
      - pip install -e ".[dev]"   # pyobjc deps auto-skipped (sys_platform marker)
      - pytest tests/test_selective_masking.py   # Vision-free bg-masking guards
```

Both jobs are required checks on `main`. The macOS job is the authoritative high-fidelity gate; the ubuntu job is fast feedback and a backstop if macOS runners are constrained.

---

## Implementation Units

### U1. Add CI workflow with a Vision-backed macOS privacy-guard lane

**Goal:** A new GitHub Actions workflow that runs `pytest -m privacy` on a macOS runner with pyobjc-Vision installed, triggered on push to `main` and on pull requests. This alone satisfies R1 and R2.

**Requirements:** R1, R2, R3

**Dependencies:** None

**Files:**
- Create: `.github/workflows/ci.yml`

**Approach:**
- Triggers: `push` filtered to `branches: [main]`, plus unfiltered `pull_request`.
- Job `privacy-guards-macos` on `macos-14` (arm64), mirroring `release.yml`'s runner and setup steps.
- Steps: `actions/checkout` → `actions/setup-python` (3.12) → `pip install -e ".[dev]"` → `pytest -m privacy`.
- `.[dev]` transitively installs `.[record]`, which includes `pyobjc-framework-Vision` on darwin, so both the Vision-free guards in `tests/test_selective_masking.py` and the Vision-backed `tests/privacy/test_ocr.py` run.
- No screen-recording/TCC permission is needed: the privacy guards operate on mock geometry, supplied test images, and PIL — not live screen capture.

**Patterns to follow:**
- `.github/workflows/release.yml` — runner labels, `setup-python`, `pip install -e ".[record]"` step shape.

**Test scenarios:**
<!-- This unit's "test" is the CI run itself; the assertions live in the existing privacy guards it executes. -->
- Happy path: On a PR, the `privacy-guards-macos` job runs `pytest -m privacy` and passes; `tests/test_selective_masking.py::TestBackgroundWindowMasking` (5 tests) and `tests/privacy/test_ocr.py` both execute (not skipped) on the macOS+Vision runner. Covers R2.
- Regression-detection (manual verification, not committed): temporarily reintroduce the SCR-110 gate (`_skip_bg = ocr_ran or cache_hit`) in `scrubber.py:mask_screenshots` locally and confirm `TestBackgroundWindowMasking` fails under `pytest -m privacy` — proving the guard actually fails the check. Covers R1.
- Marker breadth: confirm `pytest -m privacy` also collects/runs the other privacy-marked guards (`tests/test_cli_review_data.py`, `tests/test_upload.py`) so "another privacy guard" regressing also fails the check.

**Verification:**
- The workflow appears under the repo's Actions tab and runs on pull requests and on push to `main`.
- A clean run is green; the run log shows the privacy guards executing (TestBackgroundWindowMasking present and passing, test_ocr not skipped).

---

### U2. Add a Vision-free fast lane (defense-in-depth)

**Goal:** A cheap `ubuntu-latest` job in the same workflow that runs the already-Vision-free background-masking guards on every PR/push, so the core invariant is checked even when macOS runners are unavailable or slow — satisfying R4's "runs everywhere" intent.

**Requirements:** R3, R4

**Dependencies:** U1 (same workflow file)

**Files:**
- Modify: `.github/workflows/ci.yml` (add `privacy-guards-visionfree` job)

**Approach:**
- Job on `ubuntu-latest`: `actions/checkout` → `actions/setup-python` (3.12) → `pip install -e ".[dev]"` (pyobjc deps auto-skip on Linux via `sys_platform == 'darwin'` markers) → run the Vision-free guards.
- Scope pytest to the known Vision-free file (`pytest tests/test_selective_masking.py`) rather than `-m privacy`, to avoid Linux collection-import errors from modules that import pyobjc at module load.

**Execution note:** At implementation time, verify `tests/test_selective_masking.py` imports and runs on `ubuntu-latest` (i.e. `screencap.privacy.*` does not transitively import Quartz/pyobjc at module load). If it does not import cleanly, prefer a minimal fix (e.g. confirm the import chain is already lazy) or tighten the invocation; if neither is cheap, **drop this job** — U1 already satisfies all acceptance criteria. Do not weaken or skip the assertions to force a green Linux run.

**Patterns to follow:**
- Same workflow/setup shape as U1; ubuntu runner instead of macOS.

**Test scenarios:**
- Happy path: the `privacy-guards-visionfree` job runs `tests/test_selective_masking.py` on Linux; `TestBackgroundWindowMasking`'s tests execute (not skipped) and pass without Vision. Covers R4.
- Regression-detection: the same SCR-110-gate reintroduction makes this lane go red on Linux, confirming the Vision-free path is a real guard.
- Portability: collection of `tests/test_selective_masking.py` succeeds on `ubuntu-latest` (no ImportError at module load).

**Verification:**
- The ubuntu job runs on PRs/pushes and is green on a clean tree.
- Job log shows `TestBackgroundWindowMasking` tests ran (passed), not skipped, with no pyobjc installed.

---

### U3. Close the loop in the institutional learning doc

**Goal:** Update the originating learning doc to record that the CI blind spot is now closed (privacy guards run automatically), so institutional knowledge reflects reality.

**Requirements:** R1 (documentation of the resolution)

**Dependencies:** U1

**Files:**
- Modify: `docs/solutions/workflow-issues/privacy-guards-deselected-on-ci-silently-rot-2026-06-10.md`

**Approach:**
- Update the closing note (currently: "the deeper fix to the blind spot … remains open") to point at the new `.github/workflows/ci.yml` privacy lane(s) as the resolution, and reference SCR-133.
- Keep the doc's antipattern guidance intact; only the "remains open" status changes.

**Patterns to follow:**
- Existing frontmatter + prose style of the same doc and sibling docs in `docs/solutions/workflow-issues/`.

**Test scenarios:**
- Test expectation: none — documentation-only change with no behavioral surface.

**Verification:**
- The doc no longer describes the CI blind spot as open and links to the new workflow + SCR-133.

---

## System-Wide Impact

- **Interaction graph:** Adds a new automated quality gate. No runtime/product code changes. Affects contributor workflow (a new required check) and CI minutes usage.
- **API surface parity:** None — no public API, CLI, or library surface changes.
- **CI/external contract:** Introduces `.github/workflows/ci.yml`. If branch protection is later configured to require these checks, that is an admin/repo-settings action outside this plan's file changes (note for rollout).
- **Unchanged invariants:** `scrubber.py:mask_screenshots` behavior is explicitly unchanged; `release.yml` and `binary-test.yml` are untouched. The `privacy` marker registration in `pyproject.toml` is unchanged (already correct).

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| macOS runner minutes cost / queue latency on every PR | Acceptable for the highest-cost regression class; the cheap ubuntu lane (U2) gives fast feedback and a backstop. Path-filtering the macOS lane is available as a future cost optimization (deferred). |
| `tests/test_selective_masking.py` fails to import on Linux (transitive pyobjc import) | U2 execution note: verify at implementation; tighten scope or drop U2 — U1 alone satisfies acceptance. |
| pyobjc-Vision / VNRecognizeText behaves differently on a headless GitHub macOS runner | The Vision-backed `tests/privacy/test_ocr.py` runs on supplied images (not live capture); if a runner-specific Vision issue surfaces, the Vision-free lane (U2) still guards the SCR-110 invariant. Investigate at implementation if `test_ocr.py` is flaky on the runner. |
| `pytest -m privacy` pulls an unrelated collection-time import error on macOS | `.[dev]` installs the full dep set on darwin, so collection imports succeed; if a specific module errors, address by fixing the import, not by narrowing the marker. |

---

## Documentation / Operational Notes

- After merge, consider adding the new `ci.yml` jobs to branch protection's required status checks on `main` (repo-settings action; outside this plan's committed files) so the gate is enforced, not just advisory.
- U3 updates the institutional learning doc to reflect closure.

---

## Sources & References

- Linear issue: SCR-133 — Run @pytest.mark.privacy guards in CI so privacy regressions can't ship undetected
- Related: SCR-110 (the leak), SCR-30 (the refactor that introduced the gate), PR proteus-computer-use/screencap#228 (the SCR-110 fix)
- Learning doc: `docs/solutions/workflow-issues/privacy-guards-deselected-on-ci-silently-rot-2026-06-10.md`
- Existing CI: `.github/workflows/release.yml`, `.github/workflows/binary-test.yml`
- Marker + deps: `pyproject.toml` (`[tool.pytest.ini_options]`, `[project.optional-dependencies]`)
- Guards: `tests/test_selective_masking.py` (module-level `@pytest.mark.privacy`, `TestBackgroundWindowMasking`), `tests/privacy/test_ocr.py`
