"""Handler tests for the signing Cloud Function — per-user isolation (U2).

These are the integration-level security assertions the plan calls out: the most
important is AE2 (a user cannot sign-download another user's recording by name).
``verify_bearer`` is patched per-test to a uid (or to raise), and GCS is a small
in-memory fake whose ``list_blobs`` filters by prefix — so a handler that lists
the wrong prefix is provably caught (a prefix-only mock would miss it).
"""

import contextlib
import logging
from datetime import timedelta
from unittest import mock

import flask
import google.api_core.exceptions
import google.auth.exceptions
import main
import pytest
from auth import AuthInvalid, AuthUnavailable


@pytest.fixture(autouse=True)
def app_context():
    # jsonify() needs an application context.
    app = flask.Flask(__name__)
    with app.app_context():
        yield


# --------------------------------------------------------------------------
# In-memory GCS fake
# --------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, name, size=10, exists=True):
        self.name = name
        self.size = size
        self._exists = exists
        self.signed_kwargs = None

    def exists(self):
        return self._exists

    def generate_signed_url(self, **kwargs):
        self.signed_kwargs = kwargs
        return f"https://signed.example/{self.name}?m={kwargs.get('method')}"


class FakeGCS:
    def __init__(self):
        self.store = {}        # blob name -> FakeBlob (the bucket contents)
        self.list_calls = []   # prefixes passed to list_blobs
        self.blob_calls = []   # names passed to bucket.blob (upload path)
        self.preexisting = set()  # upload names whose object already exists

    def add(self, name, size=10):
        self.store[name] = FakeBlob(name, size=size, exists=True)

    def list_blobs(self, bucket, prefix=None, timeout=None):
        self.list_calls.append(prefix)
        return [b for n, b in sorted(self.store.items()) if n.startswith(prefix)]

    def blob(self, name):
        self.blob_calls.append(name)
        return FakeBlob(name, exists=(name in self.preexisting))


@pytest.fixture
def gcs():
    fake = FakeGCS()
    with mock.patch.object(main._storage_client, "list_blobs", side_effect=fake.list_blobs), \
         mock.patch.object(main._bucket, "blob", side_effect=fake.blob):
        yield fake


# --------------------------------------------------------------------------
# Request / dispatch helpers
# --------------------------------------------------------------------------


def _req(body, method="POST"):
    return mock.Mock(
        method=method,
        headers={"Authorization": "Bearer tok"},
        get_json=mock.Mock(return_value=body),
    )


@contextlib.contextmanager
def _auth(uid=None, exc=None, claims=None):
    """Patch BOTH the uid-only ``verify_bearer`` (list/sign-download/demo) and
    the additive ``verify_bearer_full`` (upload gate) so a test's auth stub
    applies on every handler path. ``claims`` seeds the decoded token's custom
    claims that the upload gate reads (default: none / unsubscribed)."""
    if exc is not None:
        with mock.patch.object(main, "verify_bearer", side_effect=exc), \
             mock.patch.object(main, "verify_bearer_full", side_effect=exc):
            yield
    else:
        with mock.patch.object(main, "verify_bearer", return_value=uid), \
             mock.patch.object(
                 main, "verify_bearer_full", return_value=(uid, claims or {})
             ):
            yield


def _invoke(req):
    resp, status, _headers = main.get_upload_urls(req)
    payload = resp.get_json() if hasattr(resp, "get_json") else resp
    return status, payload


# Bodies covering every token-gated action (upload via default + explicit).
GATED_BODIES = [
    {"recording": "r", "files": [{"name": "v.mp4"}]},
    {"action": "upload", "recording": "r", "files": [{"name": "v.mp4"}]},
    {"action": "list"},
    {"action": "sign-download", "recording": "r"},
]


# --------------------------------------------------------------------------
# AE2 — cross-user download denial (the single most important assertion)
# --------------------------------------------------------------------------


def test_ae2_cross_user_download_denied(gcs):
    # User B owns 'secret'. User A asks to sign-download 'secret' by name.
    gcs.add("users/userB/recordings/secret/video.mp4")
    with _auth(uid="userA"):
        status, payload = _invoke(_req({"action": "sign-download", "recording": "secret"}))
    assert status == 404
    assert "urls" not in payload
    # Only userA's own (empty) prefix was ever queried — userB's is unreachable.
    assert gcs.list_calls == ["users/userA/recordings/secret/"]


# --------------------------------------------------------------------------
# AE1 — list is scoped + re-based (returns name, not uid)
# --------------------------------------------------------------------------


def test_ae1_list_scoped_and_rebased(gcs):
    gcs.add("users/userA/recordings/alpha/video.mp4")
    gcs.add("users/userA/recordings/alpha/meta.json")
    gcs.add("users/userA/recordings/beta/video.mp4")
    gcs.add("users/userB/recordings/gamma/video.mp4")  # must NOT appear
    with _auth(uid="userA"):
        status, payload = _invoke(_req({"action": "list"}))
    assert status == 200
    names = {r["name"] for r in payload["recordings"]}
    assert names == {"alpha", "beta"}  # not 'gamma', and crucially not 'userA'
    assert gcs.list_calls == ["users/userA/recordings/"]
    alpha = next(r for r in payload["recordings"] if r["name"] == "alpha")
    assert alpha["file_count"] == 2


# --------------------------------------------------------------------------
# AE5 / R8 — same name across users never collides
# --------------------------------------------------------------------------


def test_ae5_same_name_no_collision(gcs):
    body = {"recording": "myrec", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA"):
        _, pa = _invoke(_req(body))
    with _auth(uid="userB"):
        _, pb = _invoke(_req(body))
    assert pa["gcs_prefix"] == f"gs://{main.BUCKET}/users/userA/recordings/myrec/"
    assert pb["gcs_prefix"] == f"gs://{main.BUCKET}/users/userB/recordings/myrec/"
    assert "users/userA/recordings/myrec/video.mp4" in gcs.blob_calls
    assert "users/userB/recordings/myrec/video.mp4" in gcs.blob_calls


# --------------------------------------------------------------------------
# Upload — signed PUT URLs under the caller's prefix
# --------------------------------------------------------------------------


def test_upload_signs_put_under_user_prefix(gcs):
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}, {"name": "screenshots/0.png"}]}
    with _auth(uid="userA"):
        status, payload = _invoke(_req(body))
    assert status == 200
    assert set(payload["urls"]) == {"video.mp4", "screenshots/0.png"}
    assert all(u and "m=PUT" in u for u in payload["urls"].values())
    assert payload["gcs_prefix"] == f"gs://{main.BUCKET}/users/userA/recordings/rec1/"


def test_upload_skips_preexisting_object(gcs):
    gcs.preexisting.add("users/userA/recordings/rec1/video.mp4")
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA"):
        status, payload = _invoke(_req(body))
    assert status == 200
    assert payload["urls"]["video.mp4"] is None  # already present -> not re-signed


# --------------------------------------------------------------------------
# U2 — entitlement hard gate (upload-only, fail-closed, enforce-gated)
# --------------------------------------------------------------------------


def test_upload_refused_when_enforced_and_not_subscribed(gcs, monkeypatch):
    # Covers AE3. Enforce on + no subscribed claim -> 402, zero PUT URLs signed.
    monkeypatch.setenv("STRIPE_PAYWALL_ENFORCE", "1")
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA", claims={"uid": "userA"}):
        status, payload = _invoke(_req(body))
    assert status == 402
    assert payload.get("code") == "subscription_required"
    assert gcs.blob_calls == [], "refused upload must not touch bucket.blob"


def test_upload_signs_when_enforced_and_subscribed(gcs, monkeypatch):
    monkeypatch.setenv("STRIPE_PAYWALL_ENFORCE", "1")
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA", claims={"uid": "userA", "subscribed": True}):
        status, payload = _invoke(_req(body))
    assert status == 200
    assert payload["urls"]["video.mp4"] and "m=PUT" in payload["urls"]["video.mp4"]


def test_upload_signs_when_enforce_off_regardless_of_claim(gcs, monkeypatch):
    # Dark-deploy: enforce off -> byte-identical to today even with no claim.
    monkeypatch.delenv("STRIPE_PAYWALL_ENFORCE", raising=False)
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA", claims={"uid": "userA"}):
        status, _ = _invoke(_req(body))
    assert status == 200


@pytest.mark.parametrize(
    "claims",
    [
        {"uid": "userA"},                        # missing subscribed
        {"uid": "userA", "subscribed": False},   # explicit false
        {"uid": "userA", "subscribed": "true"},  # wrong type -> not positively True
    ],
)
def test_upload_fail_closed_on_non_true_claim(gcs, monkeypatch, claims):
    monkeypatch.setenv("STRIPE_PAYWALL_ENFORCE", "1")
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA", claims=claims):
        status, _ = _invoke(_req(body))
    assert status == 402, claims
    assert gcs.blob_calls == []


def test_subscription_refusal_fails_closed_on_unreadable_claims(monkeypatch):
    # Unreadable (None) claims under enforce -> refuse; enforce off -> always allow.
    monkeypatch.setenv("STRIPE_PAYWALL_ENFORCE", "1")
    assert main._subscription_refusal(None) is not None
    assert main._subscription_refusal({}) is not None
    assert main._subscription_refusal({"subscribed": True}) is None
    monkeypatch.delenv("STRIPE_PAYWALL_ENFORCE", raising=False)
    assert main._subscription_refusal(None) is None


def test_download_and_list_never_gated_when_enforced(gcs, monkeypatch):
    # Covers AE5. A lapsed (no claim) account still lists and sign-downloads.
    monkeypatch.setenv("STRIPE_PAYWALL_ENFORCE", "1")
    gcs.add("users/userA/recordings/rec1/video.mp4")
    with _auth(uid="userA", claims={"uid": "userA"}):
        list_status, _ = _invoke(_req({"action": "list"}))
        dl_status, _ = _invoke(_req({"action": "sign-download", "recording": "rec1"}))
    assert list_status == 200
    assert dl_status == 200


# --------------------------------------------------------------------------
# U5 — signer two-tier contract (paid-only-launch, billing plan KTD-1)
#
# The signer's cloud hard gate (`_subscription_refusal`) is intentionally
# UNCHANGED under the two-tier split: it keeps signing only when `subscribed`
# reads exactly True, and the webhook keeps writing `subscribed = (tier ==
# "cloud")` (billing plan KTD-1). These tests are the durable regression guard
# that the cloud gate admits ONLY a cloud token under the new two-tier claim
# shape — a Local-Pro token (`tier=local` / `subscribed=false`) must never
# obtain a cloud upload URL (the cross-tier escalation the split exists to
# prevent) — while the signer prod diff stays empty. If a future edit ever lets
# `subscribed` mean "any paid tier", or lets a `tier=local` claim through, these
# fail loudly alongside `test_signing_contract.py`.
# --------------------------------------------------------------------------


def test_upload_signs_for_cloud_tier_token(gcs, monkeypatch):
    # Enforce on + a cloud-tier token (tier=cloud => subscribed=true, exactly as
    # the webhook writes it) -> signs as today. Cloud is the only tier the hard
    # gate admits.
    monkeypatch.setenv("STRIPE_PAYWALL_ENFORCE", "1")
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA", claims={"uid": "userA", "tier": "cloud", "subscribed": True}):
        status, payload = _invoke(_req(body))
    assert status == 200
    assert payload["urls"]["video.mp4"] and "m=PUT" in payload["urls"]["video.mp4"]


def test_upload_refused_for_local_tier_token(gcs, monkeypatch):
    # Cross-tier escalation guard: a Local-Pro token (tier=local, subscribed=false
    # per the fail-closed invariant `subscribed = (tier == "cloud")`) must NEVER
    # obtain a cloud upload URL. Enforce on -> 402, zero URLs signed, bucket
    # untouched.
    monkeypatch.setenv("STRIPE_PAYWALL_ENFORCE", "1")
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA", claims={"uid": "userA", "tier": "local", "subscribed": False}):
        status, payload = _invoke(_req(body))
    assert status == 402
    assert payload.get("code") == "subscription_required"
    assert "urls" not in payload
    assert gcs.blob_calls == [], "a tier=local token must not reach any signing path"


def test_upload_refused_for_local_tier_even_if_subscribed_absent(gcs, monkeypatch):
    # Belt-and-suspenders: even a malformed local claim that omits `subscribed`
    # (only ever written by a broken producer) is refused — the gate reads
    # `subscribed`, which is not positively True, so tier=local can never escalate.
    monkeypatch.setenv("STRIPE_PAYWALL_ENFORCE", "1")
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA", claims={"uid": "userA", "tier": "local"}):
        status, payload = _invoke(_req(body))
    assert status == 402
    assert gcs.blob_calls == []


def test_upload_refused_for_no_tier_token(gcs, monkeypatch):
    # A none/lapsed token (no tier, no subscribed) still hits the existing
    # fail-closed refusal — the two-tier shape does not weaken the base gate.
    monkeypatch.setenv("STRIPE_PAYWALL_ENFORCE", "1")
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA", claims={"uid": "userA"}):
        status, _ = _invoke(_req(body))
    assert status == 402
    assert gcs.blob_calls == []


def test_subscription_refusal_admits_only_cloud_under_two_tier_shape(monkeypatch):
    # Direct unit assertion on the gate itself: under enforce, ONLY subscribed=True
    # (which the webhook writes iff tier==cloud) is admitted; every non-cloud tier
    # shape is refused. `subscribed` remains the single field the gate reads — the
    # additive `tier` claim never widens it (billing plan KTD-1).
    monkeypatch.setenv("STRIPE_PAYWALL_ENFORCE", "1")
    # Cloud tier as the webhook writes it -> admitted.
    assert main._subscription_refusal({"tier": "cloud", "subscribed": True}) is None
    # Local tier (and any future non-cloud tier) -> refused.
    assert main._subscription_refusal({"tier": "local", "subscribed": False}) is not None
    assert main._subscription_refusal({"tier": "local"}) is not None
    assert main._subscription_refusal({"tier": "free_capped", "subscribed": False}) is not None
    # A tier=cloud claim WITHOUT subscribed=True must not slip through — the gate
    # never trusts `tier` in place of the derived `subscribed` field.
    assert main._subscription_refusal({"tier": "cloud"}) is not None


def test_tokenless_boundary_contract_holds_under_two_tier(gcs):
    """The tokenless-boundary contract is unchanged by the two-tier split: an
    unauthenticated request to any gated action (upload included) still 401s and
    reaches no GCS call — no unsigned/tokenless request ever reaches a `users/`
    signing path, regardless of the claim shape. This re-asserts the durable
    signing-contract invariant alongside the new tier cases above."""
    for body in GATED_BODIES:
        with _auth(exc=AuthInvalid("no token")):
            status, _ = _invoke(_req(body))
        assert status == 401, body
    assert gcs.list_calls == [], "tokenless request reached list_blobs"
    assert gcs.blob_calls == [], "tokenless request reached bucket.blob"


# --------------------------------------------------------------------------
# R6 / R2 — auth gate on every handler
# --------------------------------------------------------------------------


@pytest.mark.parametrize("body", GATED_BODIES)
def test_invalid_token_401(gcs, body):
    with _auth(exc=AuthInvalid("no token")):
        status, _ = _invoke(_req(body))
    assert status == 401


@pytest.mark.parametrize("body", GATED_BODIES)
def test_auth_unavailable_503(gcs, body):
    with _auth(exc=AuthUnavailable("firebase outage")):
        status, _ = _invoke(_req(body))
    assert status == 503


# --------------------------------------------------------------------------
# CI contract test (single-fn boundary mitigation) — no tokenless users/ access
# --------------------------------------------------------------------------


def test_contract_no_tokenless_request_reaches_users_code(gcs):
    """The chosen single-deployment mitigation: assert that across EVERY
    token-gated action, an unauthenticated request returns 401 and never makes
    a single GCS call. Auth must be the first thing each handler does."""
    for body in GATED_BODIES:
        with _auth(exc=AuthInvalid("no token")):
            status, _ = _invoke(_req(body))
        assert status == 401, body
    assert gcs.list_calls == [], "tokenless request reached list_blobs"
    assert gcs.blob_calls == [], "tokenless request reached bucket.blob"


# --------------------------------------------------------------------------
# Path-injection / source allow-list — on EVERY handler
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"recording": "a/../../x", "files": [{"name": "v.mp4"}]},
        {"action": "sign-download", "recording": "a/../../x"},
    ],
)
def test_climbing_recording_name_rejected(gcs, body):
    with _auth(uid="userA"):
        status, _ = _invoke(_req(body))
    assert status == 400


@pytest.mark.parametrize(
    "body",
    [
        {"action": "list", "source": "evil"},
        {"action": "sign-download", "recording": "r", "source": "evil"},
        {"recording": "r", "files": [{"name": "v.mp4"}], "source": "evil"},
    ],
)
def test_bad_source_rejected_everywhere(gcs, body):
    with _auth(uid="userA"):
        status, _ = _invoke(_req(body))
    assert status == 400


# --------------------------------------------------------------------------
# Dispatcher action allow-list — get-index gone, unknown -> 400
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["get-index", "bogus", "DELETE", "demo"])
def test_unknown_action_400(gcs, action):
    with _auth(uid="userA"):
        status, _ = _invoke(_req({"action": action}))
    assert status == 400


def test_get_index_handler_removed():
    assert not hasattr(main, "_handle_get_index")


# --------------------------------------------------------------------------
# Cryptographic boundary — exact object paths only
# --------------------------------------------------------------------------


def test_signed_paths_are_exact_user_objects(gcs):
    gcs.add("users/userA/recordings/r/video.mp4")
    gcs.add("users/userA/recordings/r/meta.json")
    with _auth(uid="userA"):
        status, payload = _invoke(_req({"action": "sign-download", "recording": "r"}))
    assert status == 200
    for url in payload["urls"].values():
        assert "users/userA/recordings/r/" in url
        assert "users/userB" not in url


def test_user_download_uses_short_expiry(gcs):
    gcs.add("users/userA/recordings/r/video.mp4")
    with _auth(uid="userA"):
        _invoke(_req({"action": "sign-download", "recording": "r"}))
    exp = gcs.store["users/userA/recordings/r/video.mp4"].signed_kwargs["expiration"]
    assert exp == timedelta(minutes=main.USER_DOWNLOAD_EXPIRY_MINUTES)
    assert exp < timedelta(hours=1)  # materially shorter than the old 4h window


# ==========================================================================
# U3 — public demo namespace (unauthenticated, demo/ only)
# ==========================================================================


def _req_noauth(body):
    """A demo request: no Authorization header at all."""
    return mock.Mock(method="POST", headers={}, get_json=mock.Mock(return_value=body))


def test_demo_list_no_token(gcs):
    # AE4/R10: the demo gallery lists without any token.
    gcs.add("demo/cooldemo/video.mp4")
    gcs.add("demo/cooldemo/meta.json")
    gcs.add("demo/another/video.mp4")
    with mock.patch.object(main, "verify_bearer") as vb:
        status, payload = _invoke(_req_noauth({"action": "demo-list"}))
    assert status == 200
    assert {r["name"] for r in payload["recordings"]} == {"cooldemo", "another"}
    # The demo path NEVER calls the auth gate.
    vb.assert_not_called()
    assert gcs.list_calls == ["demo/"]


def test_demo_sign_download_happy(gcs):
    gcs.add("demo/cooldemo/video.mp4")
    gcs.add("demo/cooldemo/screenshots/0.png")
    with mock.patch.object(main, "verify_bearer") as vb:
        status, payload = _invoke(_req_noauth({"action": "demo-sign-download", "recording": "cooldemo"}))
    assert status == 200
    assert set(payload["urls"]) == {"video.mp4", "screenshots/0.png"}
    assert all("demo/cooldemo/" in u for u in payload["urls"].values())
    vb.assert_not_called()


def test_demo_sign_download_not_found(gcs):
    with mock.patch.object(main, "verify_bearer"):
        status, _ = _invoke(_req_noauth({"action": "demo-sign-download", "recording": "ghost"}))
    assert status == 404


def test_demo_actions_only_ever_read_demo(gcs):
    # R9: a crafted name cannot make a demo action reach users/ or the flat
    # namespace. Either it 400s (bad name) or it lists strictly under demo/.
    gcs.add("users/userA/recordings/secret/video.mp4")
    for bad in ["../users/userA/recordings/secret", "a/b", ".."]:
        with mock.patch.object(main, "verify_bearer"):
            status, _ = _invoke(_req_noauth({"action": "demo-sign-download", "recording": bad}))
        assert status == 400
    # Every list prefix the demo path ever used stayed under demo/.
    assert all(p.startswith("demo/") for p in gcs.list_calls)
    assert not any("users/" in p for p in gcs.list_calls)


def test_demo_ignores_source_field(gcs):
    # A client-supplied source on a demo action is ignored — demo is flat.
    gcs.add("demo/cooldemo/video.mp4")
    gcs.add("users/userA/sessions/cooldemo/video.mp4")
    with mock.patch.object(main, "verify_bearer"):
        status, payload = _invoke(
            _req_noauth({"action": "demo-list", "source": "sessions"})
        )
    assert status == 200
    assert {r["name"] for r in payload["recordings"]} == {"cooldemo"}
    assert gcs.list_calls == ["demo/"]


def test_demo_list_is_marker_blind(gcs):
    # A migrated demo recording may carry a vestigial _unlisted marker; demo-list
    # must NOT honor it (the marker is superseded by namespace isolation).
    gcs.add("demo/curated/video.mp4")
    gcs.add("demo/curated/_unlisted")
    with mock.patch.object(main, "verify_bearer"):
        status, payload = _invoke(_req_noauth({"action": "demo-list"}))
    assert status == 200
    assert "curated" in {r["name"] for r in payload["recordings"]}


def test_demo_does_not_loosen_authenticated_gate(gcs):
    # Adding the demo path must not have loosened the token-gated path: a gated
    # action without a token still 401s.
    with _auth(exc=AuthInvalid("no token")):
        status, _ = _invoke(_req({"action": "list"}))
    assert status == 401


def test_demo_uses_long_expiry(gcs):
    gcs.add("demo/cooldemo/video.mp4")
    with mock.patch.object(main, "verify_bearer"):
        _invoke(_req_noauth({"action": "demo-sign-download", "recording": "cooldemo"}))
    exp = gcs.store["demo/cooldemo/video.mp4"].signed_kwargs["expiration"]
    assert exp == timedelta(hours=main.DEMO_DOWNLOAD_EXPIRY_HOURS)


# ==========================================================================
# Review fixes (PR #210)
# ==========================================================================


def test_user_list_hides_unlisted_recording(gcs):
    # #4: the OWNER's listing honors the vestigial _unlisted marker (opposite of
    # the marker-blind demo gallery). This anchors the deliberate divergence on
    # the user side too, so a future "unify the handlers" refactor can't silently
    # change it.
    gcs.add("users/userA/recordings/shown/video.mp4")
    gcs.add("users/userA/recordings/hidden/video.mp4")
    gcs.add("users/userA/recordings/hidden/_unlisted")
    with _auth(uid="userA"):
        status, payload = _invoke(_req({"action": "list"}))
    assert status == 200
    assert {r["name"] for r in payload["recordings"]} == {"shown"}


def test_demo_list_skips_invalid_named_recording(gcs):
    # #19: a curated-but-badly-named demo blob must not be listable-but-unplayable
    # (it would list fine, then 400 on sign-download). demo-list skips it.
    gcs.add("demo/good/video.mp4")
    gcs.add("demo/a..b/video.mp4")  # name fails the sign-download guard
    with mock.patch.object(main, "verify_bearer"):
        status, payload = _invoke(_req_noauth({"action": "demo-list"}))
    assert status == 200
    assert {r["name"] for r in payload["recordings"]} == {"good"}


def test_upload_signing_credential_failure_returns_503(gcs):
    # #5: a transient signing-credential failure mid-upload -> 503 with a JSON
    # body (not a bare 500 with a partial URL batch).
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA"), mock.patch.object(
        main._credentials, "refresh",
        side_effect=google.auth.exceptions.GoogleAuthError("signBlob token refresh failed"),
    ):
        status, payload = _invoke(_req(body))
    assert status == 503
    assert "error" in payload


def test_sign_download_signing_credential_failure_returns_503(gcs):
    gcs.add("users/userA/recordings/r/video.mp4")
    with _auth(uid="userA"), mock.patch.object(
        main._credentials, "refresh",
        side_effect=google.auth.exceptions.GoogleAuthError("outage"),
    ):
        status, payload = _invoke(_req({"action": "sign-download", "recording": "r"}))
    assert status == 503
    assert "error" in payload


def test_upload_refreshes_signing_credential_once_not_per_file(gcs):
    # #5: a multi-file upload refreshes the signing credential exactly once.
    body = {"recording": "rec1", "files": [{"name": f"f{i}.mp4"} for i in range(5)]}
    with _auth(uid="userA"), mock.patch.object(main._credentials, "refresh") as refresh:
        status, _ = _invoke(_req(body))
    assert status == 200
    assert refresh.call_count == 1


# --------------------------------------------------------------------------
# GCS-level failure -> structured 503 (not a bare 500 HTML traceback)
# --------------------------------------------------------------------------


# Every action that reaches GCS, and the auth stance each one needs. A new
# storage-touching action belongs here too — the dispatcher-level guard covers
# it automatically, and this list is what proves it.
GCS_TOUCHING = [
    ({"action": "demo-list"}, False),
    ({"action": "demo-sign-download", "recording": "r"}, False),
    ({"action": "resolve-share", "token": "a" * 32}, False),
    ({"action": "list"}, True),
    ({"action": "sign-download", "recording": "r"}, True),
    ({"recording": "r", "files": [{"name": "v.mp4"}]}, True),
]


@pytest.mark.parametrize("body,authed", GCS_TOUCHING)
def test_gcs_failure_returns_structured_503_not_bare_500(gcs, body, authed):
    """A GCS-level failure answers structured JSON on EVERY storage path.

    GCS returns 403 for a bucket that does not exist (it will not confirm
    existence), so a mis-set SCREENCAP_BUCKET and a revoked grant are the same
    exception. Neither is a signBlob credential failure, so the per-handler
    ``except GoogleAuthError`` never caught them: they escaped as a bare 500
    whose body named neither the action nor the bucket.
    """
    boom = google.api_core.exceptions.Forbidden(
        "does not have storage.objects.list access "
        "(or it may not exist)"
    )
    ctx = _auth(uid="userA") if authed else contextlib.nullcontext()
    req = _req(body) if authed else _req_noauth(body)
    with ctx, \
         mock.patch.object(main._storage_client, "list_blobs", side_effect=boom), \
         mock.patch.object(main._bucket, "blob", side_effect=boom):
        status, payload = _invoke(req)

    assert status == 503
    assert "error" in payload


def test_gcs_failure_logs_the_bucket_it_could_not_reach(gcs, caplog):
    # The 503 body deliberately says nothing about infrastructure, so the log is
    # the ONLY place the operator can learn which bucket was unreachable. A
    # misconfigured SCREENCAP_BUCKET must be readable at a glance here rather
    # than reconstructed from a stack trace.
    boom = google.api_core.exceptions.Forbidden("denied")
    with mock.patch.object(main._storage_client, "list_blobs", side_effect=boom), \
         caplog.at_level(logging.ERROR):
        status, payload = _invoke(_req_noauth({"action": "demo-list"}))

    assert status == 503
    assert main.BUCKET in caplog.text
    assert "demo-list" in caplog.text
    # The failure must not leak infrastructure detail to an anonymous caller.
    assert main.BUCKET not in payload["error"]


def test_gcs_guard_does_not_mask_auth_denials(gcs):
    # The dispatcher-level guard wraps the handlers, so it must not swallow the
    # 401/403-shaped outcomes those handlers return as values.
    with _auth(exc=AuthInvalid("bad token")):
        status, _ = _invoke(_req({"action": "list"}))
    assert status == 401


def test_resolve_project_id_ignores_ambient_google_cloud_project(monkeypatch):
    # #9: SCREENCAP_PROJECT_ID is the ONLY override — no ambient
    # GOOGLE_CLOUD_PROJECT fallback that could pin the wrong project.
    monkeypatch.delenv("SCREENCAP_PROJECT_ID", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "some-other-hosting-project")
    assert main._resolve_project_id() == "proteus-photos"
    monkeypatch.setenv("SCREENCAP_PROJECT_ID", "explicit-project")
    assert main._resolve_project_id() == "explicit-project"
