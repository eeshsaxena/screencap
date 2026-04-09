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
    # Reject entries with URL path components (e.g. "app.proton.me/pass")
    if "/" in d:
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


# Known second-level domain suffixes — these are TLD-like and should not
# be created as parent domain entries (e.g. "co.uk", "com.au").
_SLD_SUFFIXES: frozenset[str] = frozenset({
    "co", "com", "org", "net", "ac", "gov", "edu", "mil",
    "sch", "nhs", "police", "mod",
    # Country-code SLDs found leaking from UT1 parent expansion
    "or", "go", "gr", "ne", "ad", "lg", "asn", "web", "my", "id",
})


def _is_tld_like(parent: str) -> bool:
    """Return True if parent looks like a TLD rather than a real domain.

    Examples that should be rejected: co.uk, com.au, org.br
    Examples that should be kept: bankofamerica.com, google.com
    """
    labels = parent.split(".")
    if len(labels) != 2:
        return False
    return labels[0] in _SLD_SUFFIXES



def extract_root_domain(domain: str) -> str:
    """Collapse subdomains to the registrable root domain.

    Uses ``_SLD_SUFFIXES`` for known second-level suffixes (co.uk, com.au, etc.).

    Examples::

        secure.chase.com     → chase.com
        login.bank.co.uk     → bank.co.uk
        a.b.c.example.com    → example.com
        github.com           → github.com
    """
    parts = domain.lower().rstrip(".").split(".")
    if len(parts) <= 2:
        return ".".join(parts)
    # Check for two-part TLD (e.g., co.uk)
    if len(parts) >= 3 and parts[-2] in _SLD_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def build_domain_index(
    ut1: dict[str, ContextClass] | None = None,
    supplement: dict[str, ContextClass] | None = None,
) -> dict[str, ContextClass]:
    """Merge UT1 + supplement into a single domain index.

    Supplement overwrites UT1 on conflicts (curated > automated).
    Parent matching is handled at query time by the walk-up in
    ``DefaultContextClassifier._classify_domain()`` — no pre-expansion
    is done here, to avoid false positives on shared hosts like
    ``google.com`` (which would be wrongly created from ``mail.google.com``).
    """
    if ut1 is None:
        ut1 = load_ut1_domains()
    if supplement is None:
        supplement = load_supplement()

    # Start with UT1, then overlay supplement (supplement wins on conflict)
    merged = dict(ut1)
    merged.update(supplement)

    return merged
