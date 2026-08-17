"""Query Graph value types, validation helpers, and deterministic normalization."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from research.evidence_fabric import ClaimStatus

MAX_IDENTIFIER_CHARS = 200
MAX_QUESTION_TEXT_CHARS = 16_384
MAX_EVENT_PAYLOAD_CHARS = 16_384
MAX_EVENT_REASON_CHARS = 2_048

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
    id: str; research_run_id: str; name: str; purpose: str; role: GraphRole; workflow_state: GraphWorkflowState
    created_by_agent: str; created_by_profile: str | None; created_at: datetime; updated_at: datetime
    closed_at: datetime | None; closed_by_agent: str | None; closed_by_profile: str | None

@dataclass(frozen=True)
class ResearchQuestion:
    id: str; graph_id: str; research_run_id: str; question_text: str; normalized_fingerprint: str
    role: QuestionRole; workflow_state: QuestionWorkflowState; creation_reason: QuestionCreationReason
    blocked_reason: str | None; closed_resolution: QuestionResolution | None
    created_by_agent: str; created_by_profile: str | None; created_at: datetime; updated_at: datetime
    closed_at: datetime | None; closed_by_agent: str | None; closed_by_profile: str | None

@dataclass(frozen=True)
class QuestionWriteResult:
    question: ResearchQuestion; created: bool

@dataclass(frozen=True)
class QuestionDependency:
    dependent_question_id: str; prerequisite_question_id: str; research_run_id: str
    acceptance_policy: DependencyAcceptancePolicy; created_by_agent: str; created_by_profile: str | None; created_at: datetime

@dataclass(frozen=True)
class QuestionClaimLink:
    question_id: str; claim_id: str; research_run_id: str; created_by_agent: str; created_by_profile: str | None; created_at: datetime

@dataclass(frozen=True)
class ClosureClaimSnapshot:
    question_id: str; claim_id: str; claim_status_at_close: ClaimStatus; claim_updated_at_at_close: datetime

@dataclass(frozen=True)
class QuestionResolutionView:
    question_id: str; resolution: QuestionResolution; stale: bool; linked_claim_ids: tuple[str, ...]

@dataclass(frozen=True)
class CompletionAssessment:
    ready: bool; reasons: tuple[str, ...]

@dataclass(frozen=True)
class QueryGraphEvent:
    id: int; research_run_id: str; graph_id: str | None; question_id: str | None; event_type: str
    actor_agent: str; actor_profile: str | None; reason: str | None; payload: Mapping[str, Any]; created_at: datetime

class QueryGraphError(Exception): pass
class QueryGraphValidationError(QueryGraphError, ValueError): pass
class QueryGraphNotFoundError(QueryGraphError, LookupError): pass
class QueryGraphScopeError(QueryGraphError, PermissionError): pass
class QueryGraphLifecycleError(QueryGraphError, ValueError): pass
class QueryGraphIntegrityError(QueryGraphError): pass
class QueryGraphCycleError(QueryGraphIntegrityError): pass
class QueryGraphDependencyError(QueryGraphIntegrityError): pass
class QueryGraphStaleResolutionError(QueryGraphLifecycleError): pass

def _id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_CHARS:
        raise QueryGraphValidationError("invalid identifier")
    return value

def _required_text(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip(): raise QueryGraphValidationError(f"{field} is required")
    return value.strip()

def _reason(value: str) -> str:
    value = _required_text(value, field="reason")
    if len(value) > MAX_EVENT_REASON_CHARS: raise QueryGraphValidationError("reason is too long")
    return value

def _now() -> datetime: return datetime.now(timezone.utc)
def _dt(value: float | int | None) -> datetime | None: return datetime.fromtimestamp(value, timezone.utc) if value is not None else None
def _required_dt(value: float | int | None) -> datetime:
    result = _dt(value)
    if result is None: raise QueryGraphIntegrityError("required timestamp is missing")
    return result

def normalize_question_text(text: str) -> str:
    if not isinstance(text, str): raise QueryGraphValidationError("question text is required")
    if len(text) > MAX_QUESTION_TEXT_CHARS: raise QueryGraphValidationError("question text is too long")
    normalized = " ".join(unicodedata.normalize("NFC", text).split()).strip().casefold()
    if not normalized: raise QueryGraphValidationError("question text is required")
    return normalized

def question_fingerprint(text: str) -> str: return hashlib.sha256(normalize_question_text(text).encode("utf-8")).hexdigest()
def _stored_question_text(text: str) -> str: normalize_question_text(text); return text.strip()

def _encode_payload(payload: Mapping[str, Any] | None) -> str:
    if payload is None: return "{}"
    if not isinstance(payload, Mapping): raise QueryGraphValidationError("event payload must be an object")
    try: encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc: raise QueryGraphValidationError("event payload is not JSON serializable") from exc
    if len(encoded) > MAX_EVENT_PAYLOAD_CHARS: raise QueryGraphValidationError("event payload is too large")
    return encoded

def _decode_payload(raw: str | None) -> Mapping[str, Any]:
    if raw is None: return MappingProxyType({})
    if not isinstance(raw, str) or len(raw) > MAX_EVENT_PAYLOAD_CHARS: raise QueryGraphIntegrityError("invalid event payload")
    try: decoded = json.loads(raw)
    except (TypeError, ValueError) as exc: raise QueryGraphIntegrityError("invalid event payload") from exc
    if not isinstance(decoded, dict): raise QueryGraphIntegrityError("event payload must be an object")
    return MappingProxyType(decoded)
