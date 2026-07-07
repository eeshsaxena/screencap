"""Endpoint classifier — the privacy boundary (U7, KTD8, SCR-239).

The adversarial set is an acceptance gate: every disguised / encoded / redirecting
form must classify REMOTE so a day-split summary can never leave the Mac via a
misclassified endpoint. Covers AE2.
"""

from __future__ import annotations

import pytest

from screencap.segmentation.endpoint import LOCAL, REMOTE, classify_endpoint


@pytest.mark.privacy
class TestLocal:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:11434",
            "http://127.0.0.1:11434/v1",
            "http://localhost:1234",
            "https://localhost:8443",
            "http://[::1]:1234",
            "http://127.0.0.5:9000",  # anywhere in 127.0.0.0/8
        ],
    )
    def test_loopback_literals_are_local(self, url):
        assert classify_endpoint(url) == LOCAL


@pytest.mark.privacy
class TestRemote:
    @pytest.mark.parametrize(
        "url",
        [
            "http://192.168.1.10:11434",  # private LAN → REMOTE
            "http://10.0.0.5:1234",
            "http://api.openai.com/v1",  # public host
            "http://localhost.evil.com/",  # DNS trick
            "http://127.0.0.1.evil.com/",  # DNS trick
            "http://0.0.0.0:1234",  # unspecified, not loopback
            "http://2130706433/",  # decimal 127.0.0.1
            "http://0x7f000001/",  # hex 127.0.0.1
            "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback → REMOTE (conservative)
            "http://[::ffff:8.8.8.8]/",  # IPv4-mapped public
            "http://127.0.0.1@evil.com/",  # userinfo trick — real host is evil.com
            "file:///etc/passwd",  # non-http(s) scheme
            "unix:///tmp/sock",
            "gopher://127.0.0.1/",
            "not a url",
            "",
            None,
        ],
    )
    def test_adversarial_forms_are_remote(self, url):
        assert classify_endpoint(url) == REMOTE
