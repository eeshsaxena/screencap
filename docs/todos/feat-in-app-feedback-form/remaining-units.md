# In-app feedback form — remaining units

This PR ships the verified **backend** of the in-app feedback feature (plan:
`docs/plans/2026-07-19-001-feat-in-app-feedback-form-plan.md`). The units below
were out of reach in the autonomous run that produced the backend and remain to
be done. They are ordered by what unblocks what.

## Blocked on external access

- **U0 — Linear API pre-flight spike.** Verify `fileUpload` + `issueCreate`
  against the real Linear workspace with a 25 MB `.mov`, a `.png`, and a `.heic`:
  confirm upload acceptance at these sizes, the `assetUrl`/`uploadUrl` host (the
  relay + CLI `LINEAR_UPLOAD_HOSTS` allowlist currently assumes
  `uploads.linear.app` — **confirm and correct if wrong**), the signed-URL
  expiry window (must be ≥ the 60-min claim), and inline rendering. If a type or
  size is rejected, lower the caps in **both** `scripts/cloud-function/feedback.py`
  and `src/screencap/feedback.py` (the mirrored-constants test pins them equal).
  Needs a Linear API key.

- **U6 — Deploy + configure.** Provision the dedicated least-privilege Linear
  account + scoped API key, the `screencap-feedback` runtime service account, and
  `FEEDBACK_HMAC_KEY` (`openssl rand -base64 32`); resolve `teamId`, Triage
  `stateId`, and the three label ids; deploy per the docstring in `feedback.py`
  (`--max-instances=2`, `--set-secrets`, `--service-account`); add the runbook
  lines (key revocation, HMAC rotation, storage-abuse watch). Needs GCP deploy
  access + Linear admin. The operational half depends only on the relay (U1,
  done) — it can run in parallel with the Swift work.

## Blocked on a safe Swift build environment

`xcodebuild` in a `~/Documents` checkout TCC-bricks the agent session, so the
Swift units were not implemented in the backend run. They are fully specified in
the plan (U3–U5: Files, Approach, Patterns, Test scenarios). Implement in an
environment where the macOS app target can compile and `xcodebuild test` can run.

- **U3 — `FeedbackController` + `FeedbackModels`** (state enum, service seam over
  `CLIClient.runJSONRawStdin` with the payload-scaled timeout of KTD-12, async
  daemon-version fetch, per-kind error copy, selection-time validation).
- **U4 — `FeedbackSheetView`** (embeddable form; `NSOpenPanel` intake for launch,
  drag-and-drop deferred; per-row attachment remove; dismissal disabled while
  sending; copyable issue link on success; per-kind error copy).
- **U5 — Entry points** (menu bar "Send Feedback…" via the notification bridge +
  main-window affordance; no new window scene).

## Delivery

- The app changes reach users via the separate `macos-app-release` track
  (build/sign/notarize/DMG). Name its owner on the launch timeline.

## Open decision (plan Open Questions)

- **Single-shot vs. two-endpoint attachment design.** The two-endpoint HMAC-claim
  design exists because the 60 MB total cap exceeds the 32 MiB Cloud Run request
  ceiling. A smaller cap would satisfy R5 with one endpoint and less surface. The
  backend implements the two-endpoint design (following the repo's `get-upload-urls`
  precedent); revisit against the U0 spike findings before the Swift side hardens
  against the contract.
