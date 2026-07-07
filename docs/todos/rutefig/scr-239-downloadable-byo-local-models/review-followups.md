# SCR-239 — code-review follow-ups (deferred, non-blocking)

Engineering follow-ups surfaced by the multi-agent review of PR #347
(downloadable + bring-your-own local models). The **actionable defects were
fixed in that PR** (commit `fix(review): address SCR-239 code-review findings`);
the items below are advisory refactors, residual hardening, and test-coverage
gaps intentionally left out of the review-fix commit. None block merge.

Reviewers: correctness, security, reliability, api-contract, swift-ios,
performance, testing, maintainability, project-standards, agent-native,
learnings, adversarial. No P0s; security lens found no actionable findings.

## Advisory refactors

- [ ] **CLI error envelope vs Swift decode (learnings, advisory).**
  `screencap settings intelligence <row> set …` (`src/screencap/cli/__init__.py`,
  `_emit_error` ~line 3266) writes `{ok:false,error:…}` to stdout then
  `raise SystemExit(1)`. `IntelligenceController.setEndpoint`/`setProvider`
  (`macos/ScreenCap/Controllers/IntelligenceController.swift`) discard stdout on a
  non-zero exit and fall back to `error.localizedDescription`, so the structured
  reason is lost. Follow the documented `cli-json-envelope-nonzero-exit` solution:
  either emit the envelope + exit 0 on Swift-consumed writes, or decode the stdout
  error envelope in the controller before falling back.

- [ ] **Rename `_backfill_running` → `_job_is_running` (maintainability, P3).**
  `src/screencap/daemon/_idle_shutdown.py` (~line 152) reuses the backfill-named
  helper for the model-download busy check; it only duck-types `.is_running()`.
  Rename and update both call sites; drop the "same is_running() shape" comment.

- [ ] **Collapse `_model_call_or_exit` (maintainability, P3).**
  `src/screencap/cli/__init__.py` (~line 3411) is an identity wrapper over
  `_backfill_call_or_exit`. Call the shared helper directly at the model command
  sites, or rename it to a provider-neutral `_daemon_call_or_exit`.

## Residual hardening (inert today — defense-in-depth)

- [ ] **`stripped=True` AST guard breadth (adversarial).**
  `tests/segmentation/test_stripped_marker_guard.py` detects only `Subscript`
  assignment and dict-literal keys — not attribute-assignment, `.setdefault(...)`,
  `dict(stripped=True)`, or aliased keys. Its docstring overclaims "key/attribute".
  Broaden the guard (or narrow the docstring). Consumers use `.get`, so inert now.

- [ ] **`sanitize._MARKUP_RE` unterminated fragments (adversarial).**
  `src/screencap/redaction`/`segmentation/sanitize.py` `<[^>]*>` misses an
  unterminated `<img src=x onerror=` with no closing `>`. No current HTML sink, so
  defense-in-depth only; tighten if task names ever reach an HTML surface.

- [ ] **Inference single-flight is process-wide (adversarial/reliability).**
  `src/screencap/segmentation/inference_guard.py` — two daemon processes could
  each spawn a ~2 GB worker; `has_ram_headroom` fails **open** when `psutil` is
  absent (deliberate). Acceptable under the same-EUID model; revisit if multi-proc
  becomes real.

## Test-coverage gaps (no CI coverage today)

- [ ] **KTD11 fail-open branches through the provider.**
  `DownloadedProvider._run_worker` slot-not-acquired / no-RAM-headroom →
  `PROVIDER_UNAVAILABLE` paths are untested end-to-end (the `inference_slot`
  primitive itself is covered in `tests/segmentation/test_chained_provider.py`).

- [ ] **`local_server` import-lightness test.**
  `downloaded.py` has `test_module_is_import_light`; `local_server.py` claims
  "cloud-free at import" in its docstring but has no parallel guard. Add one so a
  future non-lazy `requests`/cloud import is caught.

- [ ] **Real-backend + KTD8 connect-time behavior is manual-only.**
  `LocalServerProvider._default_raw_call` (redirects-off, streaming size cap,
  localhost pin), `download._default_snapshot`, and the MLX/llama.cpp `_raw_default`
  calls are not run in CI. The review added a stubbed-`requests` test for the URL
  de-dup + size cap; the live backends still need the manual/hardware lane.

- [ ] **Cross-language payload-key guard.**
  No test pins the daemon `model.status` / `model.download.status` payload keys
  (`as_payload()`) against the Swift `ModelDownloadStatus`/`ModelStatusEnvelope`
  decoder key set. A rename would silently break Swift decoding.

- [ ] **`benchmark_segmentation.score_run` label alignment.**
  Indexes fixture labels positionally against the model's task list; `run_model_eval`
  can `IndexError` / misalign when counts differ. Manual harness (`pragma: no cover`),
  low production impact — add a length-guard + a small test.

- [ ] **Model-download `_should_emit` time-flush clause.**
  Only the percentage-delta clause is exercised; the `>= _MIN_INTERVAL_S` flush is
  untested (`tests/daemon/test_model_download_job.py`).
