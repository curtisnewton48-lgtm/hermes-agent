from __future__ import annotations

import pytest

from hermes_state import SessionDB
from research.evidence_fabric import ClaimStatus, EvidenceFabricService, EvidenceScope, ResearchRunStatus
from research.query_graph import (
    DependencyAcceptancePolicy,
    QueryGraphCycleError,
    QueryGraphDependencyError,
    QueryGraphIntegrityError,
    QueryGraphLifecycleError,
    QueryGraphService,
    QuestionResolution,
    QuestionWorkflowState,
)


def _services(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    scope = EvidenceScope("scope", "profile", "connection", "agent")
    return db, EvidenceFabricService(db, scope), QueryGraphService(db, scope)


def _run_graphs_questions(tmp_path, graph_count=3, question_count=5):
    db, evidence, service = _services(tmp_path)
    run = evidence.create_research_run("Objective")
    graphs = [
        service.create_graph(run.id, name=f"Graph {i}", purpose=f"Purpose {i}")
        for i in range(graph_count)
    ]
    questions = [
        service.propose_question(
            graphs[i % graph_count].id, f"Question {i}?"
        ).question
        for i in range(question_count)
    ]
    return db, evidence, service, run, graphs, questions


def test_valid_run_wide_topologies_cross_graph_diamond_and_removal(tmp_path):
    db, evidence, service, run, graphs, q = _run_graphs_questions(tmp_path)
    try:
        # q0 depends on q1 and q2; q1/q2 share q3. q4 is an independent chain
        # leaf. This spans all three owning graphs and has multiple parents.
        e01 = service.add_dependency(q[0].id, q[1].id)
        e02 = service.add_dependency(
            q[0].id,
            q[2].id,
            acceptance_policy=DependencyAcceptancePolicy.PARTIAL_OR_BETTER,
        )
        e13 = service.add_dependency(q[1].id, q[3].id)
        e23 = service.add_dependency(q[2].id, q[3].id)
        e34 = service.add_dependency(q[3].id, q[4].id)

        assert e01.research_run_id == run.id
        assert e02.acceptance_policy is DependencyAcceptancePolicy.PARTIAL_OR_BETTER
        assert {edge.prerequisite_question_id for edge in service.list_dependencies(q[0].id)} == {
            q[1].id,
            q[2].id,
        }
        assert service.list_dependencies(q[1].id) == (e13,)
        assert service.list_dependencies(q[2].id) == (e23,)
        assert service.list_dependencies(q[3].id) == (e34,)

        service.remove_dependency(q[0].id, q[2].id)
        assert service.list_dependencies(q[0].id) == (e01,)
        events = service.list_events(run.id, question_id=q[0].id)
        assert [event.event_type for event in events][-3:] == [
            "DEPENDENCY_ADDED",
            "DEPENDENCY_ADDED",
            "DEPENDENCY_REMOVED",
        ]
    finally:
        db.close()


def test_self_two_node_and_long_cross_graph_cycles_are_rejected(tmp_path):
    db, evidence, service, run, graphs, q = _run_graphs_questions(tmp_path)
    try:
        with pytest.raises(QueryGraphCycleError, match="itself"):
            service.add_dependency(q[0].id, q[0].id)

        service.add_dependency(q[0].id, q[1].id)
        with pytest.raises(QueryGraphCycleError, match="cycle"):
            service.add_dependency(q[1].id, q[0].id)

        service.add_dependency(q[1].id, q[2].id)
        service.add_dependency(q[2].id, q[3].id)
        with pytest.raises(QueryGraphCycleError, match="cycle"):
            service.add_dependency(q[3].id, q[0].id)

        # Rejections leave no partial edges/events behind.
        assert all(
            edge.prerequisite_question_id != q[0].id
            for edge in service.list_dependencies(q[3].id)
        )
    finally:
        db.close()


def test_foreign_run_duplicate_and_missing_dependencies_are_domain_errors(tmp_path):
    db, evidence, service, run, graphs, q = _run_graphs_questions(tmp_path)
    try:
        other_run = evidence.create_research_run("Other")
        other_graph = service.create_graph(other_run.id, name="Other", purpose="Other")
        other_q = service.propose_question(other_graph.id, "Other question?").question

        with pytest.raises(QueryGraphIntegrityError, match="different research runs"):
            service.add_dependency(q[0].id, other_q.id)
        with pytest.raises(QueryGraphIntegrityError, match="prerequisite"):
            service.add_dependency(q[0].id, "missing")

        service.add_dependency(q[0].id, q[1].id)
        with pytest.raises(QueryGraphDependencyError, match="already exists"):
            service.add_dependency(q[0].id, q[1].id)
        with pytest.raises(QueryGraphDependencyError, match="does not exist"):
            service.remove_dependency(q[0].id, q[2].id)
    finally:
        db.close()


def test_closed_dependent_and_terminal_run_reject_dependency_mutation(tmp_path):
    db, evidence, service, run, graphs, q = _run_graphs_questions(tmp_path)
    try:
        with db._lock:
            db._conn.execute(
                "UPDATE research_questions SET workflow_state='CLOSED',"
                "closed_resolution='SUPPORTED',closed_at=2 WHERE id=?",
                (q[0].id,),
            )
        with pytest.raises(QueryGraphLifecycleError, match="closed dependent"):
            service.add_dependency(q[0].id, q[1].id)

        with db._lock:
            db._conn.execute(
                "UPDATE research_questions SET workflow_state='OPEN',"
                "closed_resolution=NULL,closed_at=NULL WHERE id=?",
                (q[0].id,),
            )
        service.add_dependency(q[0].id, q[1].id)
        with db._lock:
            db._conn.execute(
                "UPDATE research_questions SET workflow_state='CLOSED',"
                "closed_resolution='SUPPORTED',closed_at=2 WHERE id=?",
                (q[0].id,),
            )
        with pytest.raises(QueryGraphLifecycleError, match="closed dependent"):
            service.remove_dependency(q[0].id, q[1].id)

        with db._lock:
            db._conn.execute(
                "UPDATE research_questions SET workflow_state='OPEN',"
                "closed_resolution=NULL,closed_at=NULL WHERE id=?",
                (q[0].id,),
            )
        evidence.transition_research_run(run.id, ResearchRunStatus.COMPLETED)
        with pytest.raises(QueryGraphLifecycleError, match="terminal research run"):
            service.add_dependency(q[0].id, q[2].id)
    finally:
        db.close()


def _close_prerequisite_fixture(db, evidence, question, resolution, claim_status):
    claim = evidence.create_claim(question.research_run_id, f"claim {question.id}")
    claim = evidence.set_claim_status(claim.id, claim_status)
    with db._lock:
        db._conn.execute(
            "INSERT INTO question_claim_links "
            "(question_id,claim_id,research_run_id,created_by_agent,created_at) "
            "VALUES (?,?,?,'fixture',1)",
            (question.id, claim.id, question.research_run_id),
        )
        db._conn.execute(
            "INSERT INTO question_closure_claims "
            "(question_id,claim_id,research_run_id,claim_status_at_close,"
            "claim_updated_at_at_close,snapshot_at) VALUES (?,?,?,?,?,2)",
            (
                question.id,
                claim.id,
                question.research_run_id,
                claim.status.value,
                claim.updated_at.timestamp(),
            ),
        )
        db._conn.execute(
            "UPDATE research_questions SET workflow_state='CLOSED',"
            "closed_resolution=?,closed_at=2 WHERE id=?",
            (resolution.value, question.id),
        )
    return claim


@pytest.mark.parametrize(
    "resolution,policy,expected",
    [
        (QuestionResolution.SUPPORTED, DependencyAcceptancePolicy.SUPPORTED, True),
        (QuestionResolution.PARTIALLY_ANSWERED, DependencyAcceptancePolicy.SUPPORTED, False),
        (QuestionResolution.CONTESTED, DependencyAcceptancePolicy.SUPPORTED, False),
        (QuestionResolution.SUPPORTED, DependencyAcceptancePolicy.PARTIAL_OR_BETTER, True),
        (QuestionResolution.PARTIALLY_ANSWERED, DependencyAcceptancePolicy.PARTIAL_OR_BETTER, True),
        (QuestionResolution.CONTESTED, DependencyAcceptancePolicy.PARTIAL_OR_BETTER, False),
        (QuestionResolution.SUPPORTED, DependencyAcceptancePolicy.ANY_CLOSED, True),
        (QuestionResolution.PARTIALLY_ANSWERED, DependencyAcceptancePolicy.ANY_CLOSED, True),
        (QuestionResolution.CONTESTED, DependencyAcceptancePolicy.ANY_CLOSED, True),
        (QuestionResolution.UNANSWERED, DependencyAcceptancePolicy.ANY_CLOSED, False),
    ],
)
def test_dependency_acceptance_policy_mapping(tmp_path, resolution, policy, expected):
    db, evidence, service, run, graphs, q = _run_graphs_questions(tmp_path, question_count=2)
    try:
        prerequisite = q[1]
        status = {
            QuestionResolution.SUPPORTED: ClaimStatus.SUPPORTED,
            QuestionResolution.PARTIALLY_ANSWERED: ClaimStatus.PARTIALLY_SUPPORTED,
            QuestionResolution.CONTESTED: ClaimStatus.CONTRADICTED,
            QuestionResolution.UNANSWERED: ClaimStatus.UNVERIFIED,
        }[resolution]
        _close_prerequisite_fixture(db, evidence, prerequisite, resolution, status)
        edge = service.add_dependency(q[0].id, prerequisite.id, acceptance_policy=policy)
        assert service._dependency_is_satisfied(edge) is expected
    finally:
        db.close()


def test_stale_prerequisite_fails_every_acceptance_policy(tmp_path):
    db, evidence, service, run, graphs, q = _run_graphs_questions(tmp_path, question_count=4)
    try:
        prerequisite = q[3]
        claim = _close_prerequisite_fixture(
            db, evidence, prerequisite, QuestionResolution.SUPPORTED, ClaimStatus.SUPPORTED
        )
        edges = [
            service.add_dependency(q[i].id, prerequisite.id, acceptance_policy=policy)
            for i, policy in enumerate(
                (
                    DependencyAcceptancePolicy.SUPPORTED,
                    DependencyAcceptancePolicy.PARTIAL_OR_BETTER,
                    DependencyAcceptancePolicy.ANY_CLOSED,
                )
            )
        ]
        assert all(service._dependency_is_satisfied(edge) for edge in edges)

        evidence.set_claim_status(claim.id, ClaimStatus.CONTRADICTED)
        assert not any(service._dependency_is_satisfied(edge) for edge in edges)
    finally:
        db.close()


def test_dependency_does_not_block_parallel_investigation(tmp_path):
    db, evidence, service, run, graphs, q = _run_graphs_questions(tmp_path, question_count=2)
    try:
        edge = service.add_dependency(q[0].id, q[1].id)
        assert service._dependency_is_satisfied(edge) is False

        started = service.start_question(q[0].id)
        assert started.workflow_state is QuestionWorkflowState.IN_PROGRESS
        blocked = service.block_question(q[0].id, reason="waiting on another source")
        assert blocked.workflow_state is QuestionWorkflowState.BLOCKED
        # Task 8 binds the same satisfaction helper into close_question().
    finally:
        db.close()
