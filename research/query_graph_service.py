"""Scoped Query Graph service core and graph lifecycle."""

from __future__ import annotations

import sqlite3
import uuid
from typing import TYPE_CHECKING, Any, Mapping

from research.evidence_fabric import EvidenceScope
from research.query_graph_questions import QueryGraphQuestionMixin
from research.query_graph_types import (
    GraphCreationReason,
    GraphRole,
    GraphWorkflowState,
    QueryGraph,
    QueryGraphEvent,
    QueryGraphIntegrityError,
    QueryGraphLifecycleError,
    QueryGraphNotFoundError,
    QueryGraphScopeError,
    QueryGraphValidationError,
    QuestionCreationReason,
    QuestionResolution,
    QuestionRole,
    QuestionWorkflowState,
    ResearchQuestion,
    _decode_payload,
    _dt,
    _encode_payload,
    _id,
    _now,
    _reason,
    _required_dt,
    _required_text,
)

if TYPE_CHECKING:
    from hermes_state import SessionDB


class QueryGraphService(QueryGraphQuestionMixin):
    def __init__(self, db: SessionDB, scope: EvidenceScope) -> None:
        self.db = db
        self.scope = scope

    def _write(self, fn):
        return self.db._execute_write(fn)

    def _fetch(self, sql: str, params: tuple[Any, ...] = ()):
        with self.db._lock:
            return self.db._conn.execute(sql, params).fetchall()

    @staticmethod
    def _is_lifecycle_integrity_error(exc: sqlite3.IntegrityError) -> bool:
        return str(exc) in {
            "research run is not open",
            "terminal research run is immutable",
        }

    def _run(self, run_id: str):
        run_id = _id(run_id)
        rows = self._fetch("SELECT * FROM research_runs WHERE id=?", (run_id,))
        if not rows:
            raise QueryGraphNotFoundError("research run not found")
        row = rows[0]
        if row["owner_scope_key"] != self.scope.scope_key:
            raise QueryGraphScopeError("research run belongs to another scope")
        return row

    def _run_in_cursor(self, cursor, run_id: str, *, require_open: bool = False):
        run_id = _id(run_id)
        row = cursor.execute(
            "SELECT * FROM research_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise QueryGraphNotFoundError("research run not found")
        if row["owner_scope_key"] != self.scope.scope_key:
            raise QueryGraphScopeError("research run belongs to another scope")
        if require_open and row["status"] != "OPEN":
            raise QueryGraphLifecycleError("terminal research run is immutable")
        return row

    def _graph_row_in_cursor(self, cursor, graph_id: str):
        graph_id = _id(graph_id)
        row = cursor.execute(
            "SELECT * FROM query_graphs WHERE id=?", (graph_id,)
        ).fetchone()
        if row is None:
            raise QueryGraphNotFoundError("query graph not found")
        self._run_in_cursor(cursor, row["research_run_id"], require_open=True)
        return row

    def _question_row_in_cursor(self, cursor, question_id: str):
        question_id = _id(question_id)
        row = cursor.execute(
            "SELECT * FROM research_questions WHERE id=?", (question_id,)
        ).fetchone()
        if row is None:
            raise QueryGraphNotFoundError("research question not found")
        self._run_in_cursor(cursor, row["research_run_id"], require_open=True)
        return row

    def _append_event(
        self,
        cursor,
        *,
        run_id: str,
        event_type: str,
        graph_id: str | None = None,
        question_id: str | None = None,
        reason: str | None = None,
        payload: Mapping[str, Any] | None = None,
        created_at=None,
    ) -> None:
        event_type = _required_text(event_type, field="event type")
        if len(event_type) > 100:
            raise QueryGraphValidationError("event type is too long")
        graph_id = _id(graph_id) if graph_id is not None else None
        question_id = _id(question_id) if question_id is not None else None
        clean_reason = _reason(reason) if reason is not None else None
        now = created_at or _now()
        cursor.execute(
            "INSERT INTO query_graph_events "
            "(research_run_id,graph_id,question_id,event_type,actor_agent,"
            "actor_profile,reason,payload_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                _id(run_id),
                graph_id,
                question_id,
                event_type,
                self.scope.agent_id,
                self.scope.profile_name,
                clean_reason,
                _encode_payload(payload),
                now.timestamp(),
            ),
        )

    @staticmethod
    def _graph_dto(row) -> QueryGraph:
        return QueryGraph(
            row["id"],
            row["research_run_id"],
            row["name"],
            row["purpose"],
            GraphRole(row["role"]),
            GraphWorkflowState(row["workflow_state"]),
            row["created_by_agent"],
            row["created_by_profile"],
            _required_dt(row["created_at"]),
            _required_dt(row["updated_at"]),
            _dt(row["closed_at"]),
            row["closed_by_agent"],
            row["closed_by_profile"],
        )

    @staticmethod
    def _question_dto(row) -> ResearchQuestion:
        closed = row["closed_resolution"]
        return ResearchQuestion(
            row["id"],
            row["graph_id"],
            row["research_run_id"],
            row["question_text"],
            row["normalized_fingerprint"],
            QuestionRole(row["role"]),
            QuestionWorkflowState(row["workflow_state"]),
            QuestionCreationReason(row["creation_reason"]),
            row["blocked_reason"],
            QuestionResolution(closed) if closed is not None else None,
            row["created_by_agent"],
            row["created_by_profile"],
            _required_dt(row["created_at"]),
            _required_dt(row["updated_at"]),
            _dt(row["closed_at"]),
            row["closed_by_agent"],
            row["closed_by_profile"],
        )

    @staticmethod
    def _event_dto(row) -> QueryGraphEvent:
        return QueryGraphEvent(
            int(row["id"]),
            row["research_run_id"],
            row["graph_id"],
            row["question_id"],
            row["event_type"],
            row["actor_agent"],
            row["actor_profile"],
            row["reason"],
            _decode_payload(row["payload_json"]),
            _required_dt(row["created_at"]),
        )

    def create_graph(
        self,
        run_id: str,
        *,
        name: str,
        purpose: str,
        role: GraphRole = GraphRole.OPTIONAL,
        reason: GraphCreationReason = GraphCreationReason.ROOT,
    ) -> QueryGraph:
        run_id = _id(run_id)
        name = _required_text(name, field="graph name")
        purpose = _required_text(purpose, field="graph purpose")
        try:
            role = GraphRole(role)
            creation_reason = GraphCreationReason(reason)
        except ValueError as exc:
            raise QueryGraphValidationError(
                "invalid graph role or creation reason"
            ) from exc
        graph_id = str(uuid.uuid4())
        now = _now()

        def write(cursor):
            self._run_in_cursor(cursor, run_id, require_open=True)
            cursor.execute(
                "INSERT INTO query_graphs "
                "(id,research_run_id,name,purpose,role,workflow_state,"
                "created_by_agent,created_by_profile,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'OPEN',?,?,?,?)",
                (
                    graph_id,
                    run_id,
                    name,
                    purpose,
                    role.value,
                    self.scope.agent_id,
                    self.scope.profile_name,
                    now.timestamp(),
                    now.timestamp(),
                ),
            )
            self._append_event(
                cursor,
                run_id=run_id,
                graph_id=graph_id,
                event_type="GRAPH_CREATED",
                reason=creation_reason.value,
                payload={"name": name, "purpose": purpose, "role": role.value},
                created_at=now,
            )

        try:
            self._write(write)
        except sqlite3.IntegrityError as exc:
            if self._is_lifecycle_integrity_error(exc):
                raise QueryGraphLifecycleError(str(exc)) from exc
            raise QueryGraphIntegrityError(str(exc)) from exc
        return self.get_graph(graph_id)

    def set_graph_role(
        self, graph_id: str, role: GraphRole, *, reason: str
    ) -> QueryGraph:
        graph_id = _id(graph_id)
        clean_reason = _reason(reason)
        try:
            role = GraphRole(role)
        except ValueError as exc:
            raise QueryGraphValidationError("invalid graph role") from exc
        now = _now()

        def write(cursor):
            row = self._graph_row_in_cursor(cursor, graph_id)
            if row["workflow_state"] == GraphWorkflowState.CLOSED.value:
                raise QueryGraphLifecycleError(
                    "closed query graph role is immutable"
                )
            if row["role"] == role.value:
                raise QueryGraphLifecycleError(
                    f"graph role is already {role.value}"
                )
            old_role = row["role"]
            cursor.execute(
                "UPDATE query_graphs SET role=?,updated_at=? WHERE id=?",
                (role.value, now.timestamp(), graph_id),
            )
            self._append_event(
                cursor,
                run_id=row["research_run_id"],
                graph_id=graph_id,
                event_type="GRAPH_ROLE_CHANGED",
                reason=clean_reason,
                payload={"old_role": old_role, "new_role": role.value},
                created_at=now,
            )

        try:
            self._write(write)
        except sqlite3.IntegrityError as exc:
            if self._is_lifecycle_integrity_error(exc):
                raise QueryGraphLifecycleError(str(exc)) from exc
            raise QueryGraphIntegrityError(str(exc)) from exc
        return self.get_graph(graph_id)

    def get_graph(self, graph_id: str) -> QueryGraph:
        graph_id = _id(graph_id)
        rows = self._fetch("SELECT * FROM query_graphs WHERE id=?", (graph_id,))
        if not rows:
            raise QueryGraphNotFoundError("query graph not found")
        row = rows[0]
        self._run(row["research_run_id"])
        return self._graph_dto(row)

    def list_graphs(self, run_id: str) -> tuple[QueryGraph, ...]:
        run = self._run(run_id)
        return tuple(
            self._graph_dto(row)
            for row in self._fetch(
                "SELECT * FROM query_graphs WHERE research_run_id=? "
                "ORDER BY created_at,id",
                (run["id"],),
            )
        )

    def get_question(self, question_id: str) -> ResearchQuestion:
        question_id = _id(question_id)
        rows = self._fetch(
            "SELECT * FROM research_questions WHERE id=?", (question_id,)
        )
        if not rows:
            raise QueryGraphNotFoundError("research question not found")
        row = rows[0]
        self._run(row["research_run_id"])
        return self._question_dto(row)

    def list_questions(self, graph_id: str) -> tuple[ResearchQuestion, ...]:
        graph = self.get_graph(graph_id)
        return tuple(
            self._question_dto(row)
            for row in self._fetch(
                "SELECT * FROM research_questions WHERE graph_id=? "
                "ORDER BY created_at,id",
                (graph.id,),
            )
        )

    def list_events(
        self,
        run_id: str,
        *,
        graph_id: str | None = None,
        question_id: str | None = None,
    ) -> tuple[QueryGraphEvent, ...]:
        run = self._run(run_id)
        clauses = ["research_run_id=?"]
        params: list[Any] = [run["id"]]
        if graph_id is not None:
            clauses.append("graph_id=?")
            params.append(_id(graph_id))
        if question_id is not None:
            clauses.append("question_id=?")
            params.append(_id(question_id))
        rows = self._fetch(
            "SELECT * FROM query_graph_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at,id",
            tuple(params),
        )
        return tuple(self._event_dto(row) for row in rows)
