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

#: File extensions that can execute code on load — never installed. Format safety
#: is enforced as this **denylist** (plus the manifest's exact-filename allow list
#: and per-file sha256 pin in :func:`~screencap.models.download.download_model`);
#: the weight files themselves are the non-executable safetensors / GGUF a variant
#: declares via :attr:`Variant.allowed_format` (KTD5, H1).
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


#: The default (and, in v1, only) downloadable model. Revisions + per-file
#: sha256 were recorded 2026-07-15 from a known-good download of each pinned
#: commit, cross-checked against the Hub's LFS metadata for the weight files.
DEFAULT_MODEL_ID = "qwen2.5-3b-instruct"

MODELS: dict[str, ModelSpec] = {
    DEFAULT_MODEL_ID: ModelSpec(
        id=DEFAULT_MODEL_ID,
        display_name="Qwen2.5 3B Instruct (4-bit)",
        license="Apache-2.0",
        variants={
            RUNTIME_MLX: Variant(
                repo="mlx-community/Qwen2.5-3B-Instruct-4bit",
                revision="4f83f8f146fdf28b512a06562b671d7af4fab457",
                allowed_format="safetensors",
                files=(
                    FileSpec(
                        name="added_tokens.json",
                        sha256="58b54bbe36fc752f79a24a271ef66a0a0830054b4dfad94bde757d851968060b",
                        size_bytes=605,
                    ),
                    FileSpec(
                        name="config.json",
                        sha256="ceb97c46fe17f523ac42c0f0254fd18d9589f8725f0bbbe0bef541f8ef84547a",
                        size_bytes=785,
                    ),
                    FileSpec(
                        name="merges.txt",
                        sha256="8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5",
                        size_bytes=1_671_853,
                    ),
                    FileSpec(
                        name="model.safetensors",
                        sha256="f212cf6fb9923281a09c135e05d43a052ee5ef7121f5b1dc0b0fb2de80f97cfd",
                        size_bytes=1_736_293_090,
                    ),
                    FileSpec(
                        name="model.safetensors.index.json",
                        sha256="7edb73f1af3c860b1344ece4abe04b258246afdfeba4cb39d3b84a0459890bee",
                        size_bytes=66_290,
                    ),
                    FileSpec(
                        name="special_tokens_map.json",
                        sha256="76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd",
                        size_bytes=613,
                    ),
                    FileSpec(
                        name="tokenizer.json",
                        sha256="a8506e7111b80c6d8635951a02eab0f4e1a8e4e5772da83846579e97b16f61bf",
                        size_bytes=7_031_673,
                    ),
                    FileSpec(
                        name="tokenizer_config.json",
                        sha256="f7c61e32b7a17d19bf8e7037dcb74079a833e53ea9801f24008cac68458f03b7",
                        size_bytes=7_308,
                    ),
                    FileSpec(
                        name="vocab.json",
                        sha256="ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
                        size_bytes=2_776_833,
                    ),
                ),
            ),
            RUNTIME_LLAMACPP: Variant(
                repo="bartowski/Qwen2.5-3B-Instruct-GGUF",
                revision="f302c64a2269a69fb27b2f9473b362f5bb8e78d8",
                allowed_format="gguf",
                files=(
                    FileSpec(
                        name="Qwen2.5-3B-Instruct-Q4_K_M.gguf",
                        sha256="9c9f56a391a3abbd5b89d0245bf6106081bcc3173119d4229235dd9d23253f94",
                        size_bytes=1_929_903_264,
                    ),
                ),
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
