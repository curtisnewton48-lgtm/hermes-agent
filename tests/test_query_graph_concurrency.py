from __future__ import annotations

import threading

from hermes_state import SessionDB
from research.evidence_fabric import ClaimStatus, EvidenceFabricService, EvidenceScope
from research.query_graph import (
    DependencyAcceptancePolicy,
    GraphRole,
    QueryGraphCycleError,
    QueryGraphDependencyError,
    QueryGraphService,
    QuestionResolution,
    QuestionRole,
    QuestionWorkflowState,
)


def _scope(agent_id: str) -> EvidenceScope:
    return EvidenceScope("scope", "profile", "connection", agent_id)


def _seed(db_path):
    db = SessionDB(db_path)
    evidence = EvidenceFabricService(db, _scope("seed"))
    query = QueryGraphService(db, _scope("seed"))
    run = evidence.create_research_run("Concurrency objective")
    graph_a = query.create_graph(
        run.id, name="A", purpose="A", role=GraphRole.REQUIRED
    )
    graph_b = query.create_graph(run.id, name="B", purpose="B")
    return db, evidence, query, run, graph_a, graph_b


def test_identical_concurrent_question_proposals_converge_on_one_identity(tmp_path):
    db_path = tmp_path / "state.db"
    seed_db, evidence, query, run, graph_a, graph_b = _seed(db_path)
    barrier = threading.Barrier(2)
    results = []
    errors = []
    lock = threading.Lock()

    def worker(agent_id: str, graph_id: str, text: str):
        db = SessionDB(db_path)
        service = QueryGraphService(db, _scope(agent_id))
        try:
            barrier.wait(timeout=10)
            result = service.propose_question(graph_id, text)
            with lock:
                results.append(result)
        except BaseException as exc:  # captured for assertion in parent thread
            with lock:
                errors.append(exc)
        finally:
            db.close()

    try:
        threads = [
            threading.Thread(
                target=worker,
                args=("worker-a", graph_a.id, "  WHAT   is the governing rule? "),
            ),
            threading.Thread(
                target=worker,
                args=("worker-b", graph_b.id, "what is the governing rule?"),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert len(results) == 2
        assert {result.created for result in results} == {True, False}
        assert len({result.question.id for result in results}) == 1

        question_id = results[0].question.id
        with seed_db._lock:
            assert seed_db._conn.execute(
                "SELECT COUNT(*) FROM research_questions WHERE research_run_id=?",
                (run.id,),
            ).fetchone()[0] == 1
            assert seed_db._conn.execute(
                "SELECT COUNT(*) FROM query_graph_events "
                "WHERE question_id=? AND event_type='QUESTION_CREATED'",
                (question_id,),
            ).fetchone()[0] == 1
    finally:
        seed_db.close()


def test_distinct_concurrent_question_proposals_are_both_preserved(tmp_path):
    db_path = tmp_path / "state.db"
    seed_db, evidence, query, run, graph_a, graph_b = _seed(db_path)
    barrier = threading.Barrier(2)
    results = []
    errors = []
    lock = threading.Lock()

    def worker(agent_id: str, graph_id: str, text: str):
        db = SessionDB(db_path)
        service = QueryGraphService(db, _scope(agent_id))
        try:
            barrier.wait(timeout=10)
            result = service.propose_question(graph_id, text)
            with lock:
                results.append(result)
        except BaseException as exc:
            with lock:
                errors.append(exc)
        finally:
            db.close()

    try:
        threads = [
            threading.Thread(target=worker, args=("a", graph_a.id, "Question A?")),
            threading.Thread(target=worker, args=("b", graph_b.id, "Question B?")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert errors == []
        assert all(not thread.is_alive() for thread in threads)
        assert len(results) == 2
        assert all(result.created for result in results)
        assert len({result.question.id for result in results}) == 2
        with seed_db._lock:
            assert seed_db._conn.execute(
                "SELECT COUNT(*) FROM research_questions WHERE research_run_id=?",
                (run.id,),
            ).fetchone()[0] == 2
    finally:
        seed_db.close()


def test_concurrent_opposite_edges_cannot_jointly_create_cycle(tmp_path):
    db_path = tmp_path / "state.db"
    seed_db, evidence, query, run, graph_a, graph_b = _seed(db_path)
    q_a = query.propose_question(graph_a.id, "A?").question
    q_b = query.propose_question(graph_b.id, "B?").question
    barrier = threading.Barrier(2)
    successes = []
    errors = []
    lock = threading.Lock()

    def worker(agent_id: str, dependent: str, prerequisite: str):
        db = SessionDB(db_path)
        service = QueryGraphService(db, _scope(agent_id))
        try:
            barrier.wait(timeout=10)
            edge = service.add_dependency(dependent, prerequisite)
            with lock:
                successes.append(edge)
        except BaseException as exc:
            with lock:
                errors.append(exc)
        finally:
            db.close()

    try:
        threads = [
            threading.Thread(target=worker, args=("a", q_a.id, q_b.id)),
            threading.Thread(target=worker, args=("b", q_b.id, q_a.id)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads)
        assert len(successes) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], QueryGraphCycleError)

        with seed_db._lock:
            edges = seed_db._conn.execute(
                "SELECT dependent_question_id,prerequisite_question_id "
                "FROM question_dependencies WHERE research_run_id=?",
                (run.id,),
            ).fetchall()
        assert len(edges) == 1
        assert tuple(edges[0]) in {(q_a.id, q_b.id), (q_b.id, q_a.id)}
    finally:
        seed_db.close()


def test_duplicate_dependency_race_has_one_winner_and_stable_domain_error(tmp_path):
    db_path = tmp_path / "state.db"
    seed_db, evidence, query, run, graph_a, graph_b = _seed(db_path)
    q_a = query.propose_question(graph_a.id, "A?").question
    q_b = query.propose_question(graph_b.id, "B?").question
    barrier = threading.Barrier(2)
    successes = []
    errors = []
    lock = threading.Lock()

    def worker(agent_id: str):
        db = SessionDB(db_path)
        service = QueryGraphService(db, _scope(agent_id))
        try:
            barrier.wait(timeout=10)
            edge = service.add_dependency(
                q_a.id,
                q_b.id,
                acceptance_policy=DependencyAcceptancePolicy.SUPPORTED,
            )
            with lock:
                successes.append(edge)
        except BaseException as exc:
            with lock:
                errors.append(exc)
        finally:
            db.close()

    try:
        threads = [
            threading.Thread(target=worker, args=("a",)),
            threading.Thread(target=worker, args=("b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert len(successes) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], QueryGraphDependencyError)
        with seed_db._lock:
            assert seed_db._conn.execute(
                "SELECT COUNT(*) FROM question_dependencies WHERE research_run_id=?",
                (run.id,),
            ).fetchone()[0] == 1
            assert seed_db._conn.execute(
                "SELECT COUNT(*) FROM query_graph_events "
                "WHERE question_id=? AND event_type='DEPENDENCY_ADDED'",
                (q_a.id,),
            ).fetchone()[0] == 1
    finally:
        seed_db.close()


def test_multi_graph_state_events_closure_and_staleness_survive_restart(tmp_path):
    db_path = tmp_path / "state.db"
    db, evidence, query, run, graph_a, graph_b = _seed(db_path)
    q_a = query.propose_question(
        graph_a.id, "Required conclusion?", role=QuestionRole.REQUIRED
    ).question
    q_b = query.propose_question(graph_b.id, "Cross graph premise?").question
    q_c = query.propose_question(graph_b.id, "Blocked context?").question
    query.add_dependency(q_a.id, q_b.id)

    premise_claim = evidence.create_claim(run.id, "premise")
    premise_claim = evidence.set_claim_status(premise_claim.id, ClaimStatus.SUPPORTED)
    query.link_claim(q_b.id, premise_claim.id)
    q_b = query.close_question(q_b.id)

    conclusion_claim = evidence.create_claim(run.id, "conclusion")
    conclusion_claim = evidence.set_claim_status(
        conclusion_claim.id, ClaimStatus.SUPPORTED
    )
    query.link_claim(q_a.id, conclusion_claim.id)
    q_a = query.close_question(q_a.id)
    query.block_question(q_c.id, reason="external source unavailable")
    query.close_graph(graph_a.id)

    # Evolve the premise after downstream closure. This must be detected after
    # a fresh process/connection reconstructs the Query Graph from state.db.
    evidence.set_claim_status(premise_claim.id, ClaimStatus.CONTRADICTED)
    event_count = len(query.list_events(run.id))
    db.close()

    reopened_db = SessionDB(db_path)
    reopened_evidence = EvidenceFabricService(reopened_db, _scope("reader"))
    reopened_query = QueryGraphService(reopened_db, _scope("reader"))
    try:
        graphs = reopened_query.list_graphs(run.id)
        assert {graph.id for graph in graphs} == {graph_a.id, graph_b.id}
        assert reopened_query.get_question(q_a.id).workflow_state is QuestionWorkflowState.CLOSED
        assert reopened_query.get_question(q_b.id).closed_resolution is QuestionResolution.SUPPORTED
        assert reopened_query.get_question(q_c.id).workflow_state is QuestionWorkflowState.BLOCKED
        assert reopened_query.list_dependencies(q_a.id)[0].prerequisite_question_id == q_b.id
        assert reopened_query.derive_resolution(q_b.id).stale is True
        assert reopened_query.assess_graph_completion(graph_a.id).ready is False
        assert reopened_query.assess_run_completion(run.id).ready is False
        assert len(reopened_query.list_events(run.id)) == event_count
        assert reopened_evidence.get_claim(premise_claim.id).status is ClaimStatus.CONTRADICTED
    finally:
        reopened_db.close()
