# eidolon-memory-contracts

Wire contracts for the Eidolon memory service. This is the only package a client
needs in order to talk to the service — it carries no storage dependencies.

## What lives here

| Module | Contents |
|---|---|
| `subjects` | NATS subject construction, memory-space id validation and storage-name derivation |
| `payloads` | `MemoryActorContext` (caller identity) and `ConversationTurnPayload` |
| `envelope` | `MemoryEnvelope` versioned wrapper plus parse helpers |
| `intent` | `MemoryIntent` and its authority/type/operation enums |
| `kg` | Command payloads, canonical predicates, sensitive-predicate set |
| `runtime_route` | Deterministic MCP port derivation for a memory realm |

## Protocol shape

Writes are published to NATS JetStream; reads go over MCP streamable HTTP. Both
sides of that split are described by the interfaces in this package, so a client
never has to know how the service stores anything — whether it keeps a knowledge
graph at all is not part of the contract.

## Versioning

Clients and the service must agree on the major version of this package. A
mismatch is a startup failure, not a runtime surprise: the contract carries the
schema version (`eidolon.memory.v1`) that the service validates on every
envelope.
