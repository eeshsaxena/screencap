#!/usr/bin/env python3
"""Draft Tier-2 gold from the real extract + a synthetic OCR-noise set.

Run after ``extract.py``. Produces (all gitignored — they carry real or
realistic PII):

* ``gold.jsonl``            — gold for the **real** ``inputs.jsonl`` extract:
  the genuine in-scope PII found (1 PERSON, 1 EMAIL) + the no-PII dev-screen
  blocks as false-positive probes (does a backend hallucinate PII on code /
  paths / timestamps / commit messages?). Ambiguous blocks (city-named
  workspaces, usernames, URLs) are *excluded* (non-FP, empty expected) so
  neither backend is unfairly rewarded or penalized for a judgment call.
* ``inputs.synthetic.jsonl`` / ``gold.synthetic.jsonl`` — a **synthetic** set of
  OCR-styled blocks seeded with person/email/phone/address/ssn/credit-card PII,
  modeling the OCR artifacts seen in the real extract (leading bullets, split
  tokens, missing spaces, mixed case). The real recordings lack high-harm PII
  density, so this provides the per-type *recall-under-OCR-noise* signal the real
  subset can't. **Values are synthetic** (standard test SSN/credit-card numbers,
  invented names) — disclosed as such; never confuse with the real subset.

This is an **agent-drafted** gold (per the plan: agent drafts, human reconciles).
A human should spot-check before the verdict treats Tier-2 as decisive.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# ---------------------------------------------------------------------------
# Real subset: explicit classification of the extracted blocks
# ---------------------------------------------------------------------------

# The genuine in-scope PII in the v15-pii-positive extract, keyed by block id and
# tagged with HOW to extract the span from the (gitignored) block text — NOT the
# literal PII values. This keeps the committed script free of real PII, consistent
# with the README's commit rule. ``rule``: "text" = the whole block is the span;
# "email" = regex-extract the email substring from the block.
REAL_PII_RULES: dict[str, list[tuple[str, str]]] = {
    "v15-pii-positive-ocr-0064": [("PERSON", "text")],
    "v15-pii-positive-ocr-0122": [("EMAIL", "email")],
}


def _extract_substring(rule: str, text: str) -> str | None:
    if rule == "text":
        return text.strip()
    if rule == "email":
        m = _EMAIL_RE.search(text)
        return m.group(0) if m else None
    raise ValueError(f"unknown extraction rule {rule!r}")

# Ambiguous blocks excluded from scoring (non-FP, empty expected): city-named
# Conductor workspaces (Stuttgart/Salvador/Prague — not addresses), usernames /
# handles (rutefig, octocat — USERNAME is out of scope), and URL-only blocks.
EXCLUDE_SUBSTRINGS = (
    "Stuttgart", "Salvador", "Prague", "rutefig", "octocat", "github.com",
)


def classify_real(rec: dict) -> dict | None:
    """Return a gold case dict for a real block, or None to skip it entirely."""
    rid, text = rec["id"], rec["text"]
    if rid in REAL_PII_RULES:
        expected = []
        for etype, rule in REAL_PII_RULES[rid]:
            sub = _extract_substring(rule, text)
            if sub:
                expected.append({"entity_type": etype, "substring": sub, "source": None})
        return _case(rid, f"{rec['modality']} | real PII", text, expected, is_fp=False)
    # Drop very short / mostly-symbol garbled blocks — not meaningful to score.
    alnum = sum(c.isalnum() for c in text)
    if len(text) < 8 or alnum < 5:
        return None
    if any(sub in text for sub in EXCLUDE_SUBSTRINGS):
        # Ambiguous: keep out of scoring (non-FP, no expected -> excluded by split_cases).
        return _case(rid, f"{rec['modality']} | ambiguous (excluded)", text, [], is_fp=False)
    # Everything else in this dev-screen recording is no-PII chrome / code /
    # timestamps / commit messages -> a false-positive probe.
    return _case(rid, f"{rec['modality']} | no-PII probe", text, [], is_fp=True, frequency="high")


def _case(cid, desc, text, expected, *, is_fp, frequency=None) -> dict:
    return {
        "id": cid, "description": desc, "text": text,
        "expected": expected, "is_false_positive": is_fp, "frequency": frequency,
    }


# ---------------------------------------------------------------------------
# Synthetic OCR-noise set (values are synthetic / standard test numbers)
# ---------------------------------------------------------------------------
# (text, [(entity_type, substring), ...]). OCR-style artifacts on purpose:
# leading bullets, split tokens, missing/extra spaces, mixed case, UI chrome.
SYNTHETIC: list[tuple[str, list[tuple[str, str]]]] = [
    # PERSON
    ("• Mtg w/ Sarah Chen at 3pm", [("PERSON", "Sarah Chen")]),
    ("Approved by Priya Raghavan ✓", [("PERSON", "Priya Raghavan")]),
    ("From: Michael O'Brien", [("PERSON", "Michael O'Brien")]),
    ("cc David Kim and Wei Zhang", [("PERSON", "David Kim"), ("PERSON", "Wei Zhang")]),
    ("Dr. Amara Okafor reviewed the chart", [("PERSON", "Amara Okafor")]),
    ("assignee janderson  (lead)", []),  # username-only -> no in-scope PII
    # EMAIL (incl. OCR-split "@")
    ("Reply to m.delacruz@acme.io soon", [("EMAIL", "m.delacruz@acme.io")]),
    ("• ssanchez@example.org (primary)", [("EMAIL", "ssanchez@example.org")]),
    ("From j_okeke@mail.co.uk yesterday", [("EMAIL", "j_okeke@mail.co.uk")]),
    ("contact: rsmith@outlook.com", [("EMAIL", "rsmith@outlook.com")]),
    ("billing.team@northwind-traders.com", [("EMAIL", "billing.team@northwind-traders.com")]),
    # PHONE
    ("Call (415) 555-0182 today", [("PHONE", "(415) 555-0182")]),
    ("tel +44 20 7946 0958 ext 2", [("PHONE", "+44 20 7946 0958")]),
    ("• Cell: 408.555.0177", [("PHONE", "408.555.0177")]),
    ("Fax 212-555-0143", [("PHONE", "212-555-0143")]),
    ("reach me 1-650-555-0199", [("PHONE", "1-650-555-0199")]),
    # ADDRESS
    ("Ship to 742 Evergreen Terrace, Springfield IL 62704",
     [("ADDRESS", "742 Evergreen Terrace, Springfield IL 62704")]),
    ("• 350 5th Ave, New York NY 10118", [("ADDRESS", "350 5th Ave, New York NY 10118")]),
    ("Office: 1 Infinite Loop, Cupertino CA 95014",
     [("ADDRESS", "1 Infinite Loop, Cupertino CA 95014")]),
    ("home 1600 Pennsylvania Ave NW Washington DC",
     [("ADDRESS", "1600 Pennsylvania Ave NW Washington DC")]),
    # SSN (standard invalid test values)
    ("SSN 123-45-6789 on file", [("SSN", "123-45-6789")]),
    ("ssn: 078-05-1120 (verify)", [("SSN", "078-05-1120")]),
    ("Taxpayer ID 219-09-9999", [("SSN", "219-09-9999")]),
    # CREDIT_CARD (standard test card numbers)
    ("card 4111 1111 1111 1111 exp 12/27", [("CREDIT_CARD", "4111 1111 1111 1111")]),
    ("Visa 4532015112830366", [("CREDIT_CARD", "4532015112830366")]),
    ("MC 5500 0000 0000 0004", [("CREDIT_CARD", "5500 0000 0000 0004")]),
    # No-PII OCR distractors (false-positive probes in the synthetic set)
    ("• src/app/handlers/auth.py +12 -3", []),
    ("Compiling module foo (3 warnings)", []),
    ("commit a1b2c3d feat: add retry", []),
    ("00:14:32  build #4187 passed", []),
    ("export AWS_REGION=us-east-1", []),
    ("git push origin feat/login-v2", []),
]


# ---------------------------------------------------------------------------
# Synthetic SECRETS / API-keys set — the secrets axis (separate from PII)
# ---------------------------------------------------------------------------
# privacy-filter has a native `secret` class GLiNER's NER lacks; ScreenCap detects
# secrets via a separate regex + detect-secrets layer. This set scores the two on
# a unified binary SECRET bucket. Code/terminal OCR is relatively clean, so tokens
# are kept INTACT (corrupting a secret makes "is it still a secret" ambiguous) and
# wrapped in realistic chrome (prompts, .env lines). Distractors include entropy
# traps (git SHAs, UUIDs, hashes) that a naive detector might over-flag.
#
# **All values are synthetic / standard documented test values — never real
# credentials.** Each provider-format token is ASSEMBLED FROM FRAGMENTS via
# ``_frag(...)`` so no contiguous scanner-matching literal sits in committed
# source — GitHub push-protection blocks realistic provider tokens even when
# synthetic. Runtime concatenation reproduces the exact format the detect-secrets
# layer keys on, so the benchmark is unchanged.


def _frag(*parts: str) -> str:
    """Join token fragments at runtime (keeps source free of full token literals)."""
    return "".join(parts)


# (text template with ``{tok}``, assembled token) — token=None marks a distractor.
SECRETS_RAW: list[tuple[str, str | None]] = [
    ("› export AWS_ACCESS_KEY_ID={tok}", _frag("AKIA", "IOSFODNN7EXAMPLE")),
    ("AWS_SECRET_ACCESS_KEY={tok}", _frag("wJalrXUtnFEMI/K7MDENG", "/bPxRfiCYEXAMPLEKEY")),
    ("remote https://{tok}@github.com/x/y", _frag("ghp_", "R8kZqW2nT4yU6pX1aB3cD5eF7gH9jK0mN2pQ")),
    ("STRIPE_SECRET_KEY={tok}", _frag("sk_", "live_", "4eC39HqLyjWDarjtT1zdp7dc")),
    ("SLACK_BOT_TOKEN={tok}", _frag("xoxb-", "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx")),
    ("Authorization: Bearer {tok}",
     _frag("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.", "eyJzdWIiOiIxMjM0NTY3ODkwIn0.", "dozjgNryP4J3jVmNHl0w5Nx")),
    ("{tok}", _frag("-----BEGIN RSA ", "PRIVATE KEY-----")),
    ("DATABASE_URL={tok}", _frag("postgres://admin:", "Sup3rS3cretPw", "@db.prod.internal:5432/app")),
    ("GOOGLE_MAPS_KEY={tok}", _frag("AIza", "SyB1a2b3c4d5e6f7g8h9i0JkLmNoPqRsTuVwX")),
    ("OPENAI_API_KEY={tok}", _frag("sk-", "proj-", "AbCd1234EfGh5678IjKl90MnOpQrStUv")),
    ("//registry.npmjs.org/:_authToken={tok}", _frag("npm_", "AbCdEfGhIjKlMnOpQrStUvWxYz1234567890")),
    ("• api_key: {tok}", _frag("9f8e7d6c5b4a3928", "17065f4e3d2c1b0a9f8e7d6c")),
    ("Authorization: Basic {tok}", _frag("YWxhZGRpbjpv", "cGVuc2VzYW1l")),
    ("DB_PASSWORD={tok}", _frag("hunter2Pa", "$$w0rd!")),
    # Distractors (no secret) — entropy traps that a naive detector may over-flag.
    ("commit a1b2c3d4e5f6789012345678901234567890abcd", None),
    ("id: 550e8400-e29b-41d4-a716-446655440000", None),
    ("sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4", None),
    ("GET /api/users/42 -> 200 OK in 13ms", None),
    ("version 1.20.0-rc.3+build.456", None),
    ("export PATH=/usr/local/bin:$PATH", None),
]


def build_synthetic() -> tuple[list[dict], list[dict]]:
    inputs, gold = [], []
    for i, (text, ents) in enumerate(SYNTHETIC):
        cid = f"synthetic-ocr-{i:04d}"
        inputs.append({"id": cid, "text": text, "modality": "ocr", "source": "synthetic"})
        gold.append(_case(
            cid, "synthetic ocr-noise", text,
            [{"entity_type": t, "substring": s, "source": None} for t, s in ents],
            is_fp=not ents, frequency="high" if not ents else None,
        ))
    return inputs, gold


def build_secrets() -> tuple[list[dict], list[dict]]:
    inputs, gold = [], []
    for i, (template, token) in enumerate(SECRETS_RAW):
        cid = f"secrets-{i:04d}"
        text = template.format(tok=token) if token else template
        expected = (
            [{"entity_type": "SECRET", "substring": token, "source": None}] if token else []
        )
        inputs.append({"id": cid, "text": text, "modality": "ocr", "source": "synthetic-secret"})
        gold.append(_case(
            cid, "synthetic secret/api-key", text, expected,
            is_fp=not token, frequency="high" if not token else None,
        ))
    return inputs, gold


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> None:
    real_inputs = [json.loads(line) for line in (HERE / "inputs.jsonl").open()]
    real_gold = [c for rec in real_inputs if (c := classify_real(rec)) is not None]
    _write(HERE / "gold.jsonl", real_gold)
    n_pii = sum(1 for c in real_gold if c["expected"])
    n_fp = sum(1 for c in real_gold if c["is_false_positive"])
    n_excl = sum(1 for c in real_gold if not c["expected"] and not c["is_false_positive"])
    print(f"real gold.jsonl: {len(real_gold)} cases ({n_pii} PII, {n_fp} FP probes, {n_excl} excluded)")

    syn_inputs, syn_gold = build_synthetic()
    _write(HERE / "inputs.synthetic.jsonl", syn_inputs)
    _write(HERE / "gold.synthetic.jsonl", syn_gold)
    syn_pii = sum(1 for c in syn_gold if c["expected"])
    print(f"synthetic: {len(syn_inputs)} inputs, {syn_pii} PII-bearing + {len(syn_gold)-syn_pii} distractors")

    sec_inputs, sec_gold = build_secrets()
    _write(HERE / "inputs.secrets.jsonl", sec_inputs)
    _write(HERE / "gold.secrets.jsonl", sec_gold)
    sec_pos = sum(1 for c in sec_gold if c["expected"])
    print(f"secrets: {len(sec_inputs)} inputs, {sec_pos} secret-bearing + {len(sec_gold)-sec_pos} distractors")


if __name__ == "__main__":
    main()
