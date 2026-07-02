"""Handler tests for the signing Cloud Function — per-user isolation (U2).

These are the integration-level security assertions the plan calls out: the most
important is AE2 (a user cannot sign-download another user's recording by name).
``verify_bearer`` is patched per-test to a uid (or to raise), and GCS is a small
in-memory fake whose ``list_blobs`` filters by prefix — so a handler that lists
the wrong prefix is provably caught (a prefix-only mock would miss it).
"""

from datetime import timedelta
from unittest import mock

import flask
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


@pytest.fixture(autouse=True)
def entitled_by_default():
    """Default every upload caller to an active founding entitlement (U2).

    The entitlement gate in ``_handle_upload`` now reads the caller's plan LIVE
    via ``read_entitlement`` -> ``get_user``. Existing upload tests assert
    signing behaviour, not authorization, so default them to entitled; the
    dedicated U2 gate tests below override this to free / lookup-error. Harmless
    for the non-upload actions (list/sign-download/demo), which never read it.
    """
    with mock.patch.object(
        main, "read_entitlement",
        return_value={"plan": "founding", "active": True, "expires": None},
    ) as read:
        yield read


# --------------------------------------------------------------------------
# Request / dispatch helpers
# --------------------------------------------------------------------------


def _req(body, method="POST"):
    return mock.Mock(
        method=method,
        headers={"Authorization": "Bearer tok"},
        get_json=mock.Mock(return_value=body),
    )


def _auth(uid=None, exc=None):
    if exc is not None:
        return mock.patch.object(main, "verify_bearer", side_effect=exc)
    return mock.patch.object(main, "verify_bearer", return_value=uid)


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


def test_resolve_project_id_ignores_ambient_google_cloud_project(monkeypatch):
    # #9: SCREENCAP_PROJECT_ID is the ONLY override — no ambient
    # GOOGLE_CLOUD_PROJECT fallback that could pin the wrong project.
    monkeypatch.delenv("SCREENCAP_PROJECT_ID", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "some-other-hosting-project")
    assert main._resolve_project_id() == "proteus-photos"
    monkeypatch.setenv("SCREENCAP_PROJECT_ID", "explicit-project")
    assert main._resolve_project_id() == "explicit-project"


# ==========================================================================
# U2 — entitlement is the authoritative server-side upload gate
# ==========================================================================
#
# The gate reads the caller's plan LIVE (read_entitlement -> get_user) AFTER the
# uid is resolved and BEFORE any URL is signed. Free/no-claim -> 403 (permanent
# block, no URLs); lookup failure -> 503 (transient, fail-closed); entitled ->
# signs. `entitled_by_default` (autouse) is overridden per-test here.


def _free():
    return mock.patch.object(
        main, "read_entitlement",
        return_value={"plan": "free", "active": False, "expires": None},
    )


def test_u2_free_uid_rejected_403_no_urls(gcs):
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA"), _free():
        status, payload = _invoke(_req(body))
    assert status == 403
    assert "urls" not in payload
    # The gate runs BEFORE any signing work: no blob was ever touched.
    assert gcs.blob_calls == []
    assert gcs.list_calls == []


def test_u2_default_action_free_uid_also_rejected_403(gcs):
    # The gate applies to the default (actionless) upload dispatch too.
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA"), _free():
        status, _ = _invoke(_req(body))
    assert status == 403


def test_u2_entitled_uid_signs_put_urls(gcs):
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA"):  # entitled_by_default active
        status, payload = _invoke(_req(body))
    assert status == 200
    assert payload["urls"]["video.mp4"] and "m=PUT" in payload["urls"]["video.mp4"]


def test_u2_entitlement_lookup_error_returns_503(gcs):
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA"), mock.patch.object(
        main, "read_entitlement",
        side_effect=main.EntitlementUnavailable("firebase outage"),
    ):
        status, payload = _invoke(_req(body))
    assert status == 503
    assert "error" in payload
    assert gcs.blob_calls == []  # fail-closed: nothing signed


def test_u2_gate_does_not_bypass_cross_user_scoping(gcs):
    # An entitled userA still only ever gets URLs under their OWN prefix — the
    # entitlement gate authorizes uploading, it does not widen scope.
    body = {"recording": "shared", "files": [{"name": "video.mp4"}]}
    with _auth(uid="userA"):
        status, payload = _invoke(_req(body))
    assert status == 200
    assert payload["gcs_prefix"] == f"gs://{main.BUCKET}/users/userA/recordings/shared/"
    assert all("users/userA/recordings/shared/" in n for n in gcs.blob_calls)


def test_u2_free_uid_auth_still_first(gcs):
    # A tokenless upload is still 401 (auth precedes the entitlement gate); the
    # entitlement lookup is never reached without a verified uid.
    body = {"recording": "rec1", "files": [{"name": "video.mp4"}]}
    with _auth(exc=AuthInvalid("no token")), mock.patch.object(main, "read_entitlement") as read:
        status, _ = _invoke(_req(body))
    assert status == 401
    read.assert_not_called()
