---
module: testing
date: 2026-07-28
problem_type: best_practice
component: testing_framework
severity: high
related_components:
  - daemon/model_download_job
  - models/download
tags:
  - test-doubles
  - mocking
  - callback-contract
  - regression-testing
  - false-confidence
applies_when:
  - "Writing a test double for a collaborator that takes a callback"
  - "A layer's tests pass but the composed behavior is visibly broken"
  - "Deciding which layer owns asserting a callback contract"
---

# Test doubles encode the contract you want, not the implementation you have

## Context

`ModelDownloadJob` drives the model-download engine and republishes its progress
callbacks as `/v0/events`. Its lifecycle test faked the engine like this
(`tests/daemon/test_model_download_job.py:50`):

```python
class TestLifecycle:
    async def test_start_progress_completed(self, monkeypatch):
        def fake(model_id, *, progress_cb, stop_event, **kw):
            progress_cb(0, 100)
            progress_cb(100, 100)
            return DownloadResult("installed", model_id or "m", "llamacpp")
```

That fake is a faithful model of what the real engine did: fire at 0%, fire at
100%, nothing in between. And that was the bug. During a ~1.7 GB download every
UI rendered a bar frozen at 0% for minutes, indistinguishable from a hang, until
a QA tester reported it as stuck.

The test passed the whole time. It had to — it asserted the job correctly
relayed exactly the two calls the fake made, and the job did.

Meanwhile the engine's own tests (`tests/models/test_download.py`) covered the
fail-closed verify paths thoroughly — sha256 mismatch, symlink refusal,
disallowed formats, insufficient disk, interruption — and said **nothing** about
progress.

So the defect fell into a gap: the engine's tests never asserted the callback
contract, and the job's tests asserted it against a double that had copied the
defect. Neither layer *could* fail.

## Guidance

**Write the double from the contract you are promising, not from the behavior
you observed in the real thing.**

The question to ask when writing a fake is not "what does the real collaborator
do?" but "what does the real collaborator *owe* its caller?" Here the engine
owes: *report progress as the work proceeds.* A double that emits only endpoints
is not a simplified engine — it is an engine that violates the contract, promoted
to the status of expected behavior.

Concretely, when a double reproduces the collaborator's behavior:

- **Copying observed behavior is how a bug becomes the spec.** Once the fake
  mirrors the defect, every test written against it certifies the defect. The
  suite gets greener as the encoding gets more entrenched.
- **A behavior that looks too simple to be worth varying deserves the most
  scrutiny.** "It just calls back at the start and end" reads as a reasonable
  simplification for a test. It was in fact the entire bug, transcribed.
- **Ask what the double would have to do to be wrong.** If a fake cannot express
  a contract violation, no test using it can detect one.

**Assert a callback contract at the layer that owns it.**

A callback contract has two halves, and they belong to different test files:

| Half | Owner | Assertion |
|------|-------|-----------|
| *Does the producer emit progress as work proceeds?* | The producer's own tests | Drive the real code with a controllable collaborator and assert an intermediate reading exists |
| *Does the consumer relay/throttle correctly?* | The consumer's tests | A hand-written double is fine here, because the producer's tests own emission |

The job's tests were not the wrong place to use a fake — they were the wrong
place to be the *only* assertion about emission. The fix added the missing
producer-side test (`tests/models/test_download.py:91`), which drives the real
engine through its injected `snapshot_fn` seam and asserts a reading strictly
between the endpoints:

```python
def progress_cb(done, total):
    seen.append((done, total))
    if 0 < done < total:
        sampled.set()

result = _dl(tmp_path, snapshot_fn=chunked_snapshot, progress_cb=progress_cb)

assert any(0 < done < total for done, total in seen), (
    f"no intermediate progress reading; the bar would sit at 0%: {seen}"
)
```

Against pre-fix code it fails with `[(0, 1088), (1088, 1088)]` — the two-call
signature of the bug, printed in the failure message.

**A test that has never failed has not been shown to work.** Before trusting a
new regression test, run it against the broken code and confirm it fails *for
the reason you expect*. That single step would have caught this at authoring
time: an intermediate-progress assertion run against the old engine fails
immediately and obviously.

## Why This Matters

This failure mode is worse than missing coverage. Missing coverage is visibly
absent — a reviewer sees no test and asks for one. A double that encodes the bug
produces a passing, plausible-looking test that actively certifies the defect, so
both the author and the reviewer get a false signal. The bug then survives
exactly as long as no human happens to look at the feature, which here meant
until QA.

It is also self-reinforcing. Every later test written against that fake inherits
its assumption, so the cost of discovering the error grows with the suite.

The layer-ownership half matters because "we have tests at both layers" feels
like coverage while both layers can still be blind to the same defect. Coverage
of a *contract* is not the union of coverage of its participants — someone has to
assert the contract itself, against real code.

## When to Apply

Apply when writing or reviewing any test double for a collaborator that takes a
callback, emits progress or events, or is expected to produce output over time
rather than once.

The strongest signal that this trap is present: **the double was written by
reading the implementation.** If the fake's body is a transcription of what the
real function does, it can only ever confirm the status quo.

Do not over-rotate into banning simplified doubles — a double that returns a
canned value for a collaborator whose behavior is genuinely irrelevant to the
test is fine and normal. The rule is narrower: when the double reproduces the
*behavior under test*, it must reproduce the contract, and some test somewhere
must drive the real producer.

## Examples

The double that encoded the bug — the fake's shape *is* the defect:

```python
def fake(model_id, *, progress_cb, stop_event, **kw):
    progress_cb(0, 100)
    progress_cb(100, 100)   # nothing in between; exactly what was broken
    return DownloadResult("installed", model_id or "m", "llamacpp")
```

A double written from the contract instead — it emits intermediate progress
because that is what the engine *owes*, so a consumer test built on it exercises
the real relay path:

```python
def fake(model_id, *, progress_cb, stop_event, **kw):
    progress_cb(0, 100)
    for done in (25, 50, 75):      # the contract: progress as work proceeds
        progress_cb(done, 100)
    progress_cb(100, 100)
    return DownloadResult("installed", model_id or "m", "llamacpp")
```

Note what the second version buys beyond catching the original bug: it makes the
consumer's throttling logic reachable. The job throttles publishes on
"≥1% delta OR ≥0.5s elapsed" (`src/screencap/daemon/model_download_job.py:62-63`),
and with only two calls that logic was never meaningfully exercised. A
contract-shaped double tests the consumer's real behavior; an
implementation-shaped one tests a path production never takes.

Shipped in PR #437, alongside the progress fix itself. The engine-side pattern
that fix used is documented separately in
[progress-for-blocking-sdk-calls-with-no-callback.md](../design-patterns/progress-for-blocking-sdk-calls-with-no-callback.md).

## A caveat worth recording

Neither new test runs in CI. This repo's CI runs `pytest -m privacy` plus one
named lock-policy test, so unmarked tests are local-only — the whole
`TestHappyPath` class and all of `test_model_download_job.py` were already
CI-invisible before this fix. Marking a progress test `@pytest.mark.privacy` to
sneak it into the lane would be the wrong fix; the honest options are an
explicitly-named CI step (the precedent already exists for the lock-policy test)
or a general test lane.
