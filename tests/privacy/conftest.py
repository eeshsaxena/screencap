"""Shared fixtures for privacy tests."""

from __future__ import annotations

import pytest

from screencap.privacy import Anonymizer


@pytest.fixture()
def anonymizer() -> Anonymizer:
    return Anonymizer()
