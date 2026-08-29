"""The one place a configured provider becomes an implementation.

Everything else in the system holds an ``EmbeddingPort`` and cannot tell which of
the three it has. That is what makes switching a configuration change and nothing
else — this file is the only thing that has to be edited to add a fourth.

Two other jobs live here because they are the same question asked at different
times:

``embedder_from_env`` rebuilds the configuration from the environment. The
subprocess that materialises a fresh palace is a ``python -c``: it inherits the
environment but none of the parent's in-process state, and creating the collection
is precisely when the embedder matters — it fixes the vector width and the name
Chroma persists. The whole section travels as one JSON variable rather than as a
field per setting, so adding a setting does not also mean remembering to plumb it.

``active_embedder`` is the process's encoder for explicit document and query
vectors. MemPalace 3.8 exposes those parameters on its public collection API, so
the storage layer no longer installs anything into MemPalace's private provider
cache.
"""

from __future__ import annotations

import os
import threading

from eidolon.memory.config.memory_settings import EmbeddingConfig
from eidolon.memory.domain.embedding_port import EmbeddingPort
from eidolon.memory.infrastructure.http_embedder import HttpEmbedder, resolve_api_key
from eidolon.memory.infrastructure.mempalace_embedder import MemPalaceEmbedder
from eidolon.memory.infrastructure.onnx_sentence_embedder import OnnxSentenceEmbedder
from eidolon.memory.support.logging import get_logger

log = get_logger(__name__)

#: Carries the whole ``embedding`` section to any process we spawn. One variable
#: rather than one per field: the palace-init subprocess and five benchmark
#: scripts all inherit the environment, and a per-field transport is a list that
#: someone eventually forgets to extend — which is the shape of the defect that
#: made every quality figure before 2026-08-03 measure the wrong model.
EMBEDDING_CONFIG_ENV = "EIDOLON_EMBEDDING_CONFIG"

_active_lock = threading.Lock()
_active: EmbeddingPort | None = None


def build_embedder(
    config: EmbeddingConfig,
    *,
    preferred_providers: list[str] | None = None,
) -> EmbeddingPort:
    """The implementation ``config`` selects.

    ``preferred_providers`` is passed through for the local implementation only.
    """

    provider = config.resolved_provider()

    if provider == "local":
        return OnnxSentenceEmbedder(
            config.model,
            preferred_providers=preferred_providers,
            intra_op_num_threads=config.threads,
            model_dir=config.model_dir,
        )

    if provider == "http":
        identity = config.declared_identity()
        if identity is None:  # pragma: no cover - refused by the config validator
            raise ValueError("embedding.provider is 'http' but no identity resolved")
        return HttpEmbedder(
            base_url=config.http.base_url,
            model=config.endpoint_model(),
            dimension=identity.dimension,
            name=identity.name,
            api_key=resolve_api_key(config.http.api_key_env),
            query_prefix=config.http.query_prefix,
            document_prefix=config.http.document_prefix,
            timeout_seconds=config.http.timeout_seconds,
            batch_size=config.http.batch_size,
            max_retries=config.http.max_retries,
        )

    if provider == "mempalace":
        return MemPalaceEmbedder()

    # Unreachable through the config, which validates the provider against a
    # Literal. Reached only by a caller that built an EmbeddingConfig by hand, and
    # named rather than returning a default, because a default here is the silent
    # fallback this whole layer exists to prevent.
    raise ValueError(f"no embedder implementation for provider {provider!r}")


def embedding_config_env_value(config: EmbeddingConfig) -> str:
    """``config`` as the single environment variable a child process reads."""

    return config.model_dump_json()


def embedding_config_from_env() -> EmbeddingConfig:
    """The configuration a child process inherited.

    Falls back to reconstructing a section from the ``MEMPALACE_EMBEDDING_*``
    variables when ours is absent. Those are what this project set before the
    section existed, and they are also what a test that only sets a model name
    provides — so the fallback keeps the environment self-describing rather than
    letting an incomplete one resolve to a default nobody chose.
    """

    raw = os.environ.get(EMBEDDING_CONFIG_ENV, "").strip()
    if raw:
        return EmbeddingConfig.model_validate_json(raw)

    return EmbeddingConfig.model_validate(
        {
            "model": os.environ.get("MEMPALACE_EMBEDDING_MODEL", "").strip().lower(),
            "device": os.environ.get("MEMPALACE_EMBEDDING_DEVICE", "").strip().lower(),
            "model_dir": os.environ.get("MEMPALACE_EMBEDDING_MODEL_DIR", "").strip(),
            "threads": int(os.environ.get("MEMPALACE_EMBEDDING_THREADS", "0") or 0),
        }
    )


def set_active_embedder(port: EmbeddingPort) -> None:
    """Publish the process's encoder, so the read path shares one instance.

    Called by registration, which has already built the port to hand MemPalace.
    Sharing matters concretely for the local implementation: a second instance is
    a second ONNX session and a second copy of the weights.
    """

    global _active
    with _active_lock:
        _active = port


def active_embedder() -> EmbeddingPort:
    """The encoder this process reads with.

    Normally the one registration published. Falling back to MemPalace's own is
    correct rather than lenient: registration returns without installing anything
    exactly when the configured model is one of theirs, and if it never ran at all
    then MemPalace's default is also what any existing palace was built with — so
    this answers with the same encoder the collection will accept, in both cases.
    Opening that collection is itself guarded, because Chroma refuses a
    differently-named embedding function.
    """

    global _active
    if _active is not None:
        return _active
    with _active_lock:
        if _active is None:
            _active = MemPalaceEmbedder()
        return _active


def reset_active_embedder() -> None:
    """Drop the published encoder. For tests, which configure several."""

    global _active
    with _active_lock:
        _active = None
