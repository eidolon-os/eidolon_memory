"""A copy of what an Eidolon remembers, in a form the person can read.

Not the same artefact as the Host backup, and the difference is worth stating
because both were called "export" in the plan. A backup is a copy of the palace
— vectors, ledgers, the embedder identity they were produced under — opaque,
restorable, and meaningful only to the machine it came from. This is the other
half of the promise: the person can take what their Eidolon knows about them and
read it, keep it, or hand it to something else. One is for surviving a lost disk;
this one is for not being locked in.

So it carries statements rather than storage, and it carries them whole. The day
list and the library both shorten what they show, because a list a person
scrolls is worse for being long. An export that shortened anything would be a
copy that quietly is not one.

Three things it does not do:

- **It does not widen what can be seen — with one boundary it is inside rather
  than around.** The visibility predicate recall uses decides what appears, so an
  export can never see past privacy, space or device rules. The audience axis is
  different in kind: it says *which of the Owner's Eidolons was told a thing*,
  and the Owner is not one of them. A person who marked a memory 「只让它记得」
  narrowed who is told; they did not ask to lose it from their own copy. So the
  Owner's own export carries every audience in their Realm and **names the
  audience on each record**, while an export asked for on behalf of one Companion
  keeps the recall predicate exactly. Omitting what somebody marked themselves
  would be the very thing this file refuses to be: a copy that quietly is not
  one.
- **It does not dump metadata.** A named set of fields travels; the rest stays
  in. Handing over the whole internal mapping would make routing and audience
  keys part of a contract a person's file now depends on, and would carry
  details that mean nothing to them and something to whoever reads the file
  next.
- **It does not drop what it cannot date.** A record with no derivable time is
  last in the file and counted, never omitted — unlike the day list, where an
  undated entry belongs to no day. This is the copy; leaving something out of it
  is losing it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from eidolon.memory.application.mempalace_hierarchy import scan_records
from eidolon.memory.domain.ports import MemoryBackend
from eidolon_memory_contracts import OWNER_AUDIENCE

from eidolon.memory.domain.wire import MemoryWireRecord

#: What travels per record. Named rather than derived from the metadata mapping:
#: an export is the one read whose shape somebody keeps on disk, so the fields
#: are a decision instead of whatever the store happened to hold that day.
EXPORTED_FIELDS = (
    "entry_id",
    "recorded_at",
    "recorded_at_source",
    "wing_id",
    "room_id",
    "memory_type",
    "value",
    #: Who was told. Present on every record because the Owner's copy carries
    #: every audience: a file that held a companion-private memory without
    #: saying it was one would be less true than the memory it copies.
    "audience",
)


async def build_owner_export(
    backend: MemoryBackend,
    *,
    visible: Callable[[MemoryWireRecord], bool],
    max_records: int,
) -> dict[str, Any]:
    """Everything visible in this space, newest first, undated last.

    ``truncated`` is the honest half. The scan is bounded — it is a full
    enumeration of a palace and something has to bound it — so an export that
    hit the bound says so, and says how many it carried. A file that is silently
    part of a memory is the one outcome worse than a file that says it is part.
    """

    scanned, capped = await scan_records(backend, max_records=max_records)
    allowed = [record for record in scanned if visible(record)]

    dated: list[tuple[datetime, MemoryWireRecord]] = []
    undated: list[MemoryWireRecord] = []
    for record in allowed:
        when = record.memory_time
        if when is None:
            undated.append(record)
        else:
            dated.append((when, record))
    dated.sort(key=lambda pair: pair[0], reverse=True)

    records = [_exported(record, when=when) for when, record in dated]
    records.extend(_exported(record, when=None) for record in undated)

    return {
        "records": records,
        "record_count": len(records),
        #: Present, visible, and holding no usable time. They are *in* the file,
        #: at the end; the count is here so a person reading it knows why some of
        #: their memories carry no date rather than wondering what happened.
        "undated_count": len(undated),
        #: The scan stopped before the end of the palace. What is here is real;
        #: it is not all of it.
        "truncated": capped,
    }


def _exported(record: MemoryWireRecord, *, when: datetime | None) -> dict[str, Any]:
    return {
        "entry_id": record.key,
        "recorded_at": when.isoformat() if when is not None else "",
        "recorded_at_source": record.memory_time_source or "",
        "wing_id": str(record.metadata.get("wing") or ""),
        "room_id": str(record.metadata.get("room") or ""),
        "memory_type": str(record.metadata.get("memory_type") or ""),
        # Whole, not a preview. This is the copy.
        "value": record.value if isinstance(record.value, str) else str(record.value or ""),
        # A record written before the field existed belongs to the owner layer —
        # the same default a write takes, so nothing in an old palace reads as
        # having been kept from anybody.
        "audience": str(record.metadata.get("audience") or OWNER_AUDIENCE),
    }
