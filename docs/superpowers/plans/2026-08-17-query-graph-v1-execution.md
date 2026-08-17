# Query Graph v1 Execution Log

- Execution mode: Superpowers `executing-plans` (inline).
- Local shell clone/worktree attempt: blocked because the sandbox cannot resolve `github.com`.
- Isolation fallback: dedicated GitHub branch `feature/query-graph-v1` based on approved plan commit `557a4405cb5b66151f442b6d653cef85b7819fad`; the Evidence Fabric PR branch remains untouched.
- Test runner fallback: pull-request GitHub Actions on the isolated branch; no passing claim is made without CI evidence.
- Upstream `NousResearch/hermes-agent` `main` schema version at execution preflight: 26.
- Dependent Evidence Fabric branch schema version at execution preflight: 27.
- Selected additive Query Graph schema version: **28**.
- Lifecycle hardening interpretation: closed graphs reject brand-new question creation; explicit reopening of an existing closed question may make a closed graph temporarily unready until resolved again. Dependency mutation on a closed dependent question is rejected.
- Event hardening interpretation: `query_graph_events` is append-only and event insertion also requires an OPEN parent run, preventing fabricated post-terminal audit history.
- Model-facing tool remains out of scope unless a separately trusted runtime authority path is proven.

## Functional implementation

Query Graph v1 implements the approved research-specific planning layer above Evidence Fabric:

- multiple Query Graphs per `ResearchRun`;
- one run-wide DAG with cross-graph dependencies and global cycle rejection;
- exact normalized run-wide canonical question identity;
- `REQUIRED` / `OPTIONAL` graph and question roles with explicit audited role changes;
- worker-compatible question proposals with service-owned validation/commit authority;
- `SUPPORTED`, `PARTIAL_OR_BETTER`, and `ANY_CLOSED` dependency acceptance policies;
- same-run Evidence Fabric claim links without copying claim/evidence text into Query Graph;
- deterministic active resolution from Evidence Fabric claim statuses;
- evidence- and dependency-gated question closure;
- immutable frozen closure resolution and exact claim-basis snapshots;
- exact staleness detection from claim IDs, statuses, and update markers;
- explicit same-result revalidation and explicit material-change reopening;
- deterministic graph completion and advisory run completion with stable reason strings;
- append-only audit history, runtime provenance, restart durability, terminal-run immutability, and transactionally atomic state/event mutations.

## End-to-end acceptance correction

The first canonical E2E scenario incorrectly attempted to use `revalidate_question()` to upgrade a frozen `PARTIALLY_ANSWERED` closure after the live result became `SUPPORTED`. Production correctly rejected that with `QueryGraphStaleResolutionError`; the defect was in the acceptance test, not the lifecycle implementation.

The corrected scenario now proves both intended paths:

1. a same-result `SUPPORTED` claim-version change can be explicitly revalidated while preserving the frozen result; and
2. a `PARTIALLY_ANSWERED -> SUPPORTED` result change rejects revalidation and requires explicit reopen before a new closure may be recorded.

Corrected acceptance commit: `28804604462c05c9e17387737bfac59f8998dc20`.

Focused Query Graph + Evidence Fabric verification after that correction: **109 passed**.

## Final verification evidence

Authoritatively verified product HEAD: `09a8b36d076f526adde3492f8c0da4ae5ab7cc98`.

Dedicated final verification workflow run `32053738300` completed successfully:

- `python -m pytest tests/test_query_graph*.py tests/test_evidence_fabric.py tests/test_evidence_fabric_schema.py tests/test_evidence_fabric_concurrency.py -q`
  - **111 passed in 14.71s**.
- `python -m pytest tests/test_hermes_state.py tests/test_hermes_state_wal_fallback.py tests/test_session_db_context_manager.py tests/test_session_db_read_conn_pool.py -q`
  - **264 passed, 13 skipped in 32.84s**.
- `python -m compileall hermes_state_common.py hermes_state_schema.py research`
  - **PASS**.
- `git diff --check <design-base>...HEAD`
  - **PASS**.
- Changed-path scope check against `design/query-graph-v1`
  - **PASS**, 25 paths, no unexpected subsystem drift.
- Prohibited-drift scan across all Query Graph modules
  - **PASS**: no embeddings/fuzzy dedup, confidence/trust scoring, scheduler/model routing, NotebookLM/Obsidian/Knowledge Hub coupling, automatic run terminalization, or model-facing Query Graph tool.
- Trusted model-tool boundary
  - **`MODEL_TOOL_DEFERRED`**: no `tools/query_graph_tool.py`, no Query Graph toolset registration, and no newly proven runtime-owned authority path that independently supplies trusted `ResearchRun` + scope + actor context.

The first version of the final verification workflow failed only its custom changed-path allowlist because it omitted two legitimate implementation-support paths (`docs/superpowers/plans/2026-08-17-query-graph-v1-execution.md` and `tests/state/test_session_git_metadata_generation.py`). Systematic debugging isolated that verification-harness defect; the product tree was unchanged. Correcting the allowlist produced the successful final run above.

## Repository-wide CI evidence

On the same verified product HEAD:

- all **12/12 Python test slices passed**;
- Python e2e passed;
- blocking ruff passed;
- ruff + ty diff passed;
- Windows-footgun checks passed;
- Windows-only tests passed;
- macOS-only tests passed;
- desktop UI shards 1/3, 2/3, and 3/3 passed;
- desktop all/platform/plugin/lint checks passed;
- web/shared/tests-js/TUI checks passed;
- installer tests passed;
- `uv.lock` verification passed;
- supply-chain diff scans passed;
- Docker build/test workflow passed.

Repository CI still contains non-product governance/infrastructure failures:

- contributor-attribution check: unmapped connector-authored commit email;
- review-label gate: missing repository-maintainer `ci-reviewed` label;
- OSV job: the scanner, reporter, result export, and artifact upload succeeded, but GitHub code-scanning upload failed; the downstream OSV review-status job succeeded;
- aggregate `All required checks pass` therefore reports failure because those repository gates are unresolved.

None of those failures are Query Graph test, compile, lint, dependency-scan, platform, or runtime-behavior failures.

## Concurrency / durability evidence

Dedicated Query Graph tests prove:

- two separate `SessionDB` writers proposing normalized-equivalent questions converge on one durable canonical question ID with one create event and no raw SQLite leakage;
- distinct concurrent question writes both survive;
- competing dependency writes cannot persist a run-wide cycle;
- state writes roll back when their paired audit-event mutation fails;
- graphs, roles, cross-graph dependencies, claim links, closure basis, staleness state, and event order survive database restart.

## Manual code review

The prescribed reviewer-subagent path was unavailable in this session, so the accepted fallback was a manual live review of the PR diff after the fresh verification run.

Reviewed surfaces included domain types/normalization, service scope/lifecycle authority, graph/question mutations, DAG direction and cycle detection, Evidence Fabric claim integration, resolution truth table, closure snapshots, staleness/revalidation/reopen semantics, graph/run completion, facade MRO, schema v28 constraints/triggers, concurrency tests, and migration tests.

Review conclusion: **no Critical or Important findings; no production code change recommended before integration**. A few comments/docstrings are merely historical/cosmetic and do not justify reopening the verified implementation.

## Acceptance conclusion

The Query Graph v1 acceptance bar is satisfied by fresh evidence for additive migration, run-wide DAG integrity, concurrent exact dedup, cycle-race safety, same-run claim integrity, deterministic resolution, evidence/dependency-gated closure, immutable closure basis, deterministic staleness, explicit revalidation/reopen, required/optional semantics without dependency bypass, deterministic graph/run readiness, event/state atomicity, terminal-run immutability, restart durability, and LLM-independent reconstruction of research structure/state.

Temporary GitHub Actions verification infrastructure is intentionally removed before integration; its successful run above remains the evidence for the verified product tree.
