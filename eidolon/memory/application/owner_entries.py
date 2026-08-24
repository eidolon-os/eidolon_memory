"""What was recorded lately, newest first.

The same bounded scan the library browse uses, filtered by time instead of
rolled up by wing. Sharing that scan is the point: there is one way this palace
is read for a person, and it is bounded and says when it stopped.

Why not a store-level time filter — which would obviously be cheaper. The store
keeps ``occurred_at`` as an ISO string and has no ordering, so filtering there
would mean writing a numeric timestamp at ingest and living with every record
written before that field existed being invisible to a time query. That is a
write-side change plus a silent hole, in exchange for a cost this read does not
have yet: a personal Host's palace is scanned in full by the browse beside this
one. If the scan ever becomes the problem, a numeric index is the fix, and this
function's contract does not change when it arrives.

What this deliberately does **not** do is decide what "today" means. A day
depends on where the person is, and this process does not know; the caller says
``since`` and gets what is at or after it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from eidolon.memory.application.mempalace_hierarchy import scan_records
from eidolon.memory.domain.ports import MemoryBackend
from eidolon.memory.domain.wire import MemoryWireRecord


async def build_owner_entries(
    backend: MemoryBackend,
    *,
    visible: Callable[[MemoryWireRecord], bool],
    since: datetime,
    limit: int,
    max_records: int,
) -> dict[str, Any]:
    """Entries at or after ``since``, newest first.

    ``visible`` is the same predicate the browse passes — the policy recall
    uses — so a person is never shown here what their Eidolon could not have
    recalled. Injected rather than imported for the same reason as there: this
    module knows nothing about recall policy.

    A record with no derivable time is **not** silently dropped into or out of
    the window. It is counted as undated: guessing "now" would float it to the
    top of every day's list, and guessing "epoch" would bury it forever, and
    both look like the read working.
    """

    scanned, capped = await scan_records(backend, max_records=max_records)
    allowed = [record for record in scanned if visible(record)]

    dated: list[tuple[datetime, MemoryWireRecord]] = []
    undated = 0
    for record in allowed:
        when = record.memory_time
        if when is None:
            undated += 1
            continue
        if when >= since:
            dated.append((when, record))

    dated.sort(key=lambda pair: pair[0], reverse=True)
    page = dated[:limit]

    return {
        "entries": [
            {
                "entry_id": record.key,
                "recorded_at": when.isoformat(),
                #: Which field the time came from. A person does not need it, but
                #: an Eidolon that files something under yesterday's date is a
                #: real complaint, and this is what makes it answerable.
                "recorded_at_source": record.memory_time_source or "",
                "wing_id": str(record.metadata.get("wing") or ""),
                "room_id": str(record.metadata.get("room") or ""),
                "preview": _preview(record.value),
            }
            for when, record in page
        ],
        "entry_count": len(page),
        #: In the window and not listed, because the page ended. Distinct from
        #: ``truncated``, which is about the scan.
        "more_in_window": len(dated) > len(page),
        #: Present, visible, and holding no usable time. Reported rather than
        #: hidden: a person whose entry never appears in any day's list should be
        #: able to find out that is why.
        "undated_count": undated,
        #: The scan stopped before the end of the palace, so this window may be
        #: missing older entries inside it.
        "truncated": capped,
    }


def _preview(value: Any, *, limit: int = 160) -> str:
    """One line of what the entry says.

    Bounded here rather than by the caller: this is a list, and an entry that
    scrolled for a screen would push the rest of the day off it.
    """

    text = value if isinstance(value, str) else str(value or "")
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[: limit - 1]}…"
