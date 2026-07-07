"""Downloadable local-model acquisition (U4+, SCR-239).

The opt-in downloadable model is fetched from Hugging Face, commit-SHA-pinned and
sha256-verified against a shipped manifest, into ``~/.screencap/models/``. This
package owns the manifest (:mod:`registry`) and the download+verify engine
(:mod:`download`); the daemon job (U5) and CLI (U6) drive it, and the U2
provider resolves the installed model path through :func:`get_installed_model_path`.

Nothing here is cloud-vendor-coupled at import time — ``huggingface_hub`` is
imported lazily inside the download path only.
"""

from screencap.models.download import (  # noqa: F401
    DownloadResult,
    ModelNotPinnedError,
    download_model,
    get_disclosed_size,
    get_installed_model_path,
    is_model_installed,
)
from screencap.models.registry import DEFAULT_MODEL_ID  # noqa: F401
