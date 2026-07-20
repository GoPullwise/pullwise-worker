"""Versioned SQLite schema for the Agent Kernel shadow store."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


INITIAL_STATEMENTS = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version > 0),
        name TEXT NOT NULL UNIQUE,
        sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE content_objects (
        sha256 TEXT PRIMARY KEY CHECK (
            length(sha256) = 64 AND sha256 = lower(sha256)
        ),
        size_bytes INTEGER NOT NULL CHECK (
            size_bytes >= 0 AND size_bytes <= 9007199254740991
        ),
        media_type TEXT NOT NULL CHECK (
            length(media_type) BETWEEN 1 AND 120
        ),
        content_schema_id TEXT NOT NULL CHECK (length(content_schema_id) > 0),
        encoding TEXT NOT NULL CHECK (encoding IN ('utf-8', 'binary')),
        created_at TEXT NOT NULL,
        verified_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE content_bindings (
        task_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL,
        sha256 TEXT NOT NULL REFERENCES content_objects(sha256),
        size_bytes INTEGER NOT NULL,
        media_type TEXT NOT NULL,
        content_schema_id TEXT NOT NULL,
        encoding TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (task_id, artifact_id)
    )
    """,
    """
    CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY,
        task_type TEXT NOT NULL,
        scan_id TEXT,
        request_ref TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        policy_ref TEXT NOT NULL,
        policy_digest TEXT NOT NULL,
        policy_version INTEGER NOT NULL CHECK (policy_version >= 1),
        protocol_mode TEXT NOT NULL CHECK (protocol_mode = 'legacy_v1'),
        lifecycle TEXT NOT NULL,
        desired_state TEXT NOT NULL CHECK (desired_state IN ('RUN', 'CANCEL')),
        task_version INTEGER NOT NULL CHECK (task_version >= 1),
        deletion_version INTEGER NOT NULL CHECK (deletion_version >= 0),
        outer_job_id TEXT,
        run_id TEXT,
        lease_id TEXT,
        transport_epoch INTEGER,
        native_epoch INTEGER NOT NULL CHECK (native_epoch >= 0),
        current_attempt_id TEXT,
        owner_id TEXT NOT NULL,
        owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 0),
        ledger_version INTEGER NOT NULL CHECK (ledger_version >= 0),
        ledger_head_digest TEXT,
        charter_version INTEGER NOT NULL CHECK (charter_version >= 0),
        charter_ref TEXT,
        current_checkpoint_generation INTEGER NOT NULL CHECK (
            current_checkpoint_generation >= 0
        ),
        current_checkpoint_hash TEXT,
        quality_risk TEXT NOT NULL CHECK (quality_risk IN ('Q0','Q1','Q2','Q3')),
        absolute_deadline_at TEXT NOT NULL,
        terminalization_reserve_ms INTEGER NOT NULL CHECK (
            terminalization_reserve_ms >= 0
        ),
        completion_proposal_ref TEXT,
        final_observation_manifest_ref TEXT,
        terminal_kind TEXT CHECK (
            terminal_kind IS NULL OR terminal_kind IN ('task_result','transport_abandoned')
        ),
        result_ref TEXT,
        result_digest TEXT,
        outcome TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        terminal_at TEXT,
        UNIQUE (task_id, result_digest)
    )
    """,
    """
    CREATE TABLE task_events (
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        event_seq INTEGER NOT NULL CHECK (event_seq >= 1),
        idempotency_key TEXT NOT NULL,
        event_type TEXT NOT NULL,
        task_version INTEGER NOT NULL,
        payload_ref TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (task_id, event_seq),
        UNIQUE (task_id, idempotency_key)
    )
    """,
    """
    CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        native_epoch INTEGER NOT NULL CHECK (native_epoch >= 1),
        state TEXT NOT NULL,
        transport_epoch INTEGER,
        started_at TEXT,
        terminal_at TEXT,
        terminal_reason TEXT,
        UNIQUE (task_id, native_epoch)
    )
    """,
    """
    CREATE TABLE owner_incarnations (
        session_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        owner_id TEXT NOT NULL,
        owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 1),
        state TEXT NOT NULL,
        started_at TEXT NOT NULL,
        terminal_at TEXT,
        UNIQUE (task_id, owner_epoch)
    )
    """,
    """
    CREATE TABLE agent_sessions (
        session_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        role TEXT NOT NULL,
        state TEXT NOT NULL,
        input_digest TEXT NOT NULL,
        started_at TEXT NOT NULL,
        terminal_at TEXT,
        terminal_reason TEXT
    )
    """,
    """
    CREATE TABLE requirements (
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        requirement_id TEXT NOT NULL,
        ledger_version INTEGER NOT NULL CHECK (ledger_version >= 1),
        entry_ref TEXT NOT NULL,
        entry_digest TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        mandatory INTEGER NOT NULL CHECK (mandatory IN (0,1)),
        PRIMARY KEY (task_id, requirement_id)
    )
    """,
    """
    CREATE TABLE requirement_events (
        event_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        requirement_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        event_ref TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE interactions (
        interaction_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        idempotency_key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('input','approval')),
        state TEXT NOT NULL,
        request_ref TEXT NOT NULL,
        response_ref TEXT,
        deadline_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        resolved_at TEXT
    )
    """,
    """
    CREATE TABLE budget_entries (
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        budget_seq INTEGER NOT NULL CHECK (budget_seq >= 1),
        attempt_id TEXT,
        session_id TEXT,
        operation TEXT NOT NULL CHECK (
            operation IN ('RESERVE','CONSUME','RELEASE','CORRECT')
        ),
        dimension TEXT NOT NULL,
        amount INTEGER NOT NULL,
        reason TEXT NOT NULL,
        monotonic_ms INTEGER NOT NULL CHECK (monotonic_ms >= 0),
        previous_watermark INTEGER NOT NULL CHECK (previous_watermark >= 0),
        PRIMARY KEY (task_id, budget_seq)
    )
    """,
    """
    CREATE TABLE observations (
        observation_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        attempt_id TEXT NOT NULL,
        tool_invocation_id TEXT NOT NULL UNIQUE,
        observation_ref TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE checkpoint_index (
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        generation INTEGER NOT NULL CHECK (generation >= 1),
        manifest_hash TEXT NOT NULL UNIQUE,
        previous_manifest_hash TEXT,
        manifest_ref TEXT NOT NULL,
        committed_task_version INTEGER NOT NULL,
        committed_at TEXT NOT NULL,
        PRIMARY KEY (task_id, generation)
    )
    """,
    """
    CREATE TABLE verifier_slots (
        slot_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        proposal_id TEXT NOT NULL,
        slot_index INTEGER NOT NULL CHECK (slot_index >= 0),
        state TEXT NOT NULL,
        session_id TEXT,
        attestation_ref TEXT,
        UNIQUE (proposal_id, slot_index)
    )
    """,
    """
    CREATE TABLE gate_decisions (
        gate_decision_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        input_digest TEXT NOT NULL UNIQUE,
        decision_ref TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE result_publications (
        task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
        result_digest TEXT NOT NULL UNIQUE,
        result_ref TEXT NOT NULL,
        published_from_version INTEGER NOT NULL,
        terminal_task_version INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)


SLICE_2_CONTROL_STATE_STATEMENTS = (
    """
    ALTER TABLE task_events ADD COLUMN event_digest TEXT NOT NULL
        DEFAULT '0000000000000000000000000000000000000000000000000000000000000000'
        CHECK (length(event_digest) = 64 AND event_digest = lower(event_digest))
    """,
    """
    ALTER TABLE tasks ADD COLUMN terminalization_reason TEXT CHECK (
        terminalization_reason IS NULL OR terminalization_reason IN (
            'DEADLINE_REACHED',
            'BUDGET_EXHAUSTED',
            'INTERACTION_UNAVAILABLE',
            'CAPABILITY_UNAVAILABLE',
            'RUNTIME_FAILURE',
            'STORAGE_FAILURE',
            'PROTOCOL_FAILURE',
            'POLICY_INVARIANT_BROKEN'
        )
    )
    """,
    """
    ALTER TABLE attempts ADD COLUMN transport_binding TEXT NOT NULL DEFAULT '{}'
    """,
    """
    ALTER TABLE attempts ADD COLUMN state_version INTEGER NOT NULL DEFAULT 1
        CHECK (state_version >= 1)
    """,
    """
    ALTER TABLE attempts ADD COLUMN predecessor_checkpoint_generation INTEGER
        CHECK (
            predecessor_checkpoint_generation IS NULL OR
            predecessor_checkpoint_generation >= 0
        )
    """,
    """
    ALTER TABLE attempts ADD COLUMN owner_session_id TEXT
    """,
    """
    ALTER TABLE attempts ADD COLUMN lease_acquired_at TEXT
    """,
    """
    ALTER TABLE attempts ADD COLUMN budget_reservation_id TEXT
    """,
    """
    CREATE INDEX task_events_version_idx ON task_events(task_id, task_version)
    """,
    """
    CREATE INDEX attempts_current_lookup_idx ON attempts(task_id, state)
    """,
)


GLOBAL_TASK_EVENT_IDEMPOTENCY_STATEMENTS = (
    """
    CREATE UNIQUE INDEX task_events_idempotency_key_unique
        ON task_events(idempotency_key)
    """,
)


MIGRATIONS = (
    Migration(1, "initial-shadow-store", INITIAL_STATEMENTS),
    Migration(2, "slice-2-control-state", SLICE_2_CONTROL_STATE_STATEMENTS),
    Migration(
        3,
        "slice-2-global-event-idempotency",
        GLOBAL_TASK_EVENT_IDEMPOTENCY_STATEMENTS,
    ),
)
