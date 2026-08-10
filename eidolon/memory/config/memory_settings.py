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

from eidolon.memory.domain.embedding_port import (
    LOCAL_EMBEDDING_MODELS,
    MEMPALACE_EMBEDDING_MODELS,
    EmbedderIdentity,
    local_model_spec,
    mempalace_model_identity,
)
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
    # Health predicates in recall. A deployment decision, not a per-request one:
    # the agent's recall tool takes no ``include_sensitive_kg`` argument, because a
    # flag that widens visibility is a capability and the least-trusted caller
    # should not be able to grant itself one. Audience is already derived from the
    # caller's context rather than passed; this is the same rule for the other
    # visibility axis. Operator tools keep an explicit parameter.
    include_sensitive_kg: bool = False
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
    # Sized from measurement, not taste. Against the configured endpoint a
    # trivial call returns in 2.4s while the real steward prompt (8.3k chars)
    # takes 22.9s — so 20 or 30 leaves almost no headroom, ordinary variance
    # trips the timeout, and litellm's retries turn one slow turn into ~93s.
    # Extraction runs on the bus, not in anyone's reply, so waiting longer for a
    # real answer beats retrying three times for none.
    timeout_seconds: float = 90.0
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

    palaces_root: str = ""  # default $EIDOLON_STATE_ROOT/memory/mempalaces
    # Per-Realm SQLite/Chroma temporary files. Empty keeps them beside the
    # Palace root under ``.process-tmp``; env EIDOLON_MEMORY_PROCESS_TMP_ROOT
    # wins. The supervisor activates this before the child imports Chroma.
    process_tmp_root: str = ""
    log_dir: str = ""  # default $EIDOLON_LOG_ROOT/memory
    run_dir: str = ""  # default $EIDOLON_RUNTIME_ROOT/memory
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


#: The old location of each embedder setting, and where it lives now. One table so
#: the fold and the mirror below cannot drift apart, and so adding a fifth setting
#: to ``embedding`` does not accidentally acquire a legacy alias it never had.
_LEGACY_EMBEDDING_KEYS = {
    "embedding_model": "model",
    "embedding_device": "device",
    "embedding_model_dir": "model_dir",
    "embedding_threads": "threads",
}


class HttpEmbeddingConfig(BaseModel):
    """A hosted OpenAI-compatible ``/v1/embeddings`` endpoint.

    Read only when ``embedding.provider`` is ``http``. Everything here is
    something the endpoint knows and we cannot: which model id it accepts, what
    width it returns, whether it wants a credential.

    ``dimension`` has to be declared rather than discovered. It fixes the
    collection's width at creation, so a palace could otherwise only be built by
    first asking a network service — and a build that depends on the network is a
    build that fails differently on a bad day. Declared here and then checked
    against every response, so a wrong value is reported as the configuration
    error it is instead of surfacing later as a rejected write.
    """

    base_url: str = ""  # the API root, ending in /v1; /embeddings is appended
    model: str = ""  # the id the endpoint knows; defaults to embedding.model
    api_key_env: str = ""  # name of the env var holding the key; blank = no auth
    dimension: int = Field(default=0, ge=0)
    # Persisted by Chroma on the collection. Derived from the model id when
    # blank, which is enough to keep two hosted models apart.
    collection_name: str = ""
    # Present for the same reason they are on a local ModelSpec: a hosted E5
    # needs them as much as a local one, and omitting them is not an error, only
    # worse ranking.
    query_prefix: str = ""
    document_prefix: str = ""
    # Sits inside the recall path, whose end-to-end p95 is 20 ms on the local
    # embedder. This default is a ceiling for a failing call, not a target.
    timeout_seconds: float = Field(default=30.0, gt=0)
    batch_size: int = Field(default=32, ge=1)
    max_retries: int = Field(default=2, ge=0)


class EmbeddingConfig(BaseModel):
    """Which encoder runs, and how to reach it.

    Its own section rather than part of ``mempalace``, because the embedder is no
    longer MemPalace's. We choose it, we implement it, and we inject it into
    MemPalace — the settings that decide it belong with the thing they decide.
    ``mempalace.embedding_*`` is still accepted as input and is folded in here
    before validation; see ``MemorySettings``.

    ``provider`` is the switch the whole abstraction exists for. Changing it, and
    nothing else, changes which implementation runs:

    * ``local`` — an in-process quantized ONNX session (``OnnxSentenceEmbedder``).
    * ``http`` — a hosted OpenAI-compatible endpoint (``HttpEmbedder``). Never
      inferred: a model name cannot imply a network address, so this one has to
      be written down.
    * ``mempalace`` — MemPalace's own ``minilm`` or ``embeddinggemma``.
    * ``auto`` (the default) — read it off ``model``: ours if we implement that
      name, MemPalace's if they do.

    ``model`` is refused when it names an encoder nobody implements, rather than
    being passed through. MemPalace answers anything it does not recognise with
    ``minilm`` — an English-only model that scores 5/43 top-1 on our Chinese
    corpus — so a typo would not fail; it would quietly build the palace with the
    worst available retriever. That is how every quality number this project
    published before 2026-08-03 came to measure the wrong model.

    Changing the effective encoder means rebuilding the palace. Chroma persists
    the embedder's name on the collection and refuses mismatched reads, so the
    switch fails rather than silently comparing vectors from two different
    spaces. That refusal is the mechanism, not a defect.
    """

    provider: Literal["auto", "local", "http", "mempalace"] = "auto"
    model: str = "bge-small-zh"
    # ONNX Runtime execution provider for ``local``: auto, cpu, cuda, coreml, dml.
    device: str = ""
    # An operator's local copy of the model files. Our own implementation reads
    # it directly; for MemPalace's embedders it is bridged into their hub call,
    # which is the one case that still needs a process-wide patch.
    model_dir: str = ""
    # Explicit ORT intra-op cap. 0 keeps the native default (≈ core count),
    # which a background mine will happily use all of.
    threads: int = Field(default=0, ge=0)
    http: HttpEmbeddingConfig = Field(default_factory=HttpEmbeddingConfig)

    @model_validator(mode="after")
    def _the_implementation_exists_and_is_reachable(self) -> EmbeddingConfig:
        provider = self.provider
        model = self.model.strip().lower()

        if provider == "auto":
            # Blank means "pass no model and let MemPalace apply its default",
            # which is minilm. Allowed because an existing palace may have been
            # built that way, and refused by the benchmark preflight because no
            # measurement should describe an encoder nobody chose.
            unknown = local_model_spec(model) is None and mempalace_model_identity(model) is None
            if model and unknown:
                known = ", ".join(
                    sorted(set(LOCAL_EMBEDDING_MODELS) | set(MEMPALACE_EMBEDDING_MODELS))
                )
                raise ValueError(
                    f"embedding.model {self.model!r} is not an encoder anything "
                    f"here implements; available: {known}. For a hosted endpoint "
                    f"set embedding.provider: http, which takes any model id the "
                    f"endpoint accepts."
                )
        elif provider == "local":
            if local_model_spec(model) is None:
                known = ", ".join(sorted(LOCAL_EMBEDDING_MODELS))
                raise ValueError(
                    f"embedding.provider is 'local' but embedding.model "
                    f"{self.model!r} is not one we implement; available: {known}"
                )
        elif provider == "mempalace":
            if model and mempalace_model_identity(model) is None:
                known = ", ".join(sorted(MEMPALACE_EMBEDDING_MODELS))
                raise ValueError(
                    f"embedding.provider is 'mempalace' but embedding.model "
                    f"{self.model!r} is not one of theirs; available: {known}"
                )
        elif provider == "http":
            missing = []
            if not self.http.base_url.strip():
                missing.append("embedding.http.base_url")
            if self.http.dimension < 1:
                missing.append("embedding.http.dimension")
            if not (self.http.model.strip() or model):
                missing.append("embedding.http.model (or embedding.model)")
            if missing:
                raise ValueError(
                    "embedding.provider is 'http' but these are unset: "
                    + ", ".join(missing)
                    + ". The endpoint's address and the width it returns cannot "
                    "be guessed, and the width fixes the collection at creation."
                )

        return self

    def resolved_provider(self) -> str:
        """The implementation this configuration selects.

        One function, so nothing else has to re-derive the mapping. Validation
        above has already refused the combinations this would have to guess at.
        """

        if self.provider != "auto":
            return self.provider
        if local_model_spec(self.model) is not None:
            return "local"
        return "mempalace"

    def endpoint_model(self) -> str:
        """The model id to send to a hosted endpoint."""

        return self.http.model.strip() or self.model.strip()

    def declared_identity(self) -> EmbedderIdentity | None:
        """The name and width a new collection would be created with.

        ``None`` only when the configured model is one MemPalace resolves for
        itself and we do not recognise the name — in which case the width is
        whatever their probe returns and nothing here can say it in advance.
        """

        provider = self.resolved_provider()
        if provider == "local":
            spec = local_model_spec(self.model)
            if spec is None:  # pragma: no cover - refused by the validator
                return None
            return EmbedderIdentity(
                name=spec.collection_name or self.model.strip().lower(),
                dimension=spec.dimension,
            )
        if provider == "http":
            return EmbedderIdentity(
                name=self.http.collection_name.strip()
                or hosted_collection_name(self.endpoint_model()),
                dimension=self.http.dimension,
            )
        return mempalace_model_identity(self.model)


def hosted_collection_name(endpoint_model: str) -> str:
    """A Chroma-safe embedder name for a hosted model id.

    Prefixed rather than used bare so it cannot collide with one of the local
    names, which is the collision that would let a palace built by one
    implementation be read by the other — comparing vectors from two different
    spaces, silently. Model ids carry slashes and colons that Chroma's name
    validation rejects, so everything outside its allowed set becomes an
    underscore.
    """

    cleaned = "".join(c if c.isalnum() else "_" for c in endpoint_model.strip().lower()).strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return f"http_{cleaned or 'embeddings'}"


class MempalaceBackendConfig(BaseModel):
    """Vector storage selection.

    ``chroma`` keeps vectors in a file inside the palace directory, which is what
    this deployment is: local, one machine, one owning process per palace.

    The other backends MemPalace offers are deliberately not exposed.
    ``sqlite_exact`` scans every row in Python per query — correct, but its
    latency grows with the collection, and the voice path cannot absorb that.
    ``milvus``, ``qdrant`` and ``pgvector`` are server backends, and serving from
    more than one host is not a shape this project runs.

    **The four ``embedding_*`` fields have moved to the ``embedding`` section and
    survive here only as a compatibility surface.** They are read as input —
    folded into ``embedding`` before validation, so a bad value still fails there
    — and then overwritten with whatever ``embedding`` resolved to, so a reader
    that has not been repointed yet still sees the effective value rather than a
    stale default. Both halves are in ``MemorySettings``. Write to ``embedding``;
    these are for configuration files and call sites that predate it.
    """

    backend: str = "chroma"
    # Kept at the same defaults as their ``embedding`` counterparts, so a
    # deployment that sets neither reads the same value from either place.
    #
    # ``exclude=True`` keeps them out of ``model_dump``, which is what makes them a
    # compatibility surface rather than a second serialised copy of the embedder.
    # Without it, dumping settings and validating the result would present the
    # same setting in two sections, and changing one of them would be refused as a
    # disagreement — the mirror would have manufactured the conflict it exists to
    # report.
    embedding_model: str = Field(default="bge-small-zh", exclude=True)
    embedding_device: str = Field(default="", exclude=True)
    embedding_model_dir: str = Field(default="", exclude=True)
    embedding_threads: int = Field(default=0, ge=0, exclude=True)

    # Tests and benchmarks only. Substitutes a tiny hash-based vector for the
    # real embedder so a test can exercise the actual storage adapter without
    # loading a 300MB model. Recall ranking is meaningless under it — never set
    # this in a deployment.
    #
    # Not an embedding provider, though it looks like one: it also changes how
    # the storage adapter writes, passing explicit vectors instead of letting the
    # collection embed. That makes it a property of the store, not of the encoder.
    offline_embedding: bool = False


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

    ``palace`` keeps them as SQLite files beside the memories, which is what
    having a single owning process per palace allows.

    Kept as its own section rather than folded into ``kg``: the graph can be
    turned off at runtime, and reading the ledgers' location out of an optional
    section would mean turning the graph off took the ledgers with it — losing
    commitments and the invalidation chain as a side effect of a graph setting.
    """

    backend: Literal["palace"] = "palace"


class KgConfig(BaseModel):
    """Knowledge graph storage and tuning.

    The graph is optional at runtime. With ``backend="none"`` the service runs
    on vector recall alone: no graph is opened, graph tools are not offered, and
    a command that would write to one is answered honestly rather than hanging.
    Turning it back on is a config change; nothing is deleted when it is off.

    ``sqlite`` keeps the graph in the palace directory, beside the vectors and the
    ledgers.
    """

    backend: Literal["none", "sqlite"] = "sqlite"

    min_confidence_to_write: float = 0.6
    """Steward-extracted triples below this confidence get dropped before write."""

    @property
    def enabled(self) -> bool:
        return self.backend != "none"


class SupervisorConfig(BaseModel):
    """Multi-user agent_runner process supervisor."""

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

    ``system-data`` consumes the versioned, read-only Memory runtime roster
    published by the System Data authority. It does not read Admin or a sibling
    database.

    ``static`` reads a roster from a YAML file. This is what makes a standalone
    deployment possible: no admin service to stand up, and the operator declares
    the spaces directly. The file is re-read on reload, so entries can be added
    without a config change.
    """

    source: Literal["system-data", "static"] = "system-data"
    system_data_url: str = "http://127.0.0.1:8084"
    system_data_token_env: str = "EIDOLON_DATA_MEMORY_RUNTIME_ROSTER_TOKEN"
    request_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
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
    # The agent's surface: the two tools it calls, and nothing else. Unchanged from
    # when this path served all 27, so nothing addressing it needs to move.
    path: str = "/mcp"
    # Operator, benchmark and admin surface, on the same port and the same handles.
    # Separate because the agent's model reads its whole tool list every request:
    # 27 tools measured 15,602 characters of schema — ~3,900 tokens, of which ~3,347
    # described tools it must never call — and that list included forget_confirm,
    # dlq_replay and kg_invalidate, which a model reading "忘了这件事吧" had in reach.
    ops_path: str = "/ops/mcp"
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
        """The agent's endpoint. What discovery hands out."""

        return self._url(self.path, port)

    def ops_base_url(self, *, port: int | None = None) -> str:
        """The operator endpoint, on the same port as the agent's."""

        return self._url(self.ops_path, port)

    def _url(self, raw_path: str, port: int | None) -> str:
        path = raw_path if raw_path.startswith("/") else f"/{raw_path}"
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
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
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

    @model_validator(mode="before")
    @classmethod
    def _fold_the_legacy_embedding_keys(cls, data: Any) -> Any:
        """Move ``mempalace.embedding_*`` into ``embedding``, before validation.

        Before rather than after, so a value arriving under the old key is
        validated by the new section's rules instead of skipping them. A typo'd
        model name has to keep failing at load wherever it was written; the whole
        reason that validation exists is that MemPalace answers an unknown name
        with its English-only default and says nothing.

        Specifying the same setting in both places is only an error when the two
        disagree. Then it is refused rather than resolved by precedence: whichever
        rule we picked, half the readers would be right and nobody could tell
        which half from the file.
        """

        if not isinstance(data, dict):
            return data
        legacy_section = data.get("mempalace")
        if not isinstance(legacy_section, dict):
            return data

        present = {k: v for k, v in _LEGACY_EMBEDDING_KEYS.items() if k in legacy_section}
        if not present:
            return data

        new_section = data.get("embedding")
        folded = dict(new_section) if isinstance(new_section, dict) else {}
        conflicts = []
        for legacy_key, new_key in present.items():
            legacy_value = legacy_section[legacy_key]
            if new_key in folded:
                if str(folded[new_key]).strip() != str(legacy_value).strip():
                    conflicts.append(
                        f"mempalace.{legacy_key}={legacy_value!r} vs "
                        f"embedding.{new_key}={folded[new_key]!r}"
                    )
                continue
            folded[new_key] = legacy_value

        if conflicts:
            raise ValueError(
                "the embedder is configured twice and the two disagree: "
                + "; ".join(conflicts)
                + ". mempalace.embedding_* is the old location and is kept only "
                "for compatibility — delete it and keep the embedding section."
            )

        data = dict(data)
        data["embedding"] = folded
        return data

    @model_validator(mode="after")
    def _mirror_the_effective_embedder_onto_the_legacy_keys(self) -> MemorySettings:
        """Keep ``mempalace.embedding_*`` equal to what ``embedding`` resolved to.

        Not a second source of truth: the values only travel this way, after the
        real section has validated. It exists because some readers have not been
        repointed — ``entrypoints/supervisor.py`` builds a ``palace set-embedder``
        argument from ``mempalace.embedding_model``, and the supervisor is off
        limits. Without the mirror, a deployment that writes only the new section
        would hand that command a stale default, which is the recording of which
        embedder built the palace — the one record the benchmark preflight treats
        as authoritative.
        """

        self.mempalace.embedding_model = self.embedding.model
        self.mempalace.embedding_device = self.embedding.device
        self.mempalace.embedding_model_dir = self.embedding.model_dir
        self.mempalace.embedding_threads = self.embedding.threads
        return self

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
    """Resolve runtime log directory from component or host profile."""
    env = os.environ.get("EIDOLON_MEMORY_LOG_DIR", "").strip()
    if env:
        return Path(os.path.expandvars(env)).expanduser().resolve()
    configured = (settings.runtime.log_dir or "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser().resolve()
    root = Path(os.environ.get("EIDOLON_LOG_ROOT", "~/eidolon/logs")).expanduser()
    return (root / "memory").resolve()


def resolve_dlq_log_path(settings: MemorySettings) -> Path:
    """Resolve the DLQ JSONL path, anchoring relative paths under memory logs."""
    configured = (settings.nats.dlq_log_path or "").strip() or "memory_dlq.jsonl"
    path = Path(configured).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (resolve_log_dir(settings) / path).resolve()


def resolve_run_dir(settings: MemorySettings) -> Path:
    """Resolve PID / lockfile directory from component or host profile."""
    env = os.environ.get("EIDOLON_MEMORY_RUN_DIR", "").strip()
    if env:
        return Path(os.path.expandvars(env)).expanduser().resolve()
    configured = (settings.runtime.run_dir or "").strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser().resolve()
    root = Path(os.environ.get("EIDOLON_RUNTIME_ROOT", "~/eidolon/run")).expanduser()
    return (root / "memory").resolve()


def _bootstrap_dotenv() -> None:
    mode = os.environ.get("EIDOLON_MEMORY_DOTENV_MODE", "file").strip().lower() or "file"
    if mode == "environment":
        return
    if mode != "file":
        raise ValueError("EIDOLON_MEMORY_DOTENV_MODE must be file or environment")
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
