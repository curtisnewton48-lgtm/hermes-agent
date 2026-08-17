"""Run-wide dependency DAG behavior for Query Graph v1."""

from __future__ import annotations

import sqlite3

from research.query_graph_types import (
    DependencyAcceptancePolicy,
    QueryGraphCycleError,
    QueryGraphDependencyError,
    QueryGraphIntegrityError,
    QueryGraphLifecycleError,
    QueryGraphValidationError,
    QuestionDependency,
    QuestionResolution,
    QuestionWorkflowState,
    _dt,
    _id,
    _now,
    _required_dt,
)


class QueryGraphDependencyMixin:
    @staticmethod
    def _dependency_dto(row) -> QuestionDependency:
        return QuestionDependency(
            row["dependent_question_id"],
            row["prerequisite_question_id"],
            row["research_run_id"],
            DependencyAcceptancePolicy(row["acceptance_policy"]),
            row["created_by_agent"],
            row["created_by_profile"],
            _required_dt(row["created_at"]),
        )

    @staticmethod
    def _would_create_cycle(cursor, run_id: str, dependent_id: str, prerequisite_id: str) -> bool:
        row = cursor.execute(
            "WITH RECURSIVE reachable(question_id) AS ("
            "  SELECT prerequisite_question_id FROM question_dependencies "
            "  WHERE research_run_id=? AND dependent_question_id=? "
            "  UNION "
            "  SELECT d.prerequisite_question_id "
            "  FROM question_dependencies d "
            "  JOIN reachable r ON d.dependent_question_id=r.question_id "
            "  WHERE d.research_run_id=?"
            ") SELECT 1 FROM reachable WHERE question_id=? LIMIT 1",
            (run_id, prerequisite_id, run_id, dependent_id),
        ).fetchone()
        return row is not None

    @staticmethod
    def _question_basis_stale_in_cursor(cursor, question_id: str) -> bool:
        snapshots = cursor.execute(
            "SELECT claim_id,claim_status_at_close,claim_updated_at_at_close "
            "FROM question_closure_claims WHERE question_id=? ORDER BY claim_id",
            (question_id,),
        ).fetchall()
        current = cursor.execute(
            "SELECT qcl.claim_id,c.status,c.updated_at "
            "FROM question_claim_links qcl JOIN claims c "
            "ON c.id=qcl.claim_id AND c.research_run_id=qcl.research_run_id "
            "WHERE qcl.question_id=? ORDER BY qcl.claim_id",
            (question_id,),
        ).fetchall()
        if len(snapshots) != len(current):
            return True
        for snapshot, claim in zip(snapshots, current):
            if snapshot["claim_id"] != claim["claim_id"]:
                return True
            if snapshot["claim_status_at_close"] != claim["status"]:
                return True
            if snapshot["claim_updated_at_at_close"] != claim["updated_at"]:
                return True
        return False

    def _dependency_satisfied_in_cursor(self, cursor, row) -> bool:
        prerequisite = cursor.execute(
            "SELECT * FROM research_questions WHERE id=? AND research_run_id=?",
            (row["prerequisite_question_id"], row["research_run_id"]),
        ).fetchone()
        if prerequisite is None:
            raise QueryGraphIntegrityError("prerequisite question not found")
        if prerequisite["workflow_state"] != QuestionWorkflowState.CLOSED.value:
            return False
        if self._question_basis_stale_in_cursor(cursor, prerequisite["id"]):
            return False
        raw_resolution = prerequisite["closed_resolution"]
        if raw_resolution is None:
            return False
        resolution = QuestionResolution(raw_resolution)
        policy = DependencyAcceptancePolicy(row["acceptance_policy"])
        if policy is DependencyAcceptancePolicy.SUPPORTED:
            return resolution is QuestionResolution.SUPPORTED
        if policy is DependencyAcceptancePolicy.PARTIAL_OR_BETTER:
            return resolution in {
                QuestionResolution.PARTIALLY_ANSWERED,
                QuestionResolution.SUPPORTED,
            }
        return resolution in {
            QuestionResolution.PARTIALLY_ANSWERED,
            QuestionResolution.SUPPORTED,
            QuestionResolution.CONTESTED,
        }

    def _dependency_is_satisfied(self, dependency: QuestionDependency) -> bool:
        with self.db._lock:
            cursor = self.db._conn
            self._run_in_cursor(cursor, dependency.research_run_id, require_open=False)
            row = cursor.execute(
                "SELECT * FROM question_dependencies "
                "WHERE dependent_question_id=? AND prerequisite_question_id=?",
                (
                    _id(dependency.dependent_question_id),
                    _id(dependency.prerequisite_question_id),
                ),
            ).fetchone()
            if row is None:
                raise QueryGraphDependencyError("dependency does not exist")
            return self._dependency_satisfied_in_cursor(cursor, row)

    def add_dependency(
        self,
        dependent_question_id: str,
        prerequisite_question_id: str,
        *,
        acceptance_policy: DependencyAcceptancePolicy = DependencyAcceptancePolicy.SUPPORTED,
    ) -> QuestionDependency:
        dependent_question_id = _id(dependent_question_id)
        prerequisite_question_id = _id(prerequisite_question_id)
        try:
            policy = DependencyAcceptancePolicy(acceptance_policy)
        except ValueError as exc:
            raise QueryGraphValidationError("invalid dependency acceptance policy") from exc
        if dependent_question_id == prerequisite_question_id:
            raise QueryGraphCycleError("question cannot depend on itself")
        now = _now()

        def write(cursor):
            dependent = self._question_row_in_cursor(cursor, dependent_question_id)
            if dependent["workflow_state"] == QuestionWorkflowState.CLOSED.value:
                raise QueryGraphLifecycleError(
                    "closed dependent question cannot change dependencies"
                )
            prerequisite = cursor.execute(
                "SELECT * FROM research_questions WHERE id=?",
                (prerequisite_question_id,),
            ).fetchone()
            if prerequisite is None:
                raise QueryGraphIntegrityError("prerequisite question not found")
            self._run_in_cursor(
                cursor, prerequisite["research_run_id"], require_open=True
            )
            if prerequisite["research_run_id"] != dependent["research_run_id"]:
                raise QueryGraphIntegrityError(
                    "questions belong to different research runs"
                )
            duplicate = cursor.execute(
                "SELECT 1 FROM question_dependencies "
                "WHERE dependent_question_id=? AND prerequisite_question_id=?",
                (dependent_question_id, prerequisite_question_id),
            ).fetchone()
            if duplicate is not None:
                raise QueryGraphDependencyError("dependency already exists")
            if self._would_create_cycle(
                cursor,
                dependent["research_run_id"],
                dependent_question_id,
                prerequisite_question_id,
            ):
                raise QueryGraphCycleError("dependency would create a cycle")
            cursor.execute(
                "INSERT INTO question_dependencies "
                "(dependent_question_id,prerequisite_question_id,research_run_id,"
                "acceptance_policy,created_by_agent,created_by_profile,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    dependent_question_id,
                    prerequisite_question_id,
                    dependent["research_run_id"],
                    policy.value,
                    self.scope.agent_id,
                    self.scope.profile_name,
                    now.timestamp(),
                ),
            )
            self._append_event(
                cursor,
                run_id=dependent["research_run_id"],
                graph_id=dependent["graph_id"],
                question_id=dependent_question_id,
                event_type="DEPENDENCY_ADDED",
                payload={
                    "prerequisite_question_id": prerequisite_question_id,
                    "acceptance_policy": policy.value,
                },
                created_at=now,
            )

        try:
            self._write(write)
        except sqlite3.IntegrityError as exc:
            if self._is_lifecycle_integrity_error(exc):
                raise QueryGraphLifecycleError(str(exc)) from exc
            if "UNIQUE constraint failed" in str(exc):
                raise QueryGraphDependencyError("dependency already exists") from exc
            raise QueryGraphIntegrityError(str(exc)) from exc
        return next(
            edge
            for edge in self.list_dependencies(dependent_question_id)
            if edge.prerequisite_question_id == prerequisite_question_id
        )

    def remove_dependency(
        self, dependent_question_id: str, prerequisite_question_id: str
    ) -> None:
        dependent_question_id = _id(dependent_question_id)
        prerequisite_question_id = _id(prerequisite_question_id)
        now = _now()

        def write(cursor):
            dependent = self._question_row_in_cursor(cursor, dependent_question_id)
            if dependent["workflow_state"] == QuestionWorkflowState.CLOSED.value:
                raise QueryGraphLifecycleError(
                    "closed dependent question cannot change dependencies"
                )
            edge = cursor.execute(
                "SELECT * FROM question_dependencies "
                "WHERE dependent_question_id=? AND prerequisite_question_id=?",
                (dependent_question_id, prerequisite_question_id),
            ).fetchone()
            if edge is None:
                raise QueryGraphDependencyError("dependency does not exist")
            cursor.execute(
                "DELETE FROM question_dependencies "
                "WHERE dependent_question_id=? AND prerequisite_question_id=?",
                (dependent_question_id, prerequisite_question_id),
            )
            self._append_event(
                cursor,
                run_id=dependent["research_run_id"],
                graph_id=dependent["graph_id"],
                question_id=dependent_question_id,
                event_type="DEPENDENCY_REMOVED",
                payload={
                    "prerequisite_question_id": prerequisite_question_id,
                    "acceptance_policy": edge["acceptance_policy"],
                },
                created_at=now,
            )

        try:
            self._write(write)
        except sqlite3.IntegrityError as exc:
            if self._is_lifecycle_integrity_error(exc):
                raise QueryGraphLifecycleError(str(exc)) from exc
            raise QueryGraphIntegrityError(str(exc)) from exc

    def list_dependencies(
        self, question_id: str
    ) -> tuple[QuestionDependency, ...]:
        question = self.get_question(_id(question_id))
        rows = self._fetch(
            "SELECT * FROM question_dependencies WHERE dependent_question_id=? "
            "ORDER BY created_at,prerequisite_question_id",
            (question.id,),
        )
        return tuple(self._dependency_dto(row) for row in rows)
