"""Load and validate memory service settings from YAML.

进程内对默认配置路径的解析结果做缓存；请通过 :func:`get_memory_settings` 获取。
未设置 ``EIDOLON_MEMORY_SETTINGS_YAML`` 时，只认**本地一份** ``memory.default.yaml``（gitignore，
不提交）。若该文件尚不存在，则读取同目录已提交的 ``memory.default.yaml.example`` 作为模板。
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

_DEFAULT_LOCAL_SETTINGS_PATH = Path(__file__).resolve().parent / "memory.default.yaml"
_SHIPPED_EXAMPLE_SETTINGS_PATH = Path(__file__).resolve().parent / "memory.default.yaml.example"


class WingDefinition(BaseModel):
    id: str
    display_name: str = ""
    description: str = ""
    sort_order: int = 0


class RecallPolicy(BaseModel):
    top_k: int = 5
    livekit_timeout_seconds: float = 0.3
    filter_taboo_statuses: list[str] = Field(
        default_factory=lambda: ["taboo", "archived"]
    )
    voice_wings: list[str] = Field(default_factory=list)
    exclude_recent_minutes: int = 10
    exclude_current_session: bool = True
    # KG plan §5.6
    kg_in_recall: bool = True
    kg_timeout_seconds: float = 0.05
    kg_window_days: int = 30
    kg_max_entities: int = 3
    kg_max_triples_per_entity: int = 8


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


class ReadRuntimeConfig(BaseModel):
    """D1: simplified — no cross-process double-buffering (single process per palace)."""

    max_wing_parallel: int = 0  # 0 = auto (see cpu_env.recommend_max_wing_parallel)
    omp_num_threads: int = 0  # 0 = auto from CPU
    shared_query_embedding: bool = True
    voice_skip_closets: bool = True


class RuntimeConfig(BaseModel):
    """D1: per-user palace path resolves via ``resolve_palace_for_user(user_id)``.

    Runtime artefacts (palaces, logs, PID files) live under directories that
    can be relocated via these settings or overridden by environment variables
    so the repo only ships code.
    """

    palaces_root: str = ""  # default ~/eidolon/palaces; env EIDOLON_MEMORY_PALACES_ROOT
    log_dir: str = ""       # default ~/eidolon/logs;   env EIDOLON_MEMORY_LOG_DIR
    run_dir: str = ""       # default ~/eidolon/run;    env EIDOLON_MEMORY_RUN_DIR
    read: ReadRuntimeConfig = Field(default_factory=ReadRuntimeConfig)


class ChromadbConfig(BaseModel):
    """SQLite/Chroma persistence tuning (D3 半写防护)."""

    synchronous: str = "FULL"  # FULL = fsync each commit; chroma write +30% latency, safer


class WorkerConfig(BaseModel):
    """In-process NATS subscriber + steward (no longer a standalone process in D1)."""

    sync_every_n_turns: int = 5  # PASSIVE checkpoint cadence (D3)


class KgConfig(BaseModel):
    """Knowledge graph runtime tuning (T2/T3)."""

    min_confidence_to_write: float = 0.6
    """Steward-extracted triples below this confidence get dropped before write."""


class SupervisorConfig(BaseModel):
    """Multi-user agent_runner process supervisor."""

    users_file: str = ""  # path to users.yaml; env override: EIDOLON_MEMORY_USERS_YAML
    eager_init: bool = True  # on startup, mempalace init each enabled user (parallel<=4)
    restart_backoff_seconds: list[int] = Field(
        default_factory=lambda: [1, 2, 4, 8, 30]
    )
    max_failures_per_minute: int = 5  # disable user beyond this rate


class NatsConfig(BaseModel):
    url: str = "nats://localhost:4222"
    stream: str = "MEMORY_TURNS"
    # D1: NATS subject = <base>.<user_id>; ``conversation_turn_subject_base`` is the prefix
    conversation_turn_subject_base: str = "agent.memory.conversation.turn"
    durable_prefix: str = "eidolon-memory-agent"  # per-user durable = <prefix>-<user_id>
    stream_max_age_seconds: int = 86400 * 14
    stream_max_msgs: int = 5000
    stream_max_bytes: int = 536_870_912
    worker_max_deliveries: int = 3
    dlq_log_path: str = "logs/memory_dlq.jsonl"


class McpHttpConfig(BaseModel):
    """Control-plane MCP transport per agent runner.

    D1: each user has their own port; agent_runner CLI ``--port`` always wins.
    Fields here are defaults / dev-mode single-user convenience.
    """

    host: str = "127.0.0.1"
    port: int = 8030  # only used if CLI --port unset
    path: str = "/mcp"
    stateless_http: bool = False
    bearer_token: str = ""
    bearer_token_env: str = "EIDOLON_MEMORY_MCP_TOKEN"

    def base_url(self, *, port: int | None = None) -> str:
        path = self.path if self.path.startswith("/") else f"/{self.path}"
        effective_port = port if port is not None else self.port
        return f"http://{self.host}:{effective_port}{path}"

    def resolve_bearer_token(self) -> str:
        token = (self.bearer_token or "").strip()
        if token:
            return token
        return os.environ.get(self.bearer_token_env, "").strip()

    def auth_headers(self) -> dict[str, str]:
        token = self.resolve_bearer_token()
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}


class DiscoveryHttpConfig(BaseModel):
    """Standalone HTTP discovery endpoint consumed by eidolon-agent."""

    host: str = "127.0.0.1"
    port: int = 8020
    path: str = "/api/discovery/agent-routing"


class MemorySettings(BaseModel):
    """All tunable memory-service parameters: wings, recall, steward, LLM, NATS, paths."""

    wings: list[WingDefinition] = Field(default_factory=list)
    recall: RecallPolicy = Field(default_factory=RecallPolicy)
    steward: StewardConfig = Field(default_factory=StewardConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    chromadb: ChromadbConfig = Field(default_factory=ChromadbConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    kg: KgConfig = Field(default_factory=KgConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    nats: NatsConfig = Field(default_factory=NatsConfig)
    mcp_http: McpHttpConfig = Field(default_factory=McpHttpConfig)
    discovery_http: DiscoveryHttpConfig = Field(default_factory=DiscoveryHttpConfig)

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


def resolve_log_dir(settings: MemorySettings) -> Path:
    """Resolve runtime log directory. Env > config > ``~/eidolon/logs``."""
    env = os.environ.get("EIDOLON_MEMORY_LOG_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    configured = (settings.runtime.log_dir or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "eidolon" / "logs").resolve()


def resolve_run_dir(settings: MemorySettings) -> Path:
    """Resolve PID / lockfile directory. Env > config > ``~/eidolon/run``."""
    env = os.environ.get("EIDOLON_MEMORY_RUN_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    configured = (settings.runtime.run_dir or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "eidolon" / "run").resolve()


def default_memory_settings_path() -> Path:
    env = os.environ.get("EIDOLON_MEMORY_SETTINGS_YAML", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return _DEFAULT_LOCAL_SETTINGS_PATH


_default_settings_cache: MemorySettings | None = None
_default_settings_cache_key: tuple[Path, float] | None = None


def reset_memory_settings_cache() -> None:
    """Clear the in-process cache used by settings loader helpers."""
    global _default_settings_cache, _default_settings_cache_key
    _default_settings_cache = None
    _default_settings_cache_key = None


def _effective_default_settings_file() -> Path:
    p = default_memory_settings_path()
    if not p.is_file():
        log.warning("memory_settings_local_missing_using_example", path=str(p))
        return _SHIPPED_EXAMPLE_SETTINGS_PATH
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
        log.warning("memory_settings_path_missing_using_example", path=str(p))
        p = _SHIPPED_EXAMPLE_SETTINGS_PATH
    return _read_settings_file(p)
