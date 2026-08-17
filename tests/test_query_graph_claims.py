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
    QueryGraphIntegrityError,
    QueryGraphLifecycleError,
    QueryGraphNotFoundError,
    QueryGraphScopeError,
    QueryGraphService,
    QuestionResolution,
)


def _services(tmp_path, *, scope_key="scope", agent_id="worker"):
    db = SessionDB(tmp_path / "state.db")
    scope = EvidenceScope(scope_key, "profile", "connection", agent_id)
    return db, EvidenceFabricService(db, scope), QueryGraphService(db, scope)


def _question(tmp_path, *, agent_id="worker"):
    db, evidence, query = _services(tmp_path, agent_id=agent_id)
    run = evidence.create_research_run("Objective")
    graph = query.create_graph(run.id, name="Legal", purpose="Resolve issue")
    question = query.propose_question(graph.id, "What is the answer?").question
    return db, evidence, query, run, graph, question


def test_link_and_unlink_real_evidence_fabric_claims_with_runtime_provenance(tmp_path):
    db, evidence, query, run, graph, question = _question(tmp_path, agent_id="analyst")
    try:
        first = evidence.create_claim(run.id, "First claim")
        second = evidence.create_claim(run.id, "Second claim")

        link_one = query.link_claim(question.id, first.id)
        link_two = query.link_claim(question.id, second.id)

        assert link_one.question_id == question.id
        assert link_one.claim_id == first.id
        assert link_one.research_run_id == run.id
        assert link_one.created_by_agent == "analyst"
        assert link_one.created_by_profile == "profile"
        assert link_two.claim_id == second.id

        events = query.list_events(run.id, question_id=question.id)
        assert [event.event_type for event in events][-2:] == [
            "CLAIM_LINKED",
            "CLAIM_LINKED",
        ]
        assert events[-1].actor_agent == "analyst"
        assert events[-1].payload == {"claim_id": second.id}

        query.unlink_claim(question.id, first.id)
        with db._lock:
            remaining = db._conn.execute(
                "SELECT claim_id FROM question_claim_links WHERE question_id=? ORDER BY claim_id",
                (question.id,),
            ).fetchall()
        assert [row["claim_id"] for row in remaining] == [second.id]
        event = query.list_events(run.id, question_id=question.id)[-1]
        assert event.event_type == "CLAIM_UNLINKED"
        assert event.payload == {"claim_id": first.id}
    finally:
        db.close()


def test_duplicate_link_and_missing_unlink_are_stable_domain_errors(tmp_path):
    db, evidence, query, run, graph, question = _question(tmp_path)
    try:
        claim = evidence.create_claim(run.id, "Claim")
        query.link_claim(question.id, claim.id)
        with pytest.raises(QueryGraphIntegrityError, match="claim link already exists"):
            query.link_claim(question.id, claim.id)

        query.unlink_claim(question.id, claim.id)
        with pytest.raises(QueryGraphIntegrityError, match="claim link does not exist"):
            query.unlink_claim(question.id, claim.id)
    finally:
        db.close()


def test_cross_run_missing_and_foreign_scope_claim_links_are_rejected(tmp_path):
    db, evidence, query, run, graph, question = _question(tmp_path)
    try:
        other_run = evidence.create_research_run("Other objective")
        other_claim = evidence.create_claim(other_run.id, "Foreign-run claim")
        with pytest.raises(QueryGraphIntegrityError, match="different research runs"):
            query.link_claim(question.id, other_claim.id)

        with pytest.raises(QueryGraphNotFoundError, match="claim not found"):
            query.link_claim(question.id, "missing-claim")
        with pytest.raises(QueryGraphNotFoundError, match="research question not found"):
            query.link_claim("missing-question", other_claim.id)

        foreign_evidence = EvidenceFabricService(
            db,
            EvidenceScope("foreign-scope", "foreign-profile", "foreign-connection", "foreign"),
        )
        foreign_query = QueryGraphService(
            db,
            EvidenceScope("foreign-scope", "foreign-profile", "foreign-connection", "foreign"),
        )
        foreign_run = foreign_evidence.create_research_run("Private objective")
        foreign_claim = foreign_evidence.create_claim(foreign_run.id, "Private claim")

        with pytest.raises(QueryGraphScopeError):
            foreign_query.link_claim(question.id, foreign_claim.id)
        with pytest.raises(QueryGraphScopeError):
            query.link_claim(question.id, foreign_claim.id)
    finally:
        db.close()


def test_terminal_run_rejects_link_and_unlink(tmp_path):
    db, evidence, query, run, graph, question = _question(tmp_path)
    try:
        linked = evidence.create_claim(run.id, "Already linked")
        late = evidence.create_claim(run.id, "Late claim")
        query.link_claim(question.id, linked.id)
        evidence.transition_research_run(run.id, ResearchRunStatus.COMPLETED)

        with pytest.raises(QueryGraphLifecycleError, match="terminal research run"):
            query.link_claim(question.id, late.id)
        with pytest.raises(QueryGraphLifecycleError, match="terminal research run"):
            query.unlink_claim(question.id, linked.id)
    finally:
        db.close()


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ((), QuestionResolution.UNANSWERED),
        ((ClaimStatus.UNVERIFIED,), QuestionResolution.UNANSWERED),
        ((ClaimStatus.UNRESOLVED,), QuestionResolution.UNANSWERED),
        ((ClaimStatus.PARTIALLY_SUPPORTED,), QuestionResolution.PARTIALLY_ANSWERED),
        ((ClaimStatus.SUPPORTED,), QuestionResolution.SUPPORTED),
        (
            (ClaimStatus.SUPPORTED, ClaimStatus.PARTIALLY_SUPPORTED),
            QuestionResolution.PARTIALLY_ANSWERED,
        ),
        (
            (ClaimStatus.SUPPORTED, ClaimStatus.UNVERIFIED),
            QuestionResolution.PARTIALLY_ANSWERED,
        ),
        (
            (ClaimStatus.SUPPORTED, ClaimStatus.UNRESOLVED),
            QuestionResolution.PARTIALLY_ANSWERED,
        ),
        ((ClaimStatus.CONTRADICTED,), QuestionResolution.CONTESTED),
        (
            (ClaimStatus.SUPPORTED, ClaimStatus.CONTRADICTED),
            QuestionResolution.CONTESTED,
        ),
        (
            (ClaimStatus.PARTIALLY_SUPPORTED, ClaimStatus.CONTRADICTED),
            QuestionResolution.CONTESTED,
        ),
    ],
)
def test_live_resolution_truth_table(tmp_path, statuses, expected):
    db, evidence, query, run, graph, question = _question(tmp_path)
    try:
        claim_ids = []
        for index, status in enumerate(statuses):
            claim = evidence.create_claim(run.id, f"Claim {index}")
            if status is not ClaimStatus.UNVERIFIED:
                claim = evidence.set_claim_status(claim.id, status)
            query.link_claim(question.id, claim.id)
            claim_ids.append(claim.id)

        view = query.derive_resolution(question.id)
        assert view.question_id == question.id
        assert view.resolution is expected
        assert view.stale is False
        assert view.linked_claim_ids == tuple(sorted(claim_ids))
    finally:
        db.close()


def test_resolution_is_live_for_active_question_and_never_mutates_claim_state(tmp_path):
    db, evidence, query, run, graph, question = _question(tmp_path)
    try:
        claim = evidence.create_claim(run.id, "Claim")
        query.link_claim(question.id, claim.id)
        assert query.derive_resolution(question.id).resolution is QuestionResolution.UNANSWERED

        evidence.set_claim_status(claim.id, ClaimStatus.SUPPORTED)
        assert query.derive_resolution(question.id).resolution is QuestionResolution.SUPPORTED
        assert evidence.get_claim(claim.id).status is ClaimStatus.SUPPORTED

        evidence.set_claim_status(claim.id, ClaimStatus.CONTRADICTED)
        assert query.derive_resolution(question.id).resolution is QuestionResolution.CONTESTED
        assert evidence.get_claim(claim.id).status is ClaimStatus.CONTRADICTED
    finally:
        db.close()


def test_closed_question_reports_frozen_resolution_and_staleness_read_only(tmp_path):
    db, evidence, query, run, graph, question = _question(tmp_path)
    try:
        claim = evidence.create_claim(run.id, "Claim")
        claim = evidence.set_claim_status(claim.id, ClaimStatus.SUPPORTED)
        query.link_claim(question.id, claim.id)
        with db._lock:
            db._conn.execute(
                "INSERT INTO question_closure_claims "
                "(question_id,claim_id,research_run_id,claim_status_at_close,"
                "claim_updated_at_at_close,snapshot_at) VALUES (?,?,?,?,?,2)",
                (
                    question.id,
                    claim.id,
                    run.id,
                    ClaimStatus.SUPPORTED.value,
                    claim.updated_at.timestamp(),
                ),
            )
            db._conn.execute(
                "UPDATE research_questions SET workflow_state='CLOSED',"
                "closed_resolution='SUPPORTED',closed_at=2 WHERE id=?",
                (question.id,),
            )

        fresh = query.derive_resolution(question.id)
        assert fresh.resolution is QuestionResolution.SUPPORTED
        assert fresh.stale is False

        evidence.set_claim_status(claim.id, ClaimStatus.CONTRADICTED)
        stale = query.derive_resolution(question.id)
        assert stale.resolution is QuestionResolution.SUPPORTED
        assert stale.stale is True
        assert query.get_question(question.id).closed_resolution is QuestionResolution.SUPPORTED
    finally:
        db.close()
