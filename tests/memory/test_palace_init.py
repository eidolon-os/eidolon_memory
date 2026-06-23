from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from eidolon.memory.infrastructure.palace_init import (
    PalaceInitError,
    _materialize_backend_collection,
)


def test_materialize_backend_timeout_is_palace_init_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["python", "-c", "..."],
            timeout=300.0,
            output="partial stdout",
            stderr="partial stderr",
        )

    monkeypatch.setattr(subprocess, "run", _timeout)

    with pytest.raises(PalaceInitError) as exc:
        _materialize_backend_collection(tmp_path / "palace", backend="chroma")

    assert "materialization timed out" in str(exc.value)
    assert "partial stderr" in str(exc.value)
