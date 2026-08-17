import sqlite3

import pytest

from hermes_state import SessionDB


QUERY_GRAPH_TABLES = {
    "query_graphs",
    "research_questions",
    "question_dependencies",
    "question_claim_links",
    "question_closure_claims",
    "query_graph_events",
}

QUERY_GRAPH_INDEXES = {
    "ux_research_questions_run_fingerprint",
    "ux_question_dependencies_pair",
    "idx_query_graphs_run",
    "idx_research_questions_graph",
    "idx_research_questions_run",
    "idx_question_dependencies_run",
    "idx_question_claim_links_run",
    "idx_query_graph_events_run",
}

QUERY_GRAPH_TRIGGERS = {
    "query_graphs_open_run_insert_guard",
    "query_graphs_terminal_update_guard",
    "query_graphs_terminal_delete_guard",
    "research_questions_open_run_insert_guard",
    "research_questions_terminal_update_guard",
    "research_questions_terminal_delete_guard",
    "question_dependencies_open_run_insert_guard",
    "question_dependencies_terminal_update_guard",
    "question_dependencies_terminal_delete_guard",
    "question_claim_links_open_run_insert_guard",
    "question_claim_links_terminal_update_guard",
    "question_claim_links_terminal_delete_guard",
    "question_closure_claims_open_run_insert_guard",
    "question_closure_claims_terminal_update_guard",
    "question_closure_claims_terminal_delete_guard",
    "query_graph_events_open_run_insert_guard",
    "query_graph_events_append_only_update_guard",
    "query_graph_events_append_only_delete_guard",
}


def _objects(db_path, object_type):
    with sqlite3.connect(db_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = ?", (object_type,)
            )
        }


def _raw_connection(db_path):
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _create_pre_query_graph_v27_database(db_path):
    """Create a genuine minimal v27 Evidence-Fabric-era state database.

    The fixture deliberately does not execute current SCHEMA_SQL, so future
    Query Graph DDL cannot accidentally leak into the pre-migration database.
    SessionDB's declarative schema initialization creates unrelated missing
    tables on open while preserving the representative legacy rows below.
    """
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (27);

            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at REAL NOT NULL
            );

            CREATE TABLE research_runs (
                id TEXT PRIMARY KEY,
                objective TEXT NOT NULL,
                owner_scope_key TEXT NOT NULL,
                owner_profile TEXT,
                owner_connection_id TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN'
                    CHECK (status IN ('OPEN', 'COMPLETED', 'CANCELLED', 'FAILED')),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE evidence_records (
                id TEXT PRIMARY KEY,
                research_run_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                retrieval_method TEXT NOT NULL,
                source_uri TEXT,
                canonical_uri TEXT,
                title TEXT,
                publisher_or_origin TEXT,
                published_at REAL,
                retrieved_at REAL NOT NULL,
                content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
                raw_reference TEXT,
                relevant_passages_json TEXT NOT NULL DEFAULT '[]',
                created_by_agent TEXT NOT NULL,
                created_by_profile TEXT,
                provider TEXT,
                model TEXT,
                derived_from_evidence_id TEXT,
                untrusted_external_content INTEGER NOT NULL DEFAULT 1,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                UNIQUE (id, research_run_id),
                FOREIGN KEY (research_run_id) REFERENCES research_runs(id),
                FOREIGN KEY (derived_from_evidence_id, research_run_id)
                    REFERENCES evidence_records(id, research_run_id)
            );

            CREATE TABLE claims (
                id TEXT PRIMARY KEY,
                research_run_id TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'UNVERIFIED'
                    CHECK (status IN ('UNVERIFIED', 'SUPPORTED', 'PARTIALLY_SUPPORTED', 'CONTRADICTED', 'UNRESOLVED')),
                created_by_agent TEXT NOT NULL,
                created_by_profile TEXT,
                updated_by_agent TEXT,
                updated_by_profile TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE (id, research_run_id),
                FOREIGN KEY (research_run_id) REFERENCES research_runs(id)
            );

            CREATE TABLE claim_evidence_links (
                claim_id TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                research_run_id TEXT NOT NULL,
                relation TEXT NOT NULL CHECK (relation IN ('SUPPORTS', 'CONTRADICTS', 'CONTEXT')),
                passage_locator_json TEXT,
                created_by_agent TEXT NOT NULL,
                created_by_profile TEXT,
                created_at REAL NOT NULL,
                PRIMARY KEY (claim_id, evidence_id),
                FOREIGN KEY (claim_id, research_run_id)
                    REFERENCES claims(id, research_run_id),
                FOREIGN KEY (evidence_id, research_run_id)
                    REFERENCES evidence_records(id, research_run_id)
            );
            """
        )
        connection.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES ('legacy', 'test', 1)"
        )
        connection.execute(
            "INSERT INTO research_runs "
            "(id, objective, owner_scope_key, owner_profile, owner_connection_id, "
            "status, metadata_json, created_at, updated_at) "
            "VALUES ('run-old', 'legacy objective', 'scope', 'profile', 'connection', "
            "'OPEN', '{\"legacy\":true}', 2, 3)"
        )
        connection.execute(
            "INSERT INTO evidence_records "
            "(id, research_run_id, source_type, retrieval_method, retrieved_at, "
            "content_hash, raw_reference, created_by_agent, metadata_json, created_at) "
            "VALUES ('e-old', 'run-old', 'FILE', 'FILE_READ', 4, ?, "
            "'artifact:legacy', 'agent', '{\"e\":1}', 5)",
            ("e" * 64,),
        )
        connection.execute(
            "INSERT INTO claims "
            "(id, research_run_id, text, status, created_by_agent, metadata_json, "
            "created_at, updated_at) "
            "VALUES ('c-old', 'run-old', 'legacy claim', 'SUPPORTED', 'agent', "
            "'{\"c\":1}', 6, 7)"
        )
        connection.execute(
            "INSERT INTO claim_evidence_links "
            "(claim_id, evidence_id, research_run_id, relation, created_by_agent, created_at) "
            "VALUES ('c-old', 'e-old', 'run-old', 'SUPPORTS', 'agent', 8)"
        )


def _insert_open_run(connection, run_id):
    connection.execute(
        "INSERT INTO research_runs "
        "(id, objective, owner_scope_key, status, metadata_json, created_at, updated_at) "
        "VALUES (?, ?, 'scope', 'OPEN', '{}', 1, 1)",
        (run_id, f"objective {run_id}"),
    )


def _insert_graph(connection, graph_id, run_id):
    connection.execute(
        "INSERT INTO query_graphs "
        "(id, research_run_id, name, purpose, role, workflow_state, "
        "created_by_agent, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'OPTIONAL', 'OPEN', 'agent', 1, 1)",
        (graph_id, run_id, f"graph {graph_id}", f"purpose {graph_id}"),
    )


def _insert_question(connection, question_id, graph_id, run_id, fingerprint):
    connection.execute(
        "INSERT INTO research_questions "
        "(id, graph_id, research_run_id, question_text, normalized_fingerprint, "
        "role, workflow_state, creation_reason, created_by_agent, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'OPTIONAL', 'OPEN', 'ROOT', 'agent', 1, 1)",
        (question_id, graph_id, run_id, f"question {question_id}", fingerprint),
    )


def _insert_claim(connection, claim_id, run_id):
    connection.execute(
        "INSERT INTO claims "
        "(id, research_run_id, text, status, created_by_agent, metadata_json, created_at, updated_at) "
        "VALUES (?, ?, ?, 'SUPPORTED', 'agent', '{}', 1, 1)",
        (claim_id, run_id, f"claim {claim_id}"),
    )


def test_fresh_schema_has_query_graph_objects_and_v28(tmp_path):
    db_path = tmp_path / "state.db"
    with SessionDB(db_path):
        pass

    assert QUERY_GRAPH_TABLES <= _objects(db_path, "table")
    assert QUERY_GRAPH_INDEXES <= _objects(db_path, "index")
    assert QUERY_GRAPH_TRIGGERS <= _objects(db_path, "trigger")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 28
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_real_v27_database_upgrades_idempotently_without_losing_evidence_rows(tmp_path):
    db_path = tmp_path / "state.db"
    _create_pre_query_graph_v27_database(db_path)

    with SessionDB(db_path):
        pass
    with SessionDB(db_path):
        pass
    with SessionDB(db_path):
        pass

    assert QUERY_GRAPH_TABLES <= _objects(db_path, "table")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 28
        assert connection.execute(
            "SELECT id, source, started_at FROM sessions WHERE id='legacy'"
        ).fetchone() == ("legacy", "test", 1.0)
        assert connection.execute(
            "SELECT id, objective, owner_scope_key, owner_profile, owner_connection_id, "
            "status, metadata_json, created_at, updated_at FROM research_runs WHERE id='run-old'"
        ).fetchone() == (
            "run-old",
            "legacy objective",
            "scope",
            "profile",
            "connection",
            "OPEN",
            '{"legacy":true}',
            2.0,
            3.0,
        )
        assert connection.execute(
            "SELECT id, research_run_id, content_hash, raw_reference, metadata_json "
            "FROM evidence_records WHERE id='e-old'"
        ).fetchone() == (
            "e-old",
            "run-old",
            "e" * 64,
            "artifact:legacy",
            '{"e":1}',
        )
        assert connection.execute(
            "SELECT id, research_run_id, text, status, metadata_json, created_at, updated_at "
            "FROM claims WHERE id='c-old'"
        ).fetchone() == (
            "c-old",
            "run-old",
            "legacy claim",
            "SUPPORTED",
            '{"c":1}',
            6.0,
            7.0,
        )
        assert connection.execute(
            "SELECT claim_id, evidence_id, research_run_id, relation, created_at "
            "FROM claim_evidence_links WHERE claim_id='c-old'"
        ).fetchone() == ("c-old", "e-old", "run-old", "SUPPORTS", 8.0)


def test_composite_foreign_keys_reject_cross_run_graph_claim_and_dependency_links(tmp_path):
    db_path = tmp_path / "state.db"
    with SessionDB(db_path):
        pass
    connection = _raw_connection(db_path)
    try:
        _insert_open_run(connection, "run-a")
        _insert_open_run(connection, "run-b")
        _insert_graph(connection, "graph-a", "run-a")
        _insert_graph(connection, "graph-b", "run-b")
        _insert_question(connection, "q-a", "graph-a", "run-a", "a" * 64)
        _insert_question(connection, "q-b", "graph-b", "run-b", "b" * 64)
        _insert_claim(connection, "claim-b", "run-b")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_question(connection, "q-mismatch", "graph-a", "run-b", "c" * 64)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO question_claim_links "
                "(question_id, claim_id, research_run_id, created_by_agent, created_at) "
                "VALUES ('q-a', 'claim-b', 'run-a', 'agent', 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO question_dependencies "
                "(dependent_question_id, prerequisite_question_id, research_run_id, "
                "acceptance_policy, created_by_agent, created_at) "
                "VALUES ('q-a', 'q-b', 'run-a', 'SUPPORTED', 'agent', 1)"
            )
    finally:
        connection.close()


def test_run_wide_question_fingerprint_is_unique_but_other_runs_may_reuse_it(tmp_path):
    db_path = tmp_path / "state.db"
    with SessionDB(db_path):
        pass
    connection = _raw_connection(db_path)
    try:
        _insert_open_run(connection, "run-a")
        _insert_open_run(connection, "run-b")
        _insert_graph(connection, "graph-a1", "run-a")
        _insert_graph(connection, "graph-a2", "run-a")
        _insert_graph(connection, "graph-b", "run-b")
        fingerprint = "f" * 64
        _insert_question(connection, "q-a1", "graph-a1", "run-a", fingerprint)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_question(connection, "q-a2", "graph-a2", "run-a", fingerprint)
        _insert_question(connection, "q-b", "graph-b", "run-b", fingerprint)
    finally:
        connection.close()


def test_terminal_run_rejects_all_query_graph_mutations_but_keeps_reads(tmp_path):
    db_path = tmp_path / "state.db"
    with SessionDB(db_path):
        pass
    connection = _raw_connection(db_path)
    try:
        _insert_open_run(connection, "run")
        _insert_graph(connection, "graph", "run")
        _insert_question(connection, "q1", "graph", "run", "1" * 64)
        _insert_question(connection, "q2", "graph", "run", "2" * 64)
        _insert_claim(connection, "claim", "run")
        connection.execute(
            "INSERT INTO question_dependencies "
            "(dependent_question_id, prerequisite_question_id, research_run_id, "
            "acceptance_policy, created_by_agent, created_at) "
            "VALUES ('q1', 'q2', 'run', 'SUPPORTED', 'agent', 1)"
        )
        connection.execute(
            "INSERT INTO question_claim_links "
            "(question_id, claim_id, research_run_id, created_by_agent, created_at) "
            "VALUES ('q1', 'claim', 'run', 'agent', 1)"
        )
        connection.execute(
            "INSERT INTO question_closure_claims "
            "(question_id, claim_id, research_run_id, claim_status_at_close, "
            "claim_updated_at_at_close, snapshot_at) "
            "VALUES ('q1', 'claim', 'run', 'SUPPORTED', 1, 1)"
        )
        connection.execute(
            "INSERT INTO query_graph_events "
            "(research_run_id, graph_id, question_id, event_type, actor_agent, payload_json, created_at) "
            "VALUES ('run', 'graph', 'q1', 'QUESTION_CREATED', 'agent', '{}', 1)"
        )
        connection.execute("UPDATE research_runs SET status='COMPLETED' WHERE id='run'")

        assert connection.execute(
            "SELECT workflow_state FROM query_graphs WHERE id='graph'"
        ).fetchone() == ("OPEN",)
        assert connection.execute(
            "SELECT workflow_state FROM research_questions WHERE id='q1'"
        ).fetchone() == ("OPEN",)

        with pytest.raises(sqlite3.IntegrityError):
            _insert_graph(connection, "late-graph", "run")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE query_graphs SET role='REQUIRED' WHERE id='graph'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM query_graphs WHERE id='graph'")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_question(connection, "late-q", "graph", "run", "3" * 64)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE research_questions SET role='REQUIRED' WHERE id='q1'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM research_questions WHERE id='q1'")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO question_dependencies "
                "(dependent_question_id, prerequisite_question_id, research_run_id, "
                "acceptance_policy, created_by_agent, created_at) "
                "VALUES ('q2', 'q1', 'run', 'SUPPORTED', 'agent', 2)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE question_dependencies SET acceptance_policy='ANY_CLOSED' "
                "WHERE dependent_question_id='q1' AND prerequisite_question_id='q2'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM question_dependencies "
                "WHERE dependent_question_id='q1' AND prerequisite_question_id='q2'"
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO question_claim_links "
                "(question_id, claim_id, research_run_id, created_by_agent, created_at) "
                "VALUES ('q2', 'claim', 'run', 'agent', 2)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE question_claim_links SET created_by_agent='other' "
                "WHERE question_id='q1' AND claim_id='claim'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM question_claim_links WHERE question_id='q1' AND claim_id='claim'"
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO question_closure_claims "
                "(question_id, claim_id, research_run_id, claim_status_at_close, "
                "claim_updated_at_at_close, snapshot_at) "
                "VALUES ('q2', 'claim', 'run', 'SUPPORTED', 1, 2)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE question_closure_claims SET snapshot_at=2 "
                "WHERE question_id='q1' AND claim_id='claim'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM question_closure_claims WHERE question_id='q1' AND claim_id='claim'"
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO query_graph_events "
                "(research_run_id, event_type, actor_agent, payload_json, created_at) "
                "VALUES ('run', 'LATE_EVENT', 'agent', '{}', 2)"
            )
    finally:
        connection.close()


def test_query_graph_events_are_append_only(tmp_path):
    db_path = tmp_path / "state.db"
    with SessionDB(db_path):
        pass
    connection = _raw_connection(db_path)
    try:
        _insert_open_run(connection, "run")
        connection.execute(
            "INSERT INTO query_graph_events "
            "(research_run_id, event_type, actor_agent, payload_json, created_at) "
            "VALUES ('run', 'GRAPH_CREATED', 'agent', '{}', 1)"
        )
        with pytest.raises(sqlite3.IntegrityError, match="query graph events are append-only"):
            connection.execute(
                "UPDATE query_graph_events SET event_type='TAMPERED' WHERE research_run_id='run'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="query graph events are append-only"):
            connection.execute("DELETE FROM query_graph_events WHERE research_run_id='run'")
    finally:
        connection.close()
