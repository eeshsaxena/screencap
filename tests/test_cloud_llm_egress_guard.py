"""Structural guard pinning the cloud-LLM egress consent boundary (R5).

The legacy auto-namer sent raw recording content to cloud LLM APIs with no
consent gate. It is gone; this guard makes sure the same bypass cannot be
silently reintroduced anywhere under ``src/screencap/``. The sanctioned
naming/summary path is ``screencap.segmentation`` with its ``ConsentPolicy``.

Two detection axes, both pure-AST (stdlib only — no Vision/pyobjc/NLP, so the
guard runs on the Vision-free CI privacy lane):

- **Axis 1 — SDK imports**: any ``import`` / ``from ... import`` of the
  ``openai``, ``anthropic``, ``google.genai``, or ``google.generativeai``
  SDKs. ``ast.walk`` sees imports nested inside function bodies, so a
  deferred import cannot evade the guard, and relative imports are resolved
  to absolute module paths (mirroring
  ``tests/test_package_boundary_call_graph.py``) so package-internal modules
  that merely *end* in ``openai``/``anthropic`` (e.g.
  ``screencap.segmentation.providers.openai``) are not false positives.
- **Axis 2 — hostname literals**: any string literal (including the constant
  parts of f-strings, which ``ast.walk`` visits as ``ast.Constant`` children
  of ``ast.JoinedStr``) containing a cloud-LLM API hostname.

Every file allowed to touch these SDKs/hosts is pinned by repo-relative path
below; a moved or renamed sanctioned file fails the existence check loudly
instead of silently widening the boundary, and an allowlisted file that loses
its egress fails the tightness check so stale entries get removed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.privacy

_SRC = Path(__file__).resolve().parent.parent / "src"
_PKG_ROOT = _SRC / "screencap"

# A module matches when it equals a prefix or is a dotted submodule of it
# ("openai" matches "openai" and "openai.types", not "openai_utils").
_FORBIDDEN_SDK_PREFIXES = (
    "openai",
    "anthropic",
    "google.genai",
    "google.generativeai",
)

_FORBIDDEN_HOSTNAMES = (
    "api.anthropic.com",
    "api.openai.com",
    "generativelanguage.googleapis.com",
)

# The only files sanctioned to name a cloud-LLM SDK or API hostname, with WHY.
# Paths are relative to src/screencap/.
_ALLOWLIST: dict[str, str] = {
    # Consent-gated BYO-key segmentation provider; hardcodes the
    # https://api.openai.com chat-completions URL (axis 2).
    "segmentation/providers/openai.py": "consent-gated provider (OpenAI)",
    # Consent-gated BYO-key segmentation provider; hardcodes the
    # https://api.anthropic.com messages URL (axis 2).
    "segmentation/providers/anthropic.py": "consent-gated provider (Anthropic)",
    # Consent-gated segmentation provider; imports the google.genai SDK
    # inside its request methods (axis 1).
    "segmentation/providers/gemini.py": "consent-gated provider (Gemini)",
    # API-key validation: cheap authenticated no-content probes against the
    # providers' models/count_tokens endpoints (axis 2).
    "segmentation/secrets.py": "API-key validation URLs",
    # Opt-in cloud-Whisper transcription driver (`from openai import OpenAI`,
    # axis 1); sends audio the user explicitly chose to transcribe via API.
    "transcription.py": "cloud-Whisper transcription",
    # Chunk pipeline's cloud-Whisper fallback (`from openai import OpenAI`,
    # axis 1), gated on an explicit OPENAI_API_KEY.
    "chunk_processor.py": "cloud-Whisper transcription (chunk pipeline)",
    # Engine `transcribe` CLI's `--backend api` OpenAI Whisper path (axis 1).
    "engine/cli.py": "cloud-Whisper transcription (engine CLI)",
    # Egress *blocklist* data: names api.openai.com precisely to BLOCK it
    # (axis 2). Data, not an egress path.
    "network/blocklist.py": "blocklist data (hostnames to block)",
}


def _module_name(py: Path) -> str:
    """Absolute dotted module name for a file under ``src/`` (drops __init__)."""
    rel = py.relative_to(_SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _own_package(py: Path) -> tuple[str, ...]:
    parts = _module_name(py).split(".")
    return tuple(parts) if py.name == "__init__.py" else tuple(parts[:-1])


def _is_forbidden_module(mod: str) -> bool:
    return any(mod == p or mod.startswith(p + ".") for p in _FORBIDDEN_SDK_PREFIXES)


def _sdk_import_hits(tree: ast.AST, own_pkg: tuple[str, ...] = ()) -> list[tuple[int, str]]:
    """Axis 1: (lineno, resolved module) for every forbidden-SDK import.

    Walks the whole tree (module-level AND function bodies). Relative imports
    resolve against ``own_pkg``; ``from google import genai`` is caught by
    also considering ``module.name`` for each imported name.
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_module(alias.name):
                    hits.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module.split(".") if node.module else []
            else:
                # Relative import: climb ``level`` packages from own_pkg.
                pkg = list(own_pkg[: len(own_pkg) - (node.level - 1)]) if node.level > 1 else list(own_pkg)
                base = pkg + (node.module.split(".") if node.module else [])
            candidates = set()
            if base:
                candidates.add(".".join(base))
            for alias in node.names:
                candidates.add(".".join(base + [alias.name]))
            for mod in sorted(candidates):
                if _is_forbidden_module(mod):
                    hits.append((node.lineno, mod))
    return hits


def _hostname_hits(tree: ast.AST) -> list[tuple[int, str]]:
    """Axis 2: (lineno, hostname) for every string literal naming a cloud-LLM host.

    ``ast.walk`` reaches the ``ast.Constant`` parts of f-strings
    (``ast.JoinedStr``) too, so f-string-embedded hostnames are caught.
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for host in _FORBIDDEN_HOSTNAMES:
                if host in node.value:
                    hits.append((node.lineno, host))
    return hits


def _egress_hits(source: str, filename: str, own_pkg: tuple[str, ...] = ()) -> list[tuple[int, str]]:
    """All (lineno, description) egress hits in ``source``, both axes."""
    tree = ast.parse(source, filename=filename)
    hits = [(lineno, f"imports cloud-LLM SDK {mod!r}") for lineno, mod in _sdk_import_hits(tree, own_pkg)]
    hits += [
        (lineno, f"string literal contains cloud-LLM host {host!r}")
        for lineno, host in _hostname_hits(tree)
    ]
    return sorted(hits)


def _file_hits(py: Path) -> list[tuple[int, str]]:
    return _egress_hits(py.read_text(encoding="utf-8"), str(py), _own_package(py))


def test_no_cloud_llm_egress_outside_allowlist() -> None:
    """No file outside the allowlist imports a cloud-LLM SDK or names its host."""
    violations: list[str] = []
    for py in sorted(_PKG_ROOT.rglob("*.py")):
        rel = py.relative_to(_PKG_ROOT).as_posix()
        if rel in _ALLOWLIST:
            continue
        for lineno, reason in _file_hits(py):
            violations.append(f"src/screencap/{rel}:{lineno} {reason}")
    assert not violations, (
        "Cloud-LLM egress outside the sanctioned allowlist — recording content "
        "must only reach cloud LLM providers via the consent-gated "
        "screencap.segmentation path (or the sanctioned transcription/"
        "key-validation/blocklist files listed in this guard):\n  - "
        + "\n  - ".join(violations)
    )


def test_allowlist_entries_exist_on_disk() -> None:
    """A moved/renamed sanctioned file must fail loudly, not widen the boundary."""
    missing = sorted(rel for rel in _ALLOWLIST if not (_PKG_ROOT / rel).is_file())
    assert not missing, (
        "Allowlist entries with no file on disk (sanctioned file moved/renamed? "
        "update the guard's allowlist alongside it):\n  - " + "\n  - ".join(missing)
    )


def test_allowlist_entries_still_have_egress() -> None:
    """Tightness: every allowlisted file still has a hit on some axis.

    An entry whose file lost its SDK import / hostname literal is stale and
    silently widens the boundary — remove it from the allowlist instead.
    """
    stale = sorted(
        rel for rel in _ALLOWLIST if (_PKG_ROOT / rel).is_file() and not _file_hits(_PKG_ROOT / rel)
    )
    assert not stale, (
        "Stale allowlist entries (no SDK import or hostname literal today — "
        "remove them from the allowlist):\n  - " + "\n  - ".join(stale)
    )


class TestDetectionHelpers:
    """Self-tests on synthetic sources — the red-proof that detection fires."""

    def test_function_body_openai_import_detected(self) -> None:
        source = "def _call():\n    from openai import OpenAI\n    return OpenAI()\n"
        hits = _egress_hits(source, "<synthetic>")
        assert hits, "function-body `from openai import OpenAI` must be detected"
        assert hits[0][0] == 2
        assert "openai" in hits[0][1]

    def test_google_genai_import_detected(self) -> None:
        hits = _egress_hits("import google.genai\n", "<synthetic>")
        assert hits and "google.genai" in hits[0][1]

    def test_from_google_import_genai_detected(self) -> None:
        # The form gemini.py actually uses.
        hits = _egress_hits("from google import genai\n", "<synthetic>")
        assert hits and "google.genai" in hits[0][1]

    def test_anthropic_url_literal_detected(self) -> None:
        source = 'URL = "https://api.anthropic.com/v1/messages"\n'
        hits = _egress_hits(source, "<synthetic>")
        assert hits and "api.anthropic.com" in hits[0][1]

    def test_fstring_gemini_host_detected(self) -> None:
        source = 'url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}"\n'
        hits = _egress_hits(source, "<synthetic>")
        assert hits and "generativelanguage.googleapis.com" in hits[0][1]

    def test_clean_source_yields_no_hits(self) -> None:
        source = (
            "import json\n"
            "from pathlib import Path\n"
            "from screencap.segmentation.providers.openai import OpenAIProvider\n"
            'GREETING = "hello world"\n'
        )
        assert _egress_hits(source, "<synthetic>") == []
