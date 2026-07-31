"""Wiring for deployments that run alongside eidolon_data.

Two directions meet here, which is why neither belongs in the core:

``EidolonDataMemoryEngine`` implements a port *eidolon_data* defines, letting a
sovereignty-schema host query memory in-process. ``EidolonDataMemoryFanoutAuditSink``
implements a port *this service* defines, writing turn-absorption events into
eidolon_data's event log.

Requires the ``eidolon-os`` extra.
"""

from eidolon.memory.integrations.eidolon_data.engine import EidolonDataMemoryEngine
from eidolon.memory.integrations.eidolon_data.runtime import (
    EidolonDataMemoryFanoutAuditSink,
    build_eidolon_data_memory_engine,
    open_eidolon_data_store,
    open_fanout_audit_sink,
)

__all__ = [
    "EidolonDataMemoryEngine",
    "EidolonDataMemoryFanoutAuditSink",
    "build_eidolon_data_memory_engine",
    "open_eidolon_data_store",
    "open_fanout_audit_sink",
]
