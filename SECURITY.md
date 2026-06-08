# Security Policy

ScreenCap captures screen content, audio, keystrokes, and window context, all of which can be sensitive. This document explains the security boundary the recorder runs behind, the threats it does and does not defend against, and how to report a vulnerability.

## Trust boundary

The ScreenCap daemon listens on a UNIX domain socket at `~/.screencap/run/api.sock`. The directory is created with mode `0o700` and the socket file with mode `0o600`, so only processes running as the same macOS user account can connect. When a connection is accepted, the daemon also verifies via `getpeereid()` that the peer's effective UID matches the daemon's own EUID, and rejects any other connection. Together, the filesystem permissions and the EUID match define the daemon's authentication boundary: **a same-EUID process can call any daemon verb; nothing else can.**

This is the same boundary used by `ssh-agent`, `gpg-agent`, the Docker daemon, and Apple's own non-XPC daemons. The Apple Platform Security Guide treats the user account as the primary data-protection boundary on macOS; in-process privilege separation is layered on top via TCC, the App Sandbox, and code signing, not via finer-grained socket auth. ScreenCap follows the same posture deliberately, and SCR-64 ratified it after considering two stricter alternatives (see [Alternatives considered](#alternatives-considered)).

The Phase 1 and Phase 2 daemon architecture plans (`docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md` and `docs/plans/2026-05-08-002-feat-daemon-architecture-phase-2-plan.md`) both named filesystem permissions plus `getpeereid()` as the v1 authorization surface and deferred any in-band auth header to a future iteration. This document formalizes that position.

### Defense in depth around the boundary

Several mechanisms harden the boundary without changing what authenticates a caller:

- **Bind-time permission verification.** At startup, the daemon re-stats both the socket file and its parent directory and refuses to listen if either deviates from the expected mode. This catches umask drift, accidental `chmod` post-install, and tampered run-dirs before the daemon ever serves a request. See `src/screencap/daemon/socket.py`.
- **Per-call audit log.** Every `/v0/recording.start` and `/v0/recording.stop` invocation appends a JSON-lines record (timestamp, verb, peer PID, peer binary path, provenance classification, outcome) to `~/.screencap/run/audit.log` (mode `0o600`). The log is best-effort — write failures degrade to a logger warning and never block the verb — but provides a forensic trail under the same-EUID trust model. Read-only verbs (`recording.list`, `session.snapshot`, `daemon.info`, `events`) are intentionally not audited because they leak no capability.
- **Server-derived `started_by` provenance.** The daemon classifies every caller as `swiftui` / `cli` / `mcp` / `unknown` from the peer's effective PID, binary path, and argv, and uses that classification as the authoritative `started_by` on persisted metadata. The classification is advisory only; it does not gate access to any verb.

## Cloud auth trust boundary

ScreenCap's optional cloud upload/download is governed by a boundary separate from the daemon socket. Sign-in, per-user storage isolation, and the public demo gallery follow the properties below (see `docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md` and `docs/runbooks/cloud-auth-setup.md`).

- **The signing Cloud Function is the authorization boundary, in code.** It verifies a Firebase ID token and scopes every upload/list/download to a per-user `users/{uid}/…` prefix through a single key builder (`resolve_prefix`). The function stays `--allow-unauthenticated` at the Cloud Run layer **by design** — there is no Cloud Run IAM backstop — so the in-code gate is the only boundary. It is enforced by a CI contract test that asserts no tokenless request can reach any `users/` code path, and a per-action test that no unauthenticated request resolves outside the public `demo/` namespace.
- **The owner id is the verified Firebase `uid`, never the email.** The `uid` is immutable, opaque, and path-safe; emails are mutable and would leak PII into cloud-bound object paths. The function pins the trusted Firebase project (`SCREENCAP_PROJECT_ID`) and re-asserts the token's `aud`/`iss` so it cannot verify-but-misattribute a token from a foreign project.
- **Refresh token in the Keychain, default ACL.** The long-lived Firebase refresh token is stored in the macOS Keychain (`keyring`, service `screencap-auth`) using the **default "Always Allow" trusted-binary ACL** — the same posture as the network-capture KEK (`src/screencap/network/crypto.py`). This is **NOT** a code-signing-pinned ACL: any same-user trusted binary can read the entry. We state this honestly rather than claim pinning that does not exist; it is consistent with the same-user trust model below. The short-lived ID token is held only in memory.
- **Signed URLs are unrevokable bearer capabilities for their lifetime.** A v4 signed URL cannot be revoked before it expires, so private (`users/`) GET URLs use a short 30-minute window; the public `demo/` namespace uses a 4-hour window because it serves only consented, curated content. Upload (PUT) URLs expire in 15 minutes.
- **`--network` cannot capture your own credentials.** The OAuth, `signInWithIdp`, and token-refresh hosts are excluded from `--network` HTTPS interception even when the user overrides the default blocklist, and the capture proxy refuses to start if any required host is missing (fail closed).
- **Daemon cloud-credential containment.** The live-recording upload path is now wired, and the containment it relies on is live: the long-lived refresh token stays in the foreground/login context and is never handed to the daemon. The all-day daemon and its engine subprocess receive only a short-lived ID token, delivered out-of-band via a `0600` file path (passed as a path, never on the command line where `ps` could read it). The daemon re-mints + rewrites that file on a timer so a recording can outlast the ~1h token, and a token failure fails closed — the recording stays local (it is never deleted).

## Cloud recording privacy: capture-time blocking vs. post-hoc masking

The unified recording pipeline (the "disk-first pipeline" refactor) captures **rich chunks to disk as the source of truth** and converges each recording toward its destination through a single terminal stage that owns the cloud-bound scrub/mask transform. Two facts define the cloud-video privacy boundary, and the boundary differs depending on a single config flag, `masked_video_upload` (`SCREENCAP_MASKED_VIDEO_UPLOAD`, **default `false`**):

- **`recording.db` is local-only, by rule (never uploaded).** The raw `recording.db` holds unscrubbed PII (window titles, event text) and now also hosts the per-chunk pipeline ledger. It is the local source of structured truth and is **never** part of any uploaded set — enforced as one explicit deny rule at the single upload seam (`upload.list_recording_files` excludes `recording.db`, its `-wal`/`-shm` sidecars, and `*.scrub_failed`, matched by full relative path; `upload.assert_uploadable` hard-rejects any direct attempt to enqueue it). Cloud-bound structured data derives **only** from scrubbed exports (events JSONL, transcript, manifest), never the DB.

- **Cloud video privacy depends on the `masked_video_upload` flag:**
  - **Flag OFF (the default, and the current shipped posture): capture-time blocking is a *structural* guarantee.** For a cloud-bound recording, sensitive application windows are dropped from the recorded video *at capture time* (`RecorderPrivacyFilter`), so the sensitive pixels are **never written to disk** in the cloud-bound video and cannot leak even if every later stage fails. This is the strong guarantee and it is unchanged from prior releases.
  - **Flag ON (future, not yet enabled): cloud video is captured rich and masked *post-hoc*.** Video is captured for all destinations (rich local copy, R7), and the terminal stage produces a masked cloud copy by drawing opaque masks over sensitive-window regions using the recorded `window_geometry` timeline (`video_mask.mask_video_chunk`). This is an **operational** guarantee, weaker than capture-time blocking: it depends on geometry coverage and on the masker running correctly. It is defended by a **fail-closed three-way coverage gate** — for each chunk's frame span, the masker (a) produces an unmasked copy only when geometry *proves* no sensitive window densely covered the whole span; (b) **fails closed** (marks the chunk `FAILED`, produces no copy, blocks upload) when coverage is unprovable — any geometry-capture-failure marker in the span, zero samples, or an inter-sample gap exceeding a bounded maximum (2.0s); (c) otherwise masks conservatively (hold-and-pad + dilate so a window moving between samples is over-masked). A "did nothing" outcome on a sensitive chunk is loudly distinguished from success and fails closed; a missing classifier or a decode error also fails closed.

**The trade-off is explicit and accepted.** Moving cloud-video privacy from capture-time blocking (pixels never recorded) to post-hoc masking (pixels recorded, then masked, fail-closed-dependent) trades a structural guarantee for an operational one tied to STRATEGY.md's near-zero privacy-incident bar. The fail-closed coverage gate is what keeps that bar; capture-time fails safe (not recorded), post-hoc fails open (recorded; the masker must not miss) — which is why the flag defaults OFF.

**Enabling `masked_video_upload` is gated on two prerequisites that are not yet met** (so the flag MUST remain OFF until both land):

1. **Live-upload cutover.** The live in-process upload path (engine `finalize_uploads` / `chunk_processor`) is not yet routed through the post-hoc masker — only the terminal stage is. Flipping the flag ON before the live path delegates to the terminal stage could ship rich, unmasked video via the live upload. The capture-time blocking removal (`block_video`) is gated behind this same flag, so the default-OFF posture keeps the structural guarantee intact.
2. **Consent-surface reconciliation.** The native redaction-review window still labels video "local-only, not uploaded." That surface must be updated to show that masked video uploads before any operator can meaningfully consent to it.

## Threats in scope

ScreenCap aims to defend against the following:

- **Cross-user access on a shared Mac.** Another macOS user account on the same machine cannot read the socket, read recordings under `~/.screencap/recordings/`, or read the audit log. The `0o700` parent directory and per-user home directory enforce this.
- **Umask drift and accidental permission relaxation.** If `~/.screencap/run/` or `api.sock` is ever chmod-ed outside its expected mode (umask race, manual `chmod`, packaging mistake), the daemon refuses to start rather than serving over a leaky socket.
- **PID-spoofing of advisory provenance.** The provenance classifier knows the peer's PID and argv at probe time are racy (PID reuse, `exec()` between accept and probe). It treats those signals as advisory metadata, never as an auth gate. A caller cannot impersonate the SwiftUI app to bypass authorization, because no authorization decision uses the classification.
- **Caller-supplied metadata tampering.** Clients may send a `started_by` field, but the daemon overrides it with its server-derived classification before persisting. The persisted metadata reflects what the daemon observed, not what the caller claimed.

## Threats out of scope

These threats are *not* defended against by the daemon and require separate mitigations or are explicitly accepted:

- **Malware already running as the same macOS user.** Any process with your effective UID can read `~/.screencap/recordings/`, connect to the daemon socket, read the audit log, read the short-lived engine ID token from `~/.screencap/run/engine-token-<name>.jwt` (mode `0600`) while a cloud-bound recording is live, read the cloud refresh token from your Keychain (default "Always Allow" ACL), and call any verb. Same-user trust is the documented boundary; the daemon is not a sandbox for processes running with your privileges. The macOS TCC subsystem still gates the underlying screen-capture capability — code without Screen Recording permission cannot capture screen content even if it can call `/v0/recording.start` — but the daemon itself does not enforce a per-caller capability check above same-user.
- **Root, administrator, or another user with read access to your home directory.** A process running as root can read anything; a backup tool with full-disk access can copy recordings; another user with sudo can impersonate you.
- **Physical access to an unlocked Mac.** Anyone at the keyboard inherits your session privileges.
- **Compromised dependencies or supply-chain attacks.** Mitigated by Python and Homebrew's own integrity controls, not by the daemon.
- **Side-channel inference from recording metadata.** Filenames, durations, and audit-log peer paths can leak that you recorded *something*, even if the content is encrypted at rest. Treat the audit log and recording directory as the same sensitivity class as the recordings themselves.

## Alternatives considered

SCR-64 (the ticket that motivated this document) asked whether same-user trust is acceptable for ScreenCap or whether a stronger per-caller authentication mechanism should be added. Two alternatives were considered and rejected.

**Promoting provenance to an enforcement gate.** The daemon already derives a `swiftui` / `cli` / `mcp` / `unknown` classification per peer. One could imagine rejecting `unknown` peers, or only allowing known SwiftUI / CLI binaries. This was rejected because the PID-reuse and time-of-check / time-of-use races on AF_UNIX cannot be closed without code-signing requirement verification (see Scott Knight's [audit tokens writeup](https://knight.sc/reverse%20engineering/2020/03/20/audit-tokens-explained.html) and Quarkslab's [Intego LPE analysis](https://blog.quarkslab.com/intego_lpe_macos_2.html)), and ScreenCap's mixed distribution model (signed `.app` bundle plus unsigned or ad-hoc-signed CLI binaries via Homebrew and source builds) cannot support a stable code-signing requirement string today. The gate would also lock out same-user automation (cron jobs, ad-hoc scripts, future MCP-style tools) without raising the bar against an attacker who can simply invoke the bundled CLI binary directly.

**Bearer token co-installed via SMAppService.** The daemon could generate a random secret on first launch, write it to `~/.screencap/run/api.token` at mode `0o600`, and require clients to send it as an `Authorization: Bearer` header. This was rejected because any process that can connect to the socket can also read the token file (same UID, same directory, same mode), so the token adds no defense against the threat that motivates it. It also introduces worse-than-no-token failure modes: tokens leak into shell history, `ps` output, crash reports, and support bundles, and developers relaxing token-file permissions to debug ("just chmod 644 for a sec") is a documented anti-pattern. This matches Apple's DTS guidance on AF_UNIX IPC and the `ssh-agent` design rationale.

A future option — per-call code-signing verification via `SecCodeCopyGuestWithAttributes` against a Team-ID requirement string — would genuinely raise the bar against a same-user attacker by forcing a Developer ID forgery. It is blocked today by ScreenCap's mixed distribution model and would require its own design pass. The full rationale lives in the [SCR-64 plan](docs/plans/2026-05-25-001-fix-daemon-socket-per-caller-auth-plan.md).

## Reporting a vulnerability

Please report security vulnerabilities **privately** via GitHub's "Report a vulnerability" form on the [Security Advisories page](https://github.com/proteus-computer-use/screencap/security/advisories/new). This opens a private channel with the maintainers and avoids public disclosure before a fix is available.

If you cannot use GitHub for any reason, you may instead open a regular issue marked `[SECURITY]` requesting a private contact channel — but please do **not** include vulnerability details in the public issue.

We aim to acknowledge reports within three business days and to publish a coordinated advisory once a fix is shipped.
