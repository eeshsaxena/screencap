"""Centralized entity label -> EntityType mapping.

Every NER/PII backend maps its native labels through this module.
New backends only need to add their mapping dict here.
"""

from __future__ import annotations

from screencap.privacy import EntityType

# Presidio (spaCy NER backend) -> EntityType
PRESIDIO_MAP: dict[str, str] = {
    "PERSON": EntityType.PERSON,
    "EMAIL_ADDRESS": EntityType.EMAIL,
    "PHONE_NUMBER": EntityType.PHONE,
    "US_SSN": EntityType.SSN,
    "CREDIT_CARD": EntityType.CREDIT_CARD,
    "LOCATION": EntityType.ADDRESS,
    "NRP": EntityType.PERSON,
    # Unmapped Presidio types -> None (skipped)
}

# GLiNER PII model (knowledgator/gliner-pii-base-v1.0) -> Presidio entity type.
# Keys = GLiNER label strings passed to the model at inference time.
# Values = Presidio entity type constants (uppercase) that GLiNERRecognizer emits.
# These are then mapped through PRESIDIO_MAP to our EntityType.
GLINER_ENTITY_MAPPING: dict[str, str] = {
    "name": "PERSON",
    "first name": "PERSON",
    "last name": "PERSON",
    "email address": "EMAIL_ADDRESS",
    "phone number": "PHONE_NUMBER",
    "ssn": "US_SSN",
    "credit card": "CREDIT_CARD",
    "location address": "LOCATION",
    "location city": "LOCATION",
    "location country": "LOCATION",
    # GLiNER-specific types we intentionally drop (not in entity_mapping -> not detected)
    # "dob", "age", "gender", "ip address", "url", "passport number",
    # "driver license", "username", "password", "account number", etc.
}
