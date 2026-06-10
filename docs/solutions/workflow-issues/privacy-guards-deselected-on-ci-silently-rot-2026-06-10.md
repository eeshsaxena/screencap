---
title: "Privacy/security regression guards gated behind environment-only markers silently rot on CI"
date: 2026-06-10
category: workflow-issues
problem_type: workflow_issue
component: testing_framework
platform: macos
severity: high
applies_when:
  - "A test asserts a privacy/security behavior that only runs when an optional native dep is installed (e.g. pyobjc-Vision)"
  - "That test carries a marker CI deselects (e.g. `@pytest.mark.privacy`) so it never runs on runners"
  - "The guard is parked as `xfail` while a fix is deferred"
  - "Refactoring code whose only regression guard lives behind such a marker"
root_cause: test_isolation
resolution_type: code_fix
modules:
  - tests/test_selective_masking.py
  - src/screencap/scrubber.py
tags:
  - privacy
  - testing
  - ci
  - xfail
  - scrubber
  - silent-regression
related_issues:
  - "SCR-110"
  - "https://github.com/proteus-computer-use/screencap/pull/228"
---

# Privacy/security regression guards gated behind environment-only markers silently rot on CI

## Context

The scrubber's background-window masking (`scrubber.py:mask_screenshots`) had three
regression guards in `tests/test_selective_masking.py::TestBackgroundWindowMasking`.
They assert that a sensitive *background* window (banking app, password manager,
Slack) is masked even when the foreground app evaluates to `ALLOW`/`TEXT_REDACT`.

Those guards are `@pytest.mark.privacy` and only exercise the real masking path when
pyobjc-Vision (the OCR/detection stack) is installed — i.e. on a real Mac, never on
CI runners, which deselect the `privacy` marker. When SCR-30 (`73072c77`) refactored
the scrubber and introduced a gate that skipped background masking whenever the
foreground OCR pass ran, the guards started failing — but only on a machine with
Vision installed. Rather than surface, the failures were parked as
`@pytest.mark.xfail(strict=True)` and deferred. CI stayed green the whole time
because it never ran them. The privacy leak (SCR-110) shipped and survived
undetected for the lifetime of the gate.

## Guidance

A regression guard only compounds value if **something runs it on every change**.
When a security/privacy test is gated behind an optional dependency AND a
CI-deselected marker, that test protects nothing on CI — it is documentation, not a
guard. Two concrete rules:

1. **Don't let an env-gated privacy guard be your only line of defense.** Either run
   the marker in at least one CI lane that installs the dep, or add a parallel test
   that exercises the same invariant through a path that runs everywhere (a unit test
   on the gating predicate, a fake/in-memory OCR stub, etc.).
2. **Treat `xfail` on a security guard as a ticking liability, not a resting state.**
   `xfail(strict=True)` parks a *known failing* security assertion. If the marker is
   deselected on CI, the strictness buys nothing there — the leak is live in prod
   while the suite is green. When you must defer, file the follow-up against the leak
   itself (SCR-110 here), not just "re-enable the test."

The underlying code antipattern that caused the leak is worth its own caution: a
**single gate that conflated two operations on disjoint regions.** Background
masking was gated on `not (ocr_ran or cache_hit)` under the rationale that bg masking
"can over-mask the foreground content." But the foreground OCR pass scans only the
active-window ROI, while background masking masks only sensitive *background*
windows — disjoint targets. Foreground over-mask protection was already provided by
`respect_z_order=True` (the z-order bitmap clears the foreground window from the bg
mask), not by the gate. When you find a boolean that couples two features, check
whether each feature's invariant is actually enforced by that boolean or by
something else; if something else already enforces it, the coupling is dead weight
that will eventually disable the wrong thing.

## Why This Matters

Privacy leaks are the highest-cost class of regression in this codebase (sensitive
windows reaching scrubbed/cloud copies). The combination of "guard requires Vision"
+ "CI deselects `privacy`" + "deferred as xfail" produced a coverage blind spot where
the most dangerous regression was the least likely to be caught. Every other
`@pytest.mark.privacy` guard in the repo shares this exposure today.

## When to Apply

- Before deferring any failing privacy/security test as `xfail` — ask "does CI ever
  run this?" If no, the deferral hides a live defect.
- When refactoring scrubber/masking/privacy code — verify the guards for the
  behavior you're touching actually execute in your local run (Vision installed) and
  ideally in some CI lane.
- When reviewing a PR that adds an env-gated security test — confirm there is a path
  that runs it automatically somewhere.

## Examples

The gate that leaked, and the fix (`src/screencap/scrubber.py`):

```python
# BEFORE — bg masking skipped whenever the foreground OCR pass ran
bg_masked = False
_skip_bg = ocr_ran or cache_hit
if not _skip_bg and actual_action in (ALLOW, TEXT_REDACT, OCR_FALLBACK):
    ...  # mask sensitive background windows

# AFTER — runs on its disjoint region regardless of the OCR pass;
# respect_z_order=True already protects foreground content
bg_masked = False
if actual_action in (ALLOW, TEXT_REDACT, OCR_FALLBACK):
    ...
```

The guards had been parked like this (`tests/test_selective_masking.py`):

```python
@pytest.mark.xfail(
    strict=True,
    reason="SCR-110: background-window masking is skipped when the foreground "
    "OCR pass runs, so a sensitive background window can leak.",
)
def test_allow_foreground_masks_sensitive_background(self, tmp_path):
    ...
```

Removing the `xfail` markers (so they pass for the right reason) plus dropping the
gate restored the protection. Note these still only run where Vision is installed —
the deeper fix to the blind spot (a CI lane that installs Vision, or a Vision-free
unit test of the gating predicate) remains open.

## Related

- SCR-110 — Background-window masking skipped when OCR runs: sensitive windows leak
- [screencap#228](https://github.com/proteus-computer-use/screencap/pull/228) — the fix
- Introduced by SCR-30 scrubber refactor (commit `73072c77`); guards added in `98bb8165`
