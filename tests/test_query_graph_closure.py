from __future__ import annotations

import pytest

from hermes_state import SessionDB
from research.evidence_fabric import ClaimStatus, EvidenceFabricService, EvidenceScope, ResearchRunStatus
from research.query_graph import (
    DependencyAcceptancePolicy,
    QueryGraphDependencyError,
    QueryGraphLifecycleError,
    QueryGraphService,
    QuestionResolution,
    QuestionRole,
    QuestionWorkflowState,
)


def _services(tmp_path, *, agent_id="worker"):
    db = SessionDB(tmp_path / "state.db")
    scope = EvidenceScope("scope", "profile", "connection", agent_id)
    return db, EvidenceFabricService(db, scope), QueryGraphService(db, scope)


def _question(tmp_path, *, agent_id="worker"):
    db, evidence, query = _services(tmp_path, agent_id=agent_id)
    run = evidence.create_research_run("Objective")
    graph = query.create_graph(run.id, name="Legal", purpose="Resolve issue")
    question = query.propose_question(graph.id, "What is the answer?").question
    return db, evidence, query, run, graph, question


def _linked_claim(evidence, query, run_id, question_id, text, status):
    claim = evidence.create_claim(run_id, text)
    if status is not ClaimStatus.UNVERIFIED:
        claim = evidence.set_claim_status(claim.id, status)
    query.link_claim(question_id, claim.id)
    return claim


def test_close_rejects_unanswered_unsatisfied_dependency_and_terminal_run(tmp_path):
    db, evidence, query, run, graph, question = _question(tmp_path)
    try:
        with pytest.raises(QueryGraphLifecycleError, match="UNANSWERED"):
            query.close_question(question.id)

        supported = _linked_claim(
            evidence, query, run.id, question.id, "answer", ClaimStatus.SUPPORTED
        )
        prerequisite = query.propose_question(graph.id, "Prerequisite?").question
        query.add_dependency(
            question.id,
            prerequisite.id,
            acceptance_policy=DependencyAcceptancePolicy.SUPPORTED,
        )
        with pytest.raises(QueryGraphDependencyError, match="does not satisfy SUPPORTED"):
            query.close_question(question.id)

        # Removing the dependency restores evidence eligibility, but terminal
        # ResearchRun lifecycle still owns the final write gate.
        query.remove_dependency(question.id, prerequisite.id)
        evidence.transition_research_run(run.id, ResearchRunStatus.COMPLETED)
        with pytest.raises(QueryGraphLifecycleError, match="terminal research run"):
            query.close_question(question.id)
        assert evidence.get_claim(supported.id).status is ClaimStatus.SUPPORTED
    finally:
        db.close()


@pytest.mark.parametrize(
    "state,status,resolution",
    [
        (QuestionWorkflowState.OPEN, ClaimStatus.SUPPORTED, QuestionResolution.SUPPORTED),
        (
            QuestionWorkflowState.IN_PROGRESS,
            ClaimStatus.PARTIALLY_SUPPORTED,
            QuestionResolution.PARTIALLY_ANSWERED,
        ),
        (
            QuestionWorkflowState.BLOCKED,
            ClaimStatus.CONTRADICTED,
            QuestionResolution.CONTESTED,
        ),
    ],
)
def test_any_active_state_may_close_when_deterministic_gates_pass(
    tmp_path, state, status, resolution
):
    db, evidence, query, run, graph, question = _question(tmp_path)
    try:
        _linked_claim(evidence, query, run.id, question.id, "answer", status)
        if state is QuestionWorkflowState.IN_PROGRESS:
            query.start_question(question.id)
        elif state is QuestionWorkflowState.BLOCKED:
            query.block_question(question.id, reason="waiting")

        closed = query.close_question(question.id)
        assert closed.workflow_state is QuestionWorkflowState.CLOSED
        assert closed.closed_resolution is resolution
        assert closed.closed_at is not None
        assert closed.closed_by_agent == "worker"
        assert closed.closed_by_profile == "profile"
        assert closed.blocked_reason is None
        view = query.derive_resolution(question.id)
        assert view.resolution is resolution
        assert view.stale is False
    finally:
        db.close()


def test_closure_snapshots_every_linked_claim_exactly(tmp_path):
    db, evidence, query, run, graph, question = _question(tmp_path, agent_id="verifier")
    try:
        supported = _linked_claim(
            evidence, query, run.id, question.id, "supported", ClaimStatus.SUPPORTED
        )
        partial = _linked_claim(
            evidence,
            query,
            run.id,
            question.id,
            "partial",
            ClaimStatus.PARTIALLY_SUPPORTED,
        )

        closed = query.close_question(question.id)
        assert closed.closed_resolution is QuestionResolution.PARTIALLY_ANSWERED
        assert closed.closed_by_agent == "verifier"
        assert closed.closed_by_profile == "profile"

        with db._lock:
            rows = db._conn.execute(
                "SELECT claim_id,claim_status_at_close,claim_updated_at_at_close,snapshot_at "
                "FROM question_closure_claims WHERE question_id=? ORDER BY claim_id",
                (question.id,),
            ).fetchall()
        expected = {
            supported.id: (ClaimStatus.SUPPORTED.value, supported.updated_at.timestamp()),
            partial.id: (
                ClaimStatus.PARTIALLY_SUPPORTED.value,
                partial.updated_at.timestamp(),
            ),
        }
        assert len(rows) == 2
        assert {row["claim_id"] for row in rows} == set(expected)
        for row in rows:
            status, updated_at = expected[row["claim_id"]]
            assert row["claim_status_at_close"] == status
            assert row["claim_updated_at_at_close"] == updated_at
            assert row["snapshot_at"] == closed.closed_at.timestamp()

        event = query.list_events(run.id, question_id=question.id)[-1]
        assert event.event_type == "QUESTION_CLOSED"
        assert event.actor_agent == "verifier"
        assert event.payload["resolution"] == QuestionResolution.PARTIALLY_ANSWERED.value
        assert set(event.payload["claim_ids"]) == {supported.id, partial.id}
    finally:
        db.close()


def test_contested_closure_only_satisfies_any_closed_dependency(tmp_path):
    db, evidence, query, run, graph, prerequisite = _question(tmp_path)
    try:
        _linked_claim(
            evidence,
            query,
            run.id,
            prerequisite.id,
            "contradicted answer",
            ClaimStatus.CONTRADICTED,
        )
        prerequisite = query.close_question(prerequisite.id)
        assert prerequisite.closed_resolution is QuestionResolution.CONTESTED

        downstream = [
            query.propose_question(graph.id, f"Downstream {index}?").question
            for index in range(3)
        ]
        policies = (
            DependencyAcceptancePolicy.SUPPORTED,
            DependencyAcceptancePolicy.PARTIAL_OR_BETTER,
            DependencyAcceptancePolicy.ANY_CLOSED,
        )
        edges = [
            query.add_dependency(question.id, prerequisite.id, acceptance_policy=policy)
            for question, policy in zip(downstream, policies)
        ]
        assert [query._dependency_is_satisfied(edge) for edge in edges] == [
            False,
            False,
            True,
        ]
    finally:
        db.close()


def test_closed_question_is_immutable_until_future_reopen_rules(tmp_path):
    db, evidence, query, run, graph, question = _question(tmp_path)
    try:
        claim = _linked_claim(
            evidence, query, run.id, question.id, "answer", ClaimStatus.SUPPORTED
        )
        late = evidence.create_claim(run.id, "late")
        query.close_question(question.id)

        with pytest.raises(QueryGraphLifecycleError, match="closed question"):
            query.link_claim(question.id, late.id)
        with pytest.raises(QueryGraphLifecycleError, match="closed question"):
            query.unlink_claim(question.id, claim.id)
        with pytest.raises(QueryGraphLifecycleError, match="closed question"):
            query.refine_question(question.id, "Changed?", reason="too late")
        with pytest.raises(QueryGraphLifecycleError, match="closed question"):
            query.set_question_role(
                question.id, QuestionRole.REQUIRED, reason="too late"
            )
        with pytest.raises(QueryGraphLifecycleError, match="closed question"):
            query.start_question(question.id)
        with pytest.raises(QueryGraphLifecycleError, match="closed question"):
            query.block_question(question.id, reason="too late")
        with pytest.raises(QueryGraphLifecycleError, match="closed question"):
            query.close_question(question.id)

        # Evidence Fabric may legitimately evolve while the run stays open;
        # Query Graph history must not silently rewrite its closure snapshot.
        evidence.set_claim_status(claim.id, ClaimStatus.CONTRADICTED)
        closed = query.get_question(question.id)
        assert closed.workflow_state is QuestionWorkflowState.CLOSED
        assert closed.closed_resolution is QuestionResolution.SUPPORTED
        assert query.derive_resolution(question.id).stale is True
    finally:
        db.close()


def test_close_question_rechecks_dependencies_inside_the_write_transaction(tmp_path):
    db, evidence, query, run, graph, question = _question(tmp_path)
    try:
        _linked_claim(evidence, query, run.id, question.id, "answer", ClaimStatus.SUPPORTED)
        prerequisite = query.propose_question(graph.id, "Prerequisite?").question
        _linked_claim(
            evidence,
            query,
            run.id,
            prerequisite.id,
            "prerequisite answer",
            ClaimStatus.SUPPORTED,
        )
        query.close_question(prerequisite.id)
        query.add_dependency(question.id, prerequisite.id)

        assert query.close_question(question.id).closed_resolution is QuestionResolution.SUPPORTED
    finally:
        db.close()
