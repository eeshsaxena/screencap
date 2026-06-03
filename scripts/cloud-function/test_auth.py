"""Unit tests for auth.py — bearer-token verification.

``firebase_admin.auth.verify_id_token`` is mocked throughout; no network or ADC
is required. The key contract under test is that verification distinguishes an
*invalid* token (-> AuthInvalid -> 401) from a *temporarily-unavailable*
verification (-> AuthUnavailable -> 503), so the client can fail closed on an
outage rather than treating it as a hard deny.
"""

from unittest import mock

import pytest
from auth import AuthInvalid, AuthUnavailable, verify_bearer
from firebase_admin import auth as fb_auth

PROJECT = "proteus-photos"


def _req(authorization=None):
    headers = {}
    if authorization is not None:
        headers["Authorization"] = authorization
    return mock.Mock(headers=headers)


def _good_claims(uid="userA", project=PROJECT):
    return {
        "uid": uid,
        "aud": project,
        "iss": f"https://securetoken.google.com/{project}",
    }


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_valid_token_returns_uid():
    with mock.patch.object(fb_auth, "verify_id_token", return_value=_good_claims("userA")):
        assert verify_bearer(_req("Bearer good.token.here"), PROJECT) == "userA"


def test_valid_token_without_project_pin_skips_aud_check():
    # When no project_id is passed, the SDK's own aud/iss check is trusted and
    # the belt-and-suspenders assertion is skipped.
    with mock.patch.object(fb_auth, "verify_id_token", return_value=_good_claims("userB")):
        assert verify_bearer(_req("Bearer good"), None) == "userB"


def test_verify_passes_clock_skew():
    with mock.patch.object(
        fb_auth, "verify_id_token", return_value=_good_claims()
    ) as m:
        verify_bearer(_req("Bearer good"), PROJECT)
    _, kwargs = m.call_args
    assert kwargs.get("check_revoked") is False
    assert kwargs.get("clock_skew_seconds", 0) > 0


# --------------------------------------------------------------------------
# Auth-invalid -> 401
# --------------------------------------------------------------------------


def test_missing_authorization_header():
    with pytest.raises(AuthInvalid):
        verify_bearer(_req(None), PROJECT)


@pytest.mark.parametrize(
    "header",
    ["Bearer", "Bearer ", "Token abc", "abc", "Basic abc", "Bearer  "],
)
def test_malformed_header(header):
    with pytest.raises(AuthInvalid):
        verify_bearer(_req(header), PROJECT)


def test_expired_token_is_invalid():
    with mock.patch.object(
        fb_auth, "verify_id_token", side_effect=fb_auth.ExpiredIdTokenError("expired", None)
    ):
        with pytest.raises(AuthInvalid):
            verify_bearer(_req("Bearer expired"), PROJECT)


def test_bad_signature_is_invalid():
    with mock.patch.object(
        fb_auth, "verify_id_token", side_effect=fb_auth.InvalidIdTokenError("bad sig")
    ):
        with pytest.raises(AuthInvalid):
            verify_bearer(_req("Bearer bad"), PROJECT)


def test_malformed_token_value_error_is_invalid():
    with mock.patch.object(fb_auth, "verify_id_token", side_effect=ValueError("not a jwt")):
        with pytest.raises(AuthInvalid):
            verify_bearer(_req("Bearer junk"), PROJECT)


def test_foreign_project_token_rejected():
    # Token verifies (mock succeeds) but was minted for a different project.
    with mock.patch.object(
        fb_auth, "verify_id_token", return_value=_good_claims(project="someone-else")
    ):
        with pytest.raises(AuthInvalid):
            verify_bearer(_req("Bearer foreign"), PROJECT)


def test_verified_token_without_uid_rejected():
    with mock.patch.object(
        fb_auth,
        "verify_id_token",
        return_value={"aud": PROJECT, "iss": f"https://securetoken.google.com/{PROJECT}"},
    ):
        with pytest.raises(AuthInvalid):
            verify_bearer(_req("Bearer nouid"), PROJECT)


# --------------------------------------------------------------------------
# Auth-unavailable -> 503 (fail-closed, NOT a 401)
# --------------------------------------------------------------------------


def test_cert_fetch_failure_is_unavailable_not_invalid():
    with mock.patch.object(
        fb_auth,
        "verify_id_token",
        side_effect=fb_auth.CertificateFetchError("cert fetch failed", None),
    ):
        with pytest.raises(AuthUnavailable):
            verify_bearer(_req("Bearer good"), PROJECT)
