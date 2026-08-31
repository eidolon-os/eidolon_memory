from __future__ import annotations

import pytest
from eidolon_memory_contracts import MemoryActorContext, MemoryIntent

from eidolon.memory.adapters.fake_backend import FakeMemoryBackend
from eidolon.memory.application.forget import (
    DEFAULT_FORGET_PAGE_SIZE,
    ForgetResolutionLimitExceeded,
    find_forget_candidates,
    forget_exact_projections,
    forget_resolved_projections,
    normalize_privacy_target,
)
from eidolon.memory.application.recall_policy import RecallPolicyRegistry
from eidolon.memory.application.steward.common import apply_privacy_actions
from eidolon.memory.domain.canonical_fact import CanonicalFactInactive
from eidolon.memory.domain.extraction_decision import ExtractionDecisionRecord
from eidolon.memory.domain.steward import PrivacyAction, StewardDecision
from eidolon.memory.domain.wire import MemoryWireRecord
from eidolon.memory.infrastructure.canonical_facts import CanonicalFactLedger
from eidolon.memory.infrastructure.commitments import CommitmentLedger
from eidolon.memory.infrastructure.extraction_decisions import ExtractionDecisionLedger

SPACE = "default.alice.default"
OWNER_READ_SCOPE = ("owner",)


def test_default_forget_page_size_matches_55k_memory_latency_curve() -> None:
    assert DEFAULT_FORGET_PAGE_SIZE == 5_000


def _seed(backend: FakeMemoryBackend, key: str, text: str) -> None:
    backend.docs[f"{SPACE}::{key}"] = MemoryWireRecord(
        memory_space_id=SPACE,
        key=key,
        value=text,
        metadata={"memory_space_id": SPACE, "wing": "Wing_Profile"},
    )


async def _seed_canonical(
    backend: FakeMemoryBackend,
    ledger: CanonicalFactLedger,
    key: str,
    text: str,
    *,
    graph=None,
    subject: str = "$text",
    predicate: str = "remembers_text",
    object_: str | None = None,
) -> str:
    intent = MemoryIntent(
        intent_id=f"intent:{key}",
        memory_space_id=SPACE,
        source_event_id=f"turn:{key}",
        authority="extracted_user",
        intent_type="fact",
        raw_claim=text,
        operation_hint="add",
        subject=subject,
        predicate=predicate,
        object=object_ or text,
        attributes={"audience": "owner"},
    )
    targets = {"drawer", "kg"} if graph is not None else {"drawer"}
    registration = await ledger.register(intent, targets=targets)
    backend.docs[f"{SPACE}::{key}"] = MemoryWireRecord(
        memory_space_id=SPACE,
        key=key,
        value=text,
        metadata={
            "memory_space_id": SPACE,
            "wing": "Wing_Profile",
            "source_turn_id": f"canonical:{registration.projection_id}",
            "assertion_id": registration.assertion_id,
            "evidence_id": intent.intent_id,
            "projection_id": registration.projection_id,
        },
    )
    if graph is not None:
        await graph.add_triple(
            subject=subject,
            predicate=predicate,
            object=object_ or text,
            audience="owner",
            source_turn_id=f"canonical:{registration.projection_id}",
            assertion_id=registration.assertion_id,
            evidence_id=intent.intent_id,
            projection_id=registration.projection_id,
        )
    await ledger.mark_projected(SPACE, registration.assertion_id, targets=targets)
    return registration.assertion_id


async def _seed_graph_canonical(
    ledger: CanonicalFactLedger,
    graph,
    *,
    intent_id: str,
    subject: str,
    predicate: str,
    object_: str,
) -> str:
    intent = MemoryIntent(
        intent_id=intent_id,
        memory_space_id=SPACE,
        source_event_id=f"turn:{intent_id}",
        authority="extracted_user",
        intent_type="fact",
        raw_claim=f"{subject} {predicate} {object_}",
        operation_hint="add",
        subject=subject,
        predicate=predicate,
        object=object_,
        attributes={"audience": "owner"},
    )
    registration = await ledger.register(intent, targets={"kg"})
    await graph.add_triple(
        subject=subject,
        predicate=predicate,
        object=object_,
        audience="owner",
        source_turn_id=f"canonical:{registration.projection_id}",
        assertion_id=registration.assertion_id,
        evidence_id=intent.intent_id,
        projection_id=registration.projection_id,
    )
    await ledger.mark_projected(SPACE, registration.assertion_id, targets={"kg"})
    return registration.assertion_id


def test_normalize_privacy_target_only_trims_boundaries() -> None:
    assert normalize_privacy_target("  我喜欢喝绿茶。 ") == "我喜欢喝绿茶"


async def test_find_forget_candidates_is_tenant_scoped_and_content_based() -> None:
    backend = FakeMemoryBackend()
    _seed(backend, "drawer_tea", "用户现在喜欢乌龙茶，不再喝绿茶")
    _seed(backend, "drawer_city", "用户住在常州")
    backend.docs["other::drawer_tea"] = MemoryWireRecord(
        memory_space_id="other",
        key="drawer_tea",
        value="绿茶",
        metadata={"memory_space_id": "other"},
    )

    candidates = await find_forget_candidates(backend, SPACE, "绿茶")

    assert [candidate.key for candidate in candidates] == ["drawer_tea"]


async def test_privacy_delete_removes_candidate_and_verifies_invisible(tmp_path) -> None:
    backend = FakeMemoryBackend()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    await _seed_canonical(backend, ledger, "drawer_tea", "用户现在喜欢乌龙茶，不再喝绿茶")
    _seed(backend, "drawer_city", "用户住在常州")

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        audiences=OWNER_READ_SCOPE,
        actions=[
            PrivacyAction(
                action="delete_request",
                target="绿茶",
                reason="explicit user request",
            )
        ],
        canonical_facts=ledger,
        decision_store=ExtractionDecisionLedger(tmp_path / "decisions.sqlite3"),
    )

    assert result.deleted_keys == ["drawer_tea"]
    assert await backend.get(SPACE, "drawer_tea") is None
    assert await backend.get(SPACE, "drawer_city") is not None


async def test_archive_topic_keeps_drawer_but_blocks_recall(tmp_path) -> None:
    backend = FakeMemoryBackend()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    await _seed_canonical(backend, ledger, "drawer_tea", "用户喜欢喝绿茶")

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        audiences=OWNER_READ_SCOPE,
        actions=[
            PrivacyAction(
                action="archive_topic",
                target="绿茶",
                reason="explicit user request",
            )
        ],
        canonical_facts=ledger,
    )

    assert result.archived_keys == ["drawer_tea"]
    archived = await backend.get(SPACE, "drawer_tea")
    assert archived is not None
    assert archived.metadata["privacy"] == "do_not_recall"
    context = MemoryActorContext(memory_realm_id=SPACE, memory_space_id=SPACE)
    assert not RecallPolicyRegistry.default().visible(archived, context=context)


async def test_multiple_delete_candidates_are_safely_archived(tmp_path) -> None:
    class TrackingBackend(FakeMemoryBackend):
        def __init__(self) -> None:
            super().__init__()
            self.delete_many_calls: list[list[str]] = []

        async def delete_many(self, memory_space_id: str, keys: list[str]) -> list[str]:
            self.delete_many_calls.append(list(keys))
            return await super().delete_many(memory_space_id, keys)

    backend = TrackingBackend()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    await _seed_canonical(backend, ledger, "drawer_tea_1", "绿茶口味偏好")
    await _seed_canonical(backend, ledger, "drawer_tea_2", "不再购买绿茶")

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        audiences=OWNER_READ_SCOPE,
        actions=[
            PrivacyAction(
                action="delete_request",
                target="绿茶",
                reason="explicit user request",
            )
        ],
        canonical_facts=ledger,
    )

    assert result.deleted_keys == []
    assert backend.delete_many_calls == []
    assert sorted(result.archived_keys) == ["drawer_tea_1", "drawer_tea_2"]
    assert [item["drawer_id"] for item in result.confirmation_required["绿茶"]] == [
        "drawer_tea_1",
        "drawer_tea_2",
    ]


async def test_candidate_resolution_pages_through_multi_year_history() -> None:
    backend = FakeMemoryBackend()
    for index in range(12):
        text = "很久以前喜欢绿茶" if index == 10 else f"历史记录 {index}"
        _seed(backend, f"drawer_{index:02d}", text)

    candidates = await find_forget_candidates(
        backend,
        SPACE,
        "绿茶",
        max_scan=20,
        page_size=3,
    )

    assert [candidate.key for candidate in candidates] == ["drawer_10"]


async def test_candidate_resolution_fails_instead_of_silently_truncating_scan() -> None:
    backend = FakeMemoryBackend()
    for index in range(6):
        _seed(backend, f"drawer_{index:02d}", f"历史记录 {index}")

    with pytest.raises(ForgetResolutionLimitExceeded, match="exceeds 5 drawers"):
        await find_forget_candidates(
            backend,
            SPACE,
            "不存在的话题",
            max_scan=5,
            page_size=2,
        )


async def test_candidate_resolution_fails_on_ambiguous_result_overflow() -> None:
    backend = FakeMemoryBackend()
    for index in range(4):
        _seed(backend, f"drawer_{index:02d}", f"绿茶记录 {index}")

    with pytest.raises(ForgetResolutionLimitExceeded, match="more than 3 drawers"):
        await find_forget_candidates(
            backend,
            SPACE,
            "绿茶",
            max_scan=10,
            max_candidates=3,
            page_size=2,
        )


def _seed_with_turn(backend, key: str, text: str, *, turn_id: str) -> None:
    backend.docs[f"{SPACE}::{key}"] = MemoryWireRecord(
        memory_space_id=SPACE,
        key=key,
        value=text,
        metadata={
            "memory_space_id": SPACE,
            "wing": "Wing_Profile",
            "source_turn_id": turn_id,
        },
    )


@pytest.fixture
def graph(tmp_path):
    from eidolon.memory.adapters.kg_sqlite import SqliteKnowledgeGraph
    from eidolon.memory.domain.space_lock import SpaceLock

    made = SqliteKnowledgeGraph(tmp_path / "kg.sqlite3", space_id=SPACE, lock=SpaceLock())
    yield made
    made.close()


async def test_a_spoken_delete_reaches_the_graph(graph, tmp_path) -> None:
    """The path a person actually takes to be forgotten.

    Saying "忘掉…" runs through the steward, which needs no tool call and no
    confirmation round trip — so it is the common case, and it was the half of
    the defect that stayed broken longest: the drawer went and the triple was
    still transcribed into the next prompt.
    """

    backend = FakeMemoryBackend()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    await _seed_canonical(
        backend,
        ledger,
        "drawer_tea",
        "用户喜欢绿茶",
        graph=graph,
        subject="用户",
        predicate="likes",
        object_="绿茶",
    )

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        audiences=OWNER_READ_SCOPE,
        actions=[
            PrivacyAction(
                action="delete_request",
                target="绿茶",
                reason="explicit user request",
            )
        ],
        kg=graph,
        canonical_facts=ledger,
        decision_store=ExtractionDecisionLedger(tmp_path / "decisions.sqlite3"),
    )

    assert result.deleted_keys == ["drawer_tea"]
    assert result.statements_forgotten == 1
    assert (await graph.stats())["triples_total"] == 0


async def test_hard_forget_is_ledger_first_and_blocks_replay(graph, tmp_path) -> None:
    backend = FakeMemoryBackend()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    intent = MemoryIntent(
        intent_id="intent:tea",
        memory_space_id=SPACE,
        source_event_id="turn-tea",
        authority="extracted_user",
        intent_type="preference",
        raw_claim="用户喜欢绿茶",
        operation_hint="add",
        subject="用户",
        predicate="likes",
        object="绿茶",
        confidence=0.99,
        attributes={"audience": "owner"},
    )
    registration = await ledger.register(intent, targets={"drawer", "kg"})
    drawer_id = "drawer_tea"
    backend.docs[f"{SPACE}::{drawer_id}"] = MemoryWireRecord(
        memory_space_id=SPACE,
        key=drawer_id,
        value=intent.raw_claim,
        metadata={
            "memory_space_id": SPACE,
            "source_turn_id": intent.source_event_id,
            "assertion_id": registration.assertion_id,
            "evidence_id": intent.intent_id,
            "projection_id": registration.projection_id,
        },
    )
    await graph.add_triple(
        subject=intent.subject or "",
        predicate=intent.predicate or "",
        object=intent.object or "",
        audience="owner",
        source_turn_id=f"canonical:{registration.projection_id}",
        assertion_id=registration.assertion_id,
        evidence_id=intent.intent_id,
        projection_id=registration.projection_id,
    )
    await ledger.mark_projected(SPACE, registration.assertion_id, targets={"drawer", "kg"})
    decisions = ExtractionDecisionLedger(tmp_path / "decisions.sqlite3")
    await decisions.put_if_absent(
        ExtractionDecisionRecord(
            memory_space_id=SPACE,
            source_turn_id=intent.source_event_id,
            extractor_version="test:v1",
            input_hash="private-input-hash",
            decision=StewardDecision(should_write=True, reason="durable extraction"),
            intents=[intent],
        )
    )

    changed, statements = await forget_exact_projections(
        backend,
        graph,
        ledger,
        SPACE,
        [drawer_id],
        hard=True,
        decision_store=decisions,
    )

    assert changed == [drawer_id]
    assert statements == 1
    assert await backend.get(SPACE, drawer_id) is None
    assert (await graph.stats())["triples_total"] == 0
    forgotten = await ledger.get_fact(SPACE, "owner", "用户", "likes", "绿茶")
    assert forgotten is not None
    assert forgotten.state == "forgotten"
    assert forgotten.subject.startswith("[forgotten:fact:")
    assert forgotten.predicate == forgotten.object == "[forgotten]"
    assert await ledger.evidence_count(registration.assertion_id) == 0
    redacted = await decisions.get(SPACE, intent.source_event_id, "test:v1")
    assert redacted is not None and redacted.redacted is True
    assert intent.raw_claim.encode() not in decisions.path.read_bytes()
    with pytest.raises(CanonicalFactInactive, match="forgotten"):
        await ledger.register(intent, targets={"drawer", "kg"})


async def test_source_event_delete_closes_a_ledger_only_partial_write(graph, tmp_path) -> None:
    """A failed projection is still deletable by its durable ingestion identity."""

    backend = FakeMemoryBackend()
    canonical = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    commitments = CommitmentLedger(tmp_path / "commitments.sqlite3")
    decisions = ExtractionDecisionLedger(tmp_path / "decisions.sqlite3")
    intent = MemoryIntent(
        intent_id="intent:partial",
        memory_space_id=SPACE,
        source_event_id="turn:partial",
        authority="extracted_user",
        intent_type="preference",
        raw_claim="用户喜欢紫色彗星",
        operation_hint="add",
        subject="用户",
        predicate="likes",
        object="紫色彗星",
        attributes={"audience": "owner"},
    )
    registration = await canonical.register(intent, targets={"drawer", "kg"})
    await decisions.put_if_absent(
        ExtractionDecisionRecord(
            memory_space_id=SPACE,
            source_turn_id=intent.source_event_id,
            extractor_version="test:v1",
            input_hash="partial-input",
            decision=StewardDecision(should_write=True, reason="partial projection"),
            intents=[intent],
        )
    )

    changed, statements = await forget_resolved_projections(
        backend,
        graph,
        canonical,
        commitments,
        decisions,
        SPACE,
        drawer_ids=[],
        commitment_ids=[],
        source_event_ids=[intent.source_event_id],
        hard=True,
    )

    assert changed == []
    assert statements == 0
    assert await canonical.evidence_count(registration.assertion_id) == 0
    forgotten = await canonical.get_fact(SPACE, "owner", "用户", "likes", "紫色彗星")
    assert forgotten is not None and forgotten.state == "forgotten"
    assert await decisions.source_event_redacted(SPACE, intent.source_event_id)
    assert await canonical.assertion_ids_for_source_events(SPACE, [intent.source_event_id]) == []


async def test_source_event_delete_tombstones_a_decision_with_no_projection(tmp_path) -> None:
    backend = FakeMemoryBackend()
    canonical = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    commitments = CommitmentLedger(tmp_path / "commitments.sqlite3")
    decisions = ExtractionDecisionLedger(tmp_path / "decisions.sqlite3")
    source_event_id = "turn:decision-only"
    await decisions.put_if_absent(
        ExtractionDecisionRecord(
            memory_space_id=SPACE,
            source_turn_id=source_event_id,
            extractor_version="test:v1",
            input_hash="decision-only-input",
            decision=StewardDecision(should_write=False, reason="nothing durable"),
        )
    )

    changed, statements = await forget_resolved_projections(
        backend,
        None,
        canonical,
        commitments,
        decisions,
        SPACE,
        drawer_ids=[],
        commitment_ids=[],
        source_event_ids=[source_event_id],
        hard=True,
    )

    assert changed == []
    assert statements == 0
    assert await decisions.source_event_redacted(SPACE, source_event_id)
    redacted = await decisions.get(SPACE, source_event_id, "test:v1")
    assert redacted is not None and redacted.redacted


async def test_hard_forget_removes_every_activation_drawer(tmp_path) -> None:
    backend = FakeMemoryBackend()
    canonical = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    assertion_id = await _seed_canonical(
        backend,
        canonical,
        "drawer_current",
        "用户喜欢绿茶",
    )
    backend.docs[f"{SPACE}::drawer_old"] = MemoryWireRecord(
        memory_space_id=SPACE,
        key="drawer_old",
        value="用户以前喜欢绿茶",
        metadata={
            "memory_space_id": SPACE,
            "assertion_id": assertion_id,
            "projection_id": f"{assertion_id}:activation:0",
            "privacy": "do_not_recall",
        },
    )

    changed, statements = await forget_exact_projections(
        backend,
        None,
        canonical,
        SPACE,
        ["drawer_current"],
        hard=True,
        decision_store=ExtractionDecisionLedger(tmp_path / "decisions.sqlite3"),
    )

    assert set(changed) == {"drawer_current", "drawer_old"}
    assert statements == 0
    assert await backend.get(SPACE, "drawer_current") is None
    assert await backend.get(SPACE, "drawer_old") is None


async def test_hard_forget_keeps_evidence_and_projections_until_source_is_redacted(
    graph, tmp_path
) -> None:
    backend = FakeMemoryBackend()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    assertion_id = await _seed_canonical(
        backend,
        ledger,
        "drawer_private",
        "用户的私密识别词是紫色彗星",
        graph=graph,
    )

    class UnavailableDecisionLedger:
        async def redact_source_events(self, memory_space_id, source_event_ids):
            raise RuntimeError("decision ledger unavailable")

    with pytest.raises(RuntimeError, match="decision ledger unavailable"):
        await forget_exact_projections(
            backend,
            graph,
            ledger,
            SPACE,
            ["drawer_private"],
            hard=True,
            decision_store=UnavailableDecisionLedger(),
        )

    assert await backend.get(SPACE, "drawer_private") is not None
    assert (await graph.stats())["triples_total"] == 1
    assert await ledger.evidence_count(assertion_id) == 1

    decisions = ExtractionDecisionLedger(tmp_path / "decisions.sqlite3")
    changed, statements = await forget_exact_projections(
        backend,
        graph,
        ledger,
        SPACE,
        ["drawer_private"],
        hard=True,
        decision_store=decisions,
    )

    assert changed == ["drawer_private"]
    assert statements == 1
    assert await ledger.evidence_count(assertion_id) == 0
    tombstone = await decisions.get(SPACE, "turn:drawer_private", "future:v1")
    assert tombstone is not None and tombstone.redacted is True


async def test_a_spoken_archive_ends_the_triple_without_deleting_it(graph, tmp_path) -> None:
    backend = FakeMemoryBackend()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    await _seed_canonical(
        backend,
        ledger,
        "drawer_tea",
        "用户喜欢绿茶",
        graph=graph,
        subject="用户",
        predicate="likes",
        object_="绿茶",
    )

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        audiences=OWNER_READ_SCOPE,
        actions=[
            PrivacyAction(
                action="archive_topic",
                target="绿茶",
                reason="explicit user request",
            )
        ],
        kg=graph,
        canonical_facts=ledger,
    )

    assert result.archived_keys == ["drawer_tea"]
    assert result.statements_forgotten == 1
    stats = await graph.stats()
    assert stats["triples_total"] == 1
    assert stats["triples_active"] == 0


async def test_an_ambiguous_delete_is_archived_rather_than_abandoned(graph, tmp_path) -> None:
    """It used to find the memories and then decline, telling nobody.

    A delete matching several drawers recorded ``confirmation_required`` and did
    nothing. Nobody was ever asked: this runs on the bus about 23 seconds after
    the companion has already said "好的", so the path has no way to put a
    question to anyone, and the caller discarded the result object anyway. The
    person asked to be forgotten, was told yes, and nothing happened.

    Archiving is what "forget this" means to them — it stops being recalled,
    immediately, on every match — and it is reversible, so an over-broad match
    costs nothing that cannot be undone. No score margin, no ranking tie-break:
    those need a constant nobody can calibrate, and getting it wrong deletes
    something the person wanted.
    """

    backend = FakeMemoryBackend()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    for index, key in enumerate(("drawer_tea_1", "drawer_tea_2")):
        await _seed_canonical(
            backend,
            ledger,
            key,
            f"用户喜欢绿茶{index}",
            graph=graph,
            subject="用户",
            predicate="likes",
            object_=f"绿茶{index}",
        )

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        audiences=OWNER_READ_SCOPE,
        actions=[
            PrivacyAction(
                action="delete_request",
                target="绿茶",
                reason="explicit user request",
            )
        ],
        kg=graph,
        canonical_facts=ledger,
    )

    assert result.deleted_keys == [], "nothing irreversible on a guess"
    assert sorted(result.archived_keys) == ["drawer_tea_1", "drawer_tea_2"]
    # Recorded distinctly, so an operator can tell "the user asked to archive"
    # from "the user asked to delete and we chose the reversible half".
    assert result.downgraded_to_archive["绿茶"] == result.archived_keys
    assert result.confirmation_required, "the ambiguity is still on the record"

    # The graph half matches: ended, not removed.
    stats = await graph.stats()
    assert stats["triples_active"] == 0, "the statements stopped being recalled"
    assert stats["triples_total"] == 2, "and none of them were deleted"


async def test_a_space_without_a_graph_still_forgets_its_drawers(tmp_path) -> None:
    backend = FakeMemoryBackend()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    await _seed_canonical(backend, ledger, "drawer_tea", "用户喜欢绿茶")

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        audiences=OWNER_READ_SCOPE,
        actions=[
            PrivacyAction(
                action="delete_request",
                target="绿茶",
                reason="explicit user request",
            )
        ],
        canonical_facts=ledger,
        decision_store=ExtractionDecisionLedger(tmp_path / "decisions.sqlite3"),
    )

    assert result.deleted_keys == ["drawer_tea"]
    assert result.statements_forgotten == 0


def test_every_caller_hands_the_graph_to_the_privacy_handler() -> None:
    """A fourth call site that forgets ``kg=`` restores the bug silently.

    ``kg`` has to default to ``None`` — a space can genuinely have no graph — so
    nothing at runtime distinguishes "no graph here" from "forgot to pass it".
    That is exactly how this survived: the parameter did not exist, every caller
    was consistent, and consistency looked like correctness.
    """

    import inspect

    from eidolon.memory.application import turn_processor
    from eidolon.memory.application.steward import common, llm

    for module in (turn_processor, llm):
        source = inspect.getsource(module)
        for index, line in enumerate(source.splitlines()):
            if "apply_privacy_actions(" not in line or "def " in line:
                continue
            call = "\n".join(source.splitlines()[index : index + 8])
            assert "kg=" in call, f"{module.__name__} calls it without a graph"

    assert "kg" in inspect.signature(common.apply_privacy_actions).parameters


async def test_a_fact_that_only_the_graph_holds_can_still_be_forgotten(graph, tmp_path) -> None:
    """The hole the two independent write gates open.

    ``min_importance_to_write`` is 3 and ``min_confidence_to_write`` is 0.6, so an
    ordinary but reliable sentence — "用户喜欢绿茶", importance 2, confidence 0.9 —
    becomes a triple and no drawer. Asking to forget it scanned drawers, found
    nothing, recorded ``unmatched_targets``, and did nothing at all, while the
    statement went on being rendered into every later prompt.
    """

    backend = FakeMemoryBackend()  # no drawers at all
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    await _seed_graph_canonical(
        ledger,
        graph,
        intent_id="intent:tea",
        subject="用户",
        predicate="likes",
        object_="绿茶",
    )
    await _seed_graph_canonical(
        ledger,
        graph,
        intent_id="intent:coffee",
        subject="用户",
        predicate="likes",
        object_="咖啡",
    )

    result = await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        audiences=OWNER_READ_SCOPE,
        actions=[
            PrivacyAction(
                action="delete_request",
                target="用户 喜欢 绿茶",
                reason="explicit user request",
            )
        ],
        kg=graph,
        canonical_facts=ledger,
    )

    assert result.unmatched_targets == [], "it used to report that it found nothing"
    assert result.statements_forgotten == 1

    remaining = {r.object for r in await graph.query_entity("用户", audiences=("owner",))}
    assert remaining == {"咖啡"}, "only the statement asked about"
    # Reversible, whatever the action said: a sentence match is a looser
    # identification than a stored drawer text, so the reversible half is the
    # right answer to it.
    assert (await graph.stats())["triples_total"] == 2


async def test_forgetting_a_graph_fact_does_not_take_its_turn_mates(graph, tmp_path) -> None:
    """One turn can carry several facts and only one of them was asked about.

    Turn-scoped forgetting is right when a drawer is the thing matched — the
    drawer *is* the turn's fragment. It is wrong when a statement is matched by
    its own sentence, which is why this path forgets by statement.
    """

    backend = FakeMemoryBackend()
    ledger = CanonicalFactLedger(tmp_path / "canonical.sqlite3")
    for obj in ("杭州", "北京"):
        await _seed_graph_canonical(
            ledger,
            graph,
            intent_id=f"intent:{obj}",
            subject="妈妈" if obj == "杭州" else "爸爸",
            predicate="lives_in",
            object_=obj,
        )

    await apply_privacy_actions(
        backend,
        memory_space_id=SPACE,
        audiences=OWNER_READ_SCOPE,
        actions=[PrivacyAction(action="delete_request", target="妈妈 住在 杭州", reason="user")],
        kg=graph,
        canonical_facts=ledger,
    )

    assert (await graph.stats())["triples_active"] == 1
    assert [r.object for r in await graph.query_entity("爸爸", audiences=("owner",))] == ["北京"]
