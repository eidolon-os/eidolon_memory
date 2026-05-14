"""Load and validate memory service settings from YAML.

进程内对默认配置路径的解析结果做缓存；请通过 :func:`get_memory_settings` 获取。
未设置 ``EIDOLON_MEMORY_SETTINGS_YAML`` 时，优先读取同目录下的 ``memory.default.yaml``
（本地可选，已被 gitignore）；若不存在则回退到包内已提交的 ``memory.bundled.yaml``。
返回的 ``MemorySettings`` 视为只读；若需修改请使用 ``model_copy``，或先调用
:func:`reset_memory_settings_cache` 再改磁盘上的 YAML。显式传入路径的
:func:`load_memory_settings` 不使用该缓存。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

_BUNDLED_SETTINGS_PATH = Path(__file__).resolve().parent / "memory.bundled.yaml"


class WingDefinition(BaseModel):
    id: str
    display_name: str = ""
    description: str = ""
    sort_order: int = 0


class RecallPolicy(BaseModel):
    top_k: int = 5
    timeout_seconds: float = 1.5
    recency_weight: float = 0.35
    filter_taboo_statuses: list[str] = Field(
        default_factory=lambda: ["taboo", "archived"]
    )


class StewardConfig(BaseModel):
    mode: str = "llm"
    prompt_template_path: str = ""
    fallback_to_rules: bool = True
    max_fragments_per_turn: int = 6
    min_importance_to_write: int = 3


class LlmConfig(BaseModel):
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = "EIDOLON_MEMORY_LLM_API_KEY"
    timeout_seconds: float = 20.0
    temperature: float = 0.1


class RuntimeConfig(BaseModel):
    palace_path: str = ""
    fake_backend: bool = False


class NatsConfig(BaseModel):
    url: str = "nats://localhost:4222"
    stream: str = "MEMORY_TURNS"
    subject: str = "agent.memory.conversation.turn"
    durable: str = "eidolon-memory-worker"
    stream_max_age_seconds: int = 86400 * 14


class FilterConfig(BaseModel):
    ignore_smalltalk_regex: str = ""


class MemorySettings(BaseModel):
    """All tunable memory-service parameters: wings, recall, steward, LLM, NATS, paths."""

    wings: list[WingDefinition] = Field(default_factory=list)
    recall: RecallPolicy = Field(default_factory=RecallPolicy)
    steward: StewardConfig = Field(default_factory=StewardConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    nats: NatsConfig = Field(default_factory=NatsConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)

    @field_validator("wings")
    @classmethod
    def _unique_wing_ids(cls, wings: list[WingDefinition]) -> list[WingDefinition]:
        if not wings:
            msg = "memory_settings: at least one wing is required"
            raise ValueError(msg)
        seen: set[str] = set()
        for w in wings:
            if w.id in seen:
                msg = f"memory_settings: duplicate wing id {w.id!r}"
                raise ValueError(msg)
            seen.add(w.id)
        return wings

    def wings_prompt_block(self) -> str:
        lines = []
        for w in sorted(self.wings, key=lambda x: x.sort_order):
            lines.append(f"- **{w.id}** ({w.display_name}): {w.description}")
        return "\n".join(lines)

    def resolve_steward_template(self) -> Path | None:
        raw = (self.steward.prompt_template_path or "").strip()
        if raw:
            p = Path(raw).expanduser()
            return p if p.is_file() else None
        pkg = Path(__file__).resolve().parent / "prompts" / "memory_steward.md"
        return pkg if pkg.is_file() else None

    def render_steward_prompt(self, *, locale: str = "zh") -> str:
        path = self.resolve_steward_template()
        if path is None:
            msg = "steward prompt template not found"
            raise FileNotFoundError(msg)
        template = path.read_text(encoding="utf-8")
        return (
            template.replace("{{ wings_block }}", self.wings_prompt_block())
            .replace("{{ locale }}", locale)
        )


def default_memory_settings_path() -> Path:
    env = os.environ.get("EIDOLON_MEMORY_SETTINGS_YAML", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent / "memory.default.yaml"


_default_settings_cache: MemorySettings | None = None
_default_settings_cache_key: tuple[Path, float] | None = None


def reset_memory_settings_cache() -> None:
    """Clear the in-process cache used by :func:`get_memory_settings` / ``load_memory_settings()``."""
    global _default_settings_cache, _default_settings_cache_key
    _default_settings_cache = None
    _default_settings_cache_key = None


def _effective_default_settings_file() -> Path:
    p = default_memory_settings_path()
    if not p.is_file():
        log.warning("memory_settings_missing_using_bundled_default", path=str(p))
        return _BUNDLED_SETTINGS_PATH
    return p.resolve()


def _read_settings_file(p: Path) -> MemorySettings:
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return MemorySettings.model_validate(raw)


def get_memory_settings() -> MemorySettings:
    """返回默认路径下的 ``MemorySettings``（按路径 + 文件 mtime 缓存，避免重复读盘）。"""
    global _default_settings_cache, _default_settings_cache_key
    p = _effective_default_settings_file()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = -1.0
    key = (p, mtime)
    if _default_settings_cache is not None and _default_settings_cache_key == key:
        return _default_settings_cache
    settings = _read_settings_file(p)
    _default_settings_cache = settings
    _default_settings_cache_key = key
    return settings


def load_memory_settings(path: Path | None = None) -> MemorySettings:
    """Load settings from YAML.

    When ``path`` is ``None``, delegates to :func:`get_memory_settings` (single in-process cache).
    When ``path`` is set, always reads that file from disk (no cache).
    """
    if path is None:
        return get_memory_settings()
    p = path
    if not p.is_file():
        log.warning("memory_settings_missing_using_bundled_default", path=str(p))
        p = _BUNDLED_SETTINGS_PATH
    return _read_settings_file(p)
