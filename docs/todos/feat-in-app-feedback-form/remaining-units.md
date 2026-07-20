# In-app feedback form — remaining units

This PR ships the verified **backend** of the in-app feedback feature (plan:
`docs/plans/2026-07-19-001-feat-in-app-feedback-form-plan.md`). The units below
were out of reach in the autonomous run that produced the backend and remain to
be done. They are ordered by what unblocks what.

## Blocked on external access

- **U0 — Linear API pre-flight spike. DONE (SCR-281, 2026-07-20).** Verdict:
  GO with adjustments — full findings in the plan's Assumptions. Caps stand
  (25 MiB `.mov` / `.png` / `.heic` all accepted); `assetUrl` host confirmed
  `uploads.linear.app`, but signed PUT URLs are GCS path-style
  (`storage.googleapis.com/uploads.linear.app/...`) — both allowlists now
  accept that form (`_upload_target_allowed`); signed upload URL expires in
  **60 s**, so `CLAIM_TTL_SECONDS` dropped 60 min → 10 min. Residual: ran via
  Linear's official MCP upload flow (no raw API key pre-U6); U6's curl smoke
  exercises the raw `fileUpload` mutation.

- **U4 follow-up — transcode HEIC before upload.** The U0 spike showed Linear
  stores and serves `.heic` as raw `image/heic`, which Chromium-based Linear
  clients cannot decode — the attachment uploads fine but will not preview
  inline for the maintainer. Before U6's QA pass, convert HEIC→JPEG in the
  Swift intake (CGImage/ImageIO, cheap and native) instead of uploading raw
  HEIC; keep `image/heic` in the relay/CLI allowlists so older clients still
  pass validation. Eyeball check: SCR-284 (spike issue) shows the three test
  embeds.

- **U6 — Deploy + configure.** Provision the dedicated least-privilege Linear
  account + scoped API key, the `screencap-feedback` runtime service account, and
  `FEEDBACK_HMAC_KEY` (`openssl rand -base64 32`); resolve `teamId`, Triage
  `stateId`, and the three label ids; deploy per the docstring in `feedback.py`
  (`--max-instances=2`, `--set-secrets`, `--service-account`); add the runbook
  lines (key revocation, HMAC rotation, storage-abuse watch). Needs GCP deploy
  access + Linear admin. The operational half depends only on the relay (U1,
  done) — it can run in parallel with the Swift work.

## Swift UI units (were blocked on a safe build environment)

**DONE (SCR-282).** U3–U5 shipped from a TCC-safe checkout (`~/dev/screencap`
worktree): `FeedbackController` + `FeedbackModels` + 33 controller tests,
`FeedbackSheetView` (NSOpenPanel intake, per-row remove, sending-locked
dismissal, copyable issue link, per-kind error copy), and both entry points
(menu-bar "Send Feedback…" via the notification bridge + the sidebar-footer
"Send feedback" link). Drag-and-drop remains a fast-follow. End-to-end QA
against the deployed relay stays with U0/U6 below.

## Delivery

- The app changes reach users via the separate `macos-app-release` track
  (build/sign/notarize/DMG). Name its owner on the launch timeline.

## Open decision (plan Open Questions)

- **Single-shot vs. two-endpoint attachment design.** The two-endpoint HMAC-claim
  design exists because the 60 MB total cap exceeds the 32 MiB Cloud Run request
  ceiling. A smaller cap would satisfy R5 with one endpoint and less surface. The
  backend implements the two-endpoint design (following the repo's `get-upload-urls`
  precedent); revisit against the U0 spike findings before the Swift side hardens
  against the contract. **U0 input (2026-07-20):** Linear accepted every size we
  care about, so nothing forces smaller caps — the two-endpoint design stands.
