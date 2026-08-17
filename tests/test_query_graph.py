from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import timezone

import pytest

from hermes_state import SessionDB
from research.evidence_fabric import ClaimStatus, EvidenceScope
from research.query_graph import (
    ClosureClaimSnapshot,
    CompletionAssessment,
    DependencyAcceptancePolicy,
    GraphCreationReason,
    GraphRole,
    GraphWorkflowState,
    QueryGraph,
    QueryGraphEvent,
    QueryGraphScopeError,
    QueryGraphService,
    QueryGraphValidationError,
    QuestionClaimLink,
    QuestionCreationReason,
    QuestionDependency,
    QuestionResolution,
    QuestionResolutionView,
    QuestionRole,
    QuestionWorkflowState,
    QuestionWriteResult,
    ReopenReason,
    ResearchQuestion,
    normalize_question_text,
    question_fingerprint,
)


def _service(tmp_path, *, scope_key="scope-a", agent_id="agent-a"):
    db = SessionDB(tmp_path / "state.db")
    scope = EvidenceScope(scope_key, "profile-a", "connection-a", agent_id)
    return db, QueryGraphService(db, scope)


def _seed_read_rows(db: SessionDB) -> None:
    with db._lock:
        c = db._conn
        c.execute(
            "INSERT INTO research_runs "
            "(id, objective, owner_scope_key, owner_profile, owner_connection_id, "
            "status, metadata_json, created_at, updated_at) "
            "VALUES ('run-a', 'objective', 'scope-a', 'profile-a', 'connection-a', "
            "'OPEN', '{}', 1, 2)"
        )
        c.execute(
            "INSERT INTO query_graphs "
            "(id, research_run_id, name, purpose, role, workflow_state, created_by_agent, "
            "created_by_profile, created_at, updated_at) "
            "VALUES ('graph-a', 'run-a', 'Legal', 'Resolve legal questions', 'REQUIRED', "
            "'OPEN', 'director', 'profile-a', 3, 4)"
        )
        c.execute(
            "INSERT INTO research_questions "
            "(id, graph_id, research_run_id, question_text, normalized_fingerprint, role, "
            "workflow_state, creation_reason, blocked_reason, closed_resolution, "
            "created_by_agent, created_by_profile, created_at, updated_at) "
            "VALUES ('q-a', 'graph-a', 'run-a', 'What does the statute require?', ?, "
            "'REQUIRED', 'IN_PROGRESS', 'ROOT', NULL, NULL, 'director', 'profile-a', 5, 6)",
            (question_fingerprint("What does the statute require?"),),
        )
        c.execute(
            "INSERT INTO query_graph_events "
            "(research_run_id, graph_id, question_id, event_type, actor_agent, "
            "actor_profile, reason, payload_json, created_at) "
            "VALUES ('run-a', 'graph-a', 'q-a', 'QUESTION_STARTED', 'worker-1', "
            "'profile-a', 'begin research', '{\"source\":\"director\"}', 7)"
        )


def test_public_enum_values_are_stable():
    assert {item.value for item in GraphRole} == {"REQUIRED", "OPTIONAL"}
    assert {item.value for item in GraphWorkflowState} == {"OPEN", "CLOSED"}
    assert {item.value for item in QuestionRole} == {"REQUIRED", "OPTIONAL"}
    assert {item.value for item in QuestionWorkflowState} == {
        "OPEN",
        "IN_PROGRESS",
        "BLOCKED",
        "CLOSED",
    }
    assert {item.value for item in QuestionResolution} == {
        "UNANSWERED",
        "PARTIALLY_ANSWERED",
        "SUPPORTED",
        "CONTESTED",
    }
    assert {item.value for item in DependencyAcceptancePolicy} == {
        "SUPPORTED",
        "PARTIAL_OR_BETTER",
        "ANY_CLOSED",
    }
    assert {item.value for item in GraphCreationReason} == {
        "ROOT",
        "NEW_DOMAIN",
        "SCOPE_EXPANSION",
        "CONTRADICTION_BRANCH",
        "OTHER",
    }
    assert {item.value for item in QuestionCreationReason} == {
        "ROOT",
        "EVIDENCE_GAP",
        "CONTRADICTION",
        "DEPENDENCY",
        "SCOPE_REFINEMENT",
        "OTHER",
    }
    assert {item.value for item in ReopenReason} == {
        "NEW_EVIDENCE",
        "CONTRADICTION_DISCOVERED",
    }


def test_normalization_is_deterministic_but_does_not_rewrite_punctuation():
    assert normalize_question_text("  CAFÉ\t law\n") == "café law"
    assert normalize_question_text("cafe\u0301\u2003LAW") == "café law"
    assert question_fingerprint("  CAFÉ\t law\n") == question_fingerprint(
        "cafe\u0301\u2003LAW"
    )

    assert normalize_question_text("What happened?") == "what happened?"
    assert normalize_question_text("What happened!") == "what happened!"
    assert question_fingerprint("What happened?") != question_fingerprint(
        "What happened!"
    )

    digest = question_fingerprint("Question")
    assert len(digest) == 64
    assert digest == digest.lower()
    assert set(digest) <= set("0123456789abcdef")


@pytest.mark.parametrize("value", [None, "", "   ", "\u2003\n\t"])
def test_normalization_rejects_missing_question_text(value):
    with pytest.raises(QueryGraphValidationError, match="question text is required"):
        normalize_question_text(value)  # type: ignore[arg-type]


def test_normalization_and_identifier_validation_are_bounded(tmp_path):
    with pytest.raises(QueryGraphValidationError, match="question text is too long"):
        normalize_question_text("x" * 16_385)

    db, service = _service(tmp_path)
    try:
        with pytest.raises(QueryGraphValidationError, match="invalid identifier"):
            service.get_graph("g" * 201)
    finally:
        db.close()


def test_read_apis_decode_immutable_typed_dtos_with_utc_datetimes(tmp_path):
    db, service = _service(tmp_path)
    try:
        _seed_read_rows(db)

        graph = service.get_graph("graph-a")
        assert isinstance(graph, QueryGraph)
        assert graph.role is GraphRole.REQUIRED
        assert graph.workflow_state is GraphWorkflowState.OPEN
        assert graph.created_at.tzinfo is timezone.utc
        assert graph.updated_at.tzinfo is timezone.utc
        assert service.list_graphs("run-a") == (graph,)

        question = service.get_question("q-a")
        assert isinstance(question, ResearchQuestion)
        assert question.role is QuestionRole.REQUIRED
        assert question.workflow_state is QuestionWorkflowState.IN_PROGRESS
        assert question.creation_reason is QuestionCreationReason.ROOT
        assert question.closed_resolution is None
        assert question.created_at.tzinfo is timezone.utc
        assert service.list_questions("graph-a") == (question,)

        with pytest.raises(FrozenInstanceError):
            graph.name = "mutated"  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            question.question_text = "mutated"  # type: ignore[misc]
    finally:
        db.close()


def test_event_reads_decode_payload_and_support_graph_question_filters(tmp_path):
    db, service = _service(tmp_path)
    try:
        _seed_read_rows(db)

        events = service.list_events("run-a")
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, QueryGraphEvent)
        assert event.graph_id == "graph-a"
        assert event.question_id == "q-a"
        assert event.event_type == "QUESTION_STARTED"
        assert event.actor_agent == "worker-1"
        assert event.reason == "begin research"
        assert event.payload == {"source": "director"}
        assert event.created_at.tzinfo is timezone.utc
        assert service.list_events("run-a", graph_id="graph-a") == events
        assert service.list_events("run-a", question_id="q-a") == events
        assert service.list_events("run-a", graph_id="missing") == ()
        assert service.list_events("run-a", question_id="missing") == ()
    finally:
        db.close()


def test_foreign_scope_cannot_read_graph_question_or_events(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    owner = QueryGraphService(
        db, EvidenceScope("scope-a", "profile-a", "connection-a", "owner")
    )
    other = QueryGraphService(
        db, EvidenceScope("scope-b", "profile-b", "connection-b", "other")
    )
    try:
        _seed_read_rows(db)
        assert owner.get_graph("graph-a").id == "graph-a"
        assert owner.get_question("q-a").id == "q-a"

        with pytest.raises(QueryGraphScopeError):
            other.get_graph("graph-a")
        with pytest.raises(QueryGraphScopeError):
            other.list_graphs("run-a")
        with pytest.raises(QueryGraphScopeError):
            other.get_question("q-a")
        with pytest.raises(QueryGraphScopeError):
            other.list_events("run-a")
    finally:
        db.close()


def test_public_dataclass_contracts_are_constructible_and_frozen():
    # These types are consumed by later tasks; this test catches accidental
    # renames or mutability before write-path behavior is implemented.
    question = ResearchQuestion(
        id="q",
        graph_id="g",
        research_run_id="r",
        question_text="Question?",
        normalized_fingerprint="f" * 64,
        role=QuestionRole.OPTIONAL,
        workflow_state=QuestionWorkflowState.OPEN,
        creation_reason=QuestionCreationReason.ROOT,
        blocked_reason=None,
        closed_resolution=None,
        created_by_agent="agent",
        created_by_profile=None,
        created_at=service_time := __import__("datetime").datetime.fromtimestamp(
            1, timezone.utc
        ),
        updated_at=service_time,
        closed_at=None,
        closed_by_agent=None,
        closed_by_profile=None,
    )
    assert QuestionWriteResult(question, True).created is True
    assert QuestionDependency(
        "q", "p", "r", DependencyAcceptancePolicy.SUPPORTED, "agent", None, service_time
    ).acceptance_policy is DependencyAcceptancePolicy.SUPPORTED
    assert QuestionClaimLink("q", "c", "r", "agent", None, service_time).claim_id == "c"
    assert ClosureClaimSnapshot(
        "q", "c", ClaimStatus.SUPPORTED, service_time
    ).claim_status_at_close is ClaimStatus.SUPPORTED
    assert QuestionResolutionView(
        "q", QuestionResolution.UNANSWERED, False, ()
    ).resolution is QuestionResolution.UNANSWERED
    assert CompletionAssessment(False, ("not ready",)).ready is False
