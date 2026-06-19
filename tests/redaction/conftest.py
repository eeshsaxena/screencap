"""Shared fixtures for privacy tests."""

from __future__ import annotations

import pytest

from screencap.redaction import Anonymizer


@pytest.fixture()
def anonymizer() -> Anonymizer:
    return Anonymizer()
