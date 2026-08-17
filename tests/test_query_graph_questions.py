from __future__ import annotations

import pytest

from hermes_state import SessionDB
from research.evidence_fabric import EvidenceFabricService, EvidenceScope, ResearchRunStatus
from research.query_graph import (
    DependencyAcceptancePolicy,
    QueryGraphIntegrityError,
    QueryGraphLifecycleError,
    QueryGraphService,
    QueryGraphValidationError,
    QuestionCreationReason,
    QuestionRole,
    QuestionWorkflowState,
)


def _services(tmp_path, *, scope_key="scope", agent_id="worker"):
    db = SessionDB(tmp_path / "state.db")
    scope = EvidenceScope(scope_key, "profile", "connection", agent_id)
    return db, EvidenceFabricService(db, scope), QueryGraphService(db, scope)


def _run_and_graphs(tmp_path, count=2):
    db, evidence, service = _services(tmp_path)
    run = evidence.create_research_run("Objective")
    graphs = tuple(
        service.create_graph(run.id, name=f"Graph {i}", purpose=f"Purpose {i}")
        for i in range(count)
    )
    return db, evidence, service, run, graphs


def test_propose_question_deduplicates_run_wide_across_graphs(tmp_path):
    db, evidence, service, run, graphs = _run_and_graphs(tmp_path)
    try:
        first = service.propose_question(
            graphs[0].id,
            "  What   does the statute REQUIRE? ",
            reason=QuestionCreationReason.EVIDENCE_GAP,
        )
        duplicate = service.propose_question(
            graphs[1].id,
            "what does the statute require?",
            reason=QuestionCreationReason.SCOPE_REFINEMENT,
        )
        assert first.created is True
        assert duplicate.created is False
        assert duplicate.question.id == first.question.id
        assert duplicate.question.graph_id == graphs[0].id
        assert len(service.list_questions(graphs[0].id)) == 1
        assert service.list_questions(graphs[1].id) == ()
        assert [e.event_type for e in service.list_events(run.id, question_id=first.question.id)] == ["QUESTION_CREATED"]

        run_two = evidence.create_research_run("Other objective")
        graph_two = service.create_graph(run_two.id, name="Other", purpose="Other")
        other = service.propose_question(graph_two.id, "WHAT DOES THE STATUTE REQUIRE?")
        assert other.created is True
        assert other.question.id != first.question.id
    finally:
        db.close()


def test_propose_question_defaults_optional_and_audits_runtime_actor(tmp_path):
    db, evidence, service = _services(tmp_path, agent_id="scout")
    try:
        run = evidence.create_research_run("Objective")
        graph = service.create_graph(run.id, name="Legal", purpose="Scope")
        optional = service.propose_question(graph.id, "What is the governing rule?")
        required = service.propose_question(
            graph.id,
            "What exceptions apply?",
            role=QuestionRole.REQUIRED,
            reason=QuestionCreationReason.CONTRADICTION,
        )
        assert optional.question.role is QuestionRole.OPTIONAL
        assert required.question.role is QuestionRole.REQUIRED
        assert optional.question.workflow_state is QuestionWorkflowState.OPEN
        assert optional.question.created_by_agent == "scout"
        event = service.list_events(run.id, question_id=required.question.id)[0]
        assert event.event_type == "QUESTION_CREATED"
        assert event.actor_agent == "scout"
        assert event.actor_profile == "profile"
        assert event.reason == QuestionCreationReason.CONTRADICTION.value
        assert event.payload["role"] == QuestionRole.REQUIRED.value
        assert event.payload["question_text"] == "What exceptions apply?"
    finally:
        db.close()


def test_propose_question_rejects_closed_graph_and_terminal_run(tmp_path):
    db, evidence, service = _services(tmp_path)
    try:
        run = evidence.create_research_run("Objective")
        graph = service.create_graph(run.id, name="Legal", purpose="Scope")
        with db._lock:
            db._conn.execute("UPDATE query_graphs SET workflow_state='CLOSED',closed_at=2 WHERE id=?", (graph.id,))
        with pytest.raises(QueryGraphLifecycleError, match="closed query graph"):
            service.propose_question(graph.id, "Late question?")
        with db._lock:
            db._conn.execute("UPDATE query_graphs SET workflow_state='OPEN',closed_at=NULL WHERE id=?", (graph.id,))
        evidence.transition_research_run(run.id, ResearchRunStatus.COMPLETED)
        with pytest.raises(QueryGraphLifecycleError, match="terminal research run"):
            service.propose_question(graph.id, "Post-terminal question?")
    finally:
        db.close()


def test_propose_question_dependencies_are_atomic(tmp_path):
    db, evidence, service, run, graphs = _run_and_graphs(tmp_path)
    try:
        prerequisite = service.propose_question(graphs[0].id, "Prerequisite?").question
        created = service.propose_question(
            graphs[1].id,
            "Dependent?",
            dependencies=((prerequisite.id, DependencyAcceptancePolicy.PARTIAL_OR_BETTER),),
        )
        assert created.created is True
        with db._lock:
            row = db._conn.execute(
                "SELECT dependent_question_id,prerequisite_question_id,acceptance_policy FROM question_dependencies WHERE dependent_question_id=?",
                (created.question.id,),
            ).fetchone()
        assert tuple(row) == (created.question.id, prerequisite.id, DependencyAcceptancePolicy.PARTIAL_OR_BETTER.value)
        assert [e.event_type for e in service.list_events(run.id, question_id=created.question.id)] == ["QUESTION_CREATED", "DEPENDENCY_ADDED"]

        with pytest.raises(QueryGraphIntegrityError, match="prerequisite"):
            service.propose_question(
                graphs[1].id,
                "Must roll back?",
                dependencies=(("missing-question", DependencyAcceptancePolicy.SUPPORTED),),
            )
        assert all(q.question_text != "Must roll back?" for q in service.list_questions(graphs[1].id))
        assert not any(e.payload.get("question_text") == "Must roll back?" for e in service.list_events(run.id))
    finally:
        db.close()


def test_refine_question_preserves_identity_links_dependencies_and_audits(tmp_path):
    db, evidence, service, run, graphs = _run_and_graphs(tmp_path)
    try:
        prerequisite = service.propose_question(graphs[0].id, "Prerequisite?").question
        question = service.propose_question(graphs[0].id, "Broad question?").question
        claim = evidence.create_claim(run.id, "Claim")
        with db._lock:
            db._conn.execute(
                "INSERT INTO question_dependencies (dependent_question_id,prerequisite_question_id,research_run_id,acceptance_policy,created_by_agent,created_by_profile,created_at) VALUES (?,?,?,'SUPPORTED','fixture','profile',1)",
                (question.id, prerequisite.id, run.id),
            )
            db._conn.execute(
                "INSERT INTO question_claim_links (question_id,claim_id,research_run_id,created_by_agent,created_by_profile,created_at) VALUES (?,?,?,'fixture','profile',1)",
                (question.id, claim.id, run.id),
            )
        refined = service.refine_question(question.id, "Narrower question?", reason="evidence narrowed the issue")
        assert refined.id == question.id
        assert refined.question_text == "Narrower question?"
        assert refined.normalized_fingerprint != question.normalized_fingerprint
        with db._lock:
            assert db._conn.execute("SELECT COUNT(*) FROM question_dependencies WHERE dependent_question_id=?", (question.id,)).fetchone()[0] == 1
            assert db._conn.execute("SELECT COUNT(*) FROM question_claim_links WHERE question_id=?", (question.id,)).fetchone()[0] == 1
        event = service.list_events(run.id, question_id=question.id)[-1]
        assert event.event_type == "QUESTION_REFINED"
        assert event.reason == "evidence narrowed the issue"
        assert event.payload["old_text"] == "Broad question?"
        assert event.payload["new_text"] == "Narrower question?"
    finally:
        db.close()


def test_refine_question_rejects_duplicate_target_and_closed_question(tmp_path):
    db, evidence, service, run, graphs = _run_and_graphs(tmp_path)
    try:
        first = service.propose_question(graphs[0].id, "First?").question
        second = service.propose_question(graphs[0].id, "Second?").question
        with pytest.raises(QueryGraphIntegrityError, match="duplicate question"):
            service.refine_question(second.id, " FIRST? ", reason="collision")
        with db._lock:
            db._conn.execute("UPDATE research_questions SET workflow_state='CLOSED',closed_at=2 WHERE id=?", (first.id,))
        with pytest.raises(QueryGraphLifecycleError, match="closed question"):
            service.refine_question(first.id, "Changed?", reason="too late")
    finally:
        db.close()


def test_question_workflow_allows_only_explicit_state_machine(tmp_path):
    db, evidence, service, run, graphs = _run_and_graphs(tmp_path)
    try:
        q = service.propose_question(graphs[0].id, "Workflow?").question
        assert service.start_question(q.id).workflow_state is QuestionWorkflowState.IN_PROGRESS
        with pytest.raises(QueryGraphLifecycleError, match="already IN_PROGRESS"):
            service.start_question(q.id)
        blocked = service.block_question(q.id, reason="awaiting source")
        assert blocked.workflow_state is QuestionWorkflowState.BLOCKED
        assert blocked.blocked_reason == "awaiting source"
        with pytest.raises(QueryGraphLifecycleError, match="already BLOCKED"):
            service.block_question(q.id, reason="again")
        resumed = service.unblock_question(q.id)
        assert resumed.workflow_state is QuestionWorkflowState.IN_PROGRESS
        assert resumed.blocked_reason is None
        with pytest.raises(QueryGraphLifecycleError, match="not blocked"):
            service.unblock_question(q.id)

        q2 = service.propose_question(graphs[0].id, "Block from open?").question
        with pytest.raises(QueryGraphValidationError, match="reason is required"):
            service.block_question(q2.id, reason="  ")
        assert service.block_question(q2.id, reason="external dependency").workflow_state is QuestionWorkflowState.BLOCKED

        q3 = service.propose_question(graphs[0].id, "Cannot unblock open?").question
        with pytest.raises(QueryGraphLifecycleError, match="not blocked"):
            service.unblock_question(q3.id)
        with db._lock:
            db._conn.execute("UPDATE research_questions SET workflow_state='CLOSED',closed_at=2 WHERE id=?", (q3.id,))
        with pytest.raises(QueryGraphLifecycleError, match="closed question"):
            service.start_question(q3.id)
    finally:
        db.close()


def test_question_role_change_requires_reason_and_preserves_dependencies(tmp_path):
    db, evidence, service, run, graphs = _run_and_graphs(tmp_path)
    try:
        prerequisite = service.propose_question(graphs[0].id, "Prerequisite?", role=QuestionRole.REQUIRED).question
        dependent = service.propose_question(graphs[0].id, "Dependent?").question
        with db._lock:
            db._conn.execute(
                "INSERT INTO question_dependencies (dependent_question_id,prerequisite_question_id,research_run_id,acceptance_policy,created_by_agent,created_at) VALUES (?,?,?,'SUPPORTED','fixture',1)",
                (dependent.id, prerequisite.id, run.id),
            )
        with pytest.raises(QueryGraphValidationError, match="reason is required"):
            service.set_question_role(prerequisite.id, QuestionRole.OPTIONAL, reason="")
        changed = service.set_question_role(prerequisite.id, QuestionRole.OPTIONAL, reason="scope narrowed")
        assert changed.role is QuestionRole.OPTIONAL
        with db._lock:
            assert db._conn.execute(
                "SELECT COUNT(*) FROM question_dependencies WHERE dependent_question_id=? AND prerequisite_question_id=?",
                (dependent.id, prerequisite.id),
            ).fetchone()[0] == 1
        event = service.list_events(run.id, question_id=prerequisite.id)[-1]
        assert event.event_type == "QUESTION_ROLE_CHANGED"
        assert event.reason == "scope narrowed"
        assert event.payload == {"old_role": "REQUIRED", "new_role": "OPTIONAL"}
        with pytest.raises(QueryGraphLifecycleError, match="role is already"):
            service.set_question_role(prerequisite.id, QuestionRole.OPTIONAL, reason="no-op")
    finally:
        db.close()
