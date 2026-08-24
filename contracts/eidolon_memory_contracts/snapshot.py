"""What a copy of one memory space is, and how a reader knows it is whole.

Backups on a Host are taken by an operator tool that deliberately refuses to
guess: it names what it cannot snapshot rather than including a copy that
restores into something subtly wrong. Memory was on that list — "palace, vector
index and knowledge graph have no declared snapshot" — and this module is the
declaration that takes it off.

Two things make a copy of a space trustworthy, and both have to be written down
rather than inferred by whoever restores it:

**Completeness.** A space is a fixed set of files: MemPalace's palace directory
and our sibling ledgers directory. Some of those files are load-bearing — the
vectors, the knowledge graph, the ledgers that hold the invalidation chain and
the commitments — and a copy missing one of them is not a partial backup, it is
a backup that answers questions wrongly. So the manifest lists every file with
its digest, and required files are named as required.

**Identity.** Vectors are only meaningful under the embedder that produced
them. A collection is stamped with an embedder name — ``bge_base_zh_v15``, not
the settings key ``bge-base-zh`` — and the vector store refuses to open it under
a different one; that refusal is the existing guard, and it is why a remote
embedder deliberately uses a prefixed collection name. A snapshot records the
identity it was taken under so a restore can refuse before it writes, rather
than after recall quietly stops finding things.

This module holds only the shapes. How a file is copied — ``VACUUM INTO`` for a
live SQLite database, a plain read for a marker — belongs to the component that
owns the files, and the manifest records which was used per file so the reader
does not have to assume.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Bumped when the manifest shape changes in a way a reader must notice.
SNAPSHOT_CONTRACT_VERSION = "1"

#: Where a file sits relative to the snapshot root. The two directories are
#: kept apart in the copy for the same reason they are kept apart on disk: the
#: palace belongs to MemPalace and its own tooling renames it wholesale.
PALACE_PREFIX = "palace"
LEDGERS_PREFIX = "ledgers"

#: Without these a restore produces a space that answers, but answers wrongly:
#: no vectors, or a graph and ledgers that disagree with them. Listed here so
#: "is this snapshot usable" is a question with one answer rather than a
#: judgement made again at each call site.
REQUIRED_ENTRIES: tuple[str, ...] = (
    f"{PALACE_PREFIX}/chroma.sqlite3",
    f"{LEDGERS_PREFIX}/knowledge_graph.sqlite3",
    f"{LEDGERS_PREFIX}/canonical_facts.sqlite3",
    f"{LEDGERS_PREFIX}/commitments.sqlite3",
    f"{LEDGERS_PREFIX}/command_status.sqlite3",
    f"{LEDGERS_PREFIX}/dlq.sqlite3",
    f"{LEDGERS_PREFIX}/extraction_decisions.sqlite3",
    f"{LEDGERS_PREFIX}/sync_ledger.sqlite3",
)

CopyMethod = Literal["sqlite-vacuum-into", "file-copy"]


class SnapshotEntry(BaseModel):
    """One file in the copy, and how it got there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, max_length=256)
    #: ``sqlite-vacuum-into`` is a consistent copy of a database that may be
    #: being written; ``file-copy`` is a plain read and is only used for files
    #: nothing writes during a snapshot.
    method: CopyMethod
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)


class RealmSnapshot(BaseModel):
    """A copy of one memory space, and everything needed to trust it.

    ``taken_at`` is per space rather than per Host: each space is copied on its
    own instant, and a set of these is not a point-in-time image of a Host. The
    operator tool that collects them says the same thing about authorities, and
    this keeps that honest rather than implying more than was taken.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_version: Literal["1"] = SNAPSHOT_CONTRACT_VERSION
    operation: Literal["memory.realm-snapshot"] = "memory.realm-snapshot"
    memory_space_id: str = Field(min_length=1, max_length=128)
    owner_id: str | None = Field(default=None, max_length=64)
    taken_at: str = Field(min_length=1, max_length=64)
    #: The collection-stamped embedder name the vectors were produced under
    #: (``EmbedderIdentity.name``, e.g. ``bge_base_zh_v15``), not the settings
    #: key. A restore under a different one must be refused.
    embedder_identity: str = Field(min_length=1, max_length=256)
    #: Vector width, recorded alongside the name because a same-named encoder at
    #: a different width produces a store that cannot be read either.
    embedder_dimension: int = Field(ge=1)
    entries: tuple[SnapshotEntry, ...]

    @model_validator(mode="after")
    def _every_required_file_is_present(self) -> RealmSnapshot:
        present = {entry.path for entry in self.entries}
        if len(present) != len(self.entries):
            raise ValueError("snapshot lists the same path twice")
        missing = [path for path in REQUIRED_ENTRIES if path not in present]
        if missing:
            # Refused at the manifest rather than at restore time, so an
            # incomplete copy cannot sit in a backup directory looking valid.
            raise ValueError(f"snapshot is missing required files: {', '.join(missing)}")
        return self

    @property
    def total_bytes(self) -> int:
        return sum(entry.bytes for entry in self.entries)
