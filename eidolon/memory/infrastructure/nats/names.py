"""NATS-safe names for JetStream resources."""

from __future__ import annotations

import hashlib
import re

from eidolon_memory_contracts import memory_space_subject_token

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


def nats_safe_name(value: str, *, max_length: int = 180) -> str:
    """Return a JetStream resource name that is safe inside API subjects.

    NATS subjects use ``.`` as a token separator. JetStream consumer names are
    interpolated into API subjects, so names derived from memory_space_id must
    not contain dots even though memory subjects themselves do.
    """
    raw = (value or "").strip()
    safe = _SAFE_NAME_RE.sub("_", raw).strip("_")
    if not safe:
        safe = "default"
    if len(safe) <= max_length:
        return safe
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    keep = max(1, max_length - len(digest) - 1)
    return f"{safe[:keep]}_{digest}"


def memory_consumer_name(prefix: str, memory_space_id: str, *, role: str = "turn") -> str:
    suffix = memory_space_subject_token(memory_space_id)
    if role == "turn":
        return nats_safe_name(f"{prefix}-{suffix}")
    return nats_safe_name(f"{prefix}-{role}-{suffix}")
