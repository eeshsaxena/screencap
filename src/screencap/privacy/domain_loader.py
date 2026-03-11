"""Domain index loader for privacy classification.

Reads UT1 blocklist files and the curated supplement, merges them
into a single ``dict[str, ContextClass]`` for O(1) domain lookup.
Parent-domain entries are pre-expanded at load time so that
``secure.bankofamerica.com`` matches ``bankofamerica.com`` without
runtime iteration.

This module is imported once at ``DefaultContextClassifier.__init__()``
time.  The resulting index is immutable after construction.
"""

from __future__ import annotations

import logging
from pathlib import Path

from screencap.privacy.policy import ContextClass

logger = logging.getLogger(__name__)

# UT1 category file → ContextClass mapping
_UT1_CATEGORY_MAP: dict[str, ContextClass] = {
    "bank": ContextClass.BANKING,
    "financial": ContextClass.BANKING,
    "webmail": ContextClass.EMAIL,
    "social_networks": ContextClass.CHAT,
    "chat": ContextClass.CHAT,
    "vpn": ContextClass.ADMIN_CONSOLE,
}

# Supplement uses string enum names to avoid circular imports;
# map them here.
_SUPPLEMENT_NAME_MAP: dict[str, ContextClass] = {m.name: m for m in ContextClass}

_DATA_DIR = Path(__file__).parent / "data"
_UT1_DIR = _DATA_DIR / "ut1"


def _normalize_domain(raw: str) -> str | None:
    """Lowercase, strip trailing dots, skip non-domain lines."""
    d = raw.strip().lower().rstrip(".")
    if not d or d.startswith("#"):
        return None
    # Skip IP addresses (v4 heuristic: starts with digit and contains only digits/dots)
    if d[0].isdigit():
        parts = d.split(".")
        if all(p.isdigit() for p in parts):
            return None
    # Must contain at least one dot
    if "." not in d:
        return None
    return d


def load_ut1_domains(ut1_dir: Path | None = None) -> dict[str, ContextClass]:
    """Read UT1 ``.txt`` files and return ``{domain: ContextClass}``."""
    ut1_dir = ut1_dir or _UT1_DIR
    result: dict[str, ContextClass] = {}
    for category, ctx_class in _UT1_CATEGORY_MAP.items():
        filepath = ut1_dir / f"{category}.txt"
        if not filepath.exists():
            raise FileNotFoundError(
                f"UT1 data file missing: {filepath}. "
                f"Run scripts/update-ut1.sh to download."
            )
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                domain = _normalize_domain(line)
                if domain is not None:
                    # First category wins (bank before financial matters)
                    result.setdefault(domain, ctx_class)
    return result


def load_supplement() -> dict[str, ContextClass]:
    """Load the curated domain supplement."""
    from screencap.privacy.data.supplement import SUPPLEMENT

    result: dict[str, ContextClass] = {}
    for domain, cls_name in SUPPLEMENT.items():
        normalized = _normalize_domain(domain)
        if normalized is None:
            logger.warning("Skipping invalid supplement domain: %r", domain)
            continue
        ctx_class = _SUPPLEMENT_NAME_MAP.get(cls_name)
        if ctx_class is None:
            logger.warning(
                "Skipping supplement domain %r: unknown ContextClass %r",
                domain,
                cls_name,
            )
            continue
        result[normalized] = ctx_class
    return result


def _expand_parents(index: dict[str, ContextClass]) -> dict[str, ContextClass]:
    """Pre-expand parent domains for O(1) suffix matching.

    For each domain like ``secure.bankofamerica.com``, insert
    ``bankofamerica.com`` (and ``com`` is skipped — too broad) if not
    already present.  Only inserts parents with 2+ labels.
    """
    expansions: dict[str, ContextClass] = {}
    for domain, ctx_class in index.items():
        labels = domain.split(".")
        # Generate parent domains by stripping leading labels
        # e.g. a.b.c.com → b.c.com, c.com  (skip single-label "com")
        for i in range(1, len(labels) - 1):
            parent = ".".join(labels[i:])
            if parent not in index and parent not in expansions:
                expansions[parent] = ctx_class
    return expansions


def build_domain_index(
    ut1: dict[str, ContextClass] | None = None,
    supplement: dict[str, ContextClass] | None = None,
) -> dict[str, ContextClass]:
    """Merge UT1 + supplement into a single domain index.

    Supplement overwrites UT1 on conflicts (curated > automated).
    Parent domains are pre-expanded for O(1) lookup.
    """
    if ut1 is None:
        ut1 = load_ut1_domains()
    if supplement is None:
        supplement = load_supplement()

    # Start with UT1, then overlay supplement (supplement wins on conflict)
    merged = dict(ut1)
    merged.update(supplement)

    # Pre-expand parents
    parents = _expand_parents(merged)
    for parent, ctx_class in parents.items():
        merged.setdefault(parent, ctx_class)

    return merged
