"""Per-model native-label -> EntityType maps for the SCR-28 spike.

The head-to-head must compare both models on **identical entity definitions**
(R4). Each model speaks its own label vocabulary, so this module funnels both
into ScreenCap's canonical ``EntityType`` set:

* **GLiNER** runs through the production pipeline, which already emits
  ``EntityType`` constants (``GLINER_ENTITY_MAPPING`` -> ``PRESIDIO_MAP`` happen
  inside ``PiiDetector``). So a GLiNER "native" label *is* an ``EntityType``;
  mapping is identity-with-validation.
* **privacy-filter** emits raw span labels (``private_person`` etc.); they map
  through the spike-local ``OPENAI_ENTITY_MAPPING``.

Labels with no ``EntityType`` home map to the :data:`OUT_OF_SCOPE` sentinel
rather than ``None`` — so coverage-delta reporting (U7) can see them, while the
scorer still excludes them from the shared-type head-to-head.

Imports stay light (``EntityType`` and the production maps are pure-Python with
no ML deps), so this module loads in the repo env where the scorer runs. The
isolated privacy-filter venv does **not** import this module — its runner emits
native labels and the scorer (repo env) does the mapping.
"""

from __future__ import annotations

from screencap.privacy import EntityType
from screencap.privacy.entity_mapping import GLINER_ENTITY_MAPPING, PRESIDIO_MAP

# Sentinel for native labels that exist but have no ScreenCap EntityType home.
# Distinct from ``None`` (genuinely unmapped/unknown) so reporting can tell
# "model emits this, we just don't score it" from "we dropped an unknown label".
OUT_OF_SCOPE = "OUT_OF_SCOPE"

# openai/privacy-filter native span labels -> EntityType (or OUT_OF_SCOPE).
#
# Shared types (scored head-to-head against GLiNER):
#   private_person  -> PERSON
#   private_email   -> EMAIL
#   private_phone   -> PHONE
#   private_address -> ADDRESS
#
# privacy-filter EXTRAS with no current EntityType home (visible, not scored):
#   private_url, private_date, account_number, secret -> OUT_OF_SCOPE
#
# privacy-filter GAPS vs the current mapping: it has NO native SSN or
# credit-card class. GLiNER produces those via "ssn"/"credit card". For
# privacy-filter they fall to ScreenCap's regex/secrets layer — whether that
# compensates is measured in U6 (full-pipeline mode) and surfaced in U8 (AE3).
OPENAI_ENTITY_MAPPING: dict[str, str] = {
    "private_person": EntityType.PERSON,
    "private_email": EntityType.EMAIL,
    "private_phone": EntityType.PHONE,
    "private_address": EntityType.ADDRESS,
    "private_url": OUT_OF_SCOPE,
    "private_date": OUT_OF_SCOPE,
    "account_number": OUT_OF_SCOPE,
    "secret": OUT_OF_SCOPE,
}

# The 8 privacy-filter span labels (model card §) — used to validate runner
# output and to drive coverage-delta reporting.
OPENAI_NATIVE_LABELS: tuple[str, ...] = tuple(OPENAI_ENTITY_MAPPING)

# All canonical EntityType string constants (PII + secrets).
_VALID_ENTITY_TYPES: frozenset[str] = frozenset(
    v
    for k, v in vars(EntityType).items()
    if not k.startswith("_") and isinstance(v, str)
)


def map_privacy_filter_label(native: str) -> str | None:
    """privacy-filter native label -> EntityType / OUT_OF_SCOPE / None."""
    return OPENAI_ENTITY_MAPPING.get(native)


def map_gliner_label(native: str) -> str | None:
    """GLiNER native label -> EntityType.

    Two paths, both valid:
    * The production pipeline already emits ``EntityType`` constants, so an
      already-canonical label maps to itself.
    * A raw GLiNER request label ("first name", "email address", ...) maps
      two-hop through ``GLINER_ENTITY_MAPPING`` -> ``PRESIDIO_MAP``.
    Returns ``None`` for anything outside both, so unknowns are dropped (not
    scored as errors).
    """
    if native in _VALID_ENTITY_TYPES:
        return native
    presidio = GLINER_ENTITY_MAPPING.get(native)
    if presidio is None:
        return None
    return PRESIDIO_MAP.get(presidio)


def map_label(model: str, native: str) -> str | None:
    """Map a native label to an ``EntityType`` (or OUT_OF_SCOPE / None).

    ``model`` selects the vocabulary: ``"privacy-filter"`` or ``"gliner"``.
    Returns:
      * an ``EntityType`` string for a scorable shared type,
      * :data:`OUT_OF_SCOPE` for a known-but-unscored label,
      * ``None`` for an unknown label (dropped silently by the scorer).
    """
    if model == "privacy-filter":
        return map_privacy_filter_label(native)
    if model == "gliner":
        return map_gliner_label(native)
    raise ValueError(f"unknown model {model!r}; expected 'gliner' or 'privacy-filter'")
