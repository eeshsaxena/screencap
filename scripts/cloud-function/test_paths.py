"""Unit tests for paths.py — the single GCS object-key boundary.

paths.py is deliberately firebase-free so this whole module imports and runs
without ADC or any GCP credential. These tests are the primary proof of the
demo/users isolation invariant: an unauthenticated request can ONLY resolve
under demo/, an authenticated request ONLY under users/{uid}/, else it raises.
"""

import itertools

import pytest
from paths import (
    DEMO_NAMESPACE,
    PrefixResolutionError,
    is_valid_name,
    owner_segment,
    resolve_prefix,
)


@pytest.mark.parametrize(
    "name,ok",
    [
        ("good_name-1.mp4", True),
        ("a", True),
        ("a..b", False),   # parent climb
        ("a/b", False),    # slash
        (".hidden", False),  # leading dot
        ("", False),
        (None, False),
    ],
)
def test_is_valid_name(name, ok):
    assert is_valid_name(name) is ok

# --------------------------------------------------------------------------
# owner_segment
# --------------------------------------------------------------------------


def test_owner_segment_accepts_normal_uid():
    assert owner_segment("aBc123XYZ") == "aBc123XYZ"
    # A realistic 28-char Firebase uid.
    assert owner_segment("abcDEF012ghiJKL345mnoPQR678s") == "abcDEF012ghiJKL345mnoPQR678s"


@pytest.mark.parametrize(
    "bad",
    [
        "",            # empty
        "a/b",         # slash
        "..",          # parent climb
        "a.b",         # dot
        "a..b",        # embedded ..
        "a b",         # space
        "a-b",         # hyphen (not in the Firebase alphabet)
        "a_b",         # underscore
        "x" * 129,     # over the 128 cap
        None,          # not a string
        123,           # not a string
    ],
)
def test_owner_segment_rejects_unsafe(bad):
    with pytest.raises(PrefixResolutionError):
        owner_segment(bad)


# --------------------------------------------------------------------------
# resolve_prefix — authenticated (users/{uid}/...)
# --------------------------------------------------------------------------


def test_authenticated_list_prefix():
    assert resolve_prefix(True, "userA", "recordings", None) == "users/userA/recordings/"


def test_authenticated_recording_prefix():
    assert (
        resolve_prefix(True, "userA", "recordings", "myrec") == "users/userA/recordings/myrec/"
    )


def test_authenticated_sessions_source():
    assert resolve_prefix(True, "userA", "sessions", None) == "users/userA/sessions/"


def test_authenticated_rejects_bad_source():
    with pytest.raises(PrefixResolutionError):
        resolve_prefix(True, "userA", "demo", None)
    with pytest.raises(PrefixResolutionError):
        resolve_prefix(True, "userA", "evil", "x")


def test_authenticated_rejects_bad_uid():
    with pytest.raises(PrefixResolutionError):
        resolve_prefix(True, "../etc", "recordings", None)
    with pytest.raises(PrefixResolutionError):
        resolve_prefix(True, None, "recordings", None)


@pytest.mark.parametrize("bad_name", ["a/../../x", "..", "a/b", "../users", ".hidden", ""])
def test_authenticated_rejects_climbing_name(bad_name):
    with pytest.raises(PrefixResolutionError):
        resolve_prefix(True, "userA", "recordings", bad_name)


# --------------------------------------------------------------------------
# resolve_prefix — unauthenticated (demo/...)
# --------------------------------------------------------------------------


def test_unauthenticated_list_is_demo():
    assert resolve_prefix(False, None, None, None) == "demo/"


def test_unauthenticated_recording_is_demo():
    assert resolve_prefix(False, None, None, "cooldemo") == "demo/cooldemo/"


def test_unauthenticated_ignores_source_and_uid_but_still_demo():
    # Even if a caller smuggles a source/uid into the unauthenticated branch,
    # the result can never leave demo/. This is the core anti-overloading rule.
    assert resolve_prefix(False, "userA", "recordings", "x") == "demo/x/"


@pytest.mark.parametrize("bad_name", ["../users/userA/recordings", "a/b", ".."])
def test_unauthenticated_rejects_climbing_name(bad_name):
    with pytest.raises(PrefixResolutionError):
        resolve_prefix(False, None, None, bad_name)


# --------------------------------------------------------------------------
# The central invariant — exhaustive sweep
# --------------------------------------------------------------------------


def test_invariant_no_cross_namespace_key_ever():
    """For every combination of inputs, resolve_prefix either raises or returns
    a key strictly inside the namespace dictated by `authenticated`. There is
    NO input that yields an authenticated key from an unauthenticated request,
    or vice versa."""
    uids = [None, "userA", "../etc", ""]
    sources = [None, "recordings", "sessions", "demo", "evil"]
    names = [None, "good", "a/../b", "..", ""]
    for authenticated, uid, source, name in itertools.product(
        [True, False], uids, sources, names
    ):
        try:
            key = resolve_prefix(authenticated, uid, source, name)
        except PrefixResolutionError:
            continue  # raising is always an acceptable outcome
        if authenticated:
            assert key.startswith(f"users/{uid}/"), (authenticated, uid, source, name, key)
            assert "demo/" not in key
        else:
            assert key.startswith(f"{DEMO_NAMESPACE}/"), (authenticated, uid, source, name, key)
            assert "users/" not in key
