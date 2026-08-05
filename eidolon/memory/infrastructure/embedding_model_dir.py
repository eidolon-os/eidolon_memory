"""Point MemPalace's own embedders at a local copy of their model files.

``embedding.model_dir`` lets an operator ship model files with a deployment rather
than have every fresh runtime probe the hub. For **our** encoders that is now the
implementation's own business: ``OnnxSentenceEmbedder`` reads the directory
directly, checks it is complete, and falls back to the hub if it is not.

What is left here is the case that cannot be done that way. MemPalace's
``minilm`` and ``embeddinggemma`` resolve their files inside code we do not own,
through ``huggingface_hub.hf_hub_download``, and there is no argument, setting or
hook that redirects it. So the download function is replaced process-wide.

That is a real cost and it is stated rather than hidden: a process-global mutation
serving one library's file lookup. It used to be installed for every configured
model, including ours, which meant the mutation was paid on the default path for
no reason. Now it is installed only when the configured encoder is one of theirs —
so on the default configuration it never happens at all, and when it does happen
the reason is that the alternative is nothing.

The inspection functions below stay general and cover our models too. They read
the filesystem and change nothing, and an operator checking whether a directory is
complete wants the same answer whichever encoder it is for.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from eidolon.memory.domain.embedding_port import local_model_spec, mempalace_model_identity
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

EMBEDDINGGEMMA_REPO_ID = "onnx-community/embeddinggemma-300m-ONNX"
# MemPalace's own model, whose file list lives upstream rather than in our spec
# table. ``.onnx_data`` is the external-weights sidecar its quantized export
# needs; ours are single-file.
EMBEDDINGGEMMA_REQUIRED_FILES = frozenset(
    {
        Path("onnx/model_quantized.onnx"),
        Path("onnx/model_quantized.onnx_data"),
        Path("tokenizer.json"),
    }
)


def _repo_and_files(embedding_model: str) -> tuple[str, frozenset[Path]] | None:
    """What a fully populated local directory holds for ``embedding_model``."""

    model = embedding_model.strip().lower()
    if model == "embeddinggemma":
        return EMBEDDINGGEMMA_REPO_ID, EMBEDDINGGEMMA_REQUIRED_FILES

    spec = local_model_spec(model)
    if spec is None:
        # minilm downloads through a different upstream path, and an unknown
        # name is MemPalace's to resolve — in both cases there is nothing here
        # to map.
        return None
    return spec.repo, frozenset(Path(f) for f in spec.files)


def local_embedding_model_file(
    *,
    repo_id: str,
    filename: str,
    subfolder: str | None,
    embedding_model: str,
    model_dir: str,
) -> Path | None:
    """Return the configured local file for a MemPalace embedding request."""
    known = _repo_and_files(embedding_model)
    if known is None:
        return None
    expected_repo, required = known
    if repo_id != expected_repo:
        return None
    root = Path(model_dir).expanduser()
    if not str(root).strip():
        return None
    rel = Path(subfolder or "") / filename
    if rel not in required:
        return None
    path = root / rel
    return path if path.is_file() else None


def validate_local_embedding_model_dir(
    model_dir: str, *, embedding_model: str = "embeddinggemma"
) -> list[str]:
    """Files the directory is missing for ``embedding_model``, if any.

    Defaults to embeddinggemma so the existing call sites keep their meaning;
    callers that know which model is configured should pass it.
    """

    known = _repo_and_files(embedding_model)
    if known is None:
        return []
    _repo, required = known
    root = Path(model_dir).expanduser()
    return [str(rel) for rel in sorted(required, key=str) if not (root / rel).is_file()]


def apply_mempalace_model_dir_bridge_from_env() -> bool:
    """Patch Hugging Face downloads for MemPalace's own embedders.

    Returns True when the bridge is installed. Returns False — without touching
    anything — when the configured model is one of ours, when no directory is
    configured, when the directory is incomplete, or when the dependency is
    unavailable. In every one of those cases the caller should continue: our
    encoders resolve their own files, and MemPalace's normal download still works.

    Named for MemPalace deliberately. An earlier version was called
    ``apply_local_embedding_model_dir_from_env`` and covered every model, which
    read as "use the local directory" and was in fact "mutate this process so one
    library's download call lands somewhere else". The narrower name is the
    honest one now that only that library needs it.
    """
    model = os.environ.get("MEMPALACE_EMBEDDING_MODEL", "").strip().lower()
    model_dir = os.environ.get("MEMPALACE_EMBEDDING_MODEL_DIR", "").strip()
    if not model_dir:
        return False
    if mempalace_model_identity(model) is None:
        # Ours, or a name nobody implements. Either way the process-wide patch
        # buys nothing: OnnxSentenceEmbedder reads model_dir itself.
        return False
    if _repo_and_files(model) is None:
        # minilm — upstream fetches it from a different place entirely, not
        # through hf_hub_download, so there is nothing to intercept.
        return False

    missing = validate_local_embedding_model_dir(model_dir, embedding_model=model)
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
