"""Durable Query Graph domain primitives over Hermes SessionDB.

Query Graph v1 is a research-planning layer above Evidence Fabric. Evidence
Fabric remains authoritative for ResearchRun lifecycle and claim state; this
module owns question/graph structure, deterministic normalization, and scoped
read decoding. Mutation behavior is added incrementally by the implementation
plan and must preserve the same runtime-owned scope boundary.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from research.evidence_fabric import ClaimStatus, EvidenceScope

MAX_IDENTIFIER_CHARS = 200
MAX_QUESTION_TEXT_CHARS = 16_384
MAX_EVENT_PAYLOAD_CHARS = 16_384


class GraphRole(StrEnum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"


class GraphWorkflowState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class QuestionRole(StrEnum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"


class QuestionWorkflowState(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    CLOSED = "CLOSED"


class QuestionResolution(StrEnum):
    UNANSWERED = "UNANSWERED"
    PARTIALLY_ANSWERED = "PARTIALLY_ANSWERED"
    SUPPORTED = "SUPPORTED"
    CONTESTED = "CONTESTED"


class DependencyAcceptancePolicy(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIAL_OR_BETTER = "PARTIAL_OR_BETTER"
    ANY_CLOSED = "ANY_CLOSED"


class GraphCreationReason(StrEnum):
    ROOT = "ROOT"
    NEW_DOMAIN = "NEW_DOMAIN"
    SCOPE_EXPANSION = "SCOPE_EXPANSION"
    CONTRADICTION_BRANCH = "CONTRADICTION_BRANCH"
    OTHER = "OTHER"


class QuestionCreationReason(StrEnum):
    ROOT = "ROOT"
    EVIDENCE_GAP = "EVIDENCE_GAP"
    CONTRADICTION = "CONTRADICTION"
    DEPENDENCY = "DEPENDENCY"
    SCOPE_REFINEMENT = "SCOPE_REFINEMENT"
    OTHER = "OTHER"


class ReopenReason(StrEnum):
    NEW_EVIDENCE = "NEW_EVIDENCE"
    CONTRADICTION_DISCOVERED = "CONTRADICTION_DISCOVERED"


@dataclass(frozen=True)
class QueryGraph:
    id: str
    research_run_id: str
    name: str
    purpose: str
    role: GraphRole
    workflow_state: GraphWorkflowState
    created_by_agent: str
    created_by_profile: str | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    closed_by_agent: str | None
    closed_by_profile: str | None


@dataclass(frozen=True)
class ResearchQuestion:
    id: str
    graph_id: str
    research_run_id: str
    question_text: str
    normalized_fingerprint: str
    role: QuestionRole
    workflow_state: QuestionWorkflowState
    creation_reason: QuestionCreationReason
    blocked_reason: str | None
    closed_resolution: QuestionResolution | None
    created_by_agent: str
    created_by_profile: str | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    closed_by_agent: str | None
    closed_by_profile: str | None


@dataclass(frozen=True)
class QuestionWriteResult:
    question: ResearchQuestion
    created: bool


@dataclass(frozen=True)
class QuestionDependency:
    dependent_question_id: str
    prerequisite_question_id: str
    research_run_id: str
    acceptance_policy: DependencyAcceptancePolicy
    created_by_agent: str
    created_by_profile: str | None
    created_at: datetime


@dataclass(frozen=True)
class QuestionClaimLink:
    question_id: str
    claim_id: str
    research_run_id: str
    created_by_agent: str
    created_by_profile: str | None
    created_at: datetime


@dataclass(frozen=True)
class ClosureClaimSnapshot:
    question_id: str
    claim_id: str
    claim_status_at_close: ClaimStatus
    claim_updated_at_at_close: datetime


@dataclass(frozen=True)
class QuestionResolutionView:
    question_id: str
    resolution: QuestionResolution
    stale: bool
    linked_claim_ids: tuple[str, ...]


@dataclass(frozen=True)
class CompletionAssessment:
    ready: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class QueryGraphEvent:
    id: int
    research_run_id: str
    graph_id: str | None
    question_id: str | None
    event_type: str
    actor_agent: str
    actor_profile: str | None
    reason: str | None
    payload: Mapping[str, Any]
    created_at: datetime


class QueryGraphError(Exception):
    pass


class QueryGraphValidationError(QueryGraphError, ValueError):
    pass


class QueryGraphNotFoundError(QueryGraphError, LookupError):
    pass


class QueryGraphScopeError(QueryGraphError, PermissionError):
    pass


class QueryGraphLifecycleError(QueryGraphError, ValueError):
    pass


class QueryGraphIntegrityError(QueryGraphError):
    pass


class QueryGraphCycleError(QueryGraphIntegrityError):
    pass


class QueryGraphDependencyError(QueryGraphIntegrityError):
    pass


class QueryGraphStaleResolutionError(QueryGraphLifecycleError):
    pass


def _id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_CHARS:
        raise QueryGraphValidationError("invalid identifier")
    return value


def _dt(value: float | int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc)


def _required_dt(value: float | int | None) -> datetime:
    decoded = _dt(value)
    if decoded is None:
        raise QueryGraphIntegrityError("required timestamp is missing")
    return decoded


def normalize_question_text(text: str) -> str:
    if not isinstance(text, str):
        raise QueryGraphValidationError("question text is required")
    if len(text) > MAX_QUESTION_TEXT_CHARS:
        raise QueryGraphValidationError("question text is too long")
    normalized = unicodedata.normalize("NFC", text)
    normalized = " ".join(normalized.split()).strip().casefold()
    if not normalized:
        raise QueryGraphValidationError("question text is required")
    return normalized


def question_fingerprint(text: str) -> str:
    normalized = normalize_question_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _decode_payload(raw: str | None) -> Mapping[str, Any]:
    if raw is None:
        return MappingProxyType({})
    if not isinstance(raw, str) or len(raw) > MAX_EVENT_PAYLOAD_CHARS:
        raise QueryGraphIntegrityError("invalid event payload")
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise QueryGraphIntegrityError("invalid event payload") from exc
    if not isinstance(decoded, dict):
        raise QueryGraphIntegrityError("event payload must be an object")
    return MappingProxyType(decoded)


class QueryGraphService:
    def __init__(self, db: "SessionDB", scope: EvidenceScope) -> None:
        self.db = db
        self.scope = scope

    def _fetch(self, sql: str, params: tuple[Any, ...] = ()):
        with self.db._lock:
            return self.db._conn.execute(sql, params).fetchall()

    def _run(self, run_id: str):
        run_id = _id(run_id)
        rows = self._fetch("SELECT * FROM research_runs WHERE id=?", (run_id,))
        if not rows:
            raise QueryGraphNotFoundError("research run not found")
        row = rows[0]
        if row["owner_scope_key"] != self.scope.scope_key:
            raise QueryGraphScopeError("research run belongs to another scope")
        return row

    @staticmethod
    def _graph_dto(row) -> QueryGraph:
        return QueryGraph(
            id=row["id"],
            research_run_id=row["research_run_id"],
            name=row["name"],
            purpose=row["purpose"],
            role=GraphRole(row["role"]),
            workflow_state=GraphWorkflowState(row["workflow_state"]),
            created_by_agent=row["created_by_agent"],
            created_by_profile=row["created_by_profile"],
            created_at=_required_dt(row["created_at"]),
            updated_at=_required_dt(row["updated_at"]),
            closed_at=_dt(row["closed_at"]),
            closed_by_agent=row["closed_by_agent"],
            closed_by_profile=row["closed_by_profile"],
        )

    @staticmethod
    def _question_dto(row) -> ResearchQuestion:
        closed_resolution = row["closed_resolution"]
        return ResearchQuestion(
            id=row["id"],
            graph_id=row["graph_id"],
            research_run_id=row["research_run_id"],
            question_text=row["question_text"],
            normalized_fingerprint=row["normalized_fingerprint"],
            role=QuestionRole(row["role"]),
            workflow_state=QuestionWorkflowState(row["workflow_state"]),
            creation_reason=QuestionCreationReason(row["creation_reason"]),
            blocked_reason=row["blocked_reason"],
            closed_resolution=(
                QuestionResolution(closed_resolution)
                if closed_resolution is not None
                else None
            ),
            created_by_agent=row["created_by_agent"],
            created_by_profile=row["created_by_profile"],
            created_at=_required_dt(row["created_at"]),
            updated_at=_required_dt(row["updated_at"]),
            closed_at=_dt(row["closed_at"]),
            closed_by_agent=row["closed_by_agent"],
            closed_by_profile=row["closed_by_profile"],
        )

    @staticmethod
    def _event_dto(row) -> QueryGraphEvent:
        return QueryGraphEvent(
            id=int(row["id"]),
            research_run_id=row["research_run_id"],
            graph_id=row["graph_id"],
            question_id=row["question_id"],
            event_type=row["event_type"],
            actor_agent=row["actor_agent"],
            actor_profile=row["actor_profile"],
            reason=row["reason"],
            payload=_decode_payload(row["payload_json"]),
            created_at=_required_dt(row["created_at"]),
        )

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
                "SELECT * FROM query_graphs WHERE research_run_id=? ORDER BY created_at,id",
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
                "SELECT * FROM research_questions WHERE graph_id=? ORDER BY created_at,id",
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
        sql = (
            "SELECT * FROM query_graph_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at,id"
        )
        return tuple(
            self._event_dto(row) for row in self._fetch(sql, tuple(params))
        )


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_state import SessionDB
