---
title: "test: SCR-140 U5 carry-forward EFFECT tests (mitmdump tunnel proof + gcs 3.x signing-contract re-test)"
type: test
status: completed
date: 2026-06-25
origin: docs/todos/feat/per-user-cloud-storage-isolation/000-handoff.md
---

# test: SCR-140 U5 carry-forward EFFECT tests

## Summary

Discharge the two open U5 carry-forwards from the per-user cloud-storage-isolation work with **EFFECT/contract tests that exercise the real third-party tool** (not a mock): (1) a gated integration test proving real `mitmdump` *tunnels* every `REQUIRED_AUTH_IGNORE_HOSTS` host so `--network` self-recording physically cannot capture the user's bearer/refresh-token exchange, and (2) an offline real-library test proving the forced `google-cloud-storage` 3.x crc32c-default change does not leak a checksum requirement into the signed-URL upload contract. The third ticket item — two "minor cleanups" — is **already complete on `main`**; this plan records that and only updates the stale carry-forward references.

---

## Problem Frame

SCR-140 collects the U5 carry-forwards that were deliberately not blocking U5's merge but must land **before the cloud function deploys** (see origin: `docs/todos/feat/per-user-cloud-storage-isolation/000-handoff.md`). Both remaining test items are trust-critical *proofs*: the blocklist logic and the gcs pin already shipped with unit tests, but those unit tests **mock the real tool entirely** — `tests/network/test_blocklist.py` proves the regex *matches* the auth hosts but not that mitmproxy *tunnels* them at runtime; the cloud-function suite mocks `google.cloud.storage.Client` so it proves nothing about the actual 3.x signing behavior. The documented team lesson is explicit: *"End-to-end EFFECT check >> regex unit test"* (`docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md`). This plan closes that mock-vs-reality gap for both items.

---

## Requirements

- R1. Prove end-to-end (real mitmproxy, EFFECT not regex) that with the production `ignore_hosts`, each host in `REQUIRED_AUTH_IGNORE_HOSTS` is **CONNECT-tunneled, not TLS-intercepted** — so the capture addon physically cannot observe the OAuth / token-exchange / refresh / ID-token plaintext.
- R2. Prove the forced `google-cloud-storage` 3.x crc32c-default change does **not** alter the signed-URL upload contract: a v4 signed PUT URL requires only `Content-Type` and carries no `x-goog-hash` / checksum requirement, so the client's raw PUT (Content-Type only) still succeeds.
- R3. Both new tests are correctly **gated** (skip cleanly when their prerequisite — `mitmdump` on PATH + network reachability — is absent) so CI without those prerequisites stays green, with no false failures and no vacuous passes.
- R4. The carry-forward references in the handoff and origin plan are **discharged**: the two minor cleanups recorded as already-complete (with commit + test citations), and the two EFFECT tests marked as the remaining carry-forwards closed.

---

## Scope Boundaries

- **Not re-doing the two "minor cleanups"** — both already shipped on `main` in commit `b3fc9853` and are tested. No new code for them (see Context & Research → Already Complete). This was the user's chosen disposition: note as already-done.
- **Not changing any production code** — `blocklist.py`, the `proxy_runner` fail-closed gate, the cloud-function signing handler, and the client PUT path all stay exactly as shipped. This plan is additive tests + a docs closeout only.
- **Not a full screencap-stack integration** of the mitmdump proof — U1 proves the proxy *tunnels* each auth host (cert issuer ≠ test-proxy CA), not that the production `run_proxy` mp.Process + `NetworkCapture` addon + `recording.db` stack records zero rows for them. The equivalence rests on mitmproxy's tunneled-flow opacity (a tunneled flow never reaches addon hooks — `docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md`); the deeper `run_proxy` → DB layer is deferred for review to judge (see Deferred to Follow-Up Work). Because this EFFECT test is gated/skip-by-default, the **continuous** regression guard against a dropped auth host remains the always-on `missing_required_auth_hosts(patterns) == set()` unit test (`tests/network/test_blocklist.py`) + the `proxy_runner` fail-closed gate — U1 adds the runtime EFFECT proof on top of them, it does not replace them.
- **Not the migration per-object `crc32c` verify** (the path that genuinely uses the bumped library's transfer methods) — that lives in the migration scripts (U8) and is covered by SCR-145.
- **Not deploying the function and not provisioning OAuth credentials** — both are separate, gated by the pre-deploy hold (see origin handoff "🚨 CRITICAL"); SCR-137 / the runbook own them.

### Deferred to Follow-Up Work

- Deeper full-stack mitmdump proof (real `run_proxy` → `recording.db` → assert zero `network_event` rows for auth hosts): a follow-up only if review judges the proxy-level cert-issuer proof insufficient.
- **`login.microsoftonline.com` (and other third-party SSO) auth-capture gap** — Microsoft SSO is in the *user-overridable* `DEFAULT_BLOCKLIST` but **not** in the non-overridable `REQUIRED_AUTH_IGNORE_HOSTS`, so a user who sets `override_default_blocklist = true` could expose their own Microsoft OAuth/token exchange to capture. This is a pre-existing production-code design question (not introduced by this plan); changing the non-overridable set is out of scope for a test-authoring task. Recommend a **separate Linear ticket** to decide whether non-Google SSO auth hosts belong in `REQUIRED_AUTH_IGNORE_HOSTS`. U1 should carry a one-line comment that its 4-host scope is intentionally screencap's own Firebase/Google auth flow, so a reader doesn't infer the set is exhaustive across all providers.

---

## Context & Research

### Relevant Code and Patterns

- **`src/screencap/network/blocklist.py`** — `REQUIRED_AUTH_IGNORE_HOSTS` (the 4 non-overridable auth hosts: `accounts.google.com`, `oauth2.googleapis.com`, `identitytoolkit.googleapis.com`, `securetoken.googleapis.com`), `build_ignore_hosts_regex(privacy_config, network_config)` (the production pattern builder), `missing_required_auth_hosts(patterns)` (the fail-closed check).
- **`src/screencap/network/proxy_runner.py:153-164`** — the fail-closed gate: `build_ignore_hosts_regex` output is passed to mitmproxy `Options(ignore_hosts=...)`; if any required host is missing the proxy refuses to start. This is the production wiring the EFFECT test must mirror (build patterns the same way).
- **`tests/network/test_blocklist.py`** — existing unit tests, including `TestBuildIgnoreHostsRegex::test_normal_host_does_not_match` (the IPv4-peername adversarial guard) and the `missing_required_auth_hosts` coverage; reuse its `_privacy()` / `_network()` default-config helpers to build the production patterns in the new EFFECT test.
- **`tests/network/test_ca_lifecycle.py`** — how the proxy CA / confdir is created in tests; reference for spawning `mitmdump` with a generated CA so interception is provable.
- **Gated-integration precedent** — `tests/test_export_integration.py:870` (`@pytest.mark.slow`), `tests/daemon/test_launchagent.py:92` (`@pytest.mark.skipif(shutil.which(...) is None)`): the exact skip/gate idiom for a test that needs an external binary.
- **`scripts/cloud-function/main.py:372-428`** — `_handle_upload` signs v4 PUT URLs via `blob.generate_signed_url(version="v4", method="PUT", content_type=..., service_account_email=..., access_token=...)`. The function **never uploads bytes** — it only signs. `scripts/cloud-function/test_main.py` + `conftest.py` mock `google.cloud.storage.Client` session-wide (so the suite is offline), with a `FakeBlob.generate_signed_url` that records kwargs but returns a stub URL — i.e., the real library's signing behavior is never exercised today.
- **`src/screencap/upload.py:712-745`** — `_upload_with_progress` PUTs to the signed URL via raw `requests.put(signed_url, headers={"Content-Type": ..., "Content-Length": ...})` — **no checksum header**. The other end of the contract R2 pins.

### Already Complete (the "minor cleanups" — no work needed)

- **`config.get_sessions_dir` removed** — vestigial helper deleted in commit `b3fc9853` ("fix(cloud): address U5 code-review findings"). Zero references remain anywhere in `src/`, `tests/`, `macos/`, `scripts/`.
- **Stale `engine-token-*.jwt` prune on startup** — implemented as `Supervisor._prune_stale_engine_token_files` (`src/screencap/daemon/supervisor.py:791`), wired into the daemon startup-recovery `finally` (`supervisor.py:782`), and tested by `tests/daemon/test_supervisor.py::test_reconcile_prunes_stale_engine_token_file`. Also introduced in `b3fc9853`.

### Institutional Learnings

- **`docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md`** — the load-bearing reference for U1. Establishes (a) the failure mode where a wrong `ignore_hosts` pattern silently tunnels *everything* while unit tests pass, (b) the **smoking-gun signal**: curl-through-proxy cert issuer is the *real upstream* CA when tunneled vs the *proxy* CA when intercepted, and (c) Prevention #1 — *"Spawn the tool with the exact production options, then check the EFFECT … End-to-end EFFECT check >> regex unit test."* U1 operationalizes that prevention as a permanent test.

### External References

- google-cloud-storage 3.0 changed upload/download defaults to `checksum="auto"` (crc32c). This applies to the **transfer methods** (`Blob.upload_from_*` / `download_to_*`) — **not** to `generate_signed_url` (a pure signing op) and not to a raw HTTP PUT. Confirms R2's premise: neither the function (signs only) nor the client (raw PUT) touches a checksummed transfer path; the migration `rewrite()` path (separate, SCR-145) is the only place the change bites. ([Cloud Storage release notes](https://docs.cloud.google.com/storage/docs/release-notes), [Data validation with checksums](https://docs.cloud.google.com/storage/docs/data-validation))

---

## Key Technical Decisions

- **U1 builds patterns from the real `build_ignore_hosts_regex`, not a hand-copied regex** — the EFFECT test tracks production config drift automatically; if a future change drops a required host from the builder, the test's tunnel assertion changes. Rationale: a hand-copied pattern would silently diverge from production (the exact class of bug the origin solution doc describes).
- **U1 uses proxy-level cert-issuer fidelity, with a mandatory positive control** — the decisive signal is the TLS cert issuer seen through the proxy (real upstream CA = tunneled; test-proxy CA = intercepted). A positive control (a *non-listed* host that **must** be intercepted) is required so a globally-misconfigured proxy that tunnels everything can't make the auth-host assertions pass vacuously. Rationale: mirrors the documented smoking gun; mitmproxy's architecture already guarantees the addon can't see a tunneled flow, so cert-issuer is a sound proxy for "capturable."
- **U1 is real-network + gated, not hermetic** — proving the *literal* Google auth hosts are tunneled needs the real upstream, so the test hits them and skips cleanly when offline or `mitmdump` is absent. It is a **local pre-deploy proof** (the ticket says "do before the function deploys"), not an always-on CI gate. Rationale: user-selected; matches the team lesson that the end-to-end EFFECT is what counts here.
- **U2 exercises the real gcs 3.x `generate_signed_url` with a deterministic local signer (offline)** — pass an explicit `credentials=` (an ephemeral, generated-at-test-time service-account key) **and an explicit `api_access_endpoint="https://storage.googleapis.com"`** so v4 signing happens locally with no network and no IAM call, on the **real** `storage.Blob` class. The explicit endpoint is load-bearing: gcs 3.x v4 signing reads the bucket's client `api_endpoint` to build the `host` SignedHeader, and under conftest's session-wide `storage.Client` mock that attribute is a `MagicMock` that makes signing raise — so the test must supply the endpoint (or construct a real `storage.Client` with a concrete endpoint) rather than rely on the mocked client. Assert the signed URL's `X-Goog-SignedHeaders` is `content-type;host` (no `x-goog-hash`), no `x-goog-hash` query param, and a real `X-Goog-Signature` is present (not the `FakeBlob` stub). **Fidelity caveat:** local-key signing pins the *shared* v4 SignedHeaders/query-param construction (the checksum-relevant surface), but it exercises a different gcs branch than production's IAM `signBlob` path (`service_account_email=` + `access_token=`, which Cloud Run must use because its compute credentials can't sign locally) — so the test is a library-contract guard, not a function-signing-path regression guard. Rationale: user-selected; CI-friendly and fails loudly if a future bump makes signing checksum-aware — which a mock-only assertion never could.

---

## Open Questions

### Resolved During Planning

- *Are the two "minor cleanups" still open?* — No. Both shipped in `b3fc9853` with tests (research-verified). Disposition: note as already-done (user choice).
- *Does the gcs 3.x crc32c default affect the function or client?* — No. It affects `upload_from_*` / `download_to_*` only; the function signs and the client raw-PUTs. R2 is a proof, not a fix (external-docs verified).
- *Which fidelity for each EFFECT test?* — mitmdump: real-network gated; checksum: real-library local-signer contract test (both user-selected).

### Deferred to Implementation

- **Cert-issuer extraction mechanism for U1** — `curl -v` stderr parse vs a small Python `ssl.get_server_certificate` / `cryptography` issuer read through the proxy. Decide when wiring; assert on *"issuer ≠ test-proxy CA"* (robust to whichever real CA Google presents) rather than a hard-coded CA name.
- **Ephemeral signer-key mechanism for U2** — the exact API to generate the throwaway RSA key at test time (e.g. `cryptography` `rsa.generate_private_key` wrapped into `service_account.Credentials`). The *ephemeral* property is a hard requirement (no in-repo private key), not a choice; only the generation mechanism is left to the implementer.
- **Home for the client-side companion assertion** — extend an existing `tests/test_upload.py` case vs a new focused test; decide against the current file layout.
- **Whether a deeper full-stack mitmdump layer is warranted** — only if review finds the proxy-level proof insufficient (see Deferred to Follow-Up Work).

---

## Implementation Units

### U1. mitmdump EFFECT test — prove `REQUIRED_AUTH_IGNORE_HOSTS` are tunneled

**Goal:** A gated integration test that spawns real `mitmdump` with the production `ignore_hosts` and proves, by TLS cert issuer, that each `REQUIRED_AUTH_IGNORE_HOSTS` host is CONNECT-tunneled (real upstream cert) while a non-listed control host is TLS-intercepted (test-proxy cert) — discharging the "mitmdump EFFECT test" carry-forward.

**Requirements:** R1, R3

**Dependencies:** None

**Files:**
- Create: `tests/network/test_required_auth_effect.py`
- Test: `tests/network/test_required_auth_effect.py` (this unit *is* the test)
- Reference (no change): `src/screencap/network/blocklist.py`, `src/screencap/network/proxy_runner.py`

**Approach:**
- Build the `ignore_hosts` patterns via the **real** `build_ignore_hosts_regex(_privacy(), _network())` (reuse `tests/network/test_blocklist.py`'s default-config helpers) so the test mirrors `proxy_runner.py:147`.
- Spawn `mitmdump` as a subprocess on a free loopback port with those patterns (`--set ignore_hosts=…` per pattern) and a fresh tmp `--set confdir=…` (mitmproxy auto-generates a CA there); wait for "listening" before probing; guarantee subprocess teardown in a `finally` / fixture even on assertion failure.
- For each `REQUIRED_AUTH_IGNORE_HOSTS` host: connect through the proxy and read the served TLS cert issuer → assert it is **not** the test-proxy CA (→ tunneled).
- Positive control: a non-listed host (e.g. `example.com`) → assert the issuer **is** the test-proxy CA (→ intercepted). This guards against a vacuous all-tunnel pass. **First assert the control host is genuinely not matched** by the built patterns (`assert not any(re.search(p, "example.com:443", re.IGNORECASE) for p in patterns)`) so a future `DEFAULT_BLOCKLIST` / `extra_blocklist` expansion can't silently turn the control into a tunneled host and neutralize the guard.
- Gate the whole module: `@pytest.mark.slow` + `skipif` on `shutil.which("mitmdump") is None` + skip on network-unreachable (connection error to the control host).

**Technical design:** *(directional guidance, not implementation specification)*

The single assertion table the test encodes — same `ignore_hosts`, opposite outcomes:

| Host (through proxy) | On `ignore_hosts`? | Expected cert issuer | Meaning | Asserts |
|---|---|---|---|---|
| `accounts.google.com` (+ 3 other auth hosts) | yes (required) | real upstream CA | **tunneled** — addon can't see plaintext | R1 |
| `example.com` (control) | no | test-proxy generated CA | **intercepted** | not-vacuous |

**Patterns to follow:**
- Skip/gate idiom: `tests/daemon/test_launchagent.py:92` (`shutil.which`), `tests/test_export_integration.py:870` (`@pytest.mark.slow`).
- Pattern construction + default configs: `tests/network/test_blocklist.py` (`_privacy()`, `_network()`).
- Proxy CA / confdir handling in tests: `tests/network/test_ca_lifecycle.py`.

**Test scenarios:**
- Happy path: for each of the 4 `REQUIRED_AUTH_IGNORE_HOSTS`, cert issuer through the proxy ≠ test-proxy CA → tunneled (the credential-exchange host is never intercepted).
- Integration (positive control): `example.com` (not on the list) → cert issuer == test-proxy CA → intercepted. Without this the suite could pass vacuously. Setup first asserts the control host is *not* matched by the built patterns, so a future blocklist expansion can't neutralize the control.
- Edge case: `mitmdump` absent on PATH → test SKIPS (not fails).
- Edge case: network/auth-host unreachable (offline CI) → test SKIPS (not fails).
- Edge case: `mitmdump` subprocess is always terminated on teardown, including when an assertion fails mid-test (no orphaned proxy / leaked port).
- Edge case (optional, belt-and-suspenders): a subdomain probe of one auth host (e.g. `login.accounts.google.com:443`) is also tunneled — exercises the pattern's `^(.+\.)?{host}:\d+$` subdomain anchor at the EFFECT level, not just the apex.
- Covers the origin solution doc's Prevention #1 (EFFECT check with the exact production options).
- Carry a one-line comment that the 4-host scope is intentionally screencap's own Firebase/Google auth flow (not exhaustive across all SSO providers) — see Deferred to Follow-Up Work re: `login.microsoftonline.com`.

**Verification:**
- Run locally with `mitmdump` installed + network: the test passes — all 4 auth hosts tunneled, control host intercepted.
- Run with `mitmdump` uninstalled or offline: the test reports SKIPPED, suite stays green.
- Temporarily removing a host from `REQUIRED_AUTH_IGNORE_HOSTS` (thought-experiment / mutation) flips that host's assertion to failure — i.e., the test is load-bearing, not decorative.

---

### U2. Upload-checksum re-test — prove gcs 3.x signing stays checksum-free

**Goal:** A real-library, offline test proving that under the installed `google-cloud-storage` 3.x, a v4 signed PUT URL requires only `Content-Type` (no `x-goog-hash` / checksum), plus a companion assertion pinning that the client PUT sends no checksum header — discharging the "upload-checksum re-test" carry-forward.

**Requirements:** R2, R3

**Dependencies:** None

**Files:**
- Create: `scripts/cloud-function/test_signing_contract.py` (keeps the real-library test clearly separated from the `Client`-mocked `test_main.py` suite)
- Test: `scripts/cloud-function/test_signing_contract.py`; companion assertion in `tests/test_upload.py` (client side)
- Modify (docs): `scripts/cloud-function/requirements.txt` comment to cite this test as the discharge of "re-tested in U2"
- Reference (no change): `scripts/cloud-function/main.py`, `src/screencap/upload.py`

**Approach:**
- Build an ephemeral `service_account.Credentials` — the RSA key **must** be generated at test time (e.g. `cryptography` `rsa.generate_private_key`); never check a private key into the repo. Call the **real** `storage.Blob("video.mp4", bucket).generate_signed_url(version="v4", method="PUT", content_type="video/mp4", credentials=<creds>, api_access_endpoint="https://storage.googleapis.com", expiration=…)`. The explicit `api_access_endpoint` is required: gcs 3.x v4 signing reads the bucket's client `api_endpoint` for the `host` SignedHeader, and conftest's session-wide `storage.Client` mock makes that attribute a `MagicMock` that raises during signing — so do **not** rely on the mocked client; supply the endpoint (or construct a real `storage.Client` with a concrete endpoint). `storage.Blob` itself is the real class, so the signature/query construction exercised is the real 3.x library's.
- Parse the returned URL's query string: assert `X-Goog-SignedHeaders` is `content-type;host` (contains `content-type` + `host`, excludes `x-goog-hash`), there is **no** `x-goog-hash` query param, `X-Goog-Signature` is present (proves it's a real signed URL, not the `FakeBlob` stub), and `method`/expiry are as requested.
- Companion (client end of the contract): assert the client PUT path sends `Content-Type` (and `Content-Length`) and **no** `x-goog-hash` / checksum header — pinning `upload.py:725` so both ends stay checksum-free together.

**Patterns to follow:**
- `scripts/cloud-function/test_main.py` for suite structure/fixtures; deliberately **bypass** its `FakeBlob` for this test (use the real `storage.Blob`).
- Existing client upload tests in `tests/test_upload.py` for how `requests.put` is mocked/asserted.

**Test scenarios:**
- Happy path: real 3.x `generate_signed_url(version="v4", method="PUT", content_type="video/mp4")` → `X-Goog-SignedHeaders=content-type;host`, no `x-goog-hash`, has `X-Goog-Signature`. Proves checksum-free signing under the bumped library.
- Contract guard (not-vacuous): the URL is a genuine signed URL (`X-Goog-Signature` present), not the `https://signed.example/...` `FakeBlob` stub — i.e., the real library was exercised.
- Integration (cross-end): the client PUT path (`upload.py`) sends `Content-Type` only, no checksum header → both ends of the upload contract agree, so a checksum-free signed URL is satisfiable.
- Edge case (documentation assertion, optional): a comment/docstring records that gcs 3.x `checksum="auto"` applies to `upload_from_*`/`download_to_*` only — neither used here.

**Verification:**
- `cd scripts/cloud-function && python -m pytest test_signing_contract.py` passes offline (no network, no credentials).
- The client companion test passes under `PYTHONPATH=src python -m pytest tests/test_upload.py`.
- The `requirements.txt` "re-tested in U2" comment points at the now-existing test.

---

### U3. Discharge the carry-forward references (docs closeout)

**Goal:** Update the handoff and origin plan so the SCR-140 carry-forwards are no longer described as open — record the two cleanups as already-done (commit + test refs) and the two EFFECT tests as the closing work. No code change.

**Requirements:** R4

**Dependencies:** U1, U2 (land the tests first, then mark discharged)

**Files:**
- Modify: `docs/todos/feat/per-user-cloud-storage-isolation/000-handoff.md` (the "### U5 carry-forwards" block)
- Modify: `docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md` (Execution Status / U5 carry-forward note, if present)

**Approach:**
- In the handoff's U5 carry-forwards bullets: mark the mitmdump EFFECT test → done (point at `tests/network/test_required_auth_effect.py`, U1); the upload-checksum re-test → done (point at `scripts/cloud-function/test_signing_contract.py`, U2); the two minor cleanups → already done in `b3fc9853` + `tests/daemon/test_supervisor.py::test_reconcile_prunes_stale_engine_token_file`.
- Mirror the same closure in the origin plan's carry-forward note.
- Mark the mitmdump carry-forward discharged only after recording the **actual local EFFECT-proof run** (date + per-host cert-issuer outcome), not merely the existence of `test_required_auth_effect.py` — because U1 skips by default, citing the test file alone proves a test was written, not that the auth hosts were observed to tunnel before deploy.
- Closing the Linear ticket SCR-140 itself is an operator action (out of scope for the diff).

**Patterns to follow:**
- The handoff's own "Already shipped (U1–U6)" style — commit SHAs + test paths inline.

**Test scenarios:**
- Test expectation: none — documentation-only closeout, no behavioral change.

**Verification:**
- The handoff no longer lists any SCR-140 item as open; the mitmdump bullet cites an observed pre-deploy EFFECT-run result (date + per-host outcome), the checksum bullet cites the new test, and the cleanups cite the commit + test.

---

## System-Wide Impact

- **Interaction graph:** U1 exercises the network-proxy subsystem (`blocklist` → `proxy_runner` → real mitmproxy); U2 exercises the cloud-function signing path + the client upload PUT. Both are **additive tests** — no production call graph changes.
- **API surface parity:** none changed. The signed-URL contract and the client PUT headers are *pinned* by U2, not altered.
- **Unchanged invariants (blast-radius assurance):** `REQUIRED_AUTH_IGNORE_HOSTS`, `build_ignore_hosts_regex`, the `proxy_runner` fail-closed gate, `_handle_upload`'s signing, and `upload.py`'s PUT headers all remain exactly as shipped. This plan adds proofs around them and touches no behavior.
- **CI coverage:** U1 skips when `mitmdump`/network are absent; U2 runs fully offline in the cloud-function suite. Neither introduces a flaky always-on gate.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| U1 real-network test is flaky (upstream cert-chain churn, transient unreachability) | Assert *"issuer ≠ test-proxy CA"* (robust to whichever real CA Google serves), not a specific CA name; skip on connection error; keep it a local pre-deploy proof, not a CI merge gate. |
| U1 passes vacuously if the proxy tunnels everything (the original bug's shape) | Mandatory positive control: a non-listed host that **must** be intercepted (issuer == test-proxy CA). |
| U1 leaks a `mitmdump` subprocess / port on assertion failure | Spawn via a fixture with guaranteed teardown (`finally` / fixture finalizer). |
| U2 accidentally exercises the mock instead of the real library (vacuous pass) | Assert `X-Goog-Signature` is present (real signed URL), explicitly not the `FakeBlob` stub; use real `storage.Blob` + explicit `credentials=`. |
| U2 recipe raises at signing time under conftest's session-wide `storage.Client` mock (v4 signing reads the bucket client's `api_endpoint` for the `host` SignedHeader) | Pass explicit `api_access_endpoint="https://storage.googleapis.com"` (or build a real `storage.Client` with a concrete endpoint) — do not rely on the mocked client. Verified to reproduce; documented in U2 Approach. |
| Forced gcs `firebase-admin ↔ google-cloud-storage` 3.x conflict re-surfaces | U2 pins the signing contract under the installed 3.x; the migration `crc32c` path is covered separately (SCR-145). |

---

## Documentation / Operational Notes

- **Run U1 locally before the function deploys** (the ticket's "do before the function deploys"): `pip install -e ".[dev]"` (or `PYTHONPATH=src`), ensure `mitmdump` is installed, then run `tests/network/test_required_auth_effect.py` with the slow marker enabled. Record the pass as the pre-deploy EFFECT proof.
- **Test entry points:** cloud-function suite → `cd scripts/cloud-function && python -m pytest`; client suite (in a worktree) → `PYTHONPATH=src python -m pytest`.
- **U3** updates `docs/todos/feat/per-user-cloud-storage-isolation/000-handoff.md` and the origin plan; closing SCR-140 in Linear is a manual operator step after the tests land.

---

## Sources & References

- **Origin handoff:** [docs/todos/feat/per-user-cloud-storage-isolation/000-handoff.md](docs/todos/feat/per-user-cloud-storage-isolation/000-handoff.md) (U5 carry-forwards block)
- **Origin plan:** [docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md](docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md)
- **EFFECT-test lesson:** [docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md](docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md)
- **Code:** `src/screencap/network/blocklist.py`, `src/screencap/network/proxy_runner.py`, `scripts/cloud-function/main.py`, `src/screencap/upload.py`
- **Already-done cleanups:** commit `b3fc9853`; test `tests/daemon/test_supervisor.py::test_reconcile_prunes_stale_engine_token_file`
- **Related ticket:** SCR-145 (migration per-object crc32c verify), SCR-137 (cloud-auth creds + deploy gate)
- **External:** [google-cloud-storage release notes](https://docs.cloud.google.com/storage/docs/release-notes), [Data validation with checksums](https://docs.cloud.google.com/storage/docs/data-validation)
