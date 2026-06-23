"""Load and validate memory service settings from YAML + config/.env.

进程内对默认配置路径的解析结果做缓存；请通过 :func:`get_memory_settings` 获取。
未设置 ``EIDOLON_MEMORY_SETTINGS_YAML`` 时，只认 ``settings.yaml``（gitignore）。
缺失时启动失败（须先 ``init`` 从 ``settings.example.yaml`` 复制）。
返回的 ``MemorySettings`` 视为只读；若需修改请使用 ``model_copy``，或先调用
:func:`reset_memory_settings_cache` 再改磁盘上的 YAML。显式传入路径的
:func:`load_memory_settings` 不使用该缓存。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from eidolon.memory.domain.wings import CANONICAL_WINGS, WingDefinition
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

_PKG_CONFIG_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PKG_CONFIG_DIR.parents[2]
_CONFIG_DIR = _REPO_ROOT / "config"
_DEFAULT_SETTINGS_PATH = _CONFIG_DIR / "settings.yaml"
_DEFAULT_ENV_PATH = _CONFIG_DIR / ".env"
_SHIPPED_EXAMPLE_SETTINGS_PATH = _CONFIG_DIR / "settings.example.yaml"


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
    # Phase 1 — BM25 + cosine RRF rerank. Toggle off here for zero-cost rollback
    # if the new sparse signal ever misbehaves on production traffic.
    rerank_enabled: bool = True
    rerank_rrf_k: int = 60
    # Phase 4 — Wing_Theme drawers surface additively (don't compete with
    # vector top_k). 0 disables the [主题] section cleanly. Capped low so
    # they don't crowd out concrete fragments in the rendered context.
    theme_top_k: int = 3
    # Phase 4.1 — only surface a theme when it's genuinely relevant to the
    # query. Themes are broad summaries; fetched unconditionally they leak
    # onto out-of-scope queries (e.g. a pet theme on "我家鸟会说话吗"),
    # measured as a -20pp hit on the negative category. Drop themes whose
    # cosine similarity is below this floor. 0.0 disables the floor.
    theme_min_similarity: float = 0.55


class StewardConfig(BaseModel):
    mode: str = "llm"
    prompt_template_path: str = ""
    fallback_to_rules: bool = True
    max_fragments_per_turn: int = 6
    min_importance_to_write: int = 3


class LlmConfig(BaseModel):
    """LLM provider configuration.

    ``api_key`` in yaml is the env-var-name placeholder (default
    ``EIDOLON_MEMORY_LLM_API_KEY``); value comes from config/.env.
    """

    model: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = "EIDOLON_MEMORY_LLM_API_KEY"
    timeout_seconds: float = 20.0
    temperature: float = 0.1

    @model_validator(mode="before")
    @classmethod
    def _normalize_api_key_placeholder(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        val = (data.get("api_key") or "").strip()
        env_name = (data.get("api_key_env") or "EIDOLON_MEMORY_LLM_API_KEY").strip()
        if val and val != env_name:
            raise ValueError(
                "llm.api_key must be empty or the placeholder "
                f"{env_name}; set that env var in config/.env"
            )
        if val == env_name:
            data.setdefault("api_key_env", env_name)
        data.pop("api_key", None)
        return data

    def resolve_api_key(self) -> str:
        env = (self.api_key_env or "").strip()
        if not env:
            return ""
        return os.environ.get(env, "").strip()


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
    # Phase 2 — in-memory short-term continuity ring. 0 disables; reasonable
    # values are 5-20. Each turn is ~1-2KB so even maxlen=20 is <40KB per user.
    working_memory_maxlen: int = 10


class ChromadbConfig(BaseModel):
    """SQLite/Chroma persistence tuning (D3 半写防护)."""

    synchronous: str = "FULL"  # FULL = fsync each commit; chroma write +30% latency, safer


class MempalaceBackendConfig(BaseModel):
    """MemPalace storage backend selection.

    Chroma remains the default. Qdrant can be enabled during development with:
    ``mempalace.backend=qdrant`` plus the local Qdrant URL/namespace below.
    ``embedding_model`` is intentionally blank by default so existing palaces
    keep the embedder they were built with; set ``embeddinggemma`` only after
    rebuilding indexes for existing palace data.
    """

    backend: str = "chroma"
    embedding_model: str = ""
    embedding_device: str = ""
    embedding_model_dir: str = ""
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_namespace: str = "eidolon"
    qdrant_timeout_seconds: float = 10.0
    qdrant_api_key_env: str = "MEMPALACE_QDRANT_API_KEY"

    def resolve_qdrant_api_key(self) -> str:
        env = (self.qdrant_api_key_env or "").strip()
        if not env:
            return ""
        return os.environ.get(env, "").strip()


class WorkerConfig(BaseModel):
    """In-process NATS subscriber + steward (no longer a standalone process in D1)."""

    sync_every_n_turns: int = 5  # PASSIVE checkpoint cadence (D3)


class KgConfig(BaseModel):
    """Knowledge graph runtime tuning (T2/T3)."""

    min_confidence_to_write: float = 0.6
    """Steward-extracted triples below this confidence get dropped before write."""


class SupervisorConfig(BaseModel):
    """Multi-user agent_runner process supervisor."""

    # Admin owns the user registry. Memory reads /api/users and only executes
    # the enabled/runtime state.
    admin_api_url: str = ""
    admin_api_timeout_seconds: float = 5.0
    eager_init: bool = True  # on startup, mempalace init each enabled user (parallel<=4)
    restart_backoff_seconds: list[int] = Field(
        default_factory=lambda: [1, 2, 4, 8, 30]
    )
    max_failures_per_minute: int = 5  # disable user beyond this rate
    # Admin HTTP control surface bound inside the supervisor process. Admin
    # talks to this to create/delete users without going through SIGHUP. The
    # surface is admin-only, never exposed to agent_runner or end users.
    admin_http_host: str = "127.0.0.1"
    admin_http_port: int = 8019


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

    ``bearer_token`` in yaml is the env-var-name placeholder (default
    ``EIDOLON_MEMORY_MCP_TOKEN``); value from config/.env.
    """

    host: str = "127.0.0.1"
    port: int = 8030  # only used if CLI --port unset
    path: str = "/mcp"
    stateless_http: bool = False
    bearer_token: str = ""
    bearer_token_env: str = "EIDOLON_MEMORY_MCP_TOKEN"

    @model_validator(mode="before")
    @classmethod
    def _normalize_bearer_token_placeholder(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        val = (data.get("bearer_token") or "").strip()
        env_name = (data.get("bearer_token_env") or "EIDOLON_MEMORY_MCP_TOKEN").strip()
        if val and val != env_name:
            raise ValueError(
                "mcp_http.bearer_token must be empty or the placeholder "
                f"{env_name}; set that env var in config/.env"
            )
        if val == env_name:
            data.setdefault("bearer_token_env", env_name)
        data.pop("bearer_token", None)
        return data

    def base_url(self, *, port: int | None = None) -> str:
        path = self.path if self.path.startswith("/") else f"/{self.path}"
        effective_port = port if port is not None else self.port
        return f"http://{self.host}:{effective_port}{path}"

    def resolve_bearer_token(self) -> str:
        env = (self.bearer_token_env or "").strip()
        if not env:
            return ""
        return os.environ.get(env, "").strip()

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
    """Memory service settings — deployment-tunable fields only.

    Product constants (wing taxonomy, NATS protocol fields, default tunables)
    live in code:
    - :mod:`eidolon.memory.domain.wings`  for the wing schema
    - default values on the sub-config classes for everything else
    """

    recall: RecallPolicy = Field(default_factory=RecallPolicy)
    steward: StewardConfig = Field(default_factory=StewardConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    chromadb: ChromadbConfig = Field(default_factory=ChromadbConfig)
    mempalace: MempalaceBackendConfig = Field(default_factory=MempalaceBackendConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    kg: KgConfig = Field(default_factory=KgConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    nats: NatsConfig = Field(default_factory=NatsConfig)
    mcp_http: McpHttpConfig = Field(default_factory=McpHttpConfig)
    discovery_http: DiscoveryHttpConfig = Field(default_factory=DiscoveryHttpConfig)

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_wings(cls, data: Any) -> Any:
        """Older settings yaml carried a ``wings:`` section. The wing
        taxonomy is now a product contract in :data:`CANONICAL_WINGS` — silently
        drop the yaml field with a log so users can clean their config at
        leisure. Never raise: backward compat for existing local yaml.
        """
        if isinstance(data, dict) and "wings" in data:
            log.warning(
                "memory_settings_wings_deprecated",
                hint=(
                    "yaml 'wings' is ignored — see eidolon.memory.domain.wings."
                    "CANONICAL_WINGS. Remove the section from your yaml."
                ),
            )
            data = {k: v for k, v in data.items() if k != "wings"}
        return data

    @property
    def wings(self) -> list[WingDefinition]:
        """Canonical wing list — product contract, not configurable."""
        return list(CANONICAL_WINGS)

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


def _bootstrap_dotenv() -> None:
    env_file = os.environ.get("EIDOLON_MEMORY_ENV_FILE", "").strip()
    if env_file:
        path = Path(env_file).expanduser()
    else:
        path = _DEFAULT_ENV_PATH
    if not path.is_file():
        raise FileNotFoundError(
            f"memory env file not found: {path}. "
            f"Copy config/.env.example to config/.env and set secrets."
        )
    from dotenv import load_dotenv

    load_dotenv(path, override=False)


def default_memory_settings_path() -> Path:
    env = os.environ.get("EIDOLON_MEMORY_SETTINGS_YAML", "").strip()
    if env:
        p = Path(env).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"EIDOLON_MEMORY_SETTINGS_YAML missing: {p}")
        return p.resolve()
    if _DEFAULT_SETTINGS_PATH.is_file():
        return _DEFAULT_SETTINGS_PATH.resolve()
    raise FileNotFoundError(
        f"memory settings not found: {_DEFAULT_SETTINGS_PATH}. "
        "Copy config/settings.example.yaml to config/settings.yaml."
    )


_default_settings_cache: MemorySettings | None = None
_default_settings_cache_key: tuple[Path, float] | None = None


def reset_memory_settings_cache() -> None:
    """Clear the in-process cache used by settings loader helpers."""
    global _default_settings_cache, _default_settings_cache_key
    _default_settings_cache = None
    _default_settings_cache_key = None


def _effective_default_settings_file() -> Path:
    return default_memory_settings_path()


def _read_settings_file(p: Path) -> MemorySettings:
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return MemorySettings.model_validate(raw)


def get_memory_settings() -> MemorySettings:
    """返回默认路径下的 ``MemorySettings``（按路径 + 文件 mtime 缓存，避免重复读盘）。"""
    global _default_settings_cache, _default_settings_cache_key
    _bootstrap_dotenv()
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
    """Load settings from YAML (and bootstrap config/.env unless path is explicit test fixture).

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
