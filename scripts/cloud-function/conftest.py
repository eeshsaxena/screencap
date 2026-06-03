"""Pytest setup for the signing Cloud Function suite.

main.py creates its GCS client, default credentials, and Firebase app at module
scope — the warm-invocation reuse pattern Cloud Run wants — so importing main.py
normally requires real ADC. These patches replace the GCP/Firebase entry points
with mocks BEFORE any test module does ``import main``, so the suite runs fully
offline. Individual tests still mock the specifics they assert on
(``verify_id_token`` / ``list_blobs`` / ``generate_signed_url``).

The patches are started at collection time and intentionally never stopped — the
process lifetime is the test session.
"""

from unittest import mock

mock.patch("google.auth.default", return_value=(mock.MagicMock(), "test-project")).start()
mock.patch("google.cloud.storage.Client").start()
mock.patch("firebase_admin.initialize_app").start()
