"""Memory settings YAML loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from eidolon.memory.config.memory_settings import (
    get_memory_settings,
    load_memory_settings,
    reset_memory_settings_cache,
)


def test_load_default_memory_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    settings = load_memory_settings()
    assert len(settings.wings) >= 1
    assert settings.nats.conversation_turn_subject_base
    assert settings.command_status.retention_days == 30
    assert settings.command_status.max_records == 100_000
    assert settings.runtime.read.normal_shared_query_embedding is True


def test_command_status_retention_limits_are_positive(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, {"command_status": {"max_records": 0}})

    with pytest.raises(ValueError, match="max_records"):
        load_memory_settings(path)


def test_get_memory_settings_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    a = get_memory_settings()
    b = get_memory_settings()
    assert a is b


def test_process_managed_environment_does_not_reopen_root_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_yaml(tmp_path, {})
    monkeypatch.setenv("EIDOLON_MEMORY_SETTINGS_YAML", str(settings_path))
    monkeypatch.setenv("EIDOLON_MEMORY_DOTENV_MODE", "environment")
    monkeypatch.setenv("EIDOLON_MEMORY_ENV_FILE", str(tmp_path / "root-only.env"))
    reset_memory_settings_cache()
    try:
        assert get_memory_settings() is not None
    finally:
        reset_memory_settings_cache()


def test_unknown_dotenv_mode_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EIDOLON_MEMORY_SETTINGS_YAML", str(_write_yaml(tmp_path, {})))
    monkeypatch.setenv("EIDOLON_MEMORY_DOTENV_MODE", "implicit")
    reset_memory_settings_cache()
    try:
        with pytest.raises(ValueError, match="must be file or environment"):
            get_memory_settings()
    finally:
        reset_memory_settings_cache()


def test_yaml_wings_rejected(tmp_path: Path):
    bad_or_stale = {
        "wings": [
            {"id": "W1", "display_name": "old config field"},
        ],
        "nats": {"url": "nats://127.0.0.1:4222"},
    }
    p = tmp_path / "stale.yaml"
    p.write_text(yaml.dump(bad_or_stale), encoding="utf-8")
    with pytest.raises(ValueError, match="wings"):
        load_memory_settings(p)


def test_inline_llm_api_key_rejected(tmp_path: Path):
    p = tmp_path / "secret.yaml"
    p.write_text(
        yaml.safe_dump({"llm": {"api_key": "sk-leaked", "model": "x"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="placeholder"):
        load_memory_settings(p)


def test_inline_bearer_token_rejected(tmp_path: Path):
    p = tmp_path / "tok.yaml"
    p.write_text(
        yaml.safe_dump({"mcp_http": {"bearer_token": "tok-leaked"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="placeholder"):
        load_memory_settings(p)


def test_empty_inline_secrets_are_silently_dropped(tmp_path: Path):
    """Backward compat: empty secret fields still load."""
    p = tmp_path / "ok.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "llm": {"api_key": "", "model": "m"},
                "mcp_http": {"bearer_token": "", "port": 8030},
            }
        ),
        encoding="utf-8",
    )
    settings = load_memory_settings(p)
    assert settings.llm.model == "m"
    assert settings.mcp_http.port == 8030


def test_env_name_placeholders_load(tmp_path: Path):
    p = tmp_path / "ph.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "api_key": "EIDOLON_MEMORY_LLM_API_KEY",
                    "api_key_env": "EIDOLON_MEMORY_LLM_API_KEY",
                    "model": "m",
                },
                "mcp_http": {
                    "bearer_token": "EIDOLON_MEMORY_MCP_TOKEN",
                    "bearer_token_env": "EIDOLON_MEMORY_MCP_TOKEN",
                    "port": 8030,
                },
            }
        ),
        encoding="utf-8",
    )
    settings = load_memory_settings(p)
    assert settings.llm.api_key_env == "EIDOLON_MEMORY_LLM_API_KEY"
    assert settings.mcp_http.bearer_token_env == "EIDOLON_MEMORY_MCP_TOKEN"


def test_render_steward_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EIDOLON_MEMORY_SETTINGS_YAML", raising=False)
    settings = load_memory_settings()
    text = settings.render_steward_prompt()
    assert "Wing_" in text or "wing" in text.lower()


def test_resolve_log_dir_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from eidolon.memory.config.memory_settings import (
        resolve_dlq_log_path,
        resolve_log_dir,
        resolve_run_dir,
    )

    monkeypatch.delenv("EIDOLON_MEMORY_LOG_DIR", raising=False)
    monkeypatch.delenv("EIDOLON_MEMORY_RUN_DIR", raising=False)
    settings = load_memory_settings()
    assert resolve_log_dir(settings) == (Path.home() / "eidolon" / "logs" / "memory").resolve()
    assert (
        resolve_dlq_log_path(settings)
        == (Path.home() / "eidolon" / "logs" / "memory" / "memory_dlq.jsonl").resolve()
    )
    assert resolve_run_dir(settings) == (Path.home() / "eidolon" / "run" / "memory").resolve()


def test_resolve_log_dir_env_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from eidolon.memory.config.memory_settings import resolve_log_dir, resolve_run_dir

    p = _write_yaml(
        tmp_path,
        {
            "runtime": {"log_dir": "/tmp/cfg-logs", "run_dir": "/tmp/cfg-run"},
        },
    )
    settings = load_memory_settings(p)
    # config wins over default
    assert resolve_log_dir(settings) == Path("/tmp/cfg-logs").resolve()
    assert resolve_run_dir(settings) == Path("/tmp/cfg-run").resolve()
    # env wins over config
    monkeypatch.setenv("EIDOLON_MEMORY_LOG_DIR", "/tmp/env-logs")
    monkeypatch.setenv("EIDOLON_MEMORY_RUN_DIR", "/tmp/env-run")
    assert resolve_log_dir(settings) == Path("/tmp/env-logs").resolve()
    assert resolve_run_dir(settings) == Path("/tmp/env-run").resolve()


def test_resolve_dlq_log_path_is_relative_to_memory_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from eidolon.memory.config.memory_settings import resolve_dlq_log_path

    monkeypatch.delenv("EIDOLON_MEMORY_LOG_DIR", raising=False)
    settings = load_memory_settings(
        _write_yaml(
            tmp_path,
            {
                "runtime": {"log_dir": str(tmp_path / "memory-logs")},
                "nats": {"dlq_log_path": "dlq/custom.jsonl"},
            },
        )
    )
    assert resolve_dlq_log_path(settings) == (tmp_path / "memory-logs" / "dlq/custom.jsonl")


def _write_yaml(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_the_host_may_choose_the_encoder_without_editing_the_checkout(
    tmp_path, monkeypatch
) -> None:
    """A laptop and a Pi 5 run different encoders; that is a Host property.

    Before this, a deployer rewrote the shipped settings file by string
    substitution, which broke the moment the file changed at all.
    """

    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(
        "embedding:\n  provider: local\n  model: bge-large-zh\n", encoding="utf-8"
    )
    monkeypatch.setenv("EIDOLON_MEMORY_EMBEDDING_MODEL", "bge-base-zh")

    settings = load_memory_settings(settings_file)

    assert settings.embedding.model == "bge-base-zh"


def test_the_host_may_say_where_that_encoder_lives(tmp_path, monkeypatch) -> None:
    """Naming the encoder is not enough if its weights cannot be found.

    A deployment carried the files to the Host and set this variable; the Host
    env carried it; and it stopped here, because the table above did not list
    it. ``model_dir`` stayed empty, so the embedder asked the model hub for
    weights already on disk — and a Host with no route out spent seventy
    seconds per query retrying before reporting that it had found nothing.
    """

    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(
        "embedding:\n  provider: local\n  model: bge-base-zh\n", encoding="utf-8"
    )
    monkeypatch.setenv(
        "EIDOLON_MEMORY_EMBEDDING_MODEL_DIR", "/var/lib/eidolon/models/bge-base-zh"
    )

    settings = load_memory_settings(settings_file)

    assert settings.embedding.model_dir == "/var/lib/eidolon/models/bge-base-zh"
    # And it reaches the children, which read a different name entirely.
    from eidolon.memory.infrastructure.mempalace_backend import mempalace_backend_env

    environment = mempalace_backend_env(settings, base={})
    assert environment["MEMPALACE_EMBEDDING_MODEL_DIR"] == (
        "/var/lib/eidolon/models/bge-base-zh"
    )


def test_an_override_cannot_name_an_encoder_nobody_implements(tmp_path, monkeypatch) -> None:
    """The override is applied before validation, so it earns the same refusal.

    MemPalace answers an unknown name with an English-only model rather than an
    error, so a typo would quietly build the palace with the worst retriever.
    """

    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(
        "embedding:\n  provider: local\n  model: bge-large-zh\n", encoding="utf-8"
    )
    monkeypatch.setenv("EIDOLON_MEMORY_EMBEDDING_MODEL", "not-a-real-encoder")

    with pytest.raises(ValidationError):
        load_memory_settings(settings_file)


def test_settings_are_unchanged_when_no_override_is_present(tmp_path, monkeypatch) -> None:
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(
        "embedding:\n  provider: local\n  model: bge-large-zh\n", encoding="utf-8"
    )
    monkeypatch.delenv("EIDOLON_MEMORY_EMBEDDING_MODEL", raising=False)

    assert load_memory_settings(settings_file).embedding.model == "bge-large-zh"
