"""The counts ARCHITECTURE.md states about structure, checked against the code.

An audit on 2026-09-02 went through every machine-checkable claim in
``docs/ARCHITECTURE.md``. Roughly two in three were stale: 41 SQLite tests had
become 81, 47 contract tests 53, "10 张表" 13, "9 个模型" 8. One paragraph
claimed zero run-to-run variance while another seventy lines later recorded a
±1 band, and nobody had put the two side by side.

The doc already knew the rule — line 518 says counts belong in TEST_REPORT.md
"这里不复制会过期的计数" — and the table immediately above it broke it twice.
So the counts that survive in ARCHITECTURE.md are only the structural ones,
the ones that change when the design changes rather than when someone adds a
test, and this pins those. It fails the moment a ledger, table, protocol or
model is added or removed, which is exactly when the prose needs rewriting.

Deliberately not asserted here: test counts. A guard that breaks on every new
test trains people to edit the number without reading what it is next to.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_ARCH = _REPO / "docs" / "ARCHITECTURE.md"


def _documented(pattern: str) -> int:
    match = re.search(pattern, _ARCH.read_text("utf-8"))
    assert match, f"ARCHITECTURE.md no longer states {pattern!r}; update this guard with it"
    return int(match.group(1))


def _create_table_names() -> set[str]:
    names: set[str] = set()
    for path in (_REPO / "eidolon" / "memory" / "infrastructure").glob("*.py"):
        # ``(?!IF\b)`` because the optional IF NOT EXISTS group does not always
        # bind, and without it the regex happily reports a table named "IF".
        for name in re.findall(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?!IF\b)([a-z_0-9]+)",
            path.read_text("utf-8"),
            re.IGNORECASE,
        ):
            names.add(name)
    return names


def test_the_documented_ledger_and_table_counts_are_the_real_ones() -> None:
    """The three tables the doc used to omit were all privacy ones.

    ``extraction_privacy_tombstones``, ``commitment_privacy`` and
    ``canonical_forgets`` were missing from the per-ledger breakdown, which is
    the table a reader consults to answer "what breaks if this is lost" — and
    those three are what make a hard deletion converge and stay deleted.
    """

    from eidolon.memory.domain.space_runtime import SpaceLedgers

    assert _documented(r"(\d+) 个 ledger，共") == len(SpaceLedgers.__dataclass_fields__)
    assert _documented(r"共 \*\*(\d+) 张表\*\*") == len(_create_table_names())


def test_the_documented_local_model_count_is_the_real_one() -> None:
    from eidolon.memory.infrastructure.onnx_sentence_embedder import LOCAL_EMBEDDING_MODELS

    assert _documented(r"进程内 ONNX,(\d+) 个模型") == len(LOCAL_EMBEDDING_MODELS)


def test_every_domain_protocol_still_has_a_consumer() -> None:
    """The claim TEST_REPORT makes about the abstraction layer, as a check.

    An unused protocol is a layer that looks like a boundary and enforces
    nothing. ``DlqReader`` is the documented exception: it has no direct
    consumer but composes ``DlqStore``, so removing it would leave ``DlqStore``
    undefinable.
    """

    domain = _REPO / "eidolon" / "memory" / "domain"
    declared: dict[str, Path] = {}
    for path in domain.glob("*.py"):
        for node in ast.parse(path.read_text("utf-8")).body:
            if isinstance(node, ast.ClassDef) and any(
                getattr(base, "id", getattr(base, "attr", "")) == "Protocol" for base in node.bases
            ):
                declared[node.name] = path

    assert declared, "no protocols found; this guard is now vacuous"

    # Only the declaring file is excluded, not all of ``domain/``. A protocol
    # another domain module composes — ``SyncLedgerPort`` inside
    # ``SpaceLedgers``, ``DlqReader`` inside ``DlqStore`` — is genuinely
    # consumed. One that appears nowhere but where it is defined is the layer
    # that looks like a boundary and enforces nothing.
    sources = [(path, path.read_text("utf-8")) for path in (_REPO / "eidolon").rglob("*.py")]
    orphans = sorted(
        name
        for name, origin in declared.items()
        if not any(name in text for path, text in sources if path != origin)
    )

    # AuditSinkPort has no implementation on purpose. The only one that existed
    # lived in the deleted eidolon_data integration and turn_processor types the
    # parameter ``Any``; that call site documents the choice at length.
    assert not [name for name in orphans if name != "AuditSinkPort"], (
        f"protocols used only in the file that declares them: {orphans}"
    )
