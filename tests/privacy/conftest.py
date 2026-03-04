"""Shared fixtures for privacy tests."""

from __future__ import annotations

import pytest

from screencap.privacy import Anonymizer, Detection, DetectionPipeline


@pytest.fixture()
def anonymizer() -> Anonymizer:
    return Anonymizer()


@pytest.fixture()
def empty_pipeline() -> DetectionPipeline:
    """Pipeline with no detectors (for testing error paths)."""
    return DetectionPipeline([])
