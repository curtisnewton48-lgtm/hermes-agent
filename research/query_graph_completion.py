"""Deterministic graph closure and ResearchRun readiness assessment."""

from __future__ import annotations

import sqlite3

from research.query_graph_types import (
    CompletionAssessment,
    GraphRole,
    GraphWorkflowState,
    QueryGraphIntegrityError,
    QueryGraphLifecycleError,
    QueryGraphNotFoundError,
    QuestionResolution,
    QuestionRole,
    QuestionWorkflowState,
    _id,
    _now,
)


def _dedupe_reasons(reasons: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reasons))


class QueryGraphCompletionMixin:
    def _graph_row_for_read_in_cursor(self, cursor, graph_id: str):
        graph_id = _id(graph_id)
        row = cursor.execute(
            "SELECT * FROM query_graphs WHERE id=?", (graph_id,)
        ).fetchone()
        if row is None:
            raise QueryGraphNotFoundError("query graph not found")
        self._run_in_cursor(cursor, row["research_run_id"], require_open=False)
        return row

    def _dependency_path_reasons_in_cursor(
        self,
        cursor,
        question_id: str,
        *,
        visited: set[str] | None = None,
    ) -> list[str]:
        question_id = _id(question_id)
        seen = set() if visited is None else visited
        if question_id in seen:
            # The run-wide DAG invariant makes this unreachable in healthy
            # data. Fail closed without recursing forever if the DB was
            # externally corrupted.
            return [f"dependency cycle detected at question {question_id}"]
        seen.add(question_id)
        reasons: list[str] = []
        edges = cursor.execute(
            "SELECT * FROM question_dependencies "
            "WHERE dependent_question_id=? "
            "ORDER BY created_at,prerequisite_question_id",
            (question_id,),
        ).fetchall()
        for edge in edges:
            prerequisite_id = edge["prerequisite_question_id"]
            if not self._dependency_satisfied_in_cursor(cursor, edge):
                reasons.append(
                    f"question {question_id} prerequisite {prerequisite_id} "
                    f"does not satisfy {edge['acceptance_policy']}"
                )
            # Readiness is live across the entire structural path. A direct
            # prerequisite may still have a fresh closure snapshot while one
            # of its own prerequisites later becomes stale/unsatisfied.
            reasons.extend(
                self._dependency_path_reasons_in_cursor(
                    cursor,
                    prerequisite_id,
                    visited=set(seen),
                )
            )
        return reasons

    def _assess_graph_completion_in_cursor(
        self, cursor, graph_id: str
    ) -> CompletionAssessment:
        graph = self._graph_row_for_read_in_cursor(cursor, graph_id)
        required_questions = cursor.execute(
            "SELECT * FROM research_questions "
            "WHERE graph_id=? AND role=? ORDER BY created_at,id",
            (graph["id"], QuestionRole.REQUIRED.value),
        ).fetchall()
        reasons: list[str] = []
        for question in required_questions:
            state = QuestionWorkflowState(question["workflow_state"])
            if state is not QuestionWorkflowState.CLOSED:
                reasons.append(
                    f"required question {question['id']} is {state.value}"
                )
            else:
                if question["closed_resolution"] in {
                    None,
                    QuestionResolution.UNANSWERED.value,
                }:
                    reasons.append(
                        f"required question {question['id']} has no resolved closure"
                    )
                elif self._question_basis_stale_in_cursor(cursor, question["id"]):
                    reasons.append(
                        f"required question {question['id']} has stale closure basis"
                    )
            reasons.extend(
                self._dependency_path_reasons_in_cursor(cursor, question["id"])
            )
        normalized = _dedupe_reasons(reasons)
        return CompletionAssessment(ready=not normalized, reasons=normalized)

    def assess_graph_completion(self, graph_id: str) -> CompletionAssessment:
        with self.db._lock:
            return self._assess_graph_completion_in_cursor(
                self.db._conn, _id(graph_id)
            )

    def close_graph(self, graph_id: str):
        graph_id = _id(graph_id)
        now = _now()

        def write(cursor):
            graph = self._graph_row_in_cursor(cursor, graph_id)
            if graph["workflow_state"] == GraphWorkflowState.CLOSED.value:
                raise QueryGraphLifecycleError("query graph is already closed")
            assessment = self._assess_graph_completion_in_cursor(cursor, graph_id)
            if not assessment.ready:
                raise QueryGraphLifecycleError(
                    "query graph is not ready: " + "; ".join(assessment.reasons)
                )
            required_ids = [
                row["id"]
                for row in cursor.execute(
                    "SELECT id FROM research_questions "
                    "WHERE graph_id=? AND role=? ORDER BY created_at,id",
                    (graph_id, QuestionRole.REQUIRED.value),
                ).fetchall()
            ]
            cursor.execute(
                "UPDATE query_graphs SET workflow_state='CLOSED',closed_at=?,"
                "closed_by_agent=?,closed_by_profile=?,updated_at=? WHERE id=?",
                (
                    now.timestamp(),
                    self.scope.agent_id,
                    self.scope.profile_name,
                    now.timestamp(),
                    graph_id,
                ),
            )
            self._append_event(
                cursor,
                run_id=graph["research_run_id"],
                graph_id=graph_id,
                event_type="GRAPH_CLOSED",
                payload={"required_question_ids": required_ids},
                created_at=now,
            )

        try:
            self._write(write)
        except sqlite3.IntegrityError as exc:
            if self._is_lifecycle_integrity_error(exc):
                raise QueryGraphLifecycleError(str(exc)) from exc
            raise QueryGraphIntegrityError(str(exc)) from exc
        return self.get_graph(graph_id)

    def assess_run_completion(self, run_id: str) -> CompletionAssessment:
        run = self._run(_id(run_id))
        reasons: list[str] = []
        with self.db._lock:
            cursor = self.db._conn
            required_graphs = cursor.execute(
                "SELECT * FROM query_graphs "
                "WHERE research_run_id=? AND role=? ORDER BY created_at,id",
                (run["id"], GraphRole.REQUIRED.value),
            ).fetchall()
            for graph in required_graphs:
                if graph["workflow_state"] != GraphWorkflowState.CLOSED.value:
                    reasons.append(f"required graph {graph['id']} is open")
                assessment = self._assess_graph_completion_in_cursor(
                    cursor, graph["id"]
                )
                reasons.extend(assessment.reasons)
        normalized = _dedupe_reasons(reasons)
        return CompletionAssessment(ready=not normalized, reasons=normalized)
