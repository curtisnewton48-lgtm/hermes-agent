"""Question mutation mixin for QueryGraphService.

The mixin deliberately depends only on the service's scoped transactional
helpers. General dependency-DAG mutation is added in the next implementation
task; proposal-time dependencies are safe because the dependent node is new.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Sequence

from research.query_graph_types import (
    DependencyAcceptancePolicy,
    GraphWorkflowState,
    QueryGraphIntegrityError,
    QueryGraphLifecycleError,
    QueryGraphValidationError,
    QuestionCreationReason,
    QuestionRole,
    QuestionWorkflowState,
    QuestionWriteResult,
    ResearchQuestion,
    _id,
    _now,
    _reason,
    _stored_question_text,
    question_fingerprint,
)


class QueryGraphQuestionMixin:
    def _insert_new_question_dependency(
        self,
        cursor,
        *,
        question_id: str,
        graph_id: str,
        run_id: str,
        prerequisite_id: str,
        policy: DependencyAcceptancePolicy,
        now,
    ) -> None:
        prerequisite_id = _id(prerequisite_id)
        prerequisite = cursor.execute(
            "SELECT * FROM research_questions WHERE id=?", (prerequisite_id,)
        ).fetchone()
        if prerequisite is None:
            raise QueryGraphIntegrityError("prerequisite question not found")
        if prerequisite["research_run_id"] != run_id:
            raise QueryGraphIntegrityError("prerequisite belongs to different run")
        cursor.execute(
            "INSERT INTO question_dependencies "
            "(dependent_question_id,prerequisite_question_id,research_run_id,"
            "acceptance_policy,created_by_agent,created_by_profile,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                question_id,
                prerequisite_id,
                run_id,
                policy.value,
                self.scope.agent_id,
                self.scope.profile_name,
                now.timestamp(),
            ),
        )
        self._append_event(
            cursor,
            run_id=run_id,
            graph_id=graph_id,
            question_id=question_id,
            event_type="DEPENDENCY_ADDED",
            payload={
                "prerequisite_question_id": prerequisite_id,
                "acceptance_policy": policy.value,
            },
            created_at=now,
        )

    def propose_question(
        self,
        graph_id: str,
        text: str,
        *,
        role: QuestionRole = QuestionRole.OPTIONAL,
        reason: QuestionCreationReason = QuestionCreationReason.ROOT,
        dependencies: Sequence[tuple[str, DependencyAcceptancePolicy]] = (),
    ) -> QuestionWriteResult:
        graph_id = _id(graph_id)
        stored_text = _stored_question_text(text)
        fingerprint = question_fingerprint(text)
        try:
            role = QuestionRole(role)
            creation_reason = QuestionCreationReason(reason)
            deps = tuple(
                (_id(prerequisite_id), DependencyAcceptancePolicy(policy))
                for prerequisite_id, policy in dependencies
            )
        except (TypeError, ValueError) as exc:
            raise QueryGraphValidationError(
                "invalid question role, reason, or dependency"
            ) from exc
        question_id = str(uuid.uuid4())
        now = _now()

        def write(cursor):
            graph = self._graph_row_in_cursor(cursor, graph_id)
            if graph["workflow_state"] == GraphWorkflowState.CLOSED.value:
                raise QueryGraphLifecycleError(
                    "closed query graph cannot accept new questions"
                )
            existing = cursor.execute(
                "SELECT id FROM research_questions "
                "WHERE research_run_id=? AND normalized_fingerprint=?",
                (graph["research_run_id"], fingerprint),
            ).fetchone()
            if existing is not None:
                return existing["id"], False

            cursor.execute(
                "INSERT INTO research_questions "
                "(id,graph_id,research_run_id,question_text,normalized_fingerprint,"
                "role,workflow_state,creation_reason,created_by_agent,created_by_profile,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,'OPEN',?,?,?,?,?)",
                (
                    question_id,
                    graph_id,
                    graph["research_run_id"],
                    stored_text,
                    fingerprint,
                    role.value,
                    creation_reason.value,
                    self.scope.agent_id,
                    self.scope.profile_name,
                    now.timestamp(),
                    now.timestamp(),
                ),
            )
            self._append_event(
                cursor,
                run_id=graph["research_run_id"],
                graph_id=graph_id,
                question_id=question_id,
                event_type="QUESTION_CREATED",
                reason=creation_reason.value,
                payload={
                    "graph_id": graph_id,
                    "question_text": stored_text,
                    "role": role.value,
                    "normalized_fingerprint": fingerprint,
                },
                created_at=now,
            )
            for prerequisite_id, policy in deps:
                self._insert_new_question_dependency(
                    cursor,
                    question_id=question_id,
                    graph_id=graph_id,
                    run_id=graph["research_run_id"],
                    prerequisite_id=prerequisite_id,
                    policy=policy,
                    now=now,
                )
            return question_id, True

        try:
            result_id, created = self._write(write)
        except sqlite3.IntegrityError as exc:
            if self._is_lifecycle_integrity_error(exc):
                raise QueryGraphLifecycleError(str(exc)) from exc
            if (
                "UNIQUE constraint failed" in str(exc)
                and "normalized_fingerprint" in str(exc)
            ):
                graph = self.get_graph(graph_id)
                rows = self._fetch(
                    "SELECT id FROM research_questions "
                    "WHERE research_run_id=? AND normalized_fingerprint=?",
                    (graph.research_run_id, fingerprint),
                )
                if rows:
                    return QuestionWriteResult(
                        self.get_question(rows[0]["id"]), False
                    )
            raise QueryGraphIntegrityError(str(exc)) from exc
        return QuestionWriteResult(self.get_question(result_id), created)

    def refine_question(
        self, question_id: str, new_text: str, *, reason: str
    ) -> ResearchQuestion:
        question_id = _id(question_id)
        stored_text = _stored_question_text(new_text)
        fingerprint = question_fingerprint(new_text)
        clean_reason = _reason(reason)
        now = _now()

        def write(cursor):
            row = self._question_row_in_cursor(cursor, question_id)
            if row["workflow_state"] == QuestionWorkflowState.CLOSED.value:
                raise QueryGraphLifecycleError(
                    "closed question must be reopened before refinement"
                )
            if row["normalized_fingerprint"] == fingerprint:
                raise QueryGraphLifecycleError("question refinement is unchanged")
            duplicate = cursor.execute(
                "SELECT id FROM research_questions "
                "WHERE research_run_id=? AND normalized_fingerprint=? AND id<>?",
                (row["research_run_id"], fingerprint, question_id),
            ).fetchone()
            if duplicate is not None:
                raise QueryGraphIntegrityError("duplicate question in research run")

            old_text = row["question_text"]
            old_fingerprint = row["normalized_fingerprint"]
            cursor.execute(
                "UPDATE research_questions SET question_text=?,"
                "normalized_fingerprint=?,updated_at=? WHERE id=?",
                (stored_text, fingerprint, now.timestamp(), question_id),
            )
            self._append_event(
                cursor,
                run_id=row["research_run_id"],
                graph_id=row["graph_id"],
                question_id=question_id,
                event_type="QUESTION_REFINED",
                reason=clean_reason,
                payload={
                    "old_text": old_text,
                    "new_text": stored_text,
                    "old_fingerprint": old_fingerprint,
                    "new_fingerprint": fingerprint,
                },
                created_at=now,
            )

        try:
            self._write(write)
        except sqlite3.IntegrityError as exc:
            if self._is_lifecycle_integrity_error(exc):
                raise QueryGraphLifecycleError(str(exc)) from exc
            if "UNIQUE constraint failed" in str(exc):
                raise QueryGraphIntegrityError(
                    "duplicate question in research run"
                ) from exc
            raise QueryGraphIntegrityError(str(exc)) from exc
        return self.get_question(question_id)

    def set_question_role(
        self,
        question_id: str,
        role: QuestionRole,
        *,
        reason: str,
    ) -> ResearchQuestion:
        question_id = _id(question_id)
        clean_reason = _reason(reason)
        try:
            role = QuestionRole(role)
        except ValueError as exc:
            raise QueryGraphValidationError("invalid question role") from exc
        now = _now()

        def write(cursor):
            row = self._question_row_in_cursor(cursor, question_id)
            if row["workflow_state"] == QuestionWorkflowState.CLOSED.value:
                raise QueryGraphLifecycleError("closed question role is immutable")
            if row["role"] == role.value:
                raise QueryGraphLifecycleError(
                    f"question role is already {role.value}"
                )
            old_role = row["role"]
            cursor.execute(
                "UPDATE research_questions SET role=?,updated_at=? WHERE id=?",
                (role.value, now.timestamp(), question_id),
            )
            self._append_event(
                cursor,
                run_id=row["research_run_id"],
                graph_id=row["graph_id"],
                question_id=question_id,
                event_type="QUESTION_ROLE_CHANGED",
                reason=clean_reason,
                payload={"old_role": old_role, "new_role": role.value},
                created_at=now,
            )

        self._write(write)
        return self.get_question(question_id)

    def start_question(self, question_id: str) -> ResearchQuestion:
        question_id = _id(question_id)
        now = _now()

        def write(cursor):
            row = self._question_row_in_cursor(cursor, question_id)
            state = QuestionWorkflowState(row["workflow_state"])
            if state is QuestionWorkflowState.CLOSED:
                raise QueryGraphLifecycleError("closed question cannot be started")
            if state is QuestionWorkflowState.IN_PROGRESS:
                raise QueryGraphLifecycleError("question is already IN_PROGRESS")
            if state is QuestionWorkflowState.BLOCKED:
                raise QueryGraphLifecycleError(
                    "blocked question must be unblocked"
                )
            cursor.execute(
                "UPDATE research_questions SET workflow_state='IN_PROGRESS',"
                "blocked_reason=NULL,updated_at=? WHERE id=?",
                (now.timestamp(), question_id),
            )
            self._append_event(
                cursor,
                run_id=row["research_run_id"],
                graph_id=row["graph_id"],
                question_id=question_id,
                event_type="QUESTION_STARTED",
                payload={"old_state": state.value, "new_state": "IN_PROGRESS"},
                created_at=now,
            )

        self._write(write)
        return self.get_question(question_id)

    def block_question(
        self, question_id: str, *, reason: str
    ) -> ResearchQuestion:
        question_id = _id(question_id)
        clean_reason = _reason(reason)
        now = _now()

        def write(cursor):
            row = self._question_row_in_cursor(cursor, question_id)
            state = QuestionWorkflowState(row["workflow_state"])
            if state is QuestionWorkflowState.CLOSED:
                raise QueryGraphLifecycleError("closed question cannot be blocked")
            if state is QuestionWorkflowState.BLOCKED:
                raise QueryGraphLifecycleError("question is already BLOCKED")
            cursor.execute(
                "UPDATE research_questions SET workflow_state='BLOCKED',"
                "blocked_reason=?,updated_at=? WHERE id=?",
                (clean_reason, now.timestamp(), question_id),
            )
            self._append_event(
                cursor,
                run_id=row["research_run_id"],
                graph_id=row["graph_id"],
                question_id=question_id,
                event_type="QUESTION_BLOCKED",
                reason=clean_reason,
                payload={"old_state": state.value, "new_state": "BLOCKED"},
                created_at=now,
            )

        self._write(write)
        return self.get_question(question_id)

    def unblock_question(self, question_id: str) -> ResearchQuestion:
        question_id = _id(question_id)
        now = _now()

        def write(cursor):
            row = self._question_row_in_cursor(cursor, question_id)
            state = QuestionWorkflowState(row["workflow_state"])
            if state is not QuestionWorkflowState.BLOCKED:
                raise QueryGraphLifecycleError("question is not blocked")
            old_reason = row["blocked_reason"]
            cursor.execute(
                "UPDATE research_questions SET workflow_state='IN_PROGRESS',"
                "blocked_reason=NULL,updated_at=? WHERE id=?",
                (now.timestamp(), question_id),
            )
            self._append_event(
                cursor,
                run_id=row["research_run_id"],
                graph_id=row["graph_id"],
                question_id=question_id,
                event_type="QUESTION_UNBLOCKED",
                payload={
                    "old_state": "BLOCKED",
                    "new_state": "IN_PROGRESS",
                    "blocked_reason": old_reason,
                },
                created_at=now,
            )

        self._write(write)
        return self.get_question(question_id)
