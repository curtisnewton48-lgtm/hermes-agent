"""Closed-question staleness, explicit revalidation, and explicit reopening."""

from __future__ import annotations

import sqlite3

from research.evidence_fabric import ClaimStatus
from research.query_graph_types import (
    QueryGraphIntegrityError,
    QueryGraphLifecycleError,
    QueryGraphStaleResolutionError,
    QueryGraphValidationError,
    QuestionResolution,
    QuestionWorkflowState,
    ReopenReason,
    ResearchQuestion,
    _id,
    _now,
)


class QueryGraphStalenessMixin:
    @staticmethod
    def _current_claim_rows(cursor, question_id: str):
        return cursor.execute(
            "SELECT qcl.claim_id,c.status,c.updated_at "
            "FROM question_claim_links qcl JOIN claims c "
            "ON c.id=qcl.claim_id AND c.research_run_id=qcl.research_run_id "
            "WHERE qcl.question_id=? ORDER BY qcl.claim_id",
            (question_id,),
        ).fetchall()

    def _replace_closure_basis(
        self,
        cursor,
        *,
        question_id: str,
        run_id: str,
        claim_rows,
        snapshot_at,
    ) -> None:
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
                    run_id,
                    claim["status"],
                    claim["updated_at"],
                    snapshot_at.timestamp(),
                ),
            )

    def revalidate_question(self, question_id: str) -> ResearchQuestion:
        question_id = _id(question_id)
        now = _now()

        def write(cursor):
            question = self._question_row_in_cursor(cursor, question_id)
            if question["workflow_state"] != QuestionWorkflowState.CLOSED.value:
                raise QueryGraphLifecycleError(
                    "revalidation requires a closed question"
                )
            if question["closed_resolution"] is None:
                raise QueryGraphIntegrityError(
                    "closed question is missing its resolution snapshot"
                )
            if not self._question_basis_stale_in_cursor(cursor, question_id):
                raise QueryGraphLifecycleError(
                    "question closure basis is not stale"
                )

            claim_rows = self._current_claim_rows(cursor, question_id)
            statuses = tuple(ClaimStatus(row["status"]) for row in claim_rows)
            current_resolution = self._resolution_from_statuses(statuses)
            closed_resolution = QuestionResolution(question["closed_resolution"])
            if current_resolution is not closed_resolution:
                raise QueryGraphStaleResolutionError(
                    f"current resolution {current_resolution.value} does not match "
                    f"closed resolution {closed_resolution.value}"
                )

            self._replace_closure_basis(
                cursor,
                question_id=question_id,
                run_id=question["research_run_id"],
                claim_rows=claim_rows,
                snapshot_at=now,
            )
            cursor.execute(
                "UPDATE research_questions SET updated_at=? WHERE id=?",
                (now.timestamp(), question_id),
            )
            self._append_event(
                cursor,
                run_id=question["research_run_id"],
                graph_id=question["graph_id"],
                question_id=question_id,
                event_type="QUESTION_REVALIDATED",
                payload={
                    "resolution": closed_resolution.value,
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

    def reopen_question(
        self,
        question_id: str,
        reason: ReopenReason,
    ) -> ResearchQuestion:
        question_id = _id(question_id)
        try:
            reopen_reason = ReopenReason(reason)
        except ValueError as exc:
            raise QueryGraphValidationError("invalid reopen reason") from exc
        now = _now()

        def write(cursor):
            question = self._question_row_in_cursor(cursor, question_id)
            if question["workflow_state"] != QuestionWorkflowState.CLOSED.value:
                raise QueryGraphLifecycleError(
                    "reopening requires a closed question"
                )
            if question["closed_resolution"] is None:
                raise QueryGraphIntegrityError(
                    "closed question is missing its resolution snapshot"
                )
            if not self._question_basis_stale_in_cursor(cursor, question_id):
                raise QueryGraphLifecycleError(
                    "question closure basis is not stale"
                )

            claim_rows = self._current_claim_rows(cursor, question_id)
            statuses = tuple(ClaimStatus(row["status"]) for row in claim_rows)
            current_resolution = self._resolution_from_statuses(statuses)
            old_resolution = QuestionResolution(question["closed_resolution"])

            cursor.execute(
                "DELETE FROM question_closure_claims WHERE question_id=?",
                (question_id,),
            )
            cursor.execute(
                "UPDATE research_questions SET workflow_state='IN_PROGRESS',"
                "blocked_reason=NULL,closed_resolution=NULL,closed_at=NULL,"
                "closed_by_agent=NULL,closed_by_profile=NULL,updated_at=? "
                "WHERE id=?",
                (now.timestamp(), question_id),
            )
            self._append_event(
                cursor,
                run_id=question["research_run_id"],
                graph_id=question["graph_id"],
                question_id=question_id,
                event_type="QUESTION_REOPENED",
                reason=reopen_reason.value,
                payload={
                    "old_resolution": old_resolution.value,
                    "current_resolution": current_resolution.value,
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
