"""The abstraction layer has to be load-bearing, not decorative.

Two things are checked here, and neither is checkable by reading the code:

* every concrete store actually satisfies the port it is handed over as — the
  project has no type checker, so a protocol is only enforced if something calls
  ``isinstance`` on it;
* the logic layer cannot see which storage is underneath it.

Both are properties that hold today and would break silently. A store that grew
a renamed method would keep working locally and fail on the request that first
reached the missing one; an import added to the logic layer would work fine until
someone swapped the backend.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from eidolon.memory.domain.ports import (
    CanonicalFactStore,
    CommandStatusStore,
    CommitmentStore,
    DlqStore,
    ExtractionDecisionStore,
    SyncLedgerPort,
)
from eidolon.memory.domain.space_runtime import SpaceLedgers
from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger
from eidolon.memory.infrastructure.command_status import CommandStatusLedger
from eidolon.memory.infrastructure.commitments import CommitmentLedger
from eidolon.memory.infrastructure.dlq import DlqLedger
from eidolon.memory.infrastructure.extraction_decisions import ExtractionDecisionLedger
from eidolon.memory.infrastructure.sync_ledger import SyncLedger

MEMORY_ROOT = Path(__file__).resolve().parents[2] / "eidolon" / "memory"

# The field on SpaceLedgers, the port it is declared as, and how the local
# implementation is built. Kept as one table so a seventh ledger cannot be added
# without deciding both answers.
#
# Sync takes its space at construction while the others take it per call. That is
# a real inconsistency between ledgers, not between a ledger's two
# implementations — each port is honoured by both of its storages, which is what
# these tests are about.
LEDGERS = [
    ("command_status", CommandStatusStore, lambda p: CommandStatusLedger(p)),
    ("dlq", DlqStore, lambda p: DlqLedger(p)),
    ("decisions", ExtractionDecisionStore, lambda p: ExtractionDecisionLedger(p)),
    ("canonical_facts", CanonicalFactStore, lambda p: CanonicalFactLedger(p)),
    ("commitments", CommitmentStore, lambda p: CommitmentLedger(p)),
    ("sync", SyncLedgerPort, lambda p: SyncLedger(p, space_id="default.alice.default")),
]


# ── the ports are real ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "port", "build"),
    LEDGERS,
    ids=[field for field, _, _ in LEDGERS],
)
def test_the_local_implementation_satisfies_the_port(
    field: str, port: type, build, tmp_path: Path
) -> None:
    """Constructed, not just inspected — a protocol check on the class would
    pass for a class whose methods are declared but not reachable."""

    instance = build(tmp_path / f"{field}.sqlite3")

    assert isinstance(instance, port), (
        f"{type(instance).__name__} is handed over as {port.__name__} but does "
        f"not satisfy it"
    )


def test_every_ledger_field_is_covered_by_this_table() -> None:
    """A ledger added without a port would otherwise go unnoticed here.

    The table above is what makes the other tests meaningful, so it has to be
    kept honest against the dataclass rather than trusted.
    """

    declared = set(SpaceLedgers.__dataclass_fields__)
    covered = {field for field, _, _ in LEDGERS}

    assert declared == covered, f"ledger fields without a port check: {declared - covered}"


def test_no_ledger_field_is_typed_as_any() -> None:
    """``Any`` at this boundary defeats the point of having ports at all.

    This is the one place local and cloud hand over different objects, so it is
    where a mismatch has to be caught. Typed as ``Any``, a cloud ledger missing a
    method fails on whichever request first reaches it instead.
    """

    source = (MEMORY_ROOT / "domain" / "space_runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "SpaceLedgers":
            continue
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                annotation = ast.unparse(statement.annotation)
                if "Any" in annotation:
                    offenders.append(f"{statement.target.id}: {annotation}")

    assert not offenders, f"ledger fields typed as Any: {offenders}"


# ── the logic layer cannot see the storage ───────────────────────────────────


def test_the_logic_layer_does_not_import_the_storage_library() -> None:
    """``application/`` holds the recall and turn logic, which must read the same
    whichever store is underneath.

    A direct MemPalace import there is not just a layering violation on paper: it
    is how ``backend != "chroma"`` checks get written, and those make the logic
    layer behave differently per deployment in a way no configuration switch can
    reach.
    """

    offenders = []
    for path in sorted((MEMORY_ROOT / "application").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "mempalace" or name.startswith("mempalace."):
                    offenders.append(f"{path.relative_to(MEMORY_ROOT)}:{node.lineno} → {name}")

    assert not offenders, (
        "the logic layer imports the storage library directly:\n  "
        + "\n  ".join(offenders)
    )


def test_the_logic_layer_does_not_branch_on_backend_identity() -> None:
    """Asking *which* backend this is means the logic differs per deployment.

    Whether a backend can be warmed, or holds its data in a local file, is
    something it should answer about itself. Comparing its name means every new
    backend needs the logic layer edited, and every such comparison is a place
    where local and cloud quietly diverge.
    """

    offenders = []
    for path in sorted((MEMORY_ROOT / "application").rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for name in ('"chroma"', "'chroma'", '"milvus"', "'milvus'"):
                if name in stripped and ("==" in stripped or "!=" in stripped):
                    offenders.append(f"{path.relative_to(MEMORY_ROOT)}:{lineno} → {stripped}")

    assert not offenders, (
        "the logic layer compares against a backend name:\n  " + "\n  ".join(offenders)
    )
