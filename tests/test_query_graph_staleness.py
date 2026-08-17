from __future__ import annotations

import pytest

from hermes_state import SessionDB
from research.evidence_fabric import (
    ClaimStatus,
    EvidenceFabricService,
    EvidenceScope,
    ResearchRunStatus,
)
from research.query_graph import (
    QueryGraphLifecycleError,
    QueryGraphService,
    QueryGraphStaleResolutionError,
    QuestionResolution,
    QuestionWorkflowState,
    ReopenReason,
)


def _services(tmp_path, *, agent_id="worker"):
    db = SessionDB(tmp_path / "state.db")
    scope = EvidenceScope("scope", "profile", "connection", agent_id)
    return db, EvidenceFabricService(db, scope), QueryGraphService(db, scope)


def _closed_supported(tmp_path, *, agent_id="worker"):
    db, evidence, query = _services(tmp_path, agent_id=agent_id)
    run = evidence.create_research_run("Objective")
    graph = query.create_graph(run.id, name="Legal", purpose="Resolve issue")
    question = query.propose_question(graph.id, "What is the answer?").question
    claim = evidence.create_claim(run.id, "Supported answer")
    claim = evidence.set_claim_status(claim.id, ClaimStatus.SUPPORTED)
    query.link_claim(question.id, claim.id)
    question = query.close_question(question.id)
    return db, evidence, query, run, graph, question, claim


def test_closed_resolution_stays_frozen_when_linked_claim_becomes_stale(tmp_path):
    db, evidence, query, run, graph, question, claim = _closed_supported(tmp_path)
    try:
        before = query.derive_resolution(question.id)
        assert before.resolution is QuestionResolution.SUPPORTED
        assert before.stale is False

        evidence.set_claim_status(claim.id, ClaimStatus.CONTRADICTED)
        after = query.derive_resolution(question.id)
        persisted = query.get_question(question.id)

        assert persisted.workflow_state is QuestionWorkflowState.CLOSED
        assert persisted.closed_resolution is QuestionResolution.SUPPORTED
        assert after.resolution is QuestionResolution.SUPPORTED
        assert after.stale is True
    finally:
        db.close()


def test_unrelated_claim_and_graph_changes_do_not_create_false_staleness(tmp_path):
    db, evidence, query, run, graph, question, claim = _closed_supported(tmp_path)
    try:
        unrelated = evidence.create_claim(run.id, "Unrelated")
        unrelated = evidence.set_claim_status(unrelated.id, ClaimStatus.SUPPORTED)
        evidence.set_claim_status(unrelated.id, ClaimStatus.CONTRADICTED)

        other_graph = query.create_graph(run.id, name="Technical", purpose="Other issue")
        other_question = query.propose_question(other_graph.id, "Other question?").question
        other_claim = evidence.create_claim(run.id, "Other answer")
        other_claim = evidence.set_claim_status(other_claim.id, ClaimStatus.SUPPORTED)
        query.link_claim(other_question.id, other_claim.id)
        query.start_question(other_question.id)

        view = query.derive_resolution(question.id)
        assert view.stale is False
        assert view.resolution is QuestionResolution.SUPPORTED
    finally:
        db.close()


def test_revalidate_refreshes_basis_when_same_closed_result_still_holds(tmp_path):
    db, evidence, query, run, graph, question, claim = _closed_supported(
        tmp_path, agent_id="verifier"
    )
    try:
        original_closed_at = question.closed_at
        original_closed_by = question.closed_by_agent
        with db._lock:
            old_snapshot = db._conn.execute(
                "SELECT claim_updated_at_at_close,snapshot_at FROM question_closure_claims "
                "WHERE question_id=? AND claim_id=?",
                (question.id, claim.id),
            ).fetchone()

        # Same semantic result, newer Evidence Fabric version marker.
        claim = evidence.set_claim_status(claim.id, ClaimStatus.SUPPORTED)
        assert query.derive_resolution(question.id).stale is True

        revalidated = query.revalidate_question(question.id)
        view = query.derive_resolution(question.id)
        assert revalidated.workflow_state is QuestionWorkflowState.CLOSED
        assert revalidated.closed_resolution is QuestionResolution.SUPPORTED
        assert revalidated.closed_at == original_closed_at
        assert revalidated.closed_by_agent == original_closed_by
        assert view.stale is False

        with db._lock:
            new_snapshot = db._conn.execute(
                "SELECT claim_updated_at_at_close,snapshot_at FROM question_closure_claims "
                "WHERE question_id=? AND claim_id=?",
                (question.id, claim.id),
            ).fetchone()
        assert new_snapshot["claim_updated_at_at_close"] == claim.updated_at.timestamp()
        assert new_snapshot["claim_updated_at_at_close"] != old_snapshot["claim_updated_at_at_close"]
        assert new_snapshot["snapshot_at"] >= old_snapshot["snapshot_at"]

        event = query.list_events(run.id, question_id=question.id)[-1]
        assert event.event_type == "QUESTION_REVALIDATED"
        assert event.actor_agent == "verifier"
        assert event.payload["resolution"] == QuestionResolution.SUPPORTED.value
        assert event.payload["claim_ids"] == [claim.id]
    finally:
        db.close()


def test_revalidate_rejects_changed_resolution_without_touching_old_closure(tmp_path):
    db, evidence, query, run, graph, question, claim = _closed_supported(tmp_path)
    try:
        with db._lock:
            old_basis = tuple(
                db._conn.execute(
                    "SELECT claim_id,claim_status_at_close,claim_updated_at_at_close,snapshot_at "
                    "FROM question_closure_claims WHERE question_id=? ORDER BY claim_id",
                    (question.id,),
                ).fetchall()
            )
        evidence.set_claim_status(claim.id, ClaimStatus.CONTRADICTED)

        with pytest.raises(
            QueryGraphStaleResolutionError, match="current resolution CONTESTED"
        ):
            query.revalidate_question(question.id)

        persisted = query.get_question(question.id)
        assert persisted.closed_resolution is QuestionResolution.SUPPORTED
        assert query.derive_resolution(question.id).stale is True
        with db._lock:
            new_basis = tuple(
                db._conn.execute(
                    "SELECT claim_id,claim_status_at_close,claim_updated_at_at_close,snapshot_at "
                    "FROM question_closure_claims WHERE question_id=? ORDER BY claim_id",
                    (question.id,),
                ).fetchall()
            )
        assert [tuple(row) for row in new_basis] == [tuple(row) for row in old_basis]
        assert query.list_events(run.id, question_id=question.id)[-1].event_type != "QUESTION_REVALIDATED"
    finally:
        db.close()


def test_revalidate_requires_closed_stale_question_and_open_run(tmp_path):
    db, evidence, query, run, graph, question, claim = _closed_supported(tmp_path)
    try:
        with pytest.raises(QueryGraphLifecycleError, match="not stale"):
            query.revalidate_question(question.id)

        active = query.propose_question(graph.id, "Still active?").question
        with pytest.raises(QueryGraphLifecycleError, match="closed question"):
            query.revalidate_question(active.id)

        evidence.set_claim_status(claim.id, ClaimStatus.SUPPORTED)
        evidence.transition_research_run(run.id, ResearchRunStatus.COMPLETED)
        with pytest.raises(QueryGraphLifecycleError, match="terminal research run"):
            query.revalidate_question(question.id)
    finally:
        db.close()


@pytest.mark.parametrize(
    "reason,new_status",
    [
        (ReopenReason.NEW_EVIDENCE, ClaimStatus.PARTIALLY_SUPPORTED),
        (ReopenReason.CONTRADICTION_DISCOVERED, ClaimStatus.CONTRADICTED),
    ],
)
def test_reopen_requires_material_staleness_and_clears_current_closure(
    tmp_path, reason, new_status
):
    db, evidence, query, run, graph, question, claim = _closed_supported(
        tmp_path, agent_id="director"
    )
    try:
        evidence.set_claim_status(claim.id, new_status)
        assert query.derive_resolution(question.id).stale is True

        reopened = query.reopen_question(question.id, reason)
        assert reopened.workflow_state is QuestionWorkflowState.IN_PROGRESS
        assert reopened.closed_resolution is None
        assert reopened.closed_at is None
        assert reopened.closed_by_agent is None
        assert reopened.closed_by_profile is None
        assert reopened.blocked_reason is None
        assert query.derive_resolution(question.id).stale is False

        with db._lock:
            assert db._conn.execute(
                "SELECT COUNT(*) FROM question_closure_claims WHERE question_id=?",
                (question.id,),
            ).fetchone()[0] == 0

        event = query.list_events(run.id, question_id=question.id)[-1]
        assert event.event_type == "QUESTION_REOPENED"
        assert event.actor_agent == "director"
        assert event.reason == reason.value
        assert event.payload["old_resolution"] == QuestionResolution.SUPPORTED.value
        assert event.payload["current_resolution"] in {
            QuestionResolution.PARTIALLY_ANSWERED.value,
            QuestionResolution.CONTESTED.value,
        }

        # Reopen restores normal active-question mutation paths.
        new_claim = evidence.create_claim(run.id, "New basis")
        query.link_claim(question.id, new_claim.id)
        query.refine_question(question.id, "Refined after reopen?", reason="new evidence")
    finally:
        db.close()


def test_reopen_rejects_no_change_active_question_and_terminal_run(tmp_path):
    db, evidence, query, run, graph, question, claim = _closed_supported(tmp_path)
    try:
        with pytest.raises(QueryGraphLifecycleError, match="not stale"):
            query.reopen_question(question.id, ReopenReason.NEW_EVIDENCE)

        active = query.propose_question(graph.id, "Active?").question
        with pytest.raises(QueryGraphLifecycleError, match="closed question"):
            query.reopen_question(active.id, ReopenReason.NEW_EVIDENCE)

        evidence.set_claim_status(claim.id, ClaimStatus.CONTRADICTED)
        evidence.transition_research_run(run.id, ResearchRunStatus.CANCELLED)
        with pytest.raises(QueryGraphLifecycleError, match="terminal research run"):
            query.reopen_question(question.id, ReopenReason.CONTRADICTION_DISCOVERED)
    finally:
        db.close()
