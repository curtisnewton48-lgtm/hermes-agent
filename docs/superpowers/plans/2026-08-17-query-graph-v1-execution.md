# Query Graph v1 Execution Log

- Execution mode: Superpowers `executing-plans` (inline).
- Local shell clone/worktree attempt: blocked because the sandbox cannot resolve `github.com`.
- Isolation fallback: dedicated GitHub branch `feature/query-graph-v1` based on approved plan commit `557a4405cb5b66151f442b6d653cef85b7819fad`; the Evidence Fabric PR branch remains untouched.
- Test runner fallback: pull-request GitHub Actions on the isolated branch; no passing claim will be made without CI evidence.
- Upstream `NousResearch/hermes-agent` `main` schema version at execution preflight: 26.
- Dependent branch schema version at execution preflight: 27.
- Selected next additive Query Graph schema version: 28.
- Lifecycle hardening interpretation: closed graphs reject brand-new question creation; explicit reopening of an existing closed question may make a closed graph temporarily unready until resolved again. Dependency mutation on a closed dependent question is rejected.
- Event hardening interpretation: `query_graph_events` is append-only and event insertion also requires an OPEN parent run, preventing fabricated post-terminal audit history.
- Model-facing tool remains out of scope unless a separately trusted runtime authority path is proven.
