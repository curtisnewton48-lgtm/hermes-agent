"""Evidence-gated question closure and immutable closure snapshots."""

from __future__ import annotations

import sqlite3

from research.evidence_fabric import ClaimStatus
from research.query_graph_types import (
    QueryGraphDependencyError,
    QueryGraphIntegrityError,
    QueryGraphLifecycleError,
    QuestionResolution,
    QuestionWorkflowState,
    ResearchQuestion,
    _id,
    _now,
)


class QueryGraphClosureMixin:
    def close_question(self, question_id: str) -> ResearchQuestion:
        question_id = _id(question_id)
        now = _now()

        def write(cursor):
            question = self._question_row_in_cursor(cursor, question_id)
            if question["workflow_state"] == QuestionWorkflowState.CLOSED.value:
                raise QueryGraphLifecycleError(
                    "closed question cannot be closed again"
                )

            claim_rows = cursor.execute(
                "SELECT qcl.claim_id,c.status,c.updated_at "
                "FROM question_claim_links qcl JOIN claims c "
                "ON c.id=qcl.claim_id AND c.research_run_id=qcl.research_run_id "
                "WHERE qcl.question_id=? ORDER BY qcl.claim_id",
                (question_id,),
            ).fetchall()
            statuses = tuple(ClaimStatus(row["status"]) for row in claim_rows)
            resolution = self._resolution_from_statuses(statuses)
            if resolution is QuestionResolution.UNANSWERED:
                raise QueryGraphLifecycleError(
                    "question resolution is UNANSWERED and cannot be closed"
                )

            dependencies = cursor.execute(
                "SELECT * FROM question_dependencies "
                "WHERE dependent_question_id=? "
                "ORDER BY created_at,prerequisite_question_id",
                (question_id,),
            ).fetchall()
            for dependency in dependencies:
                if not self._dependency_satisfied_in_cursor(cursor, dependency):
                    raise QueryGraphDependencyError(
                        f"question {question_id} prerequisite "
                        f"{dependency['prerequisite_question_id']} does not satisfy "
                        f"{dependency['acceptance_policy']}"
                    )

            # Replace any prior basis defensively. In v1 the ordinary path has
            # no prior rows until revalidation/reopen work lands in Task 9,
            # but keeping this operation replacement-safe avoids duplicate-key
            # coupling between those lifecycle operations and closure itself.
            cursor.execute(
                "DELETE FROM question_closure_claims WHERE question_id=?",
                (question_id,),
            )
            for claim in claim_rows:
                cursor.execute(
                    "INSERT INTO question_closure_claims "
                    "(question_id,claim_id,research_run_id,claim_status_at_close,"
                    "claim_updated_at_at_close,snapshot_at) VALUES (?,?,?,?,?,?)",
                    (
                        question_id,
                        claim["claim_id"],
                        question["research_run_id"],
                        claim["status"],
                        claim["updated_at"],
                        now.timestamp(),
                    ),
                )

            cursor.execute(
                "UPDATE research_questions SET workflow_state='CLOSED',"
                "blocked_reason=NULL,closed_resolution=?,closed_at=?,"
                "closed_by_agent=?,closed_by_profile=?,updated_at=? WHERE id=?",
                (
                    resolution.value,
                    now.timestamp(),
                    self.scope.agent_id,
                    self.scope.profile_name,
                    now.timestamp(),
                    question_id,
                ),
            )
            self._append_event(
                cursor,
                run_id=question["research_run_id"],
                graph_id=question["graph_id"],
                question_id=question_id,
                event_type="QUESTION_CLOSED",
                payload={
                    "resolution": resolution.value,
                    "claim_ids": [row["claim_id"] for row in claim_rows],
                },
                created_at=now,
            )

        try:
            self._write(write)
        except sqlite3.IntegrityError as exc:
            if self._is_lifecycle_integrity_error(exc):
                raise QueryGraphLifecycleError(str(exc)) from exc
            raise QueryGraphIntegrityError(str(exc)) from exc
        return self.get_question(question_id)
