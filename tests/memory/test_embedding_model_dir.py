"""The one embedder whose files we cannot resolve ourselves.

Our own encoder reads ``embedding.model_dir`` directly. MemPalace's resolve theirs
inside code we do not own, through ``huggingface_hub.hf_hub_download``, with no
argument or setting that redirects it — so for those the download function is
replaced process-wide.

These tests are therefore as much about where the bridge is *not* installed as
where it is. It used to be installed for every configured model including ours,
which meant the default configuration paid a process-global mutation for nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from eidolon.memory.infrastructure.embedding_model_dir import (
    EMBEDDINGGEMMA_REPO_ID,
    apply_mempalace_model_dir_bridge_from_env,
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


def _fake_hub(monkeypatch, calls: list[tuple[str, str]]) -> SimpleNamespace:
    def original_download(repo_id: str, filename: str, *args, **kwargs) -> str:
        del args, kwargs
        calls.append((repo_id, filename))
        return f"remote:{repo_id}/{filename}"

    hub = SimpleNamespace(hf_hub_download=original_download)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    return hub


def test_the_bridge_redirects_only_the_configured_model_s_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_embeddinggemma_files(tmp_path)
    remote_calls: list[tuple[str, str]] = []
    fake_hub = _fake_hub(monkeypatch, remote_calls)
    original_download = fake_hub.hf_hub_download
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "embeddinggemma")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL_DIR", str(tmp_path))

    assert apply_mempalace_model_dir_bridge_from_env() is True
    assert fake_hub.hf_hub_download(
        EMBEDDINGGEMMA_REPO_ID,
        "model_quantized.onnx",
        subfolder="onnx",
    ) == str(tmp_path / "onnx" / "model_quantized.onnx")
    assert fake_hub.hf_hub_download("other/repo", "weights.bin") == (
        "remote:other/repo/weights.bin"
    )
    assert remote_calls == [("other/repo", "weights.bin")]
    # The bridge keeps a handle on what it replaced, so a second install is not a
    # patch over a patch.
    assert fake_hub._eidolon_original_hf_hub_download is original_download


def test_our_own_model_installs_no_process_wide_patch(tmp_path: Path, monkeypatch) -> None:
    """The default configuration must not mutate the process at all.

    ``OnnxSentenceEmbedder`` reads ``model_dir`` itself, so replacing
    ``hf_hub_download`` globally would buy nothing and cost a global. Asserted by
    the download function still being the one the module started with — a bridge
    that installed and then happened to pass everything through would look the
    same from the outside otherwise.
    """

    (tmp_path / "onnx").mkdir(parents=True)
    (tmp_path / "onnx" / "model_quantized.onnx").write_bytes(b"onnx")
    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    fake_hub = _fake_hub(monkeypatch, [])
    untouched = fake_hub.hf_hub_download
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "bge-small-zh")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL_DIR", str(tmp_path))

    assert apply_mempalace_model_dir_bridge_from_env() is False
    assert fake_hub.hf_hub_download is untouched


def test_minilm_has_nothing_to_bridge(tmp_path: Path, monkeypatch) -> None:
    """Upstream does not fetch it through ``hf_hub_download`` at all.

    Chroma extracts MiniLM from its own archive into ``~/.cache/chroma``. So a
    directory configured for minilm has no call to intercept, and pretending
    otherwise would report a bridge that never fires.
    """

    fake_hub = _fake_hub(monkeypatch, [])
    untouched = fake_hub.hf_hub_download
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL_DIR", str(tmp_path))

    assert apply_mempalace_model_dir_bridge_from_env() is False
    assert fake_hub.hf_hub_download is untouched


def test_an_incomplete_directory_is_reported_rather_than_half_used(
    tmp_path: Path, monkeypatch
) -> None:
    """Half a model on disk must not become a bridge that resolves some files.

    The remaining ones would fall through to the hub, so the model would load from
    two places — and on a machine without network, from one place and then fail.
    """

    (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
    fake_hub = _fake_hub(monkeypatch, [])
    untouched = fake_hub.hf_hub_download
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "embeddinggemma")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL_DIR", str(tmp_path))

    assert apply_mempalace_model_dir_bridge_from_env() is False
    assert fake_hub.hf_hub_download is untouched
