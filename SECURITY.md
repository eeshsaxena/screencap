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

## Threats in scope

ScreenCap aims to defend against the following:

- **Cross-user access on a shared Mac.** Another macOS user account on the same machine cannot read the socket, read recordings under `~/.screencap/recordings/`, or read the audit log. The `0o700` parent directory and per-user home directory enforce this.
- **Umask drift and accidental permission relaxation.** If `~/.screencap/run/` or `api.sock` is ever chmod-ed outside its expected mode (umask race, manual `chmod`, packaging mistake), the daemon refuses to start rather than serving over a leaky socket.
- **PID-spoofing of advisory provenance.** The provenance classifier knows the peer's PID and argv at probe time are racy (PID reuse, `exec()` between accept and probe). It treats those signals as advisory metadata, never as an auth gate. A caller cannot impersonate the SwiftUI app to bypass authorization, because no authorization decision uses the classification.
- **Caller-supplied metadata tampering.** Clients may send a `started_by` field, but the daemon overrides it with its server-derived classification before persisting. The persisted metadata reflects what the daemon observed, not what the caller claimed.

## Threats out of scope

These threats are *not* defended against by the daemon and require separate mitigations or are explicitly accepted:

- **Malware already running as the same macOS user.** Any process with your effective UID can read `~/.screencap/recordings/`, connect to the daemon socket, read the audit log, and call any verb. Same-user trust is the documented boundary; the daemon is not a sandbox for processes running with your privileges. The macOS TCC subsystem still gates the underlying screen-capture capability — code without Screen Recording permission cannot capture screen content even if it can call `/v0/recording.start` — but the daemon itself does not enforce a per-caller capability check above same-user.
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
