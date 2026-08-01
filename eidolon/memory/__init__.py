"""Memory package — in-process MCP control plane + NATS JetStream writes.

Exports nothing on purpose. The wire contract a client needs lives in
``eidolon_memory_contracts``, which installs without this package's storage
stack. Re-exporting shapes here would give them two import paths and no answer
to which is authoritative.
"""
