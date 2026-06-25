"""Real-library proof that the forced ``google-cloud-storage`` 3.x bump leaves the
signed-URL *upload* contract checksum-free (SCR-140 / U2).

The cloud function only **signs** v4 PUT URLs (``main._handle_upload`` →
``blob.generate_signed_url``); it never uploads bytes. The gcs 3.0 ``checksum="auto"``
(crc32c) default that motivated this re-test applies to the *transfer* methods
(``Blob.upload_from_*`` / ``download_to_*``) — neither of which this path uses — so it
cannot reach signing. This test pins that invariant against the **real** installed 3.x
library (not the conftest ``storage.Client`` mock): it signs a v4 PUT URL with a
deterministic, ephemeral, locally-generated key and asserts the URL requires only
``Content-Type`` — no ``x-goog-hash`` / checksum. If a future bump ever makes signing
checksum-aware, this fails loudly.

**Why a local-key signer (and not the function's literal call):** production signs via
the IAM ``signBlob`` path (``service_account_email=`` + ``access_token=``) because Cloud
Run compute credentials cannot sign locally; that path needs the network. The v4
``SignedHeaders`` / query-param construction this test asserts on is shared between the
local-key and IAM ``signBlob`` branches, so this is a **library-contract guard**, not a
function-signing-path regression guard.

**Why an explicit ``api_access_endpoint`` and a stub client:** gcs 3.x v4 signing reads
``client.api_endpoint`` (when ``api_access_endpoint`` is omitted) and ``client.universe_domain``
off the blob's bucket client. Under this suite's session-wide ``storage.Client`` mock
(``conftest.py``) those attributes are ``MagicMock``s that corrupt or break signing, so we
pass ``api_access_endpoint`` explicitly and back the blob with a minimal real-valued stub
client. ``storage.Blob`` / ``generate_signed_url`` themselves are the real 3.x classes.
"""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.cloud import storage
from google.oauth2 import service_account


def _ephemeral_signing_credentials() -> service_account.Credentials:
    """A throwaway service-account credential that can sign v4 URLs locally.

    The RSA key is generated in-process — no private-key material is ever written to or
    read from the repo (SCR-140 security-review requirement: the signer key MUST be
    ephemeral)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    info = {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "ephemeral-test-key",
        "private_key": pem,
        "client_email": "signer@test-project.iam.gserviceaccount.com",
        "client_id": "0",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return service_account.Credentials.from_service_account_info(info)


class _StubClient:
    """The only client attributes real-3.x v4 signing reads once ``api_access_endpoint``
    and ``credentials`` are passed explicitly are ``universe_domain`` (and ``api_endpoint``
    as a belt-and-suspenders fallback). Backing the blob with this stub keeps signing
    fully offline and independent of the conftest ``storage.Client`` mock."""

    universe_domain = "googleapis.com"
    api_endpoint = "https://storage.googleapis.com"


def _sign_put_url(content_type: str = "video/mp4") -> str:
    creds = _ephemeral_signing_credentials()
    bucket = storage.Bucket(client=_StubClient(), name="test-bucket")
    blob = storage.Blob("recordings/rec1/video.mp4", bucket)
    return blob.generate_signed_url(
        version="v4",
        method="PUT",
        content_type=content_type,
        credentials=creds,
        api_access_endpoint="https://storage.googleapis.com",
        expiration=timedelta(minutes=15),
    )


def _query(url: str) -> dict:
    # parse_qs lower-cases nothing; v4 params are X-Goog-*. Normalize keys to lower for
    # robust membership checks.
    raw = parse_qs(urlparse(url).query)
    return {k.lower(): v for k, v in raw.items()}


def test_v4_put_signing_is_checksum_free():
    """The forced gcs 3.x bump does NOT make a v4 PUT signed URL require a checksum.

    A signed PUT URL for an upload must commit only to ``Content-Type`` (+ host) — no
    ``x-goog-hash``/crc32c/md5 — so the client's raw ``requests.put`` (which sends no
    checksum header) is satisfiable. This is the cloud-function side of R2."""
    url = _sign_put_url(content_type="video/mp4")
    q = _query(url)

    # It is a genuine, real-library v4 signed URL — not the FakeBlob stub from test_main.py.
    assert "x-goog-signature" in q, f"not a real signed URL: {url}"
    assert q.get("x-goog-algorithm") == ["GOOG4-RSA-SHA256"]

    # The signed headers commit to content-type and host, and NOTHING checksum-shaped.
    signed_headers = q["x-goog-signedheaders"][0].lower()
    assert "content-type" in signed_headers
    assert "host" in signed_headers
    assert "x-goog-hash" not in signed_headers, (
        f"3.x leaked a checksum into the signed PUT headers: {signed_headers!r}"
    )

    # No checksum is pinned anywhere in the URL the client must satisfy.
    assert "x-goog-hash" not in q
    assert "content-md5" not in q
    full = url.lower()
    assert "x-goog-hash" not in full and "crc32c" not in full and "md5" not in full, (
        f"checksum token leaked into the signed URL: {url}"
    )


def test_v4_put_signing_pins_content_type_exactly():
    """The signed Content-Type matches what was requested — the header the client PUTs
    must equal what the URL was signed for, or GCS rejects the PUT. Pins that the only
    upload precondition the 3.x signature carries is the caller's content type."""
    url = _sign_put_url(content_type="audio/flac")
    signed_headers = _query(url)["x-goog-signedheaders"][0].lower()
    # content-type is the only payload-shaping header in the signature; host is transport.
    assert set(signed_headers.split(";")) == {"content-type", "host"}
