# Query Graph v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable, research-specific Query Graph layer above Evidence Fabric v1 so Hermes can persist research questions, cross-graph dependencies, evidence-gated resolution, closure snapshots, staleness, audit history, and deterministic graph/run readiness without adding scheduling, verification, model routing, or a second database.

**Architecture:** Extend the existing Hermes `SessionDB` schema with Query Graph tables while keeping graph semantics, normalization, lifecycle validation, DAG checks, Evidence Fabric claim integration, and readiness evaluation in a dedicated `research/query_graph.py` service. Query Graph consumes Evidence Fabric claim/run state as authoritative input, uses the existing SQLite/WAL/`BEGIN IMMEDIATE` write discipline, records every mutation and audit event atomically, and exposes only a trusted runtime/service API in v1.

**Tech Stack:** Python 3.11+, SQLite, existing `SessionDB`, Evidence Fabric v1 (`research/evidence_fabric.py`), pytest, existing Hermes schema reconciliation/migration code.

## Global Constraints

- Authoritative implementation dependency is hardened Evidence Fabric v1 HEAD `6e0fd99f7c99e79c0017ff40092bfa80ef715390`; do not disturb the open Evidence Fabric PR branch.
- Implement Query Graph in a separate isolated branch/worktree based on the approved design branch or equivalent dependent HEAD.
- Before touching schema code, re-check upstream `NousResearch/hermes-agent` `main` and the dependent branch for `SCHEMA_VERSION`; Evidence Fabric currently uses v27 while upstream `main` is still v26. Use the next free additive version at implementation time; if another schema version lands first, renumber mechanically rather than preserving a stale constant.
- Reuse Hermes `SessionDB` and profile `state.db`; do not add a second database, process-global authoritative graph cache, arbitrary SQL API, or filesystem database-path API.
- Query Graph is research-specific v1. Do not generalize it into coding/legal/personal workflow orchestration.
- Nodes are questions only. Do not add task, search, hypothesis, conclusion, agent, or source node types.
- One `ResearchRun` may own multiple `QueryGraph` records. Each `ResearchQuestion` belongs to exactly one owning graph.
- Dependencies may cross graph boundaries only within the same `ResearchRun`; the entire run is one DAG.
- `DEPENDS_ON` blocks closure, not investigation. Agents may work dependent questions in parallel.
- Every dependency has one acceptance policy: `SUPPORTED`, `PARTIAL_OR_BETTER`, or `ANY_CLOSED`; default `SUPPORTED`.
- Questions and graphs have `REQUIRED | OPTIONAL` roles. Worker-proposed graphs and worker-discovered questions default to `OPTIONAL` unless a trusted caller explicitly requests otherwise.
- Question text deduplication is exact-normalized and run-wide. Use deterministic Unicode normalization + whitespace collapse + trim + casefold + SHA-256. No semantic embeddings, fuzzy matching, or model deduplication.
- Active question resolution is derived deterministically from same-run Evidence Fabric `ClaimRecord.status`; models never directly assign `SUPPORTED`, `PARTIALLY_ANSWERED`, or `CONTESTED`.
- Closed questions freeze an immutable closure snapshot. Later claim-basis changes derive `resolution_stale=True`; they do not rewrite historical closure state.
- A stale required question blocks graph/run readiness until explicit revalidation or reopening. Optional stale questions warn but do not block unless structurally required by a dependency from a required path.
- Reopening is explicit and validated for material reasons such as `NEW_EVIDENCE` or `CONTRADICTION_DISCOVERED`; there is no automatic reopen.
- Active question text may be refined in place with immutable audit history; a closed question must be reopened before refinement.
- Query Graph current-state tables are authoritative. `query_graph_events` is append-only audit history, not event-sourced state.
- No hard delete of graphs/questions in v1.
- State mutation and corresponding audit event must commit or roll back in the same transaction.
- Evidence Fabric remains authoritative for `ResearchRun`, claims, claim status, and terminal lifecycle. Query Graph never mutates claim status or terminalizes a run.
- If `ResearchRun.status != OPEN`, Query Graph rejects every structural/evidential mutation. Reads and audit inspection remain allowed.
- `assess_run_completion()` is advisory only and must never transition the `ResearchRun`.
- No model-facing `query_graph` tool is required for v1. Unless a trusted runtime path independently proves authorized run/scope/actor identity, record/retain `MODEL_TOOL_DEFERRED` and ship service API only.
- Do not modify Knowledge Hub, Desktop, HUD, browser isolation, grounded citations, Obsidian/NotebookLM, delegation, model routing, or Adaptive Swarm Director behavior.
- Every production behavior follows failing-first TDD: write a focused failing test, run it to observe the intended failure, implement the smallest change, rerun, then commit.
- Public domain failures must be stable Query Graph exceptions; expected behavior must not leak raw `sqlite3.IntegrityError`.

---

## File Map

- Create: `research/query_graph.py` — Query Graph enums, DTOs, normalization, service methods, deterministic resolution, DAG validation, lifecycle/readiness logic, domain errors.
- Modify: `research/__init__.py` — export Query Graph public domain names alongside Evidence Fabric exports.
- Modify: `hermes_state_common.py` — Query Graph DDL, indexes, triggers, and schema-version advance.
- Modify only if required by migration mechanics: `hermes_state_schema.py` — additive migration/reconciliation handling for the chosen next schema version.
- Create: `tests/test_query_graph_schema.py` — fresh schema, true pre-Query-Graph migration, indexes/FKs/triggers, terminal-run direct-SQL guards.
- Create: `tests/test_query_graph.py` — graph/question CRUD, scope, roles, refinement, claim links, deterministic resolution, closure/reopen/revalidation, graph/run readiness.
- Create: `tests/test_query_graph_dependencies.py` — run-wide DAG, cross-graph dependencies, acceptance policies, dependency closure semantics.
- Create: `tests/test_query_graph_concurrency.py` — exact-normalized concurrent question dedup, concurrent edge-cycle safety, atomic event/state behavior, restart durability.
- Create: `tests/test_query_graph_end_to_end.py` — canonical multi-graph research scenario spanning real Evidence Fabric claims and readiness reconstruction.
- Do not create: `tools/query_graph_tool.py` unless an independently trusted runtime-authority proof exists before implementation; v1 acceptance does not depend on it.

## Public Contracts Shared by All Tasks

Implement these exact public names in `research/query_graph.py` unless a concrete existing Hermes naming collision is discovered before code is written. If a collision exists, stop and update this plan/spec before implementation rather than silently renaming one task at a time.

```python
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence

from research.evidence_fabric import ClaimStatus, EvidenceScope


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


def normalize_question_text(text: str) -> str:
    ...


def question_fingerprint(text: str) -> str:
    ...


class QueryGraphService:
    def __init__(self, db: "SessionDB", scope: EvidenceScope) -> None:
        ...

    def create_graph(
        self,
        run_id: str,
        *,
        name: str,
        purpose: str,
        role: GraphRole = GraphRole.OPTIONAL,
        reason: GraphCreationReason = GraphCreationReason.ROOT,
    ) -> QueryGraph:
        ...

    def get_graph(self, graph_id: str) -> QueryGraph:
        ...

    def list_graphs(self, run_id: str) -> tuple[QueryGraph, ...]:
        ...

    def set_graph_role(
        self, graph_id: str, role: GraphRole, *, reason: str,
    ) -> QueryGraph:
        ...

    def propose_question(
        self,
        graph_id: str,
        text: str,
        *,
        role: QuestionRole = QuestionRole.OPTIONAL,
        reason: QuestionCreationReason = QuestionCreationReason.ROOT,
        dependencies: Sequence[
            tuple[str, DependencyAcceptancePolicy]
        ] = (),
    ) -> QuestionWriteResult:
        ...

    def get_question(self, question_id: str) -> ResearchQuestion:
        ...

    def list_questions(self, graph_id: str) -> tuple[ResearchQuestion, ...]:
        ...

    def refine_question(
        self, question_id: str, new_text: str, *, reason: str,
    ) -> ResearchQuestion:
        ...

    def set_question_role(
        self, question_id: str, role: QuestionRole, *, reason: str,
    ) -> ResearchQuestion:
        ...

    def start_question(self, question_id: str) -> ResearchQuestion:
        ...

    def block_question(
        self, question_id: str, *, reason: str,
    ) -> ResearchQuestion:
        ...

    def unblock_question(self, question_id: str) -> ResearchQuestion:
        ...

    def add_dependency(
        self,
        dependent_question_id: str,
        prerequisite_question_id: str,
        *,
        acceptance_policy: DependencyAcceptancePolicy = DependencyAcceptancePolicy.SUPPORTED,
    ) -> QuestionDependency:
        ...

    def remove_dependency(
        self, dependent_question_id: str, prerequisite_question_id: str,
    ) -> None:
        ...

    def list_dependencies(
        self, question_id: str,
    ) -> tuple[QuestionDependency, ...]:
        ...

    def link_claim(self, question_id: str, claim_id: str) -> QuestionClaimLink:
        ...

    def unlink_claim(self, question_id: str, claim_id: str) -> None:
        ...

    def derive_resolution(self, question_id: str) -> QuestionResolutionView:
        ...

    def close_question(self, question_id: str) -> ResearchQuestion:
        ...

    def reopen_question(
        self, question_id: str, *, reason: ReopenReason,
    ) -> ResearchQuestion:
        ...

    def revalidate_question(self, question_id: str) -> ResearchQuestion:
        ...

    def close_graph(self, graph_id: str) -> QueryGraph:
        ...

    def assess_graph_completion(self, graph_id: str) -> CompletionAssessment:
        ...

    def assess_run_completion(self, run_id: str) -> CompletionAssessment:
        ...

    def list_events(
        self,
        run_id: str,
        *,
        graph_id: str | None = None,
        question_id: str | None = None,
    ) -> tuple[QueryGraphEvent, ...]:
        ...
```

### Deterministic Resolution Mapping

`derive_resolution()` must map the set of linked Evidence Fabric claim statuses with this exact v1 minimum rule:

```python
def _resolution_from_claim_statuses(statuses: set[ClaimStatus]) -> QuestionResolution:
    if not statuses or statuses <= {ClaimStatus.UNVERIFIED, ClaimStatus.UNRESOLVED}:
        return QuestionResolution.UNANSWERED
    if ClaimStatus.CONTRADICTED in statuses:
        return QuestionResolution.CONTESTED
    if ClaimStatus.SUPPORTED in statuses:
        if statuses <= {ClaimStatus.SUPPORTED}:
            return QuestionResolution.SUPPORTED
        return QuestionResolution.PARTIALLY_ANSWERED
    if ClaimStatus.PARTIALLY_SUPPORTED in statuses:
        return QuestionResolution.PARTIALLY_ANSWERED
    return QuestionResolution.UNANSWERED
```

This is intentionally conservative. `CONTRADICTED` dominates to `CONTESTED`. A mix of `SUPPORTED` with `UNVERIFIED`/`UNRESOLVED`/`PARTIALLY_SUPPORTED` remains `PARTIALLY_ANSWERED` rather than claiming complete support. Future Skeptic/Verifier work may refine semantics later; Query Graph v1 must not invent confidence scoring.

### Dependency Satisfaction Mapping

A dependency is satisfied only when the prerequisite question is `CLOSED`, its closure basis is not stale, and its frozen `closed_resolution` meets the edge policy:

```python
SUPPORTED:
    prerequisite.closed_resolution is QuestionResolution.SUPPORTED

PARTIAL_OR_BETTER:
    prerequisite.closed_resolution in {
        QuestionResolution.PARTIALLY_ANSWERED,
        QuestionResolution.SUPPORTED,
    }

ANY_CLOSED:
    prerequisite.closed_resolution in {
        QuestionResolution.PARTIALLY_ANSWERED,
        QuestionResolution.SUPPORTED,
        QuestionResolution.CONTESTED,
    }
```

`UNANSWERED` never satisfies a dependency because `close_question()` must reject it.

---

### Task 1: Define the Query Graph schema contract with red tests

**Files:**
- Create: `tests/test_query_graph_schema.py`

**Interfaces:**
- Consumes: current `SessionDB`, Evidence Fabric v27 schema on the dependent branch.
- Produces: executable schema contract for six Query Graph tables, indexes, composite FKs, terminal-run guards, append-only events, and the next additive schema version.

- [ ] **Step 1: Write fresh-schema object tests.** Create `SessionDB(tmp_path / "state.db")`; assert these tables exist: `query_graphs`, `research_questions`, `question_dependencies`, `question_claim_links`, `question_closure_claims`, `query_graph_events`. Assert `PRAGMA foreign_keys=1` and schema version equals the chosen next free version discovered during execution preflight.
- [ ] **Step 2: Write required-index tests.** Assert at least:

```text
ux_research_questions_run_fingerprint
ux_question_dependencies_pair
idx_query_graphs_run
idx_research_questions_graph
idx_research_questions_run
idx_question_dependencies_run
idx_question_claim_links_run
idx_query_graph_events_run
```

- [ ] **Step 3: Write a genuine pre-Query-Graph migration test.** Build a database from the current dependent Evidence Fabric `SCHEMA_SQL`, set the actual pre-Query-Graph version, insert a representative `sessions` row plus one `ResearchRun`/claim/evidence row, then remove only Query Graph objects if the test fixture is generated from future `SCHEMA_SQL`. Open through the new `SessionDB`, reopen twice, and assert the old rows are byte/value-equivalent while Query Graph objects appear exactly once.
- [ ] **Step 4: Write direct-SQL same-run FK tests.** Create run A/B, graphs/questions/claims, then prove SQLite rejects a question claiming graph/run mismatch, cross-run `question_claim_links`, and cross-run dependency endpoints.
- [ ] **Step 5: Write direct-SQL uniqueness tests.** Insert normalized-fingerprint duplicate questions in different graphs of the same run and assert `sqlite3.IntegrityError`; insert the same fingerprint in a different run and assert success.
- [ ] **Step 6: Write direct-SQL terminal-run mutation tests.** Terminalize a run via Evidence Fabric, then assert raw SQL insert/update/delete attempts against every Query Graph current-state table fail. Reads must still succeed.
- [ ] **Step 7: Write append-only event tests.** Insert one event through a legal fixture path, then assert raw SQL `UPDATE query_graph_events` and `DELETE FROM query_graph_events` raise `sqlite3.IntegrityError`.
- [ ] **Step 8: Run red tests.**

```powershell
python -m pytest tests/test_query_graph_schema.py -q
```

Expected: failures because Query Graph tables/indexes/triggers do not yet exist.

- [ ] **Step 9: Commit red tests.**

```powershell
git add tests/test_query_graph_schema.py
git commit -m "test: define Query Graph schema contract"
```

### Task 2: Add additive Query Graph schema, indexes, and guards

**Files:**
- Modify: `hermes_state_common.py`
- Modify if required by current migration mechanics: `hermes_state_schema.py`
- Test: `tests/test_query_graph_schema.py`

**Interfaces:**
- Consumes: current `SCHEMA_SQL`, `SCHEMA_VERSION`, Evidence Fabric tables and `ResearchRun` terminal triggers.
- Produces: durable Query Graph tables and DB-level same-run/immutability/append-only constraints.

- [ ] **Step 1: Re-check schema version before editing.** Fetch/inspect upstream `main` and the dependent branch. Record the current numbers in the execution log. Set Query Graph to `max(current dependent version, current upstream version) + 1` only if that is the next free additive version after rebasing; do not assume 28 blindly.
- [ ] **Step 2: Add `query_graphs`.** Use columns equivalent to:

```sql
CREATE TABLE IF NOT EXISTS query_graphs (
    id TEXT PRIMARY KEY,
    research_run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    purpose TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'OPTIONAL'
        CHECK (role IN ('REQUIRED', 'OPTIONAL')),
    workflow_state TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (workflow_state IN ('OPEN', 'CLOSED')),
    created_by_agent TEXT NOT NULL,
    created_by_profile TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    closed_at REAL,
    closed_by_agent TEXT,
    closed_by_profile TEXT,
    UNIQUE (id, research_run_id),
    FOREIGN KEY (research_run_id) REFERENCES research_runs(id)
);
```

- [ ] **Step 3: Add `research_questions`.** Include `graph_id`, `research_run_id`, `question_text`, 64-char `normalized_fingerprint`, `role`, `workflow_state`, `creation_reason`, `blocked_reason`, `closed_resolution`, created/updated/closed provenance, `UNIQUE(id,research_run_id)`, composite FK `(graph_id,research_run_id) -> query_graphs(id,research_run_id)`, and run-wide unique index on `(research_run_id, normalized_fingerprint)`.
- [ ] **Step 4: Add `question_dependencies`.** Store columns `dependent_question_id`, `prerequisite_question_id`, `research_run_id`, `acceptance_policy`, created provenance/timestamp. Primary key or unique index on the pair. Composite same-run FKs for both question endpoints. Add `CHECK (dependent_question_id <> prerequisite_question_id)`.
- [ ] **Step 5: Add `question_claim_links`.** Store question, claim, run, created provenance/timestamp. Composite same-run FK to questions and existing Evidence Fabric claims. Primary key `(question_id, claim_id)`.
- [ ] **Step 6: Add `question_closure_claims`.** Store `question_id`, `claim_id`, `research_run_id`, `claim_status_at_close`, `claim_updated_at_at_close`, and closure generation timestamp if needed for ordering. Composite same-run FKs. Primary key `(question_id, claim_id)` for the current closure basis; revalidation replaces that basis transactionally while old history remains in `query_graph_events`.
- [ ] **Step 7: Add `query_graph_events`.** Use `INTEGER PRIMARY KEY AUTOINCREMENT`, run/graph/question references where applicable, `event_type`, runtime actor/profile, optional reason, bounded JSON payload text, and `created_at`. Add indexes for run and question chronology.
- [ ] **Step 8: Add terminal-run guards for Query Graph tables.** For every insert/update/delete on current-state Query Graph tables, reject when parent `research_runs.status <> 'OPEN'`. Use stable messages `research run is not open` for insert paths and `terminal research run is immutable` for update/delete paths so service translation can classify lifecycle errors.
- [ ] **Step 9: Add append-only event guards.** Reject `UPDATE` and `DELETE` on `query_graph_events` unconditionally with `query graph events are append-only`.
- [ ] **Step 10: Run schema tests green.**

```powershell
python -m pytest tests/test_query_graph_schema.py -q
```

- [ ] **Step 11: Run Evidence Fabric schema regression.**

```powershell
python -m pytest tests/test_evidence_fabric_schema.py -q
```

Expected: PASS with only the schema-version assertion updated where necessary for the dependent branch.

- [ ] **Step 12: Commit.**

```powershell
git add hermes_state_common.py hermes_state_schema.py tests/test_query_graph_schema.py tests/test_evidence_fabric_schema.py
git commit -m "feat: add Query Graph state schema"
```

### Task 3: Add Query Graph domain types, normalization, scope, reads, and audit decoding

**Files:**
- Create: `research/query_graph.py`
- Modify: `research/__init__.py`
- Create: `tests/test_query_graph.py`

**Interfaces:**
- Consumes: `SessionDB`, `EvidenceScope`, Evidence Fabric `ClaimStatus`/run ownership.
- Produces: all enums/dataclasses/errors in Public Contracts, deterministic `normalize_question_text`, `question_fingerprint`, graph/question read APIs, and event decoding.

- [ ] **Step 1: Write red normalization tests.** Assert NFC-equivalent text, case differences, leading/trailing/multiple Unicode whitespace normalize identically; punctuation differences remain distinct. Assert empty/whitespace-only and overlong identifiers/text raise `QueryGraphValidationError`.
- [ ] **Step 2: Write red DTO/read/scope tests.** Seed minimal legal rows with raw SQL or temporary helper fixtures, then assert `get_graph`, `list_graphs`, `get_question`, `list_questions`, and `list_events` return typed immutable DTOs and cross-scope access raises `QueryGraphScopeError` or returns an empty scoped listing as appropriate.
- [ ] **Step 3: Implement normalization exactly.**

```python
def normalize_question_text(text: str) -> str:
    if not isinstance(text, str):
        raise QueryGraphValidationError("question text is required")
    normalized = unicodedata.normalize("NFC", text)
    normalized = " ".join(normalized.split()).strip().casefold()
    if not normalized:
        raise QueryGraphValidationError("question text is required")
    return normalized


def question_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_question_text(text).encode("utf-8")).hexdigest()
```

Do not strip punctuation or rewrite semantics.

- [ ] **Step 4: Implement service scope helpers.** Mirror Evidence Fabric style: use the injected `EvidenceScope`, resolve run ownership through `research_runs.owner_scope_key`, and translate missing/foreign-scope records to Query Graph domain exceptions.
- [ ] **Step 5: Implement DTO decoding and read APIs.** Keep all datetime conversion UTC-aware and JSON payload decoding bounded/defensive.
- [ ] **Step 6: Export public names.** Extend `research/__init__.py` with Query Graph exports without breaking Evidence Fabric wildcard behavior.
- [ ] **Step 7: Run focused tests.**

```powershell
python -m pytest tests/test_query_graph.py -q
```

- [ ] **Step 8: Commit.**

```powershell
git add research/query_graph.py research/__init__.py tests/test_query_graph.py
git commit -m "feat: add Query Graph domain primitives"
```

### Task 4: Implement graph creation, role mutation, and atomic audit events

**Files:**
- Modify: `research/query_graph.py`
- Test: `tests/test_query_graph.py`

**Interfaces:**
- Produces: `create_graph`, `set_graph_role`, event helper used by later tasks.

- [ ] **Step 1: Write red graph-creation tests.** Cover valid creation, default `OPTIONAL`, explicit `REQUIRED` from trusted service caller, required name/purpose, runtime-owned actor/profile, same scope, terminal-run rejection, and creation event atomicity.
- [ ] **Step 2: Write red role-change tests.** Require nonempty `reason`, emit `GRAPH_ROLE_CHANGED`, reject no-op role changes, reject terminal-run mutation, and preserve runtime actor/provenance.
- [ ] **Step 3: Add one internal transactional event helper.** It must accept the already-open SQLite cursor from the mutation transaction; never perform a second independent commit. Example internal shape:

```python
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
) -> None:
    ...
```

- [ ] **Step 4: Implement `create_graph` in one `_execute_write` callback.** Revalidate run scope and `OPEN` status inside the write transaction, insert graph, append `GRAPH_CREATED`, and return typed DTO after commit.
- [ ] **Step 5: Implement `set_graph_role`.** Reject graph `CLOSED` role mutation if the design/service lifecycle requires frozen closed structures; otherwise permit only while run is open and always append audit event. Preserve dependency semantics: role demotion does not remove or bypass incoming dependency obligations.
- [ ] **Step 6: Run focused tests green.**

```powershell
python -m pytest tests/test_query_graph.py -q
```

- [ ] **Step 7: Commit.**

```powershell
git add research/query_graph.py tests/test_query_graph.py
git commit -m "feat: add Query Graph graph lifecycle"
```

### Task 5: Implement canonical question proposal, refinement, workflow states, and role mutation

**Files:**
- Modify: `research/query_graph.py`
- Test: `tests/test_query_graph.py`
- Extend later concurrency coverage in: `tests/test_query_graph_concurrency.py`

**Interfaces:**
- Produces: `propose_question`, `refine_question`, `set_question_role`, `start_question`, `block_question`, `unblock_question`, deterministic duplicate result.

- [ ] **Step 1: Write red proposal/dedup tests.** Propose equivalent normalized text in two different graphs of the same run; assert only one canonical question exists and the second result has `created=False` with the first question ID. Assert the same normalized text in another run creates a distinct question.
- [ ] **Step 2: Write red default-role/reason tests.** Worker-style proposal defaults to `OPTIONAL`; explicit trusted caller may request `REQUIRED`; `QUESTION_CREATED` records creation reason and runtime actor.
- [ ] **Step 3: Write red refinement tests.** Active question retains stable ID, new normalized fingerprint is stored, dependencies/claim links remain, event payload records old/new text and reason, duplicate-target refinement raises `QueryGraphIntegrityError`, and closed-question refinement raises `QueryGraphLifecycleError` until reopened.
- [ ] **Step 4: Write red workflow-state tests.** Exact allowed transitions:

```text
OPEN -> IN_PROGRESS
OPEN -> BLOCKED
IN_PROGRESS -> BLOCKED
BLOCKED -> IN_PROGRESS
```

Reject `OPEN -> OPEN`, `IN_PROGRESS -> IN_PROGRESS`, `BLOCKED -> BLOCKED`, direct `CLOSED` writes, start on `CLOSED`, and unblock a non-blocked question. `block_question` requires a nonempty reason.

- [ ] **Step 5: Write red question-role tests.** Require reason, audit every change, reject no-op role changes, and prove demoting a prerequisite does not make a dependency disappear.
- [ ] **Step 6: Implement `propose_question` with DB-unique authority.** Compute fingerprint service-side. Attempt insert in one write transaction; on uniqueness conflict, re-read `(research_run_id, normalized_fingerprint)` under scope and return `QuestionWriteResult(created=False)`. Do not perform a race-prone service-only pre-check as authority.
- [ ] **Step 7: Implement optional dependencies argument atomically.** `propose_question(... dependencies=...)` must insert question + all validated edges + all events in one transaction. If any dependency is invalid or cyclic, roll back question creation and every event.
- [ ] **Step 8: Implement refinement and workflow/role mutations.** Every mutation checks run `OPEN`, current lifecycle, required reason where specified, and appends its matching audit event in the same transaction.
- [ ] **Step 9: Run focused tests.**

```powershell
python -m pytest tests/test_query_graph.py -q
```

- [ ] **Step 10: Commit.**

```powershell
git add research/query_graph.py tests/test_query_graph.py
git commit -m "feat: add dynamic research question lifecycle"
```

### Task 6: Implement run-wide cross-graph DAG dependencies and acceptance policies

**Files:**
- Modify: `research/query_graph.py`
- Create: `tests/test_query_graph_dependencies.py`

**Interfaces:**
- Produces: `add_dependency`, `remove_dependency`, `list_dependencies`, run-wide recursive cycle check, edge-specific closure acceptance helper.

- [ ] **Step 1: Write red valid-topology tests.** Cover same-graph chain, cross-graph dependency, diamond/shared prerequisite, multiple parents, and removal.
- [ ] **Step 2: Write red invalid-topology tests.** Assert `QueryGraphCycleError` for self-edge, two-node cycle, long three-graph cycle, and a cycle introduced only through cross-graph edges. Assert `QueryGraphIntegrityError`/`QueryGraphDependencyError` for foreign-run endpoints and duplicate edge.
- [ ] **Step 3: Write red acceptance-policy tests.** Create closed prerequisite fixtures with frozen resolutions and verify exact policy mapping from the Public Contracts section. A stale prerequisite must fail every policy, including `ANY_CLOSED`.
- [ ] **Step 4: Write red parallel-investigation test.** Start or block the dependent question before its prerequisite is closed; assert allowed. Attempt `close_question` later with unsatisfied prerequisite; assert closure failure. This proves dependencies gate closure, not work start.
- [ ] **Step 5: Implement cycle validation inside the same write transaction as edge insert.** Use a recursive CTE over `question_dependencies` scoped by `research_run_id`. Before adding `dependent -> prerequisite`, reject if `dependent == prerequisite` or if the proposed prerequisite can already reach the dependent through prerequisite chains.
- [ ] **Step 6: Do not add a process-global DAG cache.** The persistent DB is authority; multi-process correctness comes from `BEGIN IMMEDIATE` serialization already provided by `SessionDB._execute_write`.
- [ ] **Step 7: Implement duplicate-edge handling as stable domain behavior.** Do not leak raw SQLite errors.
- [ ] **Step 8: Run dependency tests.**

```powershell
python -m pytest tests/test_query_graph_dependencies.py -q
```

- [ ] **Step 9: Commit.**

```powershell
git add research/query_graph.py tests/test_query_graph_dependencies.py
git commit -m "feat: add run-wide Query Graph dependencies"
```

### Task 7: Link Evidence Fabric claims and derive deterministic live question resolution

**Files:**
- Modify: `research/query_graph.py`
- Extend: `tests/test_query_graph.py`

**Interfaces:**
- Consumes: real Evidence Fabric `claims` rows and `ClaimStatus`.
- Produces: `link_claim`, `unlink_claim`, `derive_resolution`, same-run claim integrity, live resolution semantics.

- [ ] **Step 1: Write red claim-link tests using real `EvidenceFabricService`.** Create run/claims through Evidence Fabric, graph/question through Query Graph, link multiple claims, and assert typed `QuestionClaimLink` provenance.
- [ ] **Step 2: Write red cross-run/scope tests.** Foreign-run claim link must raise `QueryGraphIntegrityError`/`QueryGraphScopeError`; nonexistent claim/question uses `QueryGraphNotFoundError`; terminal run rejects link/unlink with `QueryGraphLifecycleError`.
- [ ] **Step 3: Write the resolution truth-table tests.** Assert exactly:

```text
{} -> UNANSWERED
{UNVERIFIED} -> UNANSWERED
{UNRESOLVED} -> UNANSWERED
{PARTIALLY_SUPPORTED} -> PARTIALLY_ANSWERED
{SUPPORTED} -> SUPPORTED
{SUPPORTED, PARTIALLY_SUPPORTED} -> PARTIALLY_ANSWERED
{SUPPORTED, UNVERIFIED} -> PARTIALLY_ANSWERED
{SUPPORTED, UNRESOLVED} -> PARTIALLY_ANSWERED
{CONTRADICTED} -> CONTESTED
{SUPPORTED, CONTRADICTED} -> CONTESTED
{PARTIALLY_SUPPORTED, CONTRADICTED} -> CONTESTED
```

- [ ] **Step 4: Implement claim link/unlink in one transaction each.** Revalidate question/run scope and claim same-run identity inside the write transaction; append `CLAIM_LINKED`/`CLAIM_UNLINKED` atomically.
- [ ] **Step 5: Implement `derive_resolution` as a read-only computation.** Fetch linked current claim statuses/IDs under the same run/scope, apply the exact deterministic mapping, and if the question is closed also calculate `stale` against closure snapshots without mutating state.
- [ ] **Step 6: Run focused tests.**

```powershell
python -m pytest tests/test_query_graph.py -q
```

- [ ] **Step 7: Commit.**

```powershell
git add research/query_graph.py tests/test_query_graph.py
git commit -m "feat: bind Query Graph questions to Evidence Fabric claims"
```

### Task 8: Implement evidence-gated question closure and immutable closure snapshots

**Files:**
- Modify: `research/query_graph.py`
- Extend: `tests/test_query_graph.py`
- Extend: `tests/test_query_graph_dependencies.py`

**Interfaces:**
- Produces: `close_question`, closure-basis snapshot table writes, dependency closure gates.

- [ ] **Step 1: Write red closure-gate tests.** Closure must fail when resolution is `UNANSWERED`, when any dependency policy is unsatisfied, or when the run is terminal. Closure may succeed from `OPEN`, `IN_PROGRESS`, or `BLOCKED` only if evidence/dependency gates pass; if design prefers requiring active work first, encode and test that exact choice before implementation. For this plan use the approved broader rule: any active state may close if deterministic gates pass.
- [ ] **Step 2: Write red contested-closure tests.** A question resolving `CONTESTED` may close; `closed_resolution` freezes `CONTESTED`. It may satisfy only `ANY_CLOSED`, never `SUPPORTED` or `PARTIAL_OR_BETTER`.
- [ ] **Step 3: Write red closure-basis tests.** On close, `question_closure_claims` contains every currently linked claim with exact status and `claims.updated_at`. The question stores frozen `closed_resolution`, closed timestamps, and runtime actor/profile.
- [ ] **Step 4: Write red immutability tests.** After closure, adding/removing claim links, changing role/text/workflow, or closing again must fail until explicit reopen/revalidation rules permit an operation. Evidence Fabric claim status itself may still change while the run is open.
- [ ] **Step 5: Implement `close_question` as one serialized write transaction.** Inside it: load fresh run/question, verify active lifecycle/open run, fetch linked claim statuses, derive current resolution, reject `UNANSWERED`, validate every dependency with current prerequisite stale check, replace current `question_closure_claims` basis, update question closure fields, append `QUESTION_CLOSED`, commit.
- [ ] **Step 6: Translate lifecycle/DB errors exactly.** Terminal mutation -> `QueryGraphLifecycleError`; unsatisfied dependency -> `QueryGraphDependencyError`; missing basis -> `QueryGraphLifecycleError` or validation error consistently as tests specify; no raw SQLite errors.
- [ ] **Step 7: Run closure/dependency tests.**

```powershell
python -m pytest tests/test_query_graph.py tests/test_query_graph_dependencies.py -q
```

- [ ] **Step 8: Commit.**

```powershell
git add research/query_graph.py tests/test_query_graph.py tests/test_query_graph_dependencies.py
git commit -m "feat: add evidence-gated question closure"
```

### Task 9: Implement deterministic staleness, explicit revalidation, and explicit reopening

**Files:**
- Modify: `research/query_graph.py`
- Extend: `tests/test_query_graph.py`

**Interfaces:**
- Produces: `resolution_stale` calculation through `derive_resolution`, `revalidate_question`, `reopen_question`.

- [ ] **Step 1: Write red staleness tests.** Close a supported question, then mutate one linked claim status or `updated_at` via Evidence Fabric. Assert question remains `CLOSED + SUPPORTED`, while `derive_resolution(...).stale is True`.
- [ ] **Step 2: Write red no-false-staleness tests.** Unrelated claim updates, new claims not linked to the question, and unrelated graph/question changes must not mark the closure stale.
- [ ] **Step 3: Write red revalidation tests.** If the current live derived resolution still equals the frozen `closed_resolution`, `revalidate_question()` refreshes `question_closure_claims`, clears staleness, records `QUESTION_REVALIDATED`, and leaves workflow `CLOSED`. If the live resolution differs, raise `QueryGraphStaleResolutionError` and leave old closure untouched.
- [ ] **Step 4: Write red reopen tests.** `reopen_question(... NEW_EVIDENCE)` and `... CONTRADICTION_DISCOVERED` are allowed only for a `CLOSED` question in an open run when the current basis is materially stale/different. Reopen moves workflow to `IN_PROGRESS`, clears current closure fields/current closure basis table, preserves historical event record, and appends `QUESTION_REOPENED` with reason. Invalid/no-material-change reopen raises `QueryGraphLifecycleError`.
- [ ] **Step 5: Implement staleness by exact basis comparison.** Compare current linked claim ID set, each current `ClaimStatus`, and each current `claims.updated_at` against `question_closure_claims`. Any add/remove/status/version-marker change makes the basis stale.
- [ ] **Step 6: Implement revalidation/reopen atomically.** Do not mutate Evidence Fabric. Do not rewrite old audit events.
- [ ] **Step 7: Run focused tests.**

```powershell
python -m pytest tests/test_query_graph.py -q
```

- [ ] **Step 8: Commit.**

```powershell
git add research/query_graph.py tests/test_query_graph.py
git commit -m "feat: add Query Graph staleness and reopening"
```

### Task 10: Implement graph closure and deterministic graph/run readiness

**Files:**
- Modify: `research/query_graph.py`
- Extend: `tests/test_query_graph.py`
- Extend: `tests/test_query_graph_dependencies.py`

**Interfaces:**
- Produces: `assess_graph_completion`, `close_graph`, `assess_run_completion`.

- [ ] **Step 1: Write graph-completion matrix tests.** For an open graph:

```text
required Q closed/fresh + optional Q open -> ready
required Q open -> not ready
required Q blocked -> not ready
required Q closed/stale -> not ready
required Q closed/CONTESTED -> ready if no dependency rejects it
```

- [ ] **Step 2: Write optional-graph tests.** Required Graph A closed/ready plus Optional Graph B open must allow run `READY` unless a required path in A structurally depends on a question in B that has not satisfied its edge.
- [ ] **Step 3: Write cross-graph structural-path tests.** An optional question/graph that is prerequisite of a required downstream question effectively blocks that downstream closure/readiness until the edge is satisfied. Role labels never erase an explicit dependency.
- [ ] **Step 4: Write graph-close tests.** `close_graph()` succeeds only when `assess_graph_completion().ready`; stores closed timestamps/runtime actor and appends `GRAPH_CLOSED`. Closing twice or closing a terminal-run graph raises `QueryGraphLifecycleError`.
- [ ] **Step 5: Write run-readiness reason tests.** Assert `CompletionAssessment.reasons` contains deterministic stable strings such as:

```text
required graph <id> is open
required question <id> is OPEN
required question <id> is BLOCKED
required question <id> has stale closure basis
question <id> prerequisite <id> does not satisfy SUPPORTED
```

Do not include model-generated prose.

- [ ] **Step 6: Implement assessment methods read-only.** They must never mutate graph/question/run state and never call a model.
- [ ] **Step 7: Implement `close_graph` using fresh assessment inside the write transaction.** Recheck readiness after acquiring write serialization so a concurrent mutation cannot invalidate the close between pre-check and commit.
- [ ] **Step 8: Run focused tests.**

```powershell
python -m pytest tests/test_query_graph.py tests/test_query_graph_dependencies.py -q
```

- [ ] **Step 9: Commit.**

```powershell
git add research/query_graph.py tests/test_query_graph.py tests/test_query_graph_dependencies.py
git commit -m "feat: add Query Graph completion assessment"
```

### Task 11: Prove concurrency, cycle-race safety, event atomicity, and restart durability

**Files:**
- Create: `tests/test_query_graph_concurrency.py`
- Modify only if a red concurrency test proves production changes are needed: `research/query_graph.py`

**Interfaces:**
- Produces: multi-connection proof for canonical question identity, run-wide DAG integrity, atomic events, and persistence across restart.

- [ ] **Step 1: Write identical-question race test.** Use two threads, a barrier, and two separate `SessionDB` instances against one temporary DB. Both call `propose_question` with normalized-equivalent text in different graphs of the same run. Assert one durable question, same ID returned to both, exactly one `created=True`, one `created=False`, no raw SQLite exception, and exactly one `QUESTION_CREATED` event.
- [ ] **Step 2: Write distinct-question concurrency test.** Two writers add different questions concurrently; both survive with unique IDs/events.
- [ ] **Step 3: Write cycle-race test.** Seed A/B/C and safe edges so two concurrent proposed edges would jointly create a cycle if both passed stale checks. Assert final persisted topology remains acyclic and at least one writer receives `QueryGraphCycleError`/stable domain rejection. Verify via recursive SQL after both threads finish.
- [ ] **Step 4: Write event-rollback test.** Force a mutation to fail after entering its transaction but before commit, using a controlled invalid dependency/claim constraint. Assert neither state row nor event persists.
- [ ] **Step 5: Write restart durability scenario.** Persist two graphs, cross-graph dependency, roles, claim links, one closed snapshot, one blocked question, one reopen/revalidation event; close all `SessionDB` objects, reopen, and assert DTO state, event order, stale calculation, and readiness are identical.
- [ ] **Step 6: Implement only minimal race handling needed by red tests.** Reuse `SessionDB._execute_write`; do not introduce a second global lock, in-memory DAG authority, or retry loop outside existing database discipline.
- [ ] **Step 7: Run concurrency suite.**

```powershell
python -m pytest tests/test_query_graph_concurrency.py -q
```

- [ ] **Step 8: Commit.**

```powershell
git add research/query_graph.py tests/test_query_graph_concurrency.py
git commit -m "test: prove Query Graph concurrency and durability"
```

### Task 12: Canonical end-to-end research scenario

**Files:**
- Create: `tests/test_query_graph_end_to_end.py`

**Interfaces:**
- Consumes: real `EvidenceFabricService` and `QueryGraphService` only.
- Produces: one human-readable acceptance test proving Hermes can reconstruct structural research state without an LLM.

- [ ] **Step 1: Create one open ResearchRun.** Objective:

```text
Determine whether Policy X is legally valid, technically feasible, and practically effective.
```

- [ ] **Step 2: Create graphs.** Legal `REQUIRED`, Technical `REQUIRED`, Context `OPTIONAL`.
- [ ] **Step 3: Create initial questions.** Legal Q1/Q2 required; Technical Q3/Q4 required; Context Q5 optional. Add a cross-graph dependency from Legal Q2 to Technical Q4.
- [ ] **Step 4: Dynamically discover Q6.** Add Technical Q6 as required with reason `CONTRADICTION`; add Legal Q2 dependency on Q6.
- [ ] **Step 5: Create real Evidence Fabric claims.** Include supported, partially supported, and contradicted claims; link them to the appropriate questions. Do not bypass Evidence Fabric with raw Query Graph-owned claim status.
- [ ] **Step 6: Close questions according to deterministic gates.** Leave one contested result legitimately closed, one optional context question open, and verify dependency policies.
- [ ] **Step 7: Make one closed required question stale.** Change a linked Evidence Fabric claim so Query Graph reports stale and run `NOT_READY` while preserving old frozen closure.
- [ ] **Step 8: Reopen or revalidate explicitly.** Use the material reason, add/link the new claim basis if needed, close again, and verify the event history reconstructs why.
- [ ] **Step 9: Close required graphs.** Leave Context graph optional/open. Assert `assess_run_completion().ready is True` once every required structural path is satisfied.
- [ ] **Step 10: Assert reconstructability without an LLM.** From service reads/events alone verify:

```text
what questions were asked
why they were created
which graphs/questions were required
what depends on what
which Evidence Fabric claims answer each question
which question is/was contested
which closed answer became stale
why it was reopened or revalidated
what remains unresolved/optional
whether each required graph may close
whether the run is READY
```

- [ ] **Step 11: Run acceptance test.**

```powershell
python -m pytest tests/test_query_graph_end_to_end.py -q
```

- [ ] **Step 12: Commit.**

```powershell
git add tests/test_query_graph_end_to_end.py
git commit -m "test: add Query Graph end-to-end research contract"
```

### Task 13: Model-tool go/no-go remains deferred unless trusted runtime authority is proven

**Files:**
- Normally no production file changes.
- Optional test/report only if implementation discovers a new trusted runtime-authority API.

**Interfaces:**
- Produces: explicit `MODEL_TOOL_DEFERRED` result unless current Hermes independently provides trusted `ResearchRun` + scope + actor context to model-facing handlers.

- [ ] **Step 1: Inspect current trusted tool-dispatch context without changing Query Graph.** Determine whether a tool handler can obtain authorized `run_id`, `scope_key`, profile/connection, and actor without accepting them from model arguments.
- [ ] **Step 2: If any authority component is model-controlled or absent, retain service-only API.** Record `MODEL_TOOL_DEFERRED` in the execution report; create no `tools/query_graph_tool.py` and do not modify `toolsets.py`.
- [ ] **Step 3: If a genuinely trusted runtime path now exists, stop before implementation.** Update the design/spec and this plan with the exact runtime contract, add authorization tests, and obtain user approval before adding a model-facing tool. Do not opportunistically expand v1 during execution.

### Task 14: Regression, adversarial self-check, and final verification

**Files:**
- Existing changed files only. Add production fixes only after a concrete red regression test.

**Interfaces:**
- Produces: fresh evidence for the full Query Graph acceptance bar without reopening already-passed unrelated surfaces.

- [ ] **Step 1: Run all Query Graph suites.**

```powershell
python -m pytest tests/test_query_graph_schema.py tests/test_query_graph.py tests/test_query_graph_dependencies.py tests/test_query_graph_concurrency.py tests/test_query_graph_end_to_end.py -q
```

- [ ] **Step 2: Run Evidence Fabric regressions.**

```powershell
python -m pytest tests/test_evidence_fabric_schema.py tests/test_evidence_fabric.py tests/test_evidence_fabric_concurrency.py -q
```

If `tests/test_evidence_fabric_concurrency.py` is absent on the final dependency branch, run the actual existing Evidence Fabric test set and record the exact files; do not invent a passing command.

- [ ] **Step 3: Run relevant SessionDB/state regressions.**

```powershell
python -m pytest tests/test_hermes_state.py tests/test_hermes_state_wal_fallback.py tests/test_session_db_context_manager.py tests/test_session_db_read_conn_pool.py -q
```

- [ ] **Step 4: Run compile verification.**

```powershell
python -m compileall hermes_state_common.py hermes_state_schema.py research
```

- [ ] **Step 5: Run diff integrity.**

```powershell
git diff --check
git status --short --branch
```

- [ ] **Step 6: Mechanically search changed code for prohibited drift.** Search for model-supplied `scope_key`, database-path parameters, direct model SQL, process-global Query Graph dictionaries/caches, embeddings/fuzzy dedup, confidence/trust scores, scheduler/model-routing code, hard-delete APIs, automatic reopen, automatic run terminalization, copied Evidence Fabric claim/evidence text, and UI/Knowledge Hub/Obsidian/NotebookLM/browser changes. Review every match manually.
- [ ] **Step 7: Verify event/state atomicity and terminal guards adversarially.** Confirm every public mutation uses one serialized write callback and appends the event on the same cursor before commit. Confirm every expected terminal-run mutation raises `QueryGraphLifecycleError`, not raw SQLite.
- [ ] **Step 8: Verify dependency SQL direction.** Mechanically inspect names/queries/tests so `dependent_question_id` always means the question that waits for `prerequisite_question_id`; reject any reversed CTE or misleading variable names before completion.
- [ ] **Step 9: Verify no completion gaming.** Confirm demoting graph/question roles never removes explicit dependency obligations, stale required paths block readiness, `ANY_CLOSED` still rejects stale prerequisites, and optional graphs only stop blocking when no required dependency path points through them.
- [ ] **Step 10: Run the repository test runner if the environment supports it.** Use the existing bounded `scripts/run_tests.sh` with Query Graph files first. Run the full suite only if the environment permits it; classify baseline/environment failures separately with reproducible evidence rather than altering Query Graph to mask them.
- [ ] **Step 11: Final report requirements.** Report workspace/worktree, branch, initial/final HEAD, actual schema version selected, changed files, exact test commands/results, migration/dedup/DAG/concurrency/claim-integrity/closure/staleness/readiness outcomes, regression results, adversarial search findings, `MODEL_TOOL_DEFERRED` status, and any baseline-only environment blockers.
- [ ] **Step 12: Completion token.** Use `QUERY_GRAPH_V1_READY` only after fresh passing evidence demonstrates every Acceptance Bar item below. Otherwise report the exact blocker and do not claim completion.

## Acceptance Bar

Fresh verification must demonstrate all of the following before `QUERY_GRAPH_V1_READY`:

- additive schema migration from a genuine pre-Query-Graph database with existing Evidence Fabric rows preserved;
- one run-wide DAG spanning multiple graphs;
- self/cross-run/cycle rejection including concurrent cycle-race safety;
- exact normalized run-wide question deduplication including concurrent writers;
- one canonical question ID returned to duplicate proposals without raw SQLite leakage;
- same-run Evidence Fabric claim-link integrity;
- deterministic active resolution from claim statuses using the specified truth table;
- evidence-gated closure with dependency-specific acceptance policies;
- immutable frozen closure resolution and normalized claim-basis snapshot;
- deterministic staleness from exact linked claim ID/status/update-marker comparison;
- explicit successful revalidation only when the same closed result still holds;
- explicit validated reopening on material evidence/contradiction change;
- active in-place question refinement with immutable audit history and closed-question refinement rejection;
- required/optional graph and question semantics without dependency bypass;
- graph closure and advisory run readiness with deterministic reason strings;
- state/event atomicity and append-only event history;
- terminal Evidence Fabric run immutability across every Query Graph mutation path;
- restart durability across graphs, questions, roles, dependencies, claim links, closure basis, staleness, and events;
- no model call required to reconstruct structure, resolution state, staleness, or readiness;
- no model-facing Query Graph tool unless a separately approved trusted-runtime authority design exists;
- no changes to Adaptive Swarm Director, Skeptic/Verifier, reliability scoring, cost governance, Knowledge Hub, Desktop/HUD, browser isolation, Obsidian/NotebookLM, or model routing.

## Plan Self-Review

- [x] **Spec coverage:** Architecture, multiple graphs per run, cross-graph DAG, exact dedup, roles, worker proposals, dependency policies, Evidence Fabric claim links, deterministic resolution, closure snapshots, staleness, revalidation/reopen, refinement, event history, terminal immutability, graph/run readiness, concurrency, persistence, service boundary, model-tool deferral, and end-to-end acceptance all map to explicit tasks.
- [x] **Placeholder scan:** No `TBD`, `TODO`, “implement later,” vague “handle edge cases,” or undefined future task references remain. The only conditional path is the explicitly approved trusted-runtime model-tool gate, whose no-go behavior is fully specified.
- [x] **Type consistency:** Public enum/dataclass/service names are defined once and used consistently across tasks. Dependency direction is named `dependent_question_id` -> `prerequisite_question_id` throughout.
- [x] **Scope check:** Query Graph remains one testable subsystem above Evidence Fabric. Adaptive Swarm Director and verification/reliability layers remain separate future projects.
- [x] **Migration precision:** The plan does not blindly hard-code v28; execution must re-check the next free additive schema version before editing.
- [x] **Concurrency precision:** Exact-question unique constraint and DAG cycle check are database-authoritative under the existing serialized write discipline; no process-global authority is introduced.
- [x] **Lifecycle precision:** Closed questions freeze historical state, staleness is derived, reopen is explicit, terminal Evidence Fabric runs remain immutable, and Query Graph run readiness is advisory only.
