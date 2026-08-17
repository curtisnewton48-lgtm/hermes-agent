from __future__ import annotations

from hermes_state import SessionDB
from research.evidence_fabric import ClaimStatus, EvidenceFabricService, EvidenceScope, ResearchRunStatus
from research.query_graph import (
    DependencyAcceptancePolicy,
    GraphCreationReason,
    GraphRole,
    GraphWorkflowState,
    QueryGraphService,
    QuestionCreationReason,
    QuestionResolution,
    QuestionRole,
    QuestionWorkflowState,
    ReopenReason,
)


def test_canonical_multi_graph_research_run_is_fully_reconstructable(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    scope = EvidenceScope("scope", "profile", "connection", "research-director")
    evidence = EvidenceFabricService(db, scope)
    query = QueryGraphService(db, scope)
    try:
        run = evidence.create_research_run(
            "Determine whether Policy X is legally valid, technically feasible, "
            "and practically effective."
        )
        legal = query.create_graph(
            run.id,
            name="Legal",
            purpose="Determine legal validity",
            role=GraphRole.REQUIRED,
            reason=GraphCreationReason.ROOT,
        )
        technical = query.create_graph(
            run.id,
            name="Technical",
            purpose="Determine technical feasibility",
            role=GraphRole.REQUIRED,
            reason=GraphCreationReason.NEW_DOMAIN,
        )
        context = query.create_graph(
            run.id,
            name="Context",
            purpose="Explain historical adoption",
            reason=GraphCreationReason.SCOPE_EXPANSION,
        )

        authority = query.propose_question(
            legal.id,
            "What legal authority permits Policy X?",
            role=QuestionRole.REQUIRED,
        ).question
        restrictions = query.propose_question(
            legal.id,
            "What legal restrictions apply?",
            role=QuestionRole.REQUIRED,
        ).question
        capability = query.propose_question(
            technical.id,
            "Can the required system technically perform X?",
            role=QuestionRole.REQUIRED,
        ).question
        limitations = query.propose_question(
            technical.id,
            "What limitations materially affect feasibility?",
            role=QuestionRole.REQUIRED,
        ).question
        history = query.propose_question(
            context.id,
            "What historical factors explain adoption?",
            role=QuestionRole.OPTIONAL,
        ).question

        # Legal restrictions depend on a technical premise across graph
        # boundaries, but both questions remain independently investigable.
        query.add_dependency(
            restrictions.id,
            limitations.id,
            acceptance_policy=DependencyAcceptancePolicy.SUPPORTED,
        )
        query.start_question(restrictions.id)

        # A worker discovers a new material question dynamically. It defaults
        # optional, then the trusted director explicitly promotes it.
        proportionality = query.propose_question(
            technical.id,
            "Does limitation Y undermine the legal proportionality analysis?",
            reason=QuestionCreationReason.EVIDENCE_GAP,
        ).question
        assert proportionality.role is QuestionRole.OPTIONAL
        proportionality = query.set_question_role(
            proportionality.id,
            QuestionRole.REQUIRED,
            reason="material to the final legal conclusion",
        )
        query.add_dependency(
            restrictions.id,
            proportionality.id,
            acceptance_policy=DependencyAcceptancePolicy.ANY_CLOSED,
        )

        def supported(question, text):
            claim = evidence.create_claim(run.id, text)
            claim = evidence.set_claim_status(claim.id, ClaimStatus.SUPPORTED)
            query.link_claim(question.id, claim.id)
            return claim

        authority_claim = supported(authority, "Statute A supplies authority")
        capability_claim = supported(capability, "System X can perform the core operation")
        limitations_claim = supported(limitations, "Limitation Y is material but bounded")
        restriction_claim = supported(restrictions, "Restriction R applies")

        # Contradiction itself is the legitimate result for the newly
        # discovered branch, so it closes CONTESTED and satisfies ANY_CLOSED.
        prop_claim = evidence.create_claim(run.id, "Limitation Y defeats proportionality")
        prop_claim = evidence.set_claim_status(prop_claim.id, ClaimStatus.CONTRADICTED)
        query.link_claim(proportionality.id, prop_claim.id)

        authority = query.close_question(authority.id)
        capability = query.close_question(capability.id)
        limitations = query.close_question(limitations.id)
        proportionality = query.close_question(proportionality.id)
        assert proportionality.closed_resolution is QuestionResolution.CONTESTED
        restrictions = query.close_question(restrictions.id)

        assert query.assess_graph_completion(legal.id).ready is True
        assert query.assess_graph_completion(technical.id).ready is True
        legal = query.close_graph(legal.id)
        technical = query.close_graph(technical.id)
        assert legal.workflow_state is GraphWorkflowState.CLOSED
        assert technical.workflow_state is GraphWorkflowState.CLOSED
        assert query.assess_run_completion(run.id).ready is True
        assert query.get_graph(context.id).workflow_state is GraphWorkflowState.OPEN
        assert query.get_question(history.id).workflow_state is QuestionWorkflowState.OPEN
        assert evidence.get_research_run(run.id).status is ResearchRunStatus.OPEN

        # Later evidence materially changes the technical premise. Historical
        # closure stays frozen, but current readiness must become unready.
        evidence.set_claim_status(limitations_claim.id, ClaimStatus.CONTRADICTED)
        stale = query.derive_resolution(limitations.id)
        assert stale.resolution is QuestionResolution.SUPPORTED
        assert stale.stale is True
        assert query.assess_run_completion(run.id).ready is False

        limitations = query.reopen_question(
            limitations.id, ReopenReason.CONTRADICTION_DISCOVERED
        )
        assert limitations.workflow_state is QuestionWorkflowState.IN_PROGRESS
        assert query.get_graph(technical.id).workflow_state is GraphWorkflowState.CLOSED

        # Add a replacement supported basis after explicit reopen. The old
        # contradicted claim remains linked, so first resolve its EF status
        # instead of hiding/deleting history, then add the new evidence-backed
        # claim and close again.
        evidence.set_claim_status(limitations_claim.id, ClaimStatus.UNRESOLVED)
        revised = supported(
            limitations,
            "New measurements bound Limitation Y within the feasible range",
        )
        limitations = query.close_question(limitations.id)
        assert limitations.closed_resolution is QuestionResolution.PARTIALLY_ANSWERED
        assert query.derive_resolution(limitations.id).stale is False

        # SUPPORTED dependency policy is intentionally conservative: a partial
        # prerequisite is not enough. Revalidate the evidence ledger to a clean
        # supported basis before the run may again be declared ready.
        evidence.set_claim_status(limitations_claim.id, ClaimStatus.SUPPORTED)
        assert query.derive_resolution(limitations.id).stale is True
        query.revalidate_question(limitations.id)
        assert query.get_question(limitations.id).closed_resolution is QuestionResolution.PARTIALLY_ANSWERED
        assert query.assess_run_completion(run.id).ready is False

        # Because the frozen result itself is PARTIALLY_ANSWERED, revalidation
        # cannot upgrade history to SUPPORTED. Explicit reopen + clean basis is
        # required to change the conclusion.
        limitations = query.reopen_question(limitations.id, ReopenReason.NEW_EVIDENCE)
        query.unlink_claim(limitations.id, revised.id)
        evidence.set_claim_status(limitations_claim.id, ClaimStatus.SUPPORTED)
        limitations = query.close_question(limitations.id)
        assert limitations.closed_resolution is QuestionResolution.SUPPORTED
        assert query.assess_run_completion(run.id).ready is True

        # Everything below is reconstructed from durable state, not model
        # inference: ownership, reasons, dependencies, claims, contested/stale
        # history, reopen history, unresolved optional scope, and run readiness.
        snapshot = {
            "graphs": {
                graph.name: {
                    "id": graph.id,
                    "role": graph.role.value,
                    "workflow": graph.workflow_state.value,
                }
                for graph in query.list_graphs(run.id)
            },
            "legal_questions": {
                q.question_text: (q.role.value, q.workflow_state.value)
                for q in query.list_questions(legal.id)
            },
            "technical_questions": {
                q.question_text: (
                    q.role.value,
                    q.workflow_state.value,
                    q.closed_resolution.value if q.closed_resolution else None,
                )
                for q in query.list_questions(technical.id)
            },
            "restriction_dependencies": {
                edge.prerequisite_question_id: edge.acceptance_policy.value
                for edge in query.list_dependencies(restrictions.id)
            },
            "ready": query.assess_run_completion(run.id).ready,
            "event_types": tuple(event.event_type for event in query.list_events(run.id)),
        }
        assert snapshot["graphs"]["Context"] == {
            "id": context.id,
            "role": GraphRole.OPTIONAL.value,
            "workflow": GraphWorkflowState.OPEN.value,
        }
        assert snapshot["restriction_dependencies"] == {
            limitations.id: DependencyAcceptancePolicy.SUPPORTED.value,
            proportionality.id: DependencyAcceptancePolicy.ANY_CLOSED.value,
        }
        assert snapshot["technical_questions"][proportionality.question_text][2] == QuestionResolution.CONTESTED.value
        assert snapshot["ready"] is True
        assert "QUESTION_REOPENED" in snapshot["event_types"]
        assert "QUESTION_REVALIDATED" in snapshot["event_types"]
        assert evidence.get_claim(authority_claim.id).status is ClaimStatus.SUPPORTED
        assert evidence.get_claim(capability_claim.id).status is ClaimStatus.SUPPORTED
        assert evidence.get_claim(restriction_claim.id).status is ClaimStatus.SUPPORTED
    finally:
        db.close()
