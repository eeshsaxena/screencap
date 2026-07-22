"""U3 — share-record store + token logic (SCR-229). Pure, GCS-free (mirrors test_paths.py)."""

from datetime import datetime, timedelta, timezone

import pytest

import shares

_NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


def test_new_token_is_urlsafe_and_unique():
    a, b = shares.new_token(), shares.new_token()
    assert a != b
    assert shares.is_valid_token(a) and shares.is_valid_token(b)
    assert not (set("+/=") & set(a))  # URL/path safe


@pytest.mark.parametrize("bad", ["", "a", "a/b", "../etc", "tok..en", "x" * 200, 123, None])
def test_is_valid_token_rejects_bad(bad):
    assert not shares.is_valid_token(bad)


def test_share_prefix_and_record_key():
    tok = shares.new_token()
    assert shares.share_prefix(tok) == f"shares/{tok}/"
    assert shares.record_key(tok) == f"shares/{tok}/.share.json"
    assert shares.artifact_key(tok, "screenshots/0.jpg") == f"shares/{tok}/screenshots/0.jpg"


def test_prefix_builders_reject_traversal():
    # A token that would climb the namespace never yields a key.
    with pytest.raises(shares.ShareError):
        shares.share_prefix("../users/victim")
    with pytest.raises(shares.ShareError):
        shares.record_key("a/b")
    with pytest.raises(shares.ShareError):
        shares.artifact_key(shares.new_token(), "../secret")


def test_record_round_trip():
    tok = shares.new_token()
    exp = shares.expires_at_iso(_NOW)
    rec = shares.build_record("uid-abc", ["video.mp4", "audio.flac"], exp, view_only=True)
    restored = shares.loads(shares.dumps(rec))
    assert restored == rec
    assert restored["owner_uid"] == "uid-abc" and restored["revoked"] is False


def test_loads_rejects_malformed():
    with pytest.raises(shares.ShareError):
        shares.loads('{"no_owner": true}')
    with pytest.raises(Exception):
        shares.loads("not json")


def test_expiry_predicate():
    future = shares.build_record("u", [], shares.expires_at_iso(_NOW, days=30))
    past = shares.build_record("u", [], (_NOW - timedelta(days=1)).isoformat())
    assert not shares.is_expired(future, _NOW)
    assert shares.is_expired(past, _NOW)
    # Missing expiry -> not expired; unparseable -> expired (fail closed).
    assert not shares.is_expired({"expires_at": None}, _NOW)
    assert shares.is_expired({"expires_at": "garbage"}, _NOW)


def test_revoked_and_owner_predicates():
    rec = shares.build_record("owner-1", [], shares.expires_at_iso(_NOW))
    assert not shares.is_revoked(rec)
    rec["revoked"] = True
    assert shares.is_revoked(rec)
    assert shares.is_owner(rec, "owner-1")
    assert not shares.is_owner(rec, "someone-else")
    assert not shares.is_owner(rec, None)
