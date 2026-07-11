"""Local embedding model directory bridge for MemPalace.

MemPalace's embeddinggemma loader currently resolves files through
``huggingface_hub.hf_hub_download``. Eidolon already has an explicit
``mempalace.embedding_model_dir`` setting, so we map that local directory into
the HF download call instead of letting every fresh runtime probe the hub/cache.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

EMBEDDINGGEMMA_REPO_ID = "onnx-community/embeddinggemma-300m-ONNX"
EMBEDDINGGEMMA_REQUIRED_FILES = frozenset(
    {
        Path("onnx/model_quantized.onnx"),
        Path("onnx/model_quantized.onnx_data"),
        Path("tokenizer.json"),
    }
)


def local_embedding_model_file(
    *,
    repo_id: str,
    filename: str,
    subfolder: str | None,
    embedding_model: str,
    model_dir: str,
) -> Path | None:
    """Return the configured local file for a MemPalace embedding request."""
    if embedding_model.strip().lower() != "embeddinggemma":
        return None
    if repo_id != EMBEDDINGGEMMA_REPO_ID:
        return None
    root = Path(model_dir).expanduser()
    if not str(root).strip():
        return None
    rel = Path(subfolder or "") / filename
    if rel not in EMBEDDINGGEMMA_REQUIRED_FILES:
        return None
    path = root / rel
    return path if path.is_file() else None


def validate_local_embedding_model_dir(model_dir: str) -> list[str]:
    root = Path(model_dir).expanduser()
    return [
        str(rel)
        for rel in sorted(EMBEDDINGGEMMA_REQUIRED_FILES, key=str)
        if not (root / rel).is_file()
    ]


def apply_local_embedding_model_dir_from_env() -> bool:
    """Patch Hugging Face downloads to use ``MEMPALACE_EMBEDDING_MODEL_DIR``.

    Returns True when the bridge is installed. If the env is absent, invalid,
    or the dependency is unavailable, the caller should continue with normal
    MemPalace behavior.
    """
    model = os.environ.get("MEMPALACE_EMBEDDING_MODEL", "").strip().lower()
    model_dir = os.environ.get("MEMPALACE_EMBEDDING_MODEL_DIR", "").strip()
    if model != "embeddinggemma" or not model_dir:
        return False

    missing = validate_local_embedding_model_dir(model_dir)
    if missing:
        log.warning(
            "embedding_model_dir_incomplete",
            model=model,
            model_dir=model_dir,
            missing=missing,
        )
        return False

    try:
        import huggingface_hub
    except Exception as exc:  # pragma: no cover - dependency missing is environment-specific
        log.warning(
            "embedding_model_dir_bridge_unavailable",
            model=model,
            model_dir=model_dir,
            error=str(exc),
        )
        return False

    original = getattr(huggingface_hub, "_eidolon_original_hf_hub_download", None)
    if original is None:
        original = huggingface_hub.hf_hub_download
        setattr(huggingface_hub, "_eidolon_original_hf_hub_download", original)

    def _hf_hub_download(repo_id: str, filename: str, *args: Any, **kwargs: Any) -> str:
        local = local_embedding_model_file(
            repo_id=repo_id,
            filename=filename,
            subfolder=kwargs.get("subfolder"),
            embedding_model=os.environ.get("MEMPALACE_EMBEDDING_MODEL", ""),
            model_dir=os.environ.get("MEMPALACE_EMBEDDING_MODEL_DIR", ""),
        )
        if local is not None:
            return str(local)
        return original(repo_id, filename, *args, **kwargs)

    huggingface_hub.hf_hub_download = _hf_hub_download
    log.info("embedding_model_dir_bridge_enabled", model=model, model_dir=model_dir)
    return True
