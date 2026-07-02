"""Client-side cloud entitlement operations (U7 founding grant).

This is the CLIENT half of the entitlement layer — distinct from the Cloud
Function's own ``scripts/cloud-function/entitlements.py`` (the authoritative
server store). Here we only *drive* the grant: a token-gated POST to the signing
Cloud Function's ``grant-founding`` action (the same endpoint
``request_signed_urls`` uses, dispatched by ``action``), then a forced ID-token
refresh so the new ``plan: founding`` custom claim is present before the app's
next entitlement read or upload.

Freshness (KTD2): a Firebase custom claim only appears in a freshly minted ID
token, so after the grant we re-read via :func:`screencap.auth.get_entitlements`,
which force-refreshes the token — a just-granted account then reads back as
``founding`` immediately.

Authorization: the v1 grant is SELF-SERVICE and self-authorized (founding is free
and confers no billable capability). When billing activates, the server-side grant
MUST move behind the payment-processor webhook / server-side eligibility check and
must not remain client-callable (plan Definition of Done).
"""

from __future__ import annotations

import requests

from screencap.upload import _describe_response_error, _get_upload_url


def grant_founding() -> dict:
    """Grant the signed-in account the founding cloud entitlement, then refresh.

    Returns the resulting entitlement dict ``{plan, active, expires}`` (via a
    forced-refresh read, so ``plan`` reflects the just-set claim). Raises
    ``RuntimeError`` with a user-facing message on sign-in / network / service
    failure (mirroring ``request_signed_urls``' error mapping), so a CLI/app
    caller can surface a clean message rather than a raw traceback.
    """
    from screencap import auth

    url = _get_upload_url()
    try:
        resp = auth.authed_post(
            requests.post, url, json={"action": "grant-founding"}, timeout=30
        )
    except auth.NotSignedIn:
        raise RuntimeError("Sign in first: run `screencap login`.")
    except auth.AuthError as e:
        raise RuntimeError(f"Cloud auth temporarily unavailable; try again: {e}")
    except requests.ConnectionError:
        raise RuntimeError("Cloud service unavailable. Check your internet connection.")
    except requests.Timeout:
        raise RuntimeError("Cloud service timed out. Try again later.")

    if resp.status_code != 200:
        raise RuntimeError(f"Could not set up cloud: {_describe_response_error(resp)}")

    # The grant set the claim server-side; get_entitlements force-refreshes the
    # ID token (KTD2) so the returned entitlement reflects the new founding claim
    # — and the freshly minted token is cached for the immediate first upload.
    return auth.get_entitlements()
