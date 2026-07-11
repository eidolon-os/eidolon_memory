from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from eidolon.memory.infrastructure.embedding_model_dir import (
    EMBEDDINGGEMMA_REPO_ID,
    apply_local_embedding_model_dir_from_env,
    local_embedding_model_file,
    validate_local_embedding_model_dir,
)


def _write_embeddinggemma_files(root: Path) -> None:
    (root / "onnx").mkdir(parents=True)
    (root / "onnx" / "model_quantized.onnx").write_bytes(b"onnx")
    (root / "onnx" / "model_quantized.onnx_data").write_bytes(b"data")
    (root / "tokenizer.json").write_text("{}", encoding="utf-8")


def test_validate_local_embedding_model_dir_reports_missing_files(tmp_path: Path) -> None:
    assert validate_local_embedding_model_dir(str(tmp_path)) == [
        "onnx/model_quantized.onnx",
        "onnx/model_quantized.onnx_data",
        "tokenizer.json",
    ]

    _write_embeddinggemma_files(tmp_path)

    assert validate_local_embedding_model_dir(str(tmp_path)) == []


def test_local_embedding_model_file_only_serves_embeddinggemma_files(tmp_path: Path) -> None:
    _write_embeddinggemma_files(tmp_path)

    assert local_embedding_model_file(
        repo_id=EMBEDDINGGEMMA_REPO_ID,
        filename="model_quantized.onnx",
        subfolder="onnx",
        embedding_model="embeddinggemma",
        model_dir=str(tmp_path),
    ) == tmp_path / "onnx" / "model_quantized.onnx"
    assert local_embedding_model_file(
        repo_id=EMBEDDINGGEMMA_REPO_ID,
        filename="tokenizer.json",
        subfolder=None,
        embedding_model="embeddinggemma",
        model_dir=str(tmp_path),
    ) == tmp_path / "tokenizer.json"
    assert local_embedding_model_file(
        repo_id=EMBEDDINGGEMMA_REPO_ID,
        filename="other.bin",
        subfolder="onnx",
        embedding_model="embeddinggemma",
        model_dir=str(tmp_path),
    ) is None
    assert local_embedding_model_file(
        repo_id=EMBEDDINGGEMMA_REPO_ID,
        filename="model_quantized.onnx",
        subfolder="onnx",
        embedding_model="minilm",
        model_dir=str(tmp_path),
    ) is None


def test_apply_local_embedding_model_dir_bridges_hf_download(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_embeddinggemma_files(tmp_path)
    remote_calls: list[tuple[str, str]] = []

    def original_download(repo_id: str, filename: str, *args, **kwargs) -> str:
        del args, kwargs
        remote_calls.append((repo_id, filename))
        return f"remote:{repo_id}/{filename}"

    fake_hub = SimpleNamespace(hf_hub_download=original_download)
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "embeddinggemma")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL_DIR", str(tmp_path))

    assert apply_local_embedding_model_dir_from_env() is True
    assert fake_hub.hf_hub_download(
        EMBEDDINGGEMMA_REPO_ID,
        "model_quantized.onnx",
        subfolder="onnx",
    ) == str(tmp_path / "onnx" / "model_quantized.onnx")
    assert fake_hub.hf_hub_download("other/repo", "weights.bin") == (
        "remote:other/repo/weights.bin"
    )
    assert remote_calls == [("other/repo", "weights.bin")]
    assert fake_hub._eidolon_original_hf_hub_download is original_download
