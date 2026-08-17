# Query Graph v1 Design

## Status

Approved design for a research-specific Query Graph layer above Evidence Fabric v1. This design intentionally stops short of implementation. It defines the durable research-question graph, evidence-gated completion semantics, lifecycle, concurrency, audit history, and service boundary required before Adaptive Swarm Director work begins.

## Context

Evidence Fabric v1 makes Hermes able to persist what was discovered: research runs, evidence, claims, provenance, claim-evidence links, contradiction state, and terminal-run immutability. Query Graph v1 adds the next missing layer: a durable map of what Hermes is trying to answer, what depends on what, what remains unresolved, and whether a research objective is structurally ready to complete.

The architectural order is:

```text
Evidence Fabric
"What do we know, and what supports it?"

        ↓

Query Graph
"What are we trying to answer, what depends on what,
and what remains unresolved?"

        ↓ later

Adaptive Swarm Director
"What should we do next, with which agent/model/tool?"
```

Query Graph v1 is research-specific, but its primitives should remain neutral enough to generalize later.

## Goals

Query Graph v1 must:

- persist one or more research graphs inside a single Evidence Fabric `ResearchRun`;
- represent only research questions as graph nodes;
- support dynamic discovery of new questions and new optional graphs;
- support cross-graph dependencies while enforcing one run-wide DAG;
- bind question completion to same-run Evidence Fabric claims;
- derive question resolution deterministically from Evidence Fabric claim state;
- preserve immutable closure snapshots and detect later staleness;
- distinguish required from optional graphs and questions;
- provide explicit, deterministic graph and run readiness assessment;
- preserve append-only mutation history without event-sourcing current state;
- remain safe under concurrent worker proposals;
- reject mutation on terminal Evidence Fabric runs;
- expose a runtime/service API without requiring a model-facing tool.

## Non-goals

Query Graph v1 does not:

- launch or schedule agents;
- choose models or providers;
- perform searches;
- score source reliability;
- verify whether claims are factually correct;
- calculate confidence or probabilistic reliability scores;
- implement semantic/fuzzy question deduplication;
- provide a UI;
- replace Evidence Fabric;
- mutate Evidence Fabric claim status;
- add Obsidian, NotebookLM, Knowledge Hub, browser-isolation, Desktop, HUD, or routing integrations;
- create a model-facing `query_graph` tool unless trusted runtime authority becomes available independently.

## Architecture

Each `ResearchRun` may contain multiple `QueryGraph` instances. A graph groups related research questions such as Legal, Technical, Market, Historical, or another discovered research domain.

Each `ResearchQuestion` belongs to exactly one owning graph. Dependencies may cross graph boundaries, but all dependency edges stay inside one `ResearchRun`. Therefore DAG validity is enforced globally across all questions in the run rather than separately per graph.

Workers may propose new questions and new graphs, but the Query Graph service is the only authority that commits structural mutation. Worker-proposed graphs default to `OPTIONAL`. Promotion to `REQUIRED` is a separate explicit audited operation.

Questions are question-only objects. Tasks, searches, hypotheses, conclusions, evidence, and claims are not represented as node types. Claims and evidence remain authoritative in Evidence Fabric.

## Domain Model

### QueryGraph

A graph stores at least:

- `id`;
- `research_run_id`;
- `name`;
- `purpose`;
- `role`: `REQUIRED | OPTIONAL`;
- `workflow_state`: `OPEN | CLOSED`;
- runtime-owned provenance and timestamps.

### ResearchQuestion

A question stores at least:

- `id`;
- `graph_id`;
- `research_run_id`;
- `question_text`;
- deterministic normalized fingerprint;
- `role`: `REQUIRED | OPTIONAL`;
- `workflow_state`: `OPEN | IN_PROGRESS | BLOCKED | CLOSED`;
- creation reason such as `ROOT`, `EVIDENCE_GAP`, `CONTRADICTION`, `DEPENDENCY`, `SCOPE_REFINEMENT`, or `OTHER`;
- immutable closure fields when closed;
- runtime-owned provenance and timestamps.

Question text may be refined in place only while the question is active. The service records the old text, new text, reason, and runtime provenance in the append-only event history. A closed question must be explicitly reopened before refinement.

### QuestionDependency

A dependency is a single semantic edge type:

```text
upstream_question DEPENDS_ON downstream prerequisite
```

The implementation should use one consistent stored direction and expose unambiguous names. Each dependency carries an acceptance policy defining what resolution of the prerequisite is sufficient for closure of the dependent question.

Supported v1 acceptance policies are:

- `SUPPORTED`;
- `PARTIAL_OR_BETTER`;
- `ANY_CLOSED`.

The default is conservative: `SUPPORTED`.

Dependencies block closure, not investigation. Hermes may research a dependent question before its prerequisites close, but the dependent question cannot close until each prerequisite satisfies the edge-specific policy.

### QuestionClaimLink

A question may link to multiple Evidence Fabric claims. A claim may support multiple questions. Links must remain inside the same `ResearchRun`.

Query Graph stores claim references, not duplicate claim or evidence content.

### Question Closure Basis

Closure basis should be normalized into a durable table rather than opaque JSON. For each claim used at closure, store at least:

- `question_id`;
- `claim_id`;
- claim status at close;
- claim update/version marker at close.

This allows deterministic staleness detection after closure.

### QueryGraphEvent

Current-state tables are authoritative for operational reads. An append-only event table records mutation history.

Events include at least:

- `GRAPH_CREATED`;
- `GRAPH_ROLE_CHANGED`;
- `GRAPH_CLOSED`;
- `QUESTION_CREATED`;
- `QUESTION_REFINED`;
- `QUESTION_ROLE_CHANGED`;
- `QUESTION_STARTED`;
- `QUESTION_BLOCKED`;
- `QUESTION_UNBLOCKED`;
- `QUESTION_CLOSED`;
- `QUESTION_REOPENED`;
- `QUESTION_REVALIDATED`;
- `DEPENDENCY_ADDED`;
- `DEPENDENCY_REMOVED`;
- `CLAIM_LINKED`;
- `CLAIM_UNLINKED`.

Each event records runtime-owned actor identity, timestamp, affected IDs, and a bounded reason payload. Model-provided reason text remains untrusted data.

## Question Identity and Deduplication

Question deduplication is deterministic and run-wide.

Normalization should use a stable pipeline such as Unicode normalization, whitespace collapse, trimming, and case-folding, then hash the normalized form. Punctuation and wording are not semantically rewritten.

A uniqueness constraint over `(research_run_id, normalized_question_fingerprint)` is authoritative.

If concurrent workers propose equivalent normalized text, exactly one canonical question is created. Other writers receive the same durable question identity with `created=False` rather than a raw SQLite integrity error.

Semantically similar but differently worded questions remain distinct in v1. No embedding or fuzzy auto-merge is allowed.

## Resolution Semantics

Question resolution is not freely assigned by a model.

For active questions, Query Graph derives a live resolution deterministically from linked Evidence Fabric claim states. The exact implementation mapping must preserve these semantics:

- no useful linked claim basis -> `UNANSWERED`;
- useful but incomplete/partial basis -> `PARTIALLY_ANSWERED`;
- supported answer without material contradictory claim state -> `SUPPORTED`;
- material supported contradiction remains -> `CONTESTED`.

Query Graph consumes Evidence Fabric claim state; it does not independently determine whether evidence supports or contradicts a claim.

A question cannot close while its derived resolution is `UNANSWERED`.

## Closure Snapshots and Staleness

Active questions use live-derived resolution. Closed questions freeze an immutable closure snapshot containing:

- `closed_resolution`;
- `closed_at`;
- `closed_by`;
- exact linked claim basis and claim status/version markers used at close.

Later Evidence Fabric changes do not silently rewrite the historical closed resolution.

Instead, Query Graph compares the current linked claim state with the closure basis and derives `resolution_stale = true` when the basis has materially changed.

A stale required question blocks graph/run readiness until it is explicitly revalidated or reopened.

Optional stale questions remain visible warnings but do not automatically block completion unless they are structurally required by a dependency from a required path.

### Revalidation

`revalidate_question()` is permitted when the current evidentiary state still justifies the same closed result. It writes a fresh audited closure basis without pretending the prior snapshot never existed.

### Reopening

Reopening is explicit and validated. Accepted reasons include material `NEW_EVIDENCE` and `CONTRADICTION_DISCOVERED`.

A reopen operation records provenance and moves the question back to an active workflow state. Closed question text can only be refined after such an explicit reopen.

No automatic reopen occurs.

## Workflow States

Question workflow transitions are controlled by service methods rather than arbitrary row updates.

Expected flows include:

```text
OPEN -> IN_PROGRESS
OPEN/IN_PROGRESS -> BLOCKED
BLOCKED -> IN_PROGRESS
active -> CLOSED        only through validated close_question()
CLOSED -> IN_PROGRESS   only through validated reopen_question()
```

A closed question remains historically closed even if its basis later becomes stale; staleness affects readiness until revalidation or reopening occurs.

## Graph Roles and Question Roles

Both graphs and questions have `REQUIRED | OPTIONAL` roles.

Only required graphs block run readiness. Only required questions block graph closure, except that structural dependencies can make an otherwise optional question necessary for a required downstream path.

Worker-proposed graphs default to `OPTIONAL`. Worker-discovered questions should also default to `OPTIONAL` unless a trusted caller explicitly requests otherwise.

Promotion or demotion is an explicit audited mutation. A role change must not bypass an existing dependency requirement.

## Dynamic Graph Creation

Workers may propose entirely new graphs during an open `ResearchRun`. The service validates the proposal and creates the graph as `OPTIONAL` by default.

Proposal provenance includes a reason such as:

- `NEW_DOMAIN`;
- `SCOPE_EXPANSION`;
- `CONTRADICTION_BRANCH`.

Promotion to `REQUIRED` is separate and audited.

## Dependency DAG Rules

The dependency topology is one DAG per `ResearchRun`, spanning all graphs in that run.

The service rejects:

- self-dependencies;
- foreign-run dependencies;
- duplicate edges;
- any edge that would introduce a cycle.

Cycle validation and insertion must occur inside the same serialized write transaction. Under SQLite this should follow the existing `BEGIN IMMEDIATE` write discipline so concurrent writers cannot jointly create a cycle after independently passing stale checks.

A recursive SQL reachability check is preferred over a process-global graph cache because persistence is authoritative and multi-process safety matters.

## Question Creation and Refinement

Conceptually, workers submit proposals such as:

```text
propose_question(
    graph_id,
    text,
    role=OPTIONAL,
    reason=EVIDENCE_GAP,
    depends_on=[...],
)
```

The service resolves trusted scope, normalizes the text, performs run-wide exact deduplication, validates same-run dependencies, verifies DAG safety, commits state and audit events atomically, and returns the canonical question.

Active question text may be refined in place through a dedicated operation. Refinement must:

- re-normalize and re-check run-wide uniqueness;
- preserve stable question identity;
- preserve dependencies and claim links;
- record old text, new text, reason, and trusted provenance;
- reject refinement of a closed question until reopened.

No hard deletion of questions or graphs is supported in v1.

## Graph Closure

`close_graph(graph_id)` is explicit and deterministic.

A graph may close only when all required owned questions satisfy closure requirements and all structurally necessary cross-graph dependencies are satisfied.

Optional unfinished questions do not by themselves block graph closure.

A graph may legitimately close while containing a required question whose closed resolution is `CONTESTED`, provided all deterministic closure rules and dependency acceptance policies are satisfied. Closure means sufficiently explored under the completion contract, not certainty.

## Run Readiness

Query Graph exposes an advisory operation such as:

```text
assess_run_completion(run_id)
```

It returns `READY` or `NOT_READY` plus deterministic reasons.

`READY` requires all `REQUIRED` graphs to be closed and no blocking stale required path or unsatisfied required dependency to remain.

Typical `NOT_READY` reasons include:

- required graph remains open;
- required question is open, blocked, or unresolved;
- required closed question has stale closure basis;
- cross-graph prerequisite has not satisfied its acceptance policy.

Query Graph does not terminalize the Evidence Fabric `ResearchRun`. A trusted Research Director/runtime separately decides whether to transition the run through Evidence Fabric.

## Terminal-Run Immutability

Evidence Fabric remains lifecycle authority.

If `ResearchRun.status != OPEN`, Query Graph rejects structural or evidential mutation, including:

- graph creation;
- question creation or refinement;
- role changes;
- dependency changes;
- claim link/unlink operations;
- reopen operations;
- revalidation operations that alter durable state.

Read and audit inspection remain available.

Query Graph must never become a write path around Evidence Fabric terminal-run immutability.

## Persistence

Query Graph should reuse Hermes `SessionDB` and the existing profile `state.db` migration/WAL infrastructure. It must not introduce a second database.

The expected schema objects are approximately:

```text
query_graphs
research_questions
question_dependencies
question_claim_links
question_closure_claims
query_graph_events
```

Evidence Fabric currently occupies schema version 27 on the dependent feature branch. Query Graph should take the next free additive schema version at implementation time. If upstream advances the schema first, the implementation must rebase to the next free version rather than preserving an obsolete hard-coded number.

## Database Integrity

Database constraints should enforce everything SQLite can safely express, including:

- graph -> ResearchRun ownership;
- question -> owning graph/run consistency;
- claim link -> same-run Evidence Fabric claim;
- closure basis -> valid same-run question/claim;
- dependency endpoints -> same ResearchRun;
- run-wide normalized-question uniqueness;
- unique dependency pair;
- self-edge rejection;
- append-only event history where practical;
- terminal-run mutation rejection where practical.

Service validation remains responsible for graph-cycle checks and other invariants not naturally expressible as simple SQLite constraints.

## Concurrency

All mutation operations use the existing serialized write discipline rather than process-global authority.

Required concurrency behavior:

- identical concurrent question proposals converge on one durable identity;
- unrelated concurrent proposals both survive;
- duplicate edge races converge or return a stable domain result;
- cycle-check and edge insertion are atomic;
- state mutation and corresponding audit event commit or roll back together;
- raw SQLite errors do not escape expected domain paths.

## Event Atomicity

Every durable state mutation and its audit event must occur in one transaction.

Examples:

- a question cannot exist without `QUESTION_CREATED`;
- a reopen cannot commit without `QUESTION_REOPENED`;
- a failed mutation must leave neither partial state nor an orphan event.

The event log is audit history, not the source used to reconstruct current operational state.

## Service Boundary

The core API should be a runtime/service layer conceptually similar to:

```text
QueryGraphService(
    db=SessionDB,
    scope=QueryGraphScope,
)
```

Expected operations include:

```text
create_graph()
get_graph()
list_graphs()
set_graph_role()

propose_question()
get_question()
list_questions()
refine_question()
set_question_role()
start_question()
block_question()
unblock_question()

add_dependency()
remove_dependency()

link_claim()
unlink_claim()

derive_resolution()
close_question()
reopen_question()
revalidate_question()

close_graph()
assess_graph_completion()
assess_run_completion()

list_events()
```

The service must not expose arbitrary row update or arbitrary SQL methods.

## Runtime Authority and Security

Trusted authority fields must come from Hermes runtime context rather than model arguments.

Runtime-owned fields include at least:

- scope/home key;
- profile/connection identity where available;
- mutation actor;
- timestamps;
- authorized ResearchRun context.

Models may propose research content such as question text, graph purpose, mutation reason, dependencies, or a claim to link. They may not supply arbitrary trusted authority fields.

External text in questions, graph purposes, claims, or event reasons remains untrusted data and must never be interpreted as executable instructions merely because it is persisted.

## Model Tool Decision

Query Graph v1 does not require a model-facing `query_graph` tool.

The Evidence Fabric implementation already established that Hermes does not yet expose a sufficiently trustworthy runtime path for model-facing handlers to obtain authorized run/scope identity without weakening authority boundaries. Query Graph is even more structurally sensitive because it controls dependencies, required/optional roles, and completion state.

Therefore the expected v1 outcome is:

```text
MODEL_TOOL_DEFERRED
```

unless trusted runtime authority exists independently by implementation time. The durable service/runtime API is the v1 deliverable.

## Domain Errors

Expected public failures should be translated into stable domain errors rather than leaking raw SQLite exceptions. Candidate errors include:

```text
QueryGraphValidationError
QueryGraphNotFoundError
QueryGraphScopeError
QueryGraphLifecycleError
QueryGraphIntegrityError
QueryGraphCycleError
QueryGraphDependencyError
QueryGraphStaleResolutionError
```

The implementation plan may refine the exact class hierarchy, but it must preserve meaningful distinctions between validation, lifecycle, scope/integrity, dependency, cycle, and stale-resolution failures.

Integrity-sensitive operations fail closed when the service cannot prove same-run scope, nonterminal lifecycle, DAG safety, dependency satisfaction, or closure basis validity.

## Testing Strategy

### Migration

Use a genuine Evidence-Fabric-era database fixture and upgrade it into the Query Graph schema. Do not fake migration by creating the new schema and decrementing the schema version.

Verify:

- Query Graph tables/indexes/triggers are created;
- Evidence Fabric data survives unchanged;
- repeat startup is idempotent;
- foreign keys remain enforced;
- terminal existing runs cannot be mutated through Query Graph.

### DAG Integrity

Test:

- self-edge rejection;
- two-node cycles;
- long cross-graph cycles;
- valid diamonds/shared prerequisites;
- edge removal;
- concurrent edge proposals that would jointly form a cycle.

### Deduplication

Concurrently propose normalized equivalents and verify:

- one durable question;
- one result `created=True`;
- one result `created=False`;
- both results return the same question ID;
- no raw integrity error leaks.

Verify semantically similar but differently worded text remains distinct.

### Evidence Fabric Integration

Use real Evidence Fabric records for integrity-critical tests.

Verify:

- same-run claim linking;
- foreign-run claim rejection;
- live resolution changes as linked claim state changes;
- Query Graph cannot mutate claim status;
- closure basis persists exact claim state markers;
- later claim change marks closure stale;
- historical closed resolution does not silently change.

### Lifecycle

Assert exact domain error types for invalid transitions.

Cover:

- `OPEN -> IN_PROGRESS`;
- active -> `BLOCKED`;
- `BLOCKED -> IN_PROGRESS`;
- active -> `CLOSED` only through validated closure;
- `CLOSED -> active` only through validated reopen;
- closed refinement rejection;
- active refinement audit;
- stale required closure blocking;
- optional stale warning behavior;
- dependency policy enforcement;
- contested legitimate closure.

### Required/Optional Semantics

Verify required graphs and required questions block readiness while optional work does not, except when an optional node is structurally required by a required dependency path.

Verify audited role promotion/demotion changes readiness deterministically.

### Event Atomicity

Force mutation failures and verify no partial state or orphan event survives.

### Persistence and Restart

Build a realistic multi-graph run with cross-graph dependencies, claim links, closed and blocked questions, reopen/revalidation history, then close and reopen `SessionDB`.

Verify all graph/question identity, ownership, roles, edges, claim links, closure basis, stale detection, event history, and runtime provenance survive restart.

### Canonical End-to-End Scenario

Construct a research run such as:

```text
"Determine whether Policy X is legally valid,
technically feasible, and practically effective."
```

Use required Legal and Technical graphs plus an optional Context graph. Add cross-graph dependencies, dynamically discover a new required question, link real supporting and contradictory Evidence Fabric claims, close and stale some questions, reopen one, and verify Hermes can reconstruct without an LLM:

- what questions were asked;
- why they were created;
- which were required;
- what depended on what;
- which claims answered them;
- which answers were contested;
- which closed answers became stale;
- why something was reopened;
- what remains unresolved;
- whether each graph may close;
- whether the whole run is `READY`.

## Acceptance Bar

Query Graph v1 is ready only when fresh verification demonstrates:

- schema migration;
- run-wide DAG integrity;
- concurrent mutation safety;
- deterministic exact question deduplication;
- same-run Evidence Fabric claim integrity;
- deterministic question resolution;
- immutable closure snapshots;
- deterministic staleness detection;
- explicit revalidation/reopen behavior;
- required/optional completion semantics;
- event atomicity;
- restart durability;
- deterministic graph and run readiness assessment.

A model-facing tool is not part of the acceptance bar.

## Implementation Dependency and Branching

Query Graph v1 depends on the hardened Evidence Fabric v1 implementation. Until the Evidence Fabric PR lands upstream, Query Graph work should remain on a separate dependent branch/worktree based on the Evidence Fabric feature HEAD. It must not disturb the Evidence Fabric PR branch.

Before implementation, re-check upstream schema/version changes and rebase or renumber the additive migration as necessary.

## Design Principle

The core rule is:

> Models propose research structure; deterministic runtime and database invariants decide what becomes durable truth.

Evidence Fabric preserves what Hermes believes and why. Query Graph preserves what Hermes still needs to answer and what must be true before it can legitimately call the research complete.
