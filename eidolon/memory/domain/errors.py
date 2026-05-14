"""Domain-level errors for memory backends and stewards."""

from __future__ import annotations


class MemoryBackendError(RuntimeError):
    """Base class for backend failures."""


class MemoryBackendUnsupported(MemoryBackendError):
    """Raised when a backend cannot support a requested operation."""


class MemoryBackendUnavailable(MemoryBackendError):
    """Raised when a backend cannot be reached."""


class MemoryBackendWriteFailed(MemoryBackendError):
    """Raised when a write reaches the backend but fails."""


class StewardError(RuntimeError):
    """Base class for steward failures."""


class StewardOutputError(StewardError):
    """Raised when a steward output cannot be parsed or validated."""
