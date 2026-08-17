from __future__ import annotations

import pytest

from hermes_state import SessionDB
from research.evidence_fabric import ClaimStatus, EvidenceFabricService, EvidenceScope, ResearchRunStatus
from research.query_graph import (
    DependencyAcceptancePolicy,
    GraphRole,
    GraphWorkflowState,
    QueryGraphLifecycleError,
    QueryGraphService,
    QuestionResolution,
    QuestionRole,
    QuestionWorkflowState,
    ReopenReason,
)


def _services(tmp_path, *, agent_id="director"):
    db = SessionDB(tmp_path / "state.db")
    scope = EvidenceScope("scope", "profile", "connection", agent_id)
    return db, EvidenceFabricService(db, scope), QueryGraphService(db, scope)


def _graph(tmp_path, *, role=GraphRole.REQUIRED, agent_id="director"):
    db, evidence, query = _services(tmp_path, agent_id=agent_id)
    run = evidence.create_research_run("Objective")
    graph = query.create_graph(
        run.id, name="Legal", purpose="Resolve issue", role=role
    )
    return db, evidence, query, run, graph


def _question(query, graph, text, *, role=QuestionRole.REQUIRED):
    return query.propose_question(graph.id, text, role=role).question


def _support_and_close(evidence, query, run, question, *, status=ClaimStatus.SUPPORTED):
    claim = evidence.create_claim(run.id, f"claim for {question.id}")
    if status is not ClaimStatus.UNVERIFIED:
        claim = evidence.set_claim_status(claim.id, status)
    query.link_claim(question.id, claim.id)
    return query.close_question(question.id), claim


def test_graph_completion_matrix_required_closed_optional_open_is_ready(tmp_path):
    db, evidence, query, run, graph = _graph(tmp_path)
    try:
        required = _question(query, graph, "Required?")
        optional = _question(query, graph, "Optional?", role=QuestionRole.OPTIONAL)
        required, _ = _support_and_close(evidence, query, run, required)

        assessment = query.assess_graph_completion(graph.id)
        assert assessment.ready is True
        assert assessment.reasons == ()
        assert query.get_question(optional.id).workflow_state is QuestionWorkflowState.OPEN
    finally:
        db.close()


@pytest.mark.parametrize(
    "state,expected_reason",
    [
        (QuestionWorkflowState.OPEN, "is OPEN"),
        (QuestionWorkflowState.BLOCKED, "is BLOCKED"),
    ],
)
def test_required_active_question_blocks_graph_with_stable_reason(
    tmp_path, state, expected_reason
):
    db, evidence, query, run, graph = _graph(tmp_path)
    try:
        question = _question(query, graph, "Required?")
        if state is QuestionWorkflowState.BLOCKED:
            query.block_question(question.id, reason="external source")

        assessment = query.assess_graph_completion(graph.id)
        assert assessment.ready is False
        assert assessment.reasons == (
            f"required question {question.id} {expected_reason}",
        )
    finally:
        db.close()


def test_stale_required_question_blocks_graph_but_contested_fresh_question_can_be_ready(tmp_path):
    db, evidence, query, run, graph = _graph(tmp_path)
    try:
        stale_q = _question(query, graph, "Stale?")
        stale_q, stale_claim = _support_and_close(evidence, query, run, stale_q)
        evidence.set_claim_status(stale_claim.id, ClaimStatus.CONTRADICTED)

        assessment = query.assess_graph_completion(graph.id)
        assert assessment.ready is False
        assert assessment.reasons == (
            f"required question {stale_q.id} has stale closure basis",
        )

        query.reopen_question(stale_q.id, ReopenReason.CONTRADICTION_DISCOVERED)
        # Move the stale question out of the graph's required set so the matrix
        # can independently prove a fresh contested closure is acceptable.
        query.set_question_role(stale_q.id, QuestionRole.OPTIONAL, reason="scope narrowed")
        contested = _question(query, graph, "Contested?")
        contested, _ = _support_and_close(
            evidence, query, run, contested, status=ClaimStatus.CONTRADICTED
        )
        assert contested.closed_resolution is QuestionResolution.CONTESTED
        assert query.assess_graph_completion(graph.id).ready is True
    finally:
        db.close()


def test_cross_graph_optional_prerequisite_is_part_of_required_live_path(tmp_path):
    db, evidence, query = _services(tmp_path)
    try:
        run = evidence.create_research_run("Objective")
        legal = query.create_graph(
            run.id, name="Legal", purpose="Legal", role=GraphRole.REQUIRED
        )
        technical = query.create_graph(
            run.id, name="Technical", purpose="Technical", role=GraphRole.OPTIONAL
        )
        legal_q = _question(query, legal, "Legal conclusion?")
        technical_q = _question(
            query, technical, "Technical premise?", role=QuestionRole.OPTIONAL
        )

        edge = query.add_dependency(
            legal_q.id,
            technical_q.id,
            acceptance_policy=DependencyAcceptancePolicy.SUPPORTED,
        )
        assessment = query.assess_graph_completion(legal.id)
        assert assessment.ready is False
        assert assessment.reasons == (
            f"required question {legal_q.id} is OPEN",
            f"question {legal_q.id} prerequisite {technical_q.id} does not satisfy SUPPORTED",
        )

        technical_q, technical_claim = _support_and_close(
            evidence, query, run, technical_q
        )
        legal_q, _ = _support_and_close(evidence, query, run, legal_q)
        assert query._dependency_is_satisfied(edge) is True
        assert query.assess_graph_completion(legal.id).ready is True

        # A dependency can become unsatisfied after the downstream answer was
        # closed. Current readiness must catch that live structural break.
        evidence.set_claim_status(technical_claim.id, ClaimStatus.CONTRADICTED)
        assessment = query.assess_graph_completion(legal.id)
        assert assessment.ready is False
        assert assessment.reasons == (
            f"question {legal_q.id} prerequisite {technical_q.id} does not satisfy SUPPORTED",
        )
    finally:
        db.close()


def test_nested_cross_graph_dependency_path_is_rechecked_recursively(tmp_path):
    db, evidence, query = _services(tmp_path)
    try:
        run = evidence.create_research_run("Objective")
        required_graph = query.create_graph(
            run.id, name="Required", purpose="Required", role=GraphRole.REQUIRED
        )
        optional_graph = query.create_graph(
            run.id, name="Optional", purpose="Optional", role=GraphRole.OPTIONAL
        )
        root = _question(query, required_graph, "Root?")
        middle = _question(query, optional_graph, "Middle?", role=QuestionRole.OPTIONAL)
        leaf = _question(query, optional_graph, "Leaf?", role=QuestionRole.OPTIONAL)
        query.add_dependency(root.id, middle.id)
        query.add_dependency(middle.id, leaf.id)

        leaf, leaf_claim = _support_and_close(evidence, query, run, leaf)
        middle, _ = _support_and_close(evidence, query, run, middle)
        root, _ = _support_and_close(evidence, query, run, root)
        assert query.assess_graph_completion(required_graph.id).ready is True

        evidence.set_claim_status(leaf_claim.id, ClaimStatus.CONTRADICTED)
        assessment = query.assess_graph_completion(required_graph.id)
        assert assessment.ready is False
        assert assessment.reasons == (
            f"question {middle.id} prerequisite {leaf.id} does not satisfy SUPPORTED",
        )
    finally:
        db.close()


def test_close_graph_rechecks_ready_state_and_records_runtime_provenance(tmp_path):
    db, evidence, query, run, graph = _graph(tmp_path, agent_id="director")
    try:
        question = _question(query, graph, "Required?")
        question, _ = _support_and_close(evidence, query, run, question)

        closed = query.close_graph(graph.id)
        assert closed.workflow_state is GraphWorkflowState.CLOSED
        assert closed.closed_at is not None
        assert closed.closed_by_agent == "director"
        assert closed.closed_by_profile == "profile"
        event = query.list_events(run.id, graph_id=graph.id)[-1]
        assert event.event_type == "GRAPH_CLOSED"
        assert event.actor_agent == "director"
        assert event.payload == {"required_question_ids": [question.id]}

        with pytest.raises(QueryGraphLifecycleError, match="already closed"):
            query.close_graph(graph.id)
    finally:
        db.close()


def test_close_graph_rejects_unready_and_terminal_run(tmp_path):
    db, evidence, query, run, graph = _graph(tmp_path)
    try:
        question = _question(query, graph, "Required?")
        with pytest.raises(QueryGraphLifecycleError, match="not ready"):
            query.close_graph(graph.id)
        assert query.get_graph(graph.id).workflow_state is GraphWorkflowState.OPEN

        evidence.transition_research_run(run.id, ResearchRunStatus.COMPLETED)
        with pytest.raises(QueryGraphLifecycleError, match="terminal research run"):
            query.close_graph(graph.id)
        assert query.get_question(question.id).workflow_state is QuestionWorkflowState.OPEN
    finally:
        db.close()


def test_run_ready_requires_required_graphs_closed_but_ignores_independent_optional_graph(tmp_path):
    db, evidence, query = _services(tmp_path)
    try:
        run = evidence.create_research_run("Objective")
        required = query.create_graph(
            run.id, name="Required", purpose="Required", role=GraphRole.REQUIRED
        )
        optional = query.create_graph(
            run.id, name="Optional", purpose="Optional", role=GraphRole.OPTIONAL
        )
        rq = _question(query, required, "Required?")
        _question(query, optional, "Optional unresolved?", role=QuestionRole.REQUIRED)
        rq, _ = _support_and_close(evidence, query, run, rq)

        before = query.assess_run_completion(run.id)
        assert before.ready is False
        assert before.reasons == (f"required graph {required.id} is open",)

        query.close_graph(required.id)
        after = query.assess_run_completion(run.id)
        assert after.ready is True
        assert after.reasons == ()
        assert query.get_graph(optional.id).workflow_state is GraphWorkflowState.OPEN
        assert evidence.get_research_run(run.id).status is ResearchRunStatus.OPEN
    finally:
        db.close()


def test_closed_required_graph_becomes_run_unready_if_required_question_is_reopened(tmp_path):
    db, evidence, query, run, graph = _graph(tmp_path)
    try:
        question = _question(query, graph, "Required?")
        question, claim = _support_and_close(evidence, query, run, question)
        query.close_graph(graph.id)
        assert query.assess_run_completion(run.id).ready is True

        evidence.set_claim_status(claim.id, ClaimStatus.CONTRADICTED)
        query.reopen_question(question.id, ReopenReason.CONTRADICTION_DISCOVERED)
        assessment = query.assess_run_completion(run.id)
        assert assessment.ready is False
        assert assessment.reasons == (
            f"required question {question.id} is IN_PROGRESS",
        )
        assert query.get_graph(graph.id).workflow_state is GraphWorkflowState.CLOSED
    finally:
        db.close()
