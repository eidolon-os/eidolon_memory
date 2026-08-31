"""MemPalace's own two encoders, behind our port.

``minilm`` and ``embeddinggemma`` remain configurable, so something has to answer
for them when they are the configured choice. Without this the read path needed a
branch — our port when the model is ours, ``get_embedding_function()`` when it is
theirs — and that branch is exactly the thing the port exists to remove.

This adapter only wraps MemPalace's public native embedding function so Eidolon
can call it through the same port as its other providers. Eidolon never seeds or
mutates MemPalace's process-level cache.

There is no query/document asymmetry to honour: neither of their embedders has
one. ``embeddinggemma`` applies a single prefix to everything, on both sides, and
``minilm`` applies none. So both methods route to the same call, and that is a
property of those models rather than a shortcut.
"""

from __future__ import annotations

import threading
from typing import Any

from eidolon.memory.domain.embedding_port import (
    EmbedderIdentity,
    EmbeddingError,
    as_text_list,
    mempalace_model_identity,
)


class MemPalaceEmbedder:
    """Whatever ``mempalace.embedding.get_embedding_function()`` resolves to.

    Resolved per call rather than held, because that function is itself a cache
    keyed on the configured model and providers — holding the result would pin one
    encoder across a configuration change that the rest of the process has already
    picked up.
    """

    def __init__(self) -> None:
        self._identity: EmbedderIdentity | None = None
        self._identity_lock = threading.Lock()

    def identity(self) -> EmbedderIdentity:
        """Their name and width, from the table where possible and a probe if not.

        The table covers their two shipped models and answers without loading
        anything, which matters because the collection's width is needed at
        creation time. The probe is the fallback for a model name we do not
        recognise: upstream resolves anything unknown to minilm, and asking it to
        embed one string is the only way to learn what that actually produced.
        """

        if self._identity is not None:
            return self._identity
        with self._identity_lock:
            if self._identity is not None:
                return self._identity

            from mempalace.embedding import current_model_name

            model = current_model_name()
            known = mempalace_model_identity(model)
            if known is not None:
                self._identity = known
                return known

            from mempalace.embedding import probe_dimension

            probed = probe_dimension()
            if probed < 1:
                raise EmbeddingError(
                    f"mempalace resolved model {model!r} but its dimension probe "
                    f"returned {probed}, so the collection width is unknown. A "
                    f"palace created now would be built at a width nothing "
                    f"verified."
                )
            self._identity = EmbedderIdentity(name=model, dimension=probed)
            return self._identity

    def embed_documents(self, texts: Any) -> list[list[float]]:
        items = as_text_list(texts)
        if not items:
            return []
        return self._call(items)

    def embed_queries(self, texts: Any) -> list[list[float]]:
        return self.embed_documents(texts)

    def _call(self, texts: list[str]) -> list[list[float]]:
        from mempalace.embedding import get_embedding_function

        ef = get_embedding_function()
        vectors = ef(texts)
        if not vectors or len(vectors) != len(texts):
            raise EmbeddingError(
                f"mempalace's embedding function returned "
                f"{0 if not vectors else len(vectors)} vectors for "
                f"{len(texts)} texts"
            )
        # Their embedders return numpy arrays, ours return lists. Normalised to
        # lists here rather than at each caller, because a port whose two
        # implementations return different types is one every consumer has to
        # branch on.
        return [[float(x) for x in vector] for vector in vectors]
