"""Install our embedder as the one MemPalace resolves.

MemPalace selects its encoder with a hardcoded if/else over two model names
(``embedding.py``, ``get_embedding_function``) — no registry, no entry point, no
hook. What it does have is a process-level cache keyed on exactly what identifies
an embedder: ``(model_name, provider_tuple)``. Seeding that cache before the
first palace opens is therefore not a trick played on an unwilling library; it is
using the one extension surface the module actually has.

Doing it here also means one injection point covers every consumer of *theirs* —
MemPalace's own ingest and search internals all call
``get_embedding_function()`` with no arguments. Our own read path no longer goes
through it; it holds the port directly, because routing our calls through a
library whose choice of embedder we had to override was never necessary.

The key is computed from a model *name*, and upstream accepts any string it does
not recognise (falling back to minilm). That is what lets a hosted embedder be
installed the same way a local one is: the name is a label, and what sits behind
it is ours to decide.

**This must fail loudly.** If registration silently did not take, MemPalace falls
through to its default and the palace is built with ``minilm``, an English-only
model measured at 5/43 top-1 against our Chinese corpus. That exact failure — a
palace quietly built with the wrong embedder — invalidated every quality number
this project had produced before 2026-08-03, and nothing anywhere said so. So the
registration verifies itself rather than trusting that the key was computed
right, cross-checks the two places the model name comes from, and raises on a
missing upstream symbol instead of falling back.
"""

from __future__ import annotations

import logging

from eidolon.memory.config.memory_settings import EmbeddingConfig
from eidolon.memory.infrastructure.chroma_embedding_function import ChromaEmbeddingFunction
from eidolon.memory.infrastructure.embedder_factory import (
    build_embedder,
    embedding_config_from_env,
    set_active_embedder,
)

logger = logging.getLogger(__name__)


class EmbedderRegistrationError(RuntimeError):
    """Registration could not be completed or could not be verified.

    Raised rather than logged: continuing means running on a silently different
    embedder, which reads as poor retrieval quality for as long as nobody
    thinks to check.
    """


def _resolved_model_and_providers() -> tuple[str, tuple[str, ...]]:
    """The cache key MemPalace itself would compute for a no-argument call.

    Read through MemPalace's own config and provider resolution rather than our
    settings, because the key has to match what its callers produce — if we
    derived it independently the two could drift and the entry would sit in a
    slot nothing ever looks up.
    """

    try:
        from mempalace.config import MempalaceConfig
        from mempalace.embedding import _resolve_providers
    except ImportError as error:
        raise EmbedderRegistrationError(
            "cannot reach mempalace's embedder resolution "
            "(mempalace.config.MempalaceConfig / mempalace.embedding._resolve_providers). "
            "An embedder of ours is configured, so falling back would build the "
            "palace with mempalace's English-only default."
        ) from error

    config = MempalaceConfig()
    providers, _effective = _resolve_providers(config.embedding_device)
    return config.embedding_model, tuple(providers)


def register_embedder(config: EmbeddingConfig | None = None) -> str | None:
    """Make MemPalace resolve our encoder for the configured model.

    Returns the model name that was registered, or ``None`` when the configured
    model is one of MemPalace's own — in which case there is nothing to do and
    nothing is wrong.

    Call once per process, after the MemPalace environment is applied and before
    any store is opened. Idempotent: a second call with the same configuration
    finds the entry already present and returns without building a second
    session.
    """

    config = config or embedding_config_from_env()
    if config.resolved_provider() == "mempalace":
        return None

    model, providers = _resolved_model_and_providers()
    if model.strip().lower() != config.model.strip().lower():
        # The two names have to be the same string, because one of them is the
        # cache key MemPalace looks up and the other decides what we put in it.
        # They diverge when the environment was not applied from these settings —
        # a bench that built its child's config from some sections and not others,
        # for instance, which is how the parent and the child came to disagree
        # about the embedder once already.
        raise EmbedderRegistrationError(
            f"mempalace resolves the embedder name {model!r} from the environment "
            f"while embedding.model is {config.model!r}. Registering under one and "
            f"building the palace under the other is how a run comes to measure a "
            f"model nobody configured. Apply apply_mempalace_backend_env() from "
            f"the same settings before registering."
        )

    try:
        from mempalace.embedding import _EF_CACHE, _EF_CACHE_LOCK, get_embedding_function
    except ImportError as error:
        raise EmbedderRegistrationError(
            f"mempalace no longer exposes its embedder cache, so the configured "
            f"model {model!r} cannot be installed. Check mempalace's embedding "
            f"module for a real registration API before working around this."
        ) from error

    key = (model, providers)
    with _EF_CACHE_LOCK:
        existing = _EF_CACHE.get(key)
        if isinstance(existing, ChromaEmbeddingFunction):
            # Ours already. Republish the port anyway: the read path shares this
            # instance, and a second one would be a second copy of the weights.
            set_active_embedder(existing.port)
            return model
        port = build_embedder(config, preferred_providers=list(providers))
        embedding_function = ChromaEmbeddingFunction(port)
        _EF_CACHE[key] = embedding_function

    # Verify through the public function, which is what every MemPalace consumer
    # calls. Cheap — our loading is lazy, so this resolves the entry without
    # opening a session. Without this the failure mode is a wrong key: the entry
    # exists, nothing reads it, and the palace is built with minilm.
    resolved = get_embedding_function()
    if resolved is not embedding_function:
        raise EmbedderRegistrationError(
            f"registered {model!r} under {key!r} but mempalace resolved "
            f"{type(resolved).__name__} instead. The cache key it computes has "
            f"diverged from the one derived here, so the palace would be built "
            f"with the wrong embedder."
        )

    set_active_embedder(port)
    logger.info(
        "Embedder registered (model=%s provider=%s collection=%s dim=%d providers=%s)",
        model,
        config.resolved_provider(),
        embedding_function.name(),
        embedding_function.dimension,
        ",".join(providers),
    )
    return model
