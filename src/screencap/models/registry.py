"""Shipped model manifest — the root of trust for the downloadable model (U4, KTD5/KTD6).

Each supported model has a per-runtime variant (MLX safetensors for Apple Silicon,
GGUF for llama.cpp on Intel). A variant pins the HF repo, the exact **commit SHA**,
and the **sha256 of each file** — recorded at release QA from a known-good download,
so a Hub compromise or force-push at that commit cannot install different bytes,
and only **non-executable formats** (safetensors / GGUF) are ever accepted (KTD5).

The manifest ships inside the app/repo, so it inherits the app's
signing/notarization integrity — a stronger root of trust than the download itself.

Model choice (KTD6): a ~3B-class **Apache-2.0** instruction model (Qwen2.5-3B-Instruct
lean) — avoids the Llama-3.2 attribution/NOTICE burden and is well-covered by both
the MLX-community and GGUF quant ecosystems. The exact revision + per-file sha256
are pinned at release QA and confirmed by the U12 eval before named output ships;
until then a variant is marked unpinned and :func:`~screencap.models.download.download_model`
refuses it (fail-closed).
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Sentinel for a not-yet-release-pinned revision / file hash.
PLACEHOLDER = "PLACEHOLDER-record-at-release-QA"

#: Non-executable weight formats we will install. Everything else (pickle-based
#: `.bin`/`.pt`/`.ckpt`, or a `.py`) is rejected at download (KTD5, H1).
ALLOWED_FORMATS = frozenset({"safetensors", "gguf"})

#: File extensions that can execute code on load — never installed.
DISALLOWED_EXTS = frozenset({".bin", ".pt", ".pth", ".pickle", ".ckpt", ".py"})

# Runtime identifiers (mirror screencap.segmentation.local_model.runtime).
RUNTIME_MLX = "mlx"
RUNTIME_LLAMACPP = "llamacpp"


@dataclass(frozen=True)
class FileSpec:
    """One expected file in a variant: its name, recorded sha256, and size."""

    name: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class Variant:
    """A per-runtime download: repo, pinned commit, expected files, weight format."""

    repo: str
    revision: str  # pinned commit SHA (or PLACEHOLDER until release QA)
    allowed_format: str  # "safetensors" | "gguf"
    files: tuple[FileSpec, ...] = ()

    @property
    def size_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    def is_pinned(self) -> bool:
        """True once the revision and every file hash are recorded (release-pinned)."""
        return (
            self.revision != PLACEHOLDER
            and bool(self.files)
            and all(f.sha256 != PLACEHOLDER for f in self.files)
        )


@dataclass(frozen=True)
class ModelSpec:
    """A downloadable model with one variant per host runtime."""

    id: str
    display_name: str
    license: str
    variants: dict[str, Variant] = field(default_factory=dict)

    def variant_for(self, runtime: str) -> Variant | None:
        return self.variants.get(runtime)


#: The default (and, in v1, only) downloadable model. Revisions + hashes are
#: PLACEHOLDER until release QA pins them (see module docstring); the download
#: engine refuses an unpinned variant, so the plumbing ships without shipping an
#: unverified model.
DEFAULT_MODEL_ID = "qwen2.5-3b-instruct"

MODELS: dict[str, ModelSpec] = {
    DEFAULT_MODEL_ID: ModelSpec(
        id=DEFAULT_MODEL_ID,
        display_name="Qwen2.5 3B Instruct (4-bit)",
        license="Apache-2.0",
        variants={
            RUNTIME_MLX: Variant(
                repo="mlx-community/Qwen2.5-3B-Instruct-4bit",
                revision=PLACEHOLDER,
                allowed_format="safetensors",
                files=(),  # pinned at release QA (name/sha256/size per file)
            ),
            RUNTIME_LLAMACPP: Variant(
                repo="bartowski/Qwen2.5-3B-Instruct-GGUF",
                revision=PLACEHOLDER,
                allowed_format="gguf",
                files=(),
            ),
        },
    ),
}


def get_model(model_id: str) -> ModelSpec | None:
    return MODELS.get(model_id)


def ext_is_disallowed(filename: str) -> bool:
    """True when ``filename``'s extension can execute code on load (reject it)."""
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in DISALLOWED_EXTS)
