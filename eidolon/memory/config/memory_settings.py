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
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    filter_taboo_statuses: list[str] = Field(default_factory=lambda: ["taboo", "archived"])
    voice_wings: list[str] = Field(default_factory=list)
    exclude_recent_minutes: int = 10
    exclude_current_session: bool = True
    # KG plan §5.6
    kg_in_recall: bool = True
    # Voice budget for the graph lookup. Tight because it runs alongside the
    # vector search inside LiveKit's 300ms deadline, and the vector result alone
    # is a usable answer — a slow graph is dropped, not waited for.
    kg_timeout_seconds: float = 0.05
    # The same budget off the voice path. Chat has more room than voice but is
    # still on the critical path of a reply, so this is far below the second that
    # used to be hardcoded here.
    kg_timeout_seconds_normal: float = 0.3
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
    # Extraction is a replayable decision, not creative generation.
    temperature: float = 0.0

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
    # One embedding + one filtered collection query under the same Realm lock.
    # Keep configurable as a rollback switch for backend-specific quality issues.
    normal_shared_query_embedding: bool = True
    voice_skip_closets: bool = True


class RuntimeConfig(BaseModel):
    """D1: per-user palace path resolves via ``resolve_palace_for_user(user_id)``.

    Runtime artefacts (palaces, logs, PID files) live under directories that
    can be relocated via these settings or overridden by environment variables
    so the repo only ships code.
    """

    palaces_root: str = ""  # default ~/eidolon/memory/mempalaces; env EIDOLON_MEMORY_PALACES_ROOT
    # Per-Realm SQLite/Chroma temporary files. Empty keeps them beside the
    # Palace root under ``.process-tmp``; env EIDOLON_MEMORY_PROCESS_TMP_ROOT
    # wins. The supervisor activates this before the child imports Chroma.
    process_tmp_root: str = ""
    log_dir: str = ""  # default ~/eidolon/logs/memory; env EIDOLON_MEMORY_LOG_DIR
    run_dir: str = ""  # default ~/eidolon/run;    env EIDOLON_MEMORY_RUN_DIR
    read: ReadRuntimeConfig = Field(default_factory=ReadRuntimeConfig)
    # Phase 2 — in-memory short-term continuity ring. 0 disables; reasonable
    # values are 5-20. Each turn is ~1-2KB so even maxlen=20 is <40KB per user.
    working_memory_maxlen: int = 10


class ChromadbConfig(BaseModel):
    """Legacy compatibility surface; Chroma now owns its SQLite pragmas."""

    # Retained so existing YAML still parses. Do not apply this through an
    # external sqlite3 connection; Chroma 1.5.9 manages its own synchronous
    # and journal settings.
    synchronous: str = "FULL"


class MempalaceBackendConfig(BaseModel):
    """Vector storage selection.

    Two backends, one per deployment shape. ``chroma`` keeps vectors in a file
    inside the palace directory and is the local default. ``milvus`` talks to a
    Milvus server or Zilliz Cloud, which is what a deployment serving spaces from
    more than one host needs. Switching is a config change and nothing else.

    Backends MemPalace also offers are deliberately not exposed. ``sqlite_exact``
    scans every row in Python on each query — correct, but its latency grows with
    the collection, which the voice path cannot absorb. ``qdrant`` and
    ``pgvector`` are simply not shapes we run.

    ``embedding_model`` is blank by default so an existing palace keeps the
    embedder it was built with; MemPalace refuses to open a palace with a
    different one, and changing it means rebuilding the index.
    """

    backend: str = "chroma"
    embedding_model: str = ""
    embedding_device: str = ""
    embedding_model_dir: str = ""
    embedding_threads: int = Field(default=0, ge=0)

    # Milvus. An empty uri means Milvus Lite against a file in the palace
    # directory, which is useful for tests but is not the cloud shape — a server
    # deployment must set this.
    milvus_uri: str = ""
    milvus_token_env: str = "EIDOLON_MEMORY_MILVUS_TOKEN"
    # Milvus databases are hard tenancy boundaries; naming one keeps this
    # deployment's collections out of every other database on the instance.
    milvus_db_name: str = ""
    # Prefixes collection names, so several deployments can share a database.
    milvus_namespace: str = "eidolon"

    # Tests and benchmarks only. Substitutes a tiny hash-based vector for the
    # real embedder so a test can exercise the actual storage adapter without
    # loading a 300MB model. Recall ranking is meaningless under it — never set
    # this in a deployment.
    offline_embedding: bool = False

    @model_validator(mode="after")
    def _server_deployment_names_its_database(self) -> MempalaceBackendConfig:
        """A remote Milvus must say which database to use.

        Without one, MemPalace creates collections in the instance's default
        database — mixing this deployment's data into whatever else lives there.
        Refusing at config load is much better than discovering it later.
        """

        if self.backend.strip().lower() == "milvus" and self.milvus_uri.strip():
            if not self.milvus_db_name.strip():
                raise ValueError(
                    "mempalace.milvus_db_name is required when milvus_uri is set, so "
                    "collections are confined to a named database"
                )
        return self

    def resolve_milvus_token(self) -> str:
        env = (self.milvus_token_env or "").strip()
        if not env:
            return ""
        return os.environ.get(env, "").strip()


class WorkerConfig(BaseModel):
    """In-process NATS subscriber + steward (no longer a standalone process in D1)."""

    sync_every_n_turns: int = 5  # PASSIVE checkpoint cadence (D3)


class CommandStatusConfig(BaseModel):
    """Bound the asynchronous command projection over multi-year runtimes."""

    retention_days: int = Field(default=30, ge=1)
    max_records: int = Field(default=100_000, ge=100)
    prune_every_writes: int = Field(default=100, ge=1)


class LedgerStorageConfig(BaseModel):
    """Where a space's append-only records live.

    Six of them: extraction decisions, canonical facts, commitments, command
    status, the dead-letter queue, and device sync. Two are product behaviour
    rather than bookkeeping — canonical facts carry the invalidation chain that
    makes a corrected fact stop being recalled, and commitments are what the
    service answers commitment queries from.

    ``palace`` keeps them as SQLite files beside the memories, which needs a
    single owning process. ``postgres`` puts them in a shared database so any
    replica can serve any space.

    Configured separately from ``kg`` even though both would point at the same
    database: the graph is optional at runtime, and reading the ledgers' location
    out of an optional section would mean turning the graph off took the ledgers
    with it.
    """

    backend: Literal["palace", "postgres"] = "palace"
    postgres_dsn_env: str = "EIDOLON_MEMORY_LEDGER_PG_DSN"

    def resolve_postgres_dsn(self) -> str:
        env = (self.postgres_dsn_env or "").strip()
        if not env:
            return ""
        return os.environ.get(env, "").strip()

    @model_validator(mode="after")
    def _postgres_needs_a_dsn_source(self) -> LedgerStorageConfig:
        if self.backend == "postgres" and not (self.postgres_dsn_env or "").strip():
            raise ValueError(
                "ledgers.postgres_dsn_env must name the environment variable "
                "holding the connection string when ledgers.backend is 'postgres'"
            )
        return self


class KgConfig(BaseModel):
    """Knowledge graph storage and tuning.

    The graph is optional at runtime. With ``backend="none"`` the service runs
    on vector recall alone: no graph is opened, graph tools are not offered, and
    a command that would write to one is answered honestly rather than hanging.
    Turning it back on is a config change; nothing is deleted when it is off.

    ``sqlite`` keeps the graph in the palace directory. ``postgres`` puts it in a
    shared database, which is what a deployment spanning hosts needs — the graph
    is the one part of a palace that cannot live on local disk in that shape.
    """

    backend: Literal["none", "sqlite", "postgres"] = "sqlite"
    postgres_dsn_env: str = "EIDOLON_MEMORY_KG_PG_DSN"

    min_confidence_to_write: float = 0.6
    """Steward-extracted triples below this confidence get dropped before write."""

    @property
    def enabled(self) -> bool:
        return self.backend != "none"

    def resolve_postgres_dsn(self) -> str:
        env = (self.postgres_dsn_env or "").strip()
        if not env:
            return ""
        return os.environ.get(env, "").strip()

    @model_validator(mode="after")
    def _postgres_needs_a_dsn_source(self) -> KgConfig:
        if self.backend == "postgres" and not (self.postgres_dsn_env or "").strip():
            raise ValueError(
                "kg.postgres_dsn_env must name the environment variable holding "
                "the connection string when kg.backend is 'postgres'"
            )
        return self


class SupervisorConfig(BaseModel):
    """Multi-user agent_runner process supervisor."""

    # Admin owns the user registry. Memory reads /api/users and only executes
    # the enabled/runtime state.
    admin_api_url: str = ""
    admin_api_timeout_seconds: float = 5.0
    eager_init: bool = True  # on startup, mempalace init each enabled user (parallel<=4)
    restart_backoff_seconds: list[int] = Field(default_factory=lambda: [1, 2, 4, 8, 30])
    max_failures_per_minute: int = 5  # disable user beyond this rate
    # Admin HTTP control surface bound inside the supervisor process. Admin
    # talks to this to create/delete users without going through SIGHUP. The
    # surface is admin-only, never exposed to agent_runner or end users.
    admin_http_host: str = "127.0.0.1"
    admin_http_port: int = 8019


class RegistryConfig(BaseModel):
    """Where the list of memory spaces to serve comes from.

    ``eidolon-admin`` asks an Eidolon OS admin service over HTTP, which is right
    when the service runs inside the OS and realms are created there.

    ``static`` reads a roster from a YAML file. This is what makes a standalone
    deployment possible: no admin service to stand up, and the operator declares
    the spaces directly. The file is re-read on reload, so entries can be added
    without a config change.
    """

    source: Literal["eidolon-admin", "static"] = "eidolon-admin"
    # Path to the roster for source="static". Relative paths resolve against the
    # settings file's directory.
    static_path: str = ""


class NatsConfig(BaseModel):
    url: str = "nats://localhost:4222"
    stream: str = "MEMORY_TURNS"
    # NATS subject = <base>.<memory_space_token>. The SDK helpers own token
    # encoding; this default is kept only for the discovery advertiser and must
    # track ``MEMORY_CONVERSATION_TURN_BASE``.
    conversation_turn_subject_base: str = "eidolon.memory.turn"
    durable_prefix: str = "eidolon-memory-agent"
    stream_max_age_seconds: int = 86400 * 14
    stream_max_msgs: int = 5000
    stream_max_bytes: int = 536_870_912
    worker_max_deliveries: int = 3
    dlq_log_path: str = "memory_dlq.jsonl"


class McpHttpConfig(BaseModel):
    """Control-plane MCP transport per agent runner.

    D1: each user has their own port; agent_runner CLI ``--port`` always wins.
    Fields here are defaults / dev-mode single-user convenience.

    ``bearer_token`` in yaml is the env-var-name placeholder (default
    ``EIDOLON_MEMORY_MCP_TOKEN``); value from config/.env.
    """

    host: str = "127.0.0.1"
    port: int = 10030  # only used if CLI --port unset
    path: str = "/mcp"
    stateless_http: bool = True
    json_response: bool = True
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

    model_config = ConfigDict(extra="forbid")

    recall: RecallPolicy = Field(default_factory=RecallPolicy)
    steward: StewardConfig = Field(default_factory=StewardConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    chromadb: ChromadbConfig = Field(default_factory=ChromadbConfig)
    mempalace: MempalaceBackendConfig = Field(default_factory=MempalaceBackendConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    command_status: CommandStatusConfig = Field(default_factory=CommandStatusConfig)
    kg: KgConfig = Field(default_factory=KgConfig)
    ledgers: LedgerStorageConfig = Field(default_factory=LedgerStorageConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    nats: NatsConfig = Field(default_factory=NatsConfig)
    mcp_http: McpHttpConfig = Field(default_factory=McpHttpConfig)
    discovery_http: DiscoveryHttpConfig = Field(default_factory=DiscoveryHttpConfig)

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
        return template.replace("{{ wings_block }}", self.wings_prompt_block()).replace(
            "{{ locale }}", locale
        )


def resolve_log_dir(settings: MemorySettings) -> Path:
    """Resolve runtime log directory. Env > config > ``~/eidolon/logs/memory``."""
    env = os.environ.get("EIDOLON_MEMORY_LOG_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    configured = (settings.runtime.log_dir or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "eidolon" / "logs" / "memory").resolve()


def resolve_dlq_log_path(settings: MemorySettings) -> Path:
    """Resolve the DLQ JSONL path, anchoring relative paths under memory logs."""
    configured = (settings.nats.dlq_log_path or "").strip() or "memory_dlq.jsonl"
    path = Path(configured).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (resolve_log_dir(settings) / path).resolve()


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
