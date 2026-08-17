"""Evidence Fabric claim-link and deterministic resolution behavior for Query Graph v1."""

from __future__ import annotations

import sqlite3

from research.evidence_fabric import ClaimStatus
from research.query_graph_types import (
    QueryGraphIntegrityError,
    QueryGraphLifecycleError,
    QueryGraphNotFoundError,
    QuestionClaimLink,
    QuestionResolution,
    QuestionResolutionView,
    QuestionWorkflowState,
    _id,
    _now,
    _required_dt,
)


class QueryGraphClaimMixin:
    @staticmethod
    def _claim_link_dto(row) -> QuestionClaimLink:
        return QuestionClaimLink(
            row["question_id"],
            row["claim_id"],
            row["research_run_id"],
            row["created_by_agent"],
            row["created_by_profile"],
            _required_dt(row["created_at"]),
        )

    @staticmethod
    def _resolution_from_statuses(statuses: tuple[ClaimStatus, ...]) -> QuestionResolution:
        if not statuses or set(statuses) <= {
            ClaimStatus.UNVERIFIED,
            ClaimStatus.UNRESOLVED,
        }:
            return QuestionResolution.UNANSWERED
        if ClaimStatus.CONTRADICTED in statuses:
            return QuestionResolution.CONTESTED
        if ClaimStatus.SUPPORTED in statuses:
            if set(statuses) == {ClaimStatus.SUPPORTED}:
                return QuestionResolution.SUPPORTED
            return QuestionResolution.PARTIALLY_ANSWERED
        if ClaimStatus.PARTIALLY_SUPPORTED in statuses:
            return QuestionResolution.PARTIALLY_ANSWERED
        return QuestionResolution.UNANSWERED

    def _claim_row_in_cursor(self, cursor, claim_id: str):
        claim_id = _id(claim_id)
        row = cursor.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if row is None:
            raise QueryGraphNotFoundError("claim not found")
        # Scope validation is deliberately independent of same-run validation:
        # a foreign-scope claim must not leak merely because its id is known.
        self._run_in_cursor(cursor, row["research_run_id"], require_open=True)
        return row

    def link_claim(self, question_id: str, claim_id: str) -> QuestionClaimLink:
        question_id = _id(question_id)
        claim_id = _id(claim_id)
        now = _now()

        def write(cursor):
            question = self._question_row_in_cursor(cursor, question_id)
            if question["workflow_state"] == QuestionWorkflowState.CLOSED.value:
                raise QueryGraphLifecycleError(
                    "closed question claim links are immutable; reopen first"
                )
            claim = self._claim_row_in_cursor(cursor, claim_id)
            if claim["research_run_id"] != question["research_run_id"]:
                raise QueryGraphIntegrityError(
                    "question and claim belong to different research runs"
                )
            existing = cursor.execute(
                "SELECT 1 FROM question_claim_links WHERE question_id=? AND claim_id=?",
                (question_id, claim_id),
            ).fetchone()
            if existing is not None:
                raise QueryGraphIntegrityError("claim link already exists")
            cursor.execute(
                "INSERT INTO question_claim_links "
                "(question_id,claim_id,research_run_id,created_by_agent,"
                "created_by_profile,created_at) VALUES (?,?,?,?,?,?)",
                (
                    question_id,
                    claim_id,
                    question["research_run_id"],
                    self.scope.agent_id,
                    self.scope.profile_name,
                    now.timestamp(),
                ),
            )
            self._append_event(
                cursor,
                run_id=question["research_run_id"],
                graph_id=question["graph_id"],
                question_id=question_id,
                event_type="CLAIM_LINKED",
                payload={"claim_id": claim_id},
                created_at=now,
            )

        try:
            self._write(write)
        except sqlite3.IntegrityError as exc:
            if self._is_lifecycle_integrity_error(exc):
                raise QueryGraphLifecycleError(str(exc)) from exc
            if "UNIQUE constraint failed" in str(exc):
                raise QueryGraphIntegrityError("claim link already exists") from exc
            raise QueryGraphIntegrityError(str(exc)) from exc

        rows = self._fetch(
            "SELECT * FROM question_claim_links WHERE question_id=? AND claim_id=?",
            (question_id, claim_id),
        )
        if not rows:
            raise QueryGraphIntegrityError("claim link commit could not be reconstructed")
        return self._claim_link_dto(rows[0])

    def unlink_claim(self, question_id: str, claim_id: str) -> None:
        question_id = _id(question_id)
        claim_id = _id(claim_id)
        now = _now()

        def write(cursor):
            question = self._question_row_in_cursor(cursor, question_id)
            if question["workflow_state"] == QuestionWorkflowState.CLOSED.value:
                raise QueryGraphLifecycleError(
                    "closed question claim links are immutable; reopen first"
                )
            existing = cursor.execute(
                "SELECT 1 FROM question_claim_links WHERE question_id=? AND claim_id=?",
                (question_id, claim_id),
            ).fetchone()
            if existing is None:
                raise QueryGraphIntegrityError("claim link does not exist")
            cursor.execute(
                "DELETE FROM question_claim_links WHERE question_id=? AND claim_id=?",
                (question_id, claim_id),
            )
            self._append_event(
                cursor,
                run_id=question["research_run_id"],
                graph_id=question["graph_id"],
                question_id=question_id,
                event_type="CLAIM_UNLINKED",
                payload={"claim_id": claim_id},
                created_at=now,
            )

        try:
            self._write(write)
        except sqlite3.IntegrityError as exc:
            if self._is_lifecycle_integrity_error(exc):
                raise QueryGraphLifecycleError(str(exc)) from exc
            raise QueryGraphIntegrityError(str(exc)) from exc

    def derive_resolution(self, question_id: str) -> QuestionResolutionView:
        question = self.get_question(_id(question_id))
        with self.db._lock:
            cursor = self.db._conn
            rows = cursor.execute(
                "SELECT qcl.claim_id,c.status,c.updated_at "
                "FROM question_claim_links qcl JOIN claims c "
                "ON c.id=qcl.claim_id AND c.research_run_id=qcl.research_run_id "
                "WHERE qcl.question_id=? ORDER BY qcl.claim_id",
                (question.id,),
            ).fetchall()
            linked_ids = tuple(row["claim_id"] for row in rows)
            statuses = tuple(ClaimStatus(row["status"]) for row in rows)

            if question.workflow_state is QuestionWorkflowState.CLOSED:
                if question.closed_resolution is None:
                    raise QueryGraphIntegrityError(
                        "closed question is missing its resolution snapshot"
                    )
                stale = self._question_basis_stale_in_cursor(cursor, question.id)
                resolution = question.closed_resolution
            else:
                stale = False
                resolution = self._resolution_from_statuses(statuses)

        return QuestionResolutionView(
            question_id=question.id,
            resolution=resolution,
            stale=stale,
            linked_claim_ids=linked_ids,
        )
