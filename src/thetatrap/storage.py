"""SQLite persistence for broker identity, strategy state, and audit records."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from thetatrap.errors import AccountIdentityError
from thetatrap.events import EventConfig
from thetatrap.security import redact


# ``schema_version`` is retained for the Checkpoint 1 compatibility contract.
# Additive runtime tables have their own migration marker.
SCHEMA_VERSION = "2"
RUNTIME_SCHEMA_VERSION = "5"

STRATEGY_STATES = frozenset(
    {
        "DISCOVERING",
        "SCREENING",
        "AI_REVIEW",
        "POLICY_CHECK",
        "SUBMITTING",
        "ORDER_PENDING",
        "CANCEL_PENDING",
        "POSITION_OPEN",
        "EXIT_SUBMITTING",
        "EXIT_PENDING",
        "RISK_OFF",
        "FLAT",
        "NO_TRADE",
        "ERROR",
    }
)
TERMINAL_STRATEGY_STATES = frozenset({"FLAT", "NO_TRADE"})
STRATEGY_TRANSITIONS: dict[str, frozenset[str]] = {
    "DISCOVERING": frozenset({"SCREENING", "NO_TRADE", "RISK_OFF", "ERROR"}),
    "SCREENING": frozenset({"AI_REVIEW", "NO_TRADE", "RISK_OFF", "ERROR"}),
    "AI_REVIEW": frozenset(
        {"POLICY_CHECK", "SCREENING", "NO_TRADE", "RISK_OFF", "ERROR"}
    ),
    "POLICY_CHECK": frozenset(
        {"SUBMITTING", "SCREENING", "NO_TRADE", "RISK_OFF", "ERROR"}
    ),
    "SUBMITTING": frozenset(
        {
            "ORDER_PENDING",
            "POSITION_OPEN",
            "CANCEL_PENDING",
            "RISK_OFF",
            "NO_TRADE",
            "ERROR",
        }
    ),
    "ORDER_PENDING": frozenset(
        {"POSITION_OPEN", "CANCEL_PENDING", "RISK_OFF", "ERROR"}
    ),
    "CANCEL_PENDING": frozenset(
        {"SCREENING", "POSITION_OPEN", "NO_TRADE", "RISK_OFF", "ERROR"}
    ),
    "POSITION_OPEN": frozenset({"EXIT_SUBMITTING", "RISK_OFF", "FLAT", "ERROR"}),
    "EXIT_SUBMITTING": frozenset({"EXIT_PENDING", "FLAT", "RISK_OFF", "ERROR"}),
    "EXIT_PENDING": frozenset({"FLAT", "RISK_OFF", "ERROR"}),
    "RISK_OFF": frozenset({"EXIT_SUBMITTING", "EXIT_PENDING", "FLAT", "ERROR"}),
    "ERROR": frozenset({"RISK_OFF", "NO_TRADE"}),
    "FLAT": frozenset(),
    "NO_TRADE": frozenset(),
}

ORDER_CHAIN_STATES = frozenset(
    {
        "PLANNED",
        "SUBMITTING",
        "UNKNOWN",
        "PENDING",
        "PARTIALLY_FILLED",
        "CANCEL_PENDING",
        "REPLACEMENT_PENDING",
        "CANCELED",
        "FILLED",
        "REJECTED",
        "EXPIRED",
        "ERROR",
    }
)
ORDER_CHAIN_TRANSITIONS: dict[str, frozenset[str]] = {
    "PLANNED": frozenset({"SUBMITTING", "ERROR"}),
    "SUBMITTING": frozenset(
        {
            "UNKNOWN",
            "PENDING",
            "PARTIALLY_FILLED",
            "CANCEL_PENDING",
            "CANCELED",
            "FILLED",
            "REJECTED",
            "EXPIRED",
            "ERROR",
        }
    ),
    "UNKNOWN": frozenset(
        {"PENDING", "PARTIALLY_FILLED", "CANCEL_PENDING", "CANCELED", "FILLED", "REJECTED", "EXPIRED", "ERROR"}
    ),
    "PENDING": frozenset(
        {
            "PARTIALLY_FILLED",
            "CANCEL_PENDING",
            "REPLACEMENT_PENDING",
            "CANCELED",
            "FILLED",
            "REJECTED",
            "EXPIRED",
            "ERROR",
        }
    ),
    "PARTIALLY_FILLED": frozenset(
        {"CANCEL_PENDING", "REPLACEMENT_PENDING", "CANCELED", "FILLED", "REJECTED", "EXPIRED", "ERROR"}
    ),
    "CANCEL_PENDING": frozenset(
        {"PARTIALLY_FILLED", "CANCELED", "FILLED", "REJECTED", "EXPIRED", "ERROR"}
    ),
    "REPLACEMENT_PENDING": frozenset(
        {"SUBMITTING", "UNKNOWN", "PENDING", "PARTIALLY_FILLED", "CANCELED", "FILLED", "REJECTED", "EXPIRED", "ERROR"}
    ),
    "ERROR": frozenset({"UNKNOWN", "CANCEL_PENDING", "REPLACEMENT_PENDING"}),
    "CANCELED": frozenset(),
    "FILLED": frozenset(),
    "REJECTED": frozenset(),
    "EXPIRED": frozenset(),
}

AGENT_TERMINAL_STATUSES = frozenset({"COMPLETED", "VETOED", "FAILED", "TIMED_OUT"})
AGENT_SMOKE_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "TIMED_OUT"})
ADVISORY_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "TIMED_OUT"})
ADVISORY_MODE = "READ_ONLY_REJECTED_CANDIDATE_ADVISORY"
ADVISORY_READ_TOOLS = frozenset(
    {
        "get_account_info",
        "get_account_config",
        "get_clock",
        "get_orders",
        "get_all_positions",
        "get_news",
    }
)


class StorageInvariantError(ValueError):
    """Raised when a durable identity, transition, or immutable record conflicts."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        # Keep SQLite's WAL/index sidecars after the last writer closes.  The
        # public dashboard mounts this directory read-only; SQLite can safely
        # open a WAL database there only when both sidecars already exist.
        # Python 3.12 exposes the matching SQLite database configuration, but
        # guard it for platforms whose bundled SQLite omits the constant.
        no_checkpoint_on_close = getattr(
            sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None
        )
        if no_checkpoint_on_close is not None:
            connection.setconfig(no_checkpoint_on_close, True)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            current = connection.execute(
                "SELECT value FROM metadata WHERE key='runtime_schema_version'"
            ).fetchone()
            if current is not None:
                try:
                    current_version = int(current["value"])
                except (TypeError, ValueError) as exc:
                    raise StorageInvariantError("invalid stored schema version") from exc
                if current_version > int(RUNTIME_SCHEMA_VERSION):
                    raise StorageInvariantError(
                        "database runtime schema "
                        f"{current_version} is newer than supported {RUNTIME_SCHEMA_VERSION}"
                    )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS event_definitions (
                    symbol TEXT PRIMARY KEY,
                    strategy_version TEXT NOT NULL,
                    source_published_on TEXT,
                    event_date TEXT NOT NULL,
                    release_timing TEXT NOT NULL,
                    conference_call_at TEXT,
                    status TEXT NOT NULL,
                    exclusion_reason TEXT,
                    source_url TEXT NOT NULL,
                    trade_expiration TEXT NOT NULL,
                    term_expiration TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mcp_sessions (
                    session_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL,
                    package_version TEXT NOT NULL,
                    server_name TEXT,
                    tool_count INTEGER,
                    required_schema_hash TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS mcp_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    called_at TEXT NOT NULL,
                    principal TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_json TEXT,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES mcp_sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS worker_heartbeat (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                    observed_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    account_suffix TEXT,
                    mcp_schema_hash TEXT,
                    market_is_open INTEGER,
                    detail_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    status TEXT,
                    equity TEXT,
                    buying_power TEXT
                );

                CREATE TABLE IF NOT EXISTS strategy_runs (
                    run_id TEXT PRIMARY KEY,
                    environment TEXT NOT NULL,
                    strategy_date TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE(environment, strategy_date, strategy_version)
                );

                CREATE TABLE IF NOT EXISTS strategy_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    transitioned_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES strategy_runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS collection_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    collection_type TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES strategy_runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    snapshot_id TEXT,
                    symbol TEXT NOT NULL,
                    candidate_rank INTEGER,
                    eligible INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES strategy_runs(run_id),
                    FOREIGN KEY(snapshot_id) REFERENCES collection_snapshots(snapshot_id),
                    UNIQUE(run_id, candidate_rank)
                );

                CREATE TABLE IF NOT EXISTS candidate_gate_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL,
                    evaluation_id TEXT NOT NULL,
                    gate_name TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    reason_code TEXT,
                    detail_json TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id),
                    UNIQUE(candidate_id, evaluation_id, gate_name)
                );

                CREATE TABLE IF NOT EXISTS agent_runs (
                    agent_run_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    candidate_id TEXT,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    result_json TEXT,
                    veto_reason TEXT,
                    error_type TEXT,
                    FOREIGN KEY(run_id) REFERENCES strategy_runs(run_id),
                    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
                );

                CREATE TABLE IF NOT EXISTS agent_tool_trace (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    principal TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_json TEXT,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    is_official_mcp INTEGER NOT NULL,
                    called_at TEXT NOT NULL,
                    FOREIGN KEY(agent_run_id) REFERENCES agent_runs(agent_run_id),
                    UNIQUE(agent_run_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS agent_smoke_runs (
                    smoke_run_id TEXT PRIMARY KEY,
                    environment TEXT NOT NULL,
                    account_suffix TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    result_json TEXT,
                    error_type TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_smoke_trace (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    smoke_run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    turn INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    result_hash TEXT,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    called_at TEXT NOT NULL,
                    FOREIGN KEY(smoke_run_id) REFERENCES agent_smoke_runs(smoke_run_id),
                    UNIQUE(smoke_run_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS advisory_runs (
                    advisory_run_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(
                        mode = 'READ_ONLY_REJECTED_CANDIDATE_ADVISORY'
                    ),
                    model TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('STARTED', 'COMPLETED', 'FAILED', 'TIMED_OUT')
                    ),
                    prompt_hash TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    result_json TEXT,
                    error_type TEXT,
                    FOREIGN KEY(run_id) REFERENCES strategy_runs(run_id),
                    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id),
                    UNIQUE(run_id)
                );

                CREATE TABLE IF NOT EXISTS advisory_tool_trace (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    advisory_run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL CHECK(sequence >= 0),
                    turn INTEGER NOT NULL CHECK(turn >= 1),
                    tool_name TEXT NOT NULL CHECK(tool_name IN (
                        'get_account_info',
                        'get_account_config',
                        'get_clock',
                        'get_orders',
                        'get_all_positions',
                        'get_news'
                    )),
                    arguments_hash TEXT NOT NULL CHECK(length(arguments_hash) = 64),
                    result_hash TEXT,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
                    called_at TEXT NOT NULL,
                    FOREIGN KEY(advisory_run_id)
                        REFERENCES advisory_runs(advisory_run_id),
                    UNIQUE(advisory_run_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS order_intents (
                    intent_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    candidate_id TEXT,
                    purpose TEXT NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES strategy_runs(run_id),
                    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
                );

                CREATE TABLE IF NOT EXISTS order_chains (
                    chain_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    intent_id TEXT NOT NULL UNIQUE,
                    purpose TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES strategy_runs(run_id),
                    FOREIGN KEY(intent_id) REFERENCES order_intents(intent_id)
                );

                CREATE TABLE IF NOT EXISTS order_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    chain_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE,
                    broker_order_id TEXT UNIQUE,
                    request_json TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(chain_id) REFERENCES order_chains(chain_id),
                    UNIQUE(chain_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS order_status_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chain_id TEXT NOT NULL,
                    attempt_id TEXT,
                    event_kind TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    broker_status TEXT,
                    detail_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    FOREIGN KEY(chain_id) REFERENCES order_chains(chain_id),
                    FOREIGN KEY(attempt_id) REFERENCES order_attempts(attempt_id)
                );

                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    chain_id TEXT NOT NULL,
                    attempt_id TEXT,
                    broker_order_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    filled_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(chain_id) REFERENCES order_chains(chain_id),
                    FOREIGN KEY(attempt_id) REFERENCES order_attempts(attempt_id)
                );

                CREATE TABLE IF NOT EXISTS broker_activities (
                    activity_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    activity_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES strategy_runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS position_observations (
                    observation_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    observed_at TEXT NOT NULL,
                    is_flat INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES strategy_runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS equity_observations (
                    observation_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    observed_at TEXT NOT NULL,
                    equity TEXT NOT NULL,
                    buying_power TEXT,
                    cash TEXT,
                    portfolio_value TEXT,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES strategy_runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS runtime_controls (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                    kill_switch_enabled INTEGER NOT NULL,
                    reason TEXT,
                    requested_by TEXT,
                    activated_at TEXT,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS control_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    control_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS entry_authorizations (
                    authorization_id TEXT PRIMARY KEY,
                    environment TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    strategy_date TEXT NOT NULL,
                    state TEXT NOT NULL
                        CHECK(state IN ('ARMED', 'CONSUMED', 'REVOKED')),
                    expires_at TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    armed_at TEXT NOT NULL,
                    consumed_at TEXT,
                    consumed_run_id TEXT,
                    consumed_intent_id TEXT,
                    consumed_chain_id TEXT,
                    consumed_attempt_id TEXT,
                    revoked_at TEXT,
                    revoked_by TEXT,
                    revoke_reason TEXT,
                    UNIQUE(environment, account_id, strategy_date),
                    FOREIGN KEY(consumed_run_id) REFERENCES strategy_runs(run_id),
                    FOREIGN KEY(consumed_intent_id) REFERENCES order_intents(intent_id),
                    FOREIGN KEY(consumed_chain_id) REFERENCES order_chains(chain_id),
                    FOREIGN KEY(consumed_attempt_id) REFERENCES order_attempts(attempt_id),
                    CHECK(
                        (
                            state = 'ARMED'
                            AND consumed_at IS NULL
                            AND consumed_run_id IS NULL
                            AND consumed_intent_id IS NULL
                            AND consumed_chain_id IS NULL
                            AND consumed_attempt_id IS NULL
                            AND revoked_at IS NULL
                            AND revoked_by IS NULL
                            AND revoke_reason IS NULL
                        )
                        OR (
                            state = 'CONSUMED'
                            AND consumed_at IS NOT NULL
                            AND consumed_run_id IS NOT NULL
                            AND consumed_intent_id IS NOT NULL
                            AND consumed_chain_id IS NOT NULL
                            AND consumed_attempt_id IS NOT NULL
                            AND revoked_at IS NULL
                            AND revoked_by IS NULL
                            AND revoke_reason IS NULL
                        )
                        OR (
                            state = 'REVOKED'
                            AND consumed_at IS NULL
                            AND consumed_run_id IS NULL
                            AND consumed_intent_id IS NULL
                            AND consumed_chain_id IS NULL
                            AND consumed_attempt_id IS NULL
                            AND revoked_at IS NOT NULL
                            AND revoked_by IS NOT NULL
                            AND revoke_reason IS NOT NULL
                        )
                    )
                );

                CREATE TABLE IF NOT EXISTS runtime_leases (
                    lease_name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_strategy_transitions_run
                    ON strategy_transitions(run_id, id);
                CREATE INDEX IF NOT EXISTS idx_snapshots_run_symbol
                    ON collection_snapshots(run_id, symbol, observed_at);
                CREATE INDEX IF NOT EXISTS idx_candidates_run
                    ON candidates(run_id, eligible, candidate_rank);
                CREATE INDEX IF NOT EXISTS idx_agent_trace_run
                    ON agent_tool_trace(agent_run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_agent_smoke_trace_run
                    ON agent_smoke_trace(smoke_run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_advisory_runs_strategy_run
                    ON advisory_runs(run_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_advisory_trace_run
                    ON advisory_tool_trace(advisory_run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_order_history_chain
                    ON order_status_history(chain_id, id);
                CREATE INDEX IF NOT EXISTS idx_broker_activities_run
                    ON broker_activities(run_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_position_observed_at
                    ON position_observations(observed_at);
                CREATE INDEX IF NOT EXISTS idx_equity_observed_at
                    ON equity_observations(observed_at);
                CREATE INDEX IF NOT EXISTS idx_entry_authorizations_latest
                    ON entry_authorizations(environment, account_id, armed_at DESC);

                CREATE TRIGGER IF NOT EXISTS order_intents_no_update
                BEFORE UPDATE ON order_intents
                BEGIN
                    SELECT RAISE(ABORT, 'order_intents are immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS order_intents_no_delete
                BEFORE DELETE ON order_intents
                BEGIN
                    SELECT RAISE(ABORT, 'order_intents are immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS strategy_transitions_no_update
                BEFORE UPDATE ON strategy_transitions
                BEGIN
                    SELECT RAISE(ABORT, 'strategy_transitions are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS strategy_transitions_no_delete
                BEFORE DELETE ON strategy_transitions
                BEGIN
                    SELECT RAISE(ABORT, 'strategy_transitions are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS agent_tool_trace_no_update
                BEFORE UPDATE ON agent_tool_trace
                BEGIN
                    SELECT RAISE(ABORT, 'agent_tool_trace is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS agent_tool_trace_no_delete
                BEFORE DELETE ON agent_tool_trace
                BEGIN
                    SELECT RAISE(ABORT, 'agent_tool_trace is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS agent_smoke_trace_no_update
                BEFORE UPDATE ON agent_smoke_trace
                BEGIN
                    SELECT RAISE(ABORT, 'agent_smoke_trace is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS agent_smoke_trace_no_delete
                BEFORE DELETE ON agent_smoke_trace
                BEGIN
                    SELECT RAISE(ABORT, 'agent_smoke_trace is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS advisory_tool_trace_no_update
                BEFORE UPDATE ON advisory_tool_trace
                BEGIN
                    SELECT RAISE(ABORT, 'advisory_tool_trace is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS advisory_tool_trace_no_delete
                BEFORE DELETE ON advisory_tool_trace
                BEGIN
                    SELECT RAISE(ABORT, 'advisory_tool_trace is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS fills_no_update
                BEFORE UPDATE ON fills
                BEGIN
                    SELECT RAISE(ABORT, 'fills are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS fills_no_delete
                BEFORE DELETE ON fills
                BEGIN
                    SELECT RAISE(ABORT, 'fills are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS broker_activities_no_update
                BEFORE UPDATE ON broker_activities
                BEGIN
                    SELECT RAISE(ABORT, 'broker_activities are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS broker_activities_no_delete
                BEFORE DELETE ON broker_activities
                BEGIN
                    SELECT RAISE(ABORT, 'broker_activities are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS order_status_history_no_update
                BEFORE UPDATE ON order_status_history
                BEGIN
                    SELECT RAISE(ABORT, 'order_status_history is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS order_status_history_no_delete
                BEFORE DELETE ON order_status_history
                BEGIN
                    SELECT RAISE(ABORT, 'order_status_history is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS control_events_no_update
                BEFORE UPDATE ON control_events
                BEGIN
                    SELECT RAISE(ABORT, 'control_events is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS control_events_no_delete
                BEFORE DELETE ON control_events
                BEGIN
                    SELECT RAISE(ABORT, 'control_events is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS entry_authorizations_guard_update
                BEFORE UPDATE ON entry_authorizations
                WHEN
                    OLD.authorization_id IS NOT NEW.authorization_id
                    OR OLD.environment IS NOT NEW.environment
                    OR OLD.account_id IS NOT NEW.account_id
                    OR OLD.strategy_date IS NOT NEW.strategy_date
                    OR OLD.expires_at IS NOT NEW.expires_at
                    OR OLD.requested_by IS NOT NEW.requested_by
                    OR OLD.reason IS NOT NEW.reason
                    OR OLD.armed_at IS NOT NEW.armed_at
                    OR OLD.state != 'ARMED'
                    OR NEW.state NOT IN ('CONSUMED', 'REVOKED')
                BEGIN
                    SELECT RAISE(ABORT, 'entry authorization transition is immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS entry_authorizations_no_delete
                BEFORE DELETE ON entry_authorizations
                BEGIN
                    SELECT RAISE(ABORT, 'entry authorizations cannot be deleted');
                END;
                """
            )
            now = utc_now()
            connection.execute(
                """
                INSERT INTO runtime_controls(
                    singleton_id, kill_switch_enabled, updated_at, version
                ) VALUES (1, 0, ?, 0)
                ON CONFLICT(singleton_id) DO NOTHING
                """,
                (now,),
            )
            _migrate_event_definitions(connection)
            self._set_metadata(connection, "schema_version", SCHEMA_VERSION)
            self._set_metadata(connection, "runtime_schema_version", RUNTIME_SCHEMA_VERSION)

    def _set_metadata(self, connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            """
            INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, utc_now()),
        )

    def get_metadata(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def bind_identity(
        self, environment: str, expected_account_id: str, observed_account_id: str
    ) -> None:
        if expected_account_id != observed_account_id:
            raise AccountIdentityError("MCP account ID does not match THETATRAP_EXPECTED_ACCOUNT_ID")
        with self.connect() as connection:
            existing_environment = connection.execute(
                "SELECT value FROM metadata WHERE key='environment'"
            ).fetchone()
            existing_account = connection.execute(
                "SELECT value FROM metadata WHERE key='account_id'"
            ).fetchone()
            if existing_environment and existing_environment["value"] != environment:
                raise AccountIdentityError("database is already bound to another environment")
            if existing_account and existing_account["value"] != observed_account_id:
                raise AccountIdentityError("database is already bound to another Alpaca account")
            self._set_metadata(connection, "environment", environment)
            self._set_metadata(connection, "account_id", observed_account_id)

    def arm_entry_authorization(
        self,
        authorization_id: str,
        environment: str,
        account_id: str,
        strategy_date: str,
        expires_at: str | datetime,
        requested_by: str,
        reason: str,
        armed_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Create the sole entry authorization for an account and strategy date."""

        _require_value("authorization_id", authorization_id)
        _require_value("environment", environment)
        _require_value("account_id", account_id)
        normalized_date = _strategy_date(strategy_date)
        _require_value("requested_by", requested_by)
        _require_value("reason", reason)
        armed_when = _timestamp(armed_at)
        expires_when = _timestamp(expires_at)
        if _parse_timestamp(expires_when) <= _parse_timestamp(armed_when):
            raise StorageInvariantError("entry authorization expiry must be after arming")

        expected = {
            "authorization_id": authorization_id,
            "environment": environment,
            "account_id": account_id,
            "strategy_date": normalized_date,
            "expires_at": expires_when,
            "requested_by": requested_by,
            "reason": reason,
            "armed_at": armed_when,
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_metadata_identity_locked(connection, environment, account_id)
            if self._kill_switch_enabled_locked(connection):
                raise StorageInvariantError(
                    "kill switch blocks entry authorization"
                )
            existing_by_id = connection.execute(
                "SELECT * FROM entry_authorizations WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
            existing_for_date = connection.execute(
                """
                SELECT * FROM entry_authorizations
                WHERE environment=? AND account_id=? AND strategy_date=?
                """,
                (environment, account_id, normalized_date),
            ).fetchone()
            existing = existing_by_id or existing_for_date
            if existing is not None:
                if existing["authorization_id"] != authorization_id:
                    raise StorageInvariantError(
                        "entry authorization already exists for environment/account/date"
                    )
                stable_expected = dict(expected)
                if armed_at is None:
                    stable_expected.pop("armed_at")
                _assert_record_matches(
                    existing, stable_expected, "entry authorization"
                )
                if existing["state"] != "ARMED":
                    raise StorageInvariantError("entry authorization cannot be re-armed")
                return _entry_authorization_dict(existing)
            try:
                connection.execute(
                    """
                    INSERT INTO entry_authorizations(
                        authorization_id, environment, account_id, strategy_date,
                        state, expires_at, requested_by, reason, armed_at
                    ) VALUES (?, ?, ?, ?, 'ARMED', ?, ?, ?, ?)
                    """,
                    tuple(expected.values()),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageInvariantError(
                    "entry authorization identity or account/date conflicts"
                ) from exc
            row = connection.execute(
                "SELECT * FROM entry_authorizations WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
        return _entry_authorization_dict(
            _required_row(row, "created entry authorization")
        )

    def get_entry_authorization(
        self, environment: str, account_id: str, strategy_date: str
    ) -> dict[str, Any] | None:
        _require_value("environment", environment)
        _require_value("account_id", account_id)
        normalized_date = _strategy_date(strategy_date)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM entry_authorizations
                WHERE environment=? AND account_id=? AND strategy_date=?
                """,
                (environment, account_id, normalized_date),
            ).fetchone()
        return _entry_authorization_dict(row) if row is not None else None

    def latest_entry_authorization(
        self, environment: str, account_id: str
    ) -> dict[str, Any] | None:
        _require_value("environment", environment)
        _require_value("account_id", account_id)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM entry_authorizations
                WHERE environment=? AND account_id=?
                ORDER BY armed_at DESC, authorization_id DESC
                LIMIT 1
                """,
                (environment, account_id),
            ).fetchone()
        return _entry_authorization_dict(row) if row is not None else None

    def revoke_entry_authorization(
        self,
        authorization_id: str,
        reason: str,
        requested_by: str,
        revoked_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        _require_value("authorization_id", authorization_id)
        _require_value("reason", reason)
        _require_value("requested_by", requested_by)
        revoked_when = _timestamp(revoked_at)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _required_row(
                connection.execute(
                    "SELECT * FROM entry_authorizations WHERE authorization_id=?",
                    (authorization_id,),
                ).fetchone(),
                f"entry authorization {authorization_id}",
            )
            self._assert_metadata_identity_locked(
                connection, str(row["environment"]), str(row["account_id"])
            )
            if row["state"] == "CONSUMED":
                raise StorageInvariantError(
                    "consumed entry authorization cannot be revoked"
                )
            if row["state"] == "REVOKED":
                if row["revoke_reason"] != reason or row["revoked_by"] != requested_by:
                    raise StorageInvariantError(
                        "immutable revoked entry authorization conflicts"
                    )
                if revoked_at is not None and row["revoked_at"] != revoked_when:
                    raise StorageInvariantError(
                        "immutable revoked entry authorization conflicts"
                    )
                return _entry_authorization_dict(row)
            if _parse_timestamp(revoked_when) < _parse_timestamp(str(row["armed_at"])):
                raise StorageInvariantError("entry authorization cannot predate arming")
            updated = connection.execute(
                """
                UPDATE entry_authorizations
                SET state='REVOKED', revoked_at=?, revoked_by=?, revoke_reason=?
                WHERE authorization_id=? AND state='ARMED'
                """,
                (revoked_when, requested_by, reason, authorization_id),
            )
            if updated.rowcount != 1:
                raise StorageInvariantError(
                    "entry authorization state changed concurrently"
                )
            result = connection.execute(
                "SELECT * FROM entry_authorizations WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
        return _entry_authorization_dict(
            _required_row(result, "revoked entry authorization")
        )

    def begin_authorized_entry_submission(
        self,
        authorization_id: str,
        environment: str,
        account_id: str,
        strategy_date: str,
        run_id: str,
        intent_id: str,
        chain_id: str,
        attempt_id: str,
        client_order_id: str,
        request: dict[str, Any],
        observed_at: str | datetime,
    ) -> dict[str, Any]:
        """Atomically consume one authorization and durably begin one entry."""

        for name, value in (
            ("authorization_id", authorization_id),
            ("environment", environment),
            ("account_id", account_id),
            ("run_id", run_id),
            ("intent_id", intent_id),
            ("chain_id", chain_id),
            ("attempt_id", attempt_id),
            ("client_order_id", client_order_id),
        ):
            _require_value(name, value)
        normalized_date = _strategy_date(strategy_date)
        when = _timestamp(observed_at)
        observed = _parse_timestamp(when)
        request_json = _canonical_json(request)
        request_hash = _hash_json(request_json)

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_metadata_identity_locked(connection, environment, account_id)
            authorization = _required_row(
                connection.execute(
                    "SELECT * FROM entry_authorizations WHERE authorization_id=?",
                    (authorization_id,),
                ).fetchone(),
                f"entry authorization {authorization_id}",
            )
            if (
                authorization["environment"] != environment
                or authorization["account_id"] != account_id
                or authorization["strategy_date"] != normalized_date
            ):
                raise StorageInvariantError(
                    "entry authorization does not match environment/account/date"
                )
            if authorization["state"] != "ARMED":
                raise StorageInvariantError(
                    f"entry authorization is not ARMED: {authorization['state']}"
                )
            if observed < _parse_timestamp(str(authorization["armed_at"])):
                raise StorageInvariantError("entry authorization is not active yet")
            if observed >= _parse_timestamp(str(authorization["expires_at"])):
                raise StorageInvariantError("entry authorization has expired")
            if self._kill_switch_enabled_locked(connection):
                raise StorageInvariantError("kill switch blocks authorized entry")

            run = _required_row(
                connection.execute(
                    "SELECT * FROM strategy_runs WHERE run_id=?", (run_id,)
                ).fetchone(),
                f"strategy run {run_id}",
            )
            if (
                run["environment"] != environment
                or run["strategy_date"] != normalized_date
            ):
                raise StorageInvariantError(
                    "strategy run does not match entry authorization"
                )
            if run["state"] != "POLICY_CHECK":
                raise StorageInvariantError(
                    "authorized entry requires a POLICY_CHECK strategy run"
                )

            intent = _required_row(
                connection.execute(
                    "SELECT * FROM order_intents WHERE intent_id=?", (intent_id,)
                ).fetchone(),
                f"order intent {intent_id}",
            )
            if (
                intent["run_id"] != run_id
                or intent["purpose"] != "entry"
                or intent["client_order_id"] != client_order_id
                or intent["payload_json"] != request_json
                or intent["payload_hash"] != request_hash
            ):
                raise StorageInvariantError(
                    "authorized entry does not exactly match its durable entry intent"
                )

            chain = _required_row(
                connection.execute(
                    "SELECT * FROM order_chains WHERE chain_id=?", (chain_id,)
                ).fetchone(),
                f"order chain {chain_id}",
            )
            if (
                chain["run_id"] != run_id
                or chain["intent_id"] != intent_id
                or chain["purpose"] != "entry"
            ):
                raise StorageInvariantError(
                    "authorized entry does not exactly match its order chain"
                )
            if chain["state"] != "PLANNED":
                raise StorageInvariantError(
                    "authorized entry requires a PLANNED order chain"
                )

            collision = connection.execute(
                """
                SELECT attempt_id FROM order_attempts
                WHERE attempt_id=? OR client_order_id=?
                   OR (chain_id=? AND sequence=0)
                LIMIT 1
                """,
                (attempt_id, client_order_id, chain_id),
            ).fetchone()
            if collision is not None:
                raise StorageInvariantError(
                    "authorized entry sequence-0 attempt already exists"
                )

            try:
                connection.execute(
                    """
                    INSERT INTO order_attempts(
                        attempt_id, chain_id, sequence, client_order_id,
                        broker_order_id, request_json, request_hash, created_at
                    ) VALUES (?, ?, 0, ?, NULL, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        chain_id,
                        client_order_id,
                        request_json,
                        request_hash,
                        when,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageInvariantError(
                    "authorized entry attempt identity conflicts"
                ) from exc

            self._transition_strategy_run_locked(
                connection,
                run_id,
                "SUBMITTING",
                "ENTRY_AUTHORIZATION_CONSUMED",
                _audit_json(
                    {
                        "authorization_id": authorization_id,
                        "intent_id": intent_id,
                        "chain_id": chain_id,
                        "attempt_id": attempt_id,
                    }
                ),
                when,
            )
            chain_updated = connection.execute(
                """
                UPDATE order_chains SET state='SUBMITTING', updated_at=?
                WHERE chain_id=? AND state='PLANNED'
                """,
                (when, chain_id),
            )
            if chain_updated.rowcount != 1:
                raise StorageInvariantError("order chain state changed concurrently")
            connection.execute(
                """
                INSERT INTO order_status_history(
                    chain_id, attempt_id, event_kind, from_state, to_state,
                    broker_status, detail_json, observed_at
                ) VALUES (?, ?, 'transition', 'PLANNED', 'SUBMITTING', NULL, ?, ?)
                """,
                (
                    chain_id,
                    attempt_id,
                    _audit_json(
                        {
                            "authorization_id": authorization_id,
                            "intent_id": intent_id,
                        }
                    ),
                    when,
                ),
            )
            consumed = connection.execute(
                """
                UPDATE entry_authorizations
                SET state='CONSUMED', consumed_at=?, consumed_run_id=?,
                    consumed_intent_id=?, consumed_chain_id=?, consumed_attempt_id=?
                WHERE authorization_id=? AND state='ARMED'
                """,
                (
                    when,
                    run_id,
                    intent_id,
                    chain_id,
                    attempt_id,
                    authorization_id,
                ),
            )
            if consumed.rowcount != 1:
                raise StorageInvariantError(
                    "entry authorization state changed concurrently"
                )
            result = connection.execute(
                "SELECT * FROM entry_authorizations WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
        return _entry_authorization_dict(
            _required_row(result, "consumed entry authorization")
        )

    def record_initial_admission(self, *, environment: str, equity: str) -> None:
        """Persist the first admitted equity once, rejecting later identity drift."""

        value = _canonical_json({"environment": environment, "equity": str(equity)})
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT value FROM metadata WHERE key='initial_admission'"
            ).fetchone()
            if existing is not None and existing["value"] != value:
                raise StorageInvariantError("initial admission evidence is immutable")
            if existing is None:
                self._set_metadata(connection, "initial_admission", value)

    def upsert_events(self, config: EventConfig) -> None:
        now = utc_now()
        with self.connect() as connection:
            for event in config.events:
                connection.execute(
                    """
                    INSERT INTO event_definitions(
                        symbol, strategy_version, source_published_on, event_date,
                        release_timing, conference_call_at, status, exclusion_reason,
                        source_url, trade_expiration, term_expiration, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        strategy_version=excluded.strategy_version,
                        source_published_on=excluded.source_published_on,
                        event_date=excluded.event_date,
                        release_timing=excluded.release_timing,
                        conference_call_at=excluded.conference_call_at,
                        status=excluded.status,
                        exclusion_reason=excluded.exclusion_reason,
                        source_url=excluded.source_url,
                        trade_expiration=excluded.trade_expiration,
                        term_expiration=excluded.term_expiration,
                        updated_at=excluded.updated_at
                    """,
                    (
                        event.symbol,
                        config.strategy_version,
                        event.source_published_on.isoformat(),
                        event.event_date.isoformat(),
                        event.release_timing,
                        event.conference_call_at.isoformat(),
                        event.status,
                        event.exclusion_reason,
                        str(event.source_url),
                        config.trade_expiration.isoformat(),
                        config.term_expiration.isoformat(),
                        now,
                    ),
                )

    def start_mcp_session(
        self,
        session_id: str,
        package_version: str,
        server_name: str,
        tool_count: int,
        required_schema_hash: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO mcp_sessions(
                    session_id, started_at, status, package_version, server_name,
                    tool_count, required_schema_hash
                ) VALUES (?, ?, 'connected', ?, ?, ?, ?)
                """,
                (
                    session_id,
                    utc_now(),
                    package_version,
                    server_name,
                    tool_count,
                    required_schema_hash,
                ),
            )

    def finish_mcp_session(self, session_id: str, status: str, error: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE mcp_sessions SET ended_at=?, status=?, error=? WHERE session_id=?
                """,
                (utc_now(), status, error, session_id),
            )

    def record_mcp_call(
        self,
        session_id: str,
        principal: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        status: str,
        duration_ms: int,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO mcp_calls(
                    session_id, called_at, principal, tool_name, arguments_json,
                    result_json, status, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    utc_now(),
                    principal,
                    tool_name,
                    json.dumps(redact(arguments), separators=(",", ":"), default=str),
                    json.dumps(redact(result), separators=(",", ":"), default=str)
                    if result is not None
                    else None,
                    status,
                    duration_ms,
                ),
            )

    def record_account_snapshot(self, account: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO account_snapshots(
                    observed_at, account_id, status, equity, buying_power
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    str(account["id"]),
                    _optional_string(account.get("status")),
                    _optional_string(account.get("equity")),
                    _optional_string(account.get("buying_power")),
                ),
            )

    def record_heartbeat(
        self,
        *,
        status: str,
        environment: str,
        account_suffix: str | None,
        mcp_schema_hash: str | None,
        market_is_open: bool | None,
        detail: dict[str, Any],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO worker_heartbeat(
                    singleton_id, observed_at, status, environment, account_suffix,
                    mcp_schema_hash, market_is_open, detail_json
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    observed_at=excluded.observed_at,
                    status=excluded.status,
                    environment=excluded.environment,
                    account_suffix=excluded.account_suffix,
                    mcp_schema_hash=excluded.mcp_schema_hash,
                    market_is_open=excluded.market_is_open,
                    detail_json=excluded.detail_json
                """,
                (
                    utc_now(),
                    status,
                    environment,
                    account_suffix,
                    mcp_schema_hash,
                    int(market_is_open) if market_is_open is not None else None,
                    json.dumps(redact(detail), separators=(",", ":"), default=str),
                ),
            )

    def create_strategy_run(
        self,
        run_id: str,
        *,
        environment: str,
        strategy_date: str,
        strategy_version: str,
        config_hash: str,
        context: dict[str, Any] | None = None,
        initial_state: str = "DISCOVERING",
    ) -> dict[str, Any]:
        _require_value("run_id", run_id)
        _require_state(initial_state, STRATEGY_STATES, "strategy")
        context_json = _audit_json(context or {})
        now = utc_now()
        expected = {
            "run_id": run_id,
            "environment": environment,
            "strategy_date": strategy_date,
            "strategy_version": strategy_version,
            "config_hash": config_hash,
            "context_json": context_json,
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM strategy_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing is not None:
                _assert_record_matches(existing, expected, "strategy run")
                return _strategy_run_dict(existing)
            identity_collision = connection.execute(
                """
                SELECT run_id FROM strategy_runs
                WHERE environment=? AND strategy_date=? AND strategy_version=?
                """,
                (environment, strategy_date, strategy_version),
            ).fetchone()
            if identity_collision is not None:
                raise StorageInvariantError(
                    "canonical strategy run already exists for environment/date/version"
                )
            connection.execute(
                """
                INSERT INTO strategy_runs(
                    run_id, environment, strategy_date, strategy_version, config_hash,
                    state, context_json, created_at, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    environment,
                    strategy_date,
                    strategy_version,
                    config_hash,
                    initial_state,
                    context_json,
                    now,
                    now,
                    now if initial_state in TERMINAL_STRATEGY_STATES else None,
                ),
            )
            connection.execute(
                """
                INSERT INTO strategy_transitions(
                    run_id, from_state, to_state, reason_code, evidence_json,
                    transitioned_at
                ) VALUES (?, NULL, ?, 'RUN_CREATED', '{}', ?)
                """,
                (run_id, initial_state, now),
            )
            row = connection.execute(
                "SELECT * FROM strategy_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return _strategy_run_dict(_required_row(row, "created strategy run"))

    def get_strategy_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return _strategy_run_dict(row) if row is not None else None

    def find_strategy_run(
        self, *, environment: str, strategy_date: str, strategy_version: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM strategy_runs
                WHERE environment=? AND strategy_date=? AND strategy_version=?
                """,
                (environment, strategy_date, strategy_version),
            ).fetchone()
        return _strategy_run_dict(row) if row is not None else None

    def find_active_strategy_run(self, *, environment: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM strategy_runs
                WHERE environment=? AND state NOT IN ('FLAT', 'NO_TRADE')
                ORDER BY strategy_date DESC, created_at DESC LIMIT 1
                """,
                (environment,),
            ).fetchone()
        return _strategy_run_dict(row) if row is not None else None

    def transition_strategy_run(
        self,
        run_id: str,
        to_state: str,
        reason_code: str,
        evidence: dict[str, Any] | None = None,
        *,
        transitioned_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        _require_state(to_state, STRATEGY_STATES, "strategy")
        _require_value("reason_code", reason_code)
        when = _timestamp(transitioned_at)
        evidence_json = _audit_json(evidence or {})
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._transition_strategy_run_locked(
                connection,
                run_id,
                to_state,
                reason_code,
                evidence_json,
                when,
            )
        return _strategy_run_dict(row)

    def _transition_strategy_run_locked(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        to_state: str,
        reason_code: str,
        evidence_json: str,
        when: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM strategy_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        row = _required_row(row, f"strategy run {run_id}")
        from_state = str(row["state"])
        if from_state == to_state:
            return row
        if to_state not in STRATEGY_TRANSITIONS[from_state]:
            raise StorageInvariantError(
                f"invalid strategy transition {from_state} -> {to_state}"
            )
        if to_state == "SUBMITTING" and self._kill_switch_enabled_locked(connection):
            raise StorageInvariantError("kill switch blocks transition to SUBMITTING")
        finished_at = when if to_state in TERMINAL_STRATEGY_STATES else None
        updated = connection.execute(
            """
            UPDATE strategy_runs
            SET state=?, updated_at=?, finished_at=?
            WHERE run_id=? AND state=?
            """,
            (to_state, when, finished_at, run_id, from_state),
        )
        if updated.rowcount != 1:
            raise StorageInvariantError("strategy state changed concurrently")
        connection.execute(
            """
            INSERT INTO strategy_transitions(
                run_id, from_state, to_state, reason_code, evidence_json,
                transitioned_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, from_state, to_state, reason_code, evidence_json, when),
        )
        return _required_row(
            connection.execute(
                "SELECT * FROM strategy_runs WHERE run_id=?", (run_id,)
            ).fetchone(),
            f"strategy run {run_id}",
        )

    def list_strategy_transitions(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM strategy_transitions WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [_json_row(row, "evidence_json") for row in rows]

    def record_collection_snapshot(
        self,
        snapshot_id: str,
        *,
        run_id: str,
        symbol: str,
        collection_type: str,
        observed_at: str | datetime,
        payload: Any,
    ) -> dict[str, Any]:
        _require_value("snapshot_id", snapshot_id)
        payload_json = _audit_json(payload)
        expected = {
            "snapshot_id": snapshot_id,
            "run_id": run_id,
            "symbol": symbol,
            "collection_type": collection_type,
            "observed_at": _timestamp(observed_at),
            "payload_json": payload_json,
            "payload_hash": _hash_json(payload_json),
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM collection_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if existing is not None:
                _assert_record_matches(existing, expected, "collection snapshot")
                return _json_row(existing, "payload_json")
            connection.execute(
                """
                INSERT INTO collection_snapshots(
                    snapshot_id, run_id, symbol, collection_type, observed_at,
                    payload_json, payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*expected.values(), utc_now()),
            )
            row = connection.execute(
                "SELECT * FROM collection_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        return _json_row(_required_row(row, "collection snapshot"), "payload_json")

    def record_candidate(
        self,
        candidate_id: str,
        *,
        run_id: str,
        symbol: str,
        eligible: bool,
        payload: dict[str, Any],
        snapshot_id: str | None = None,
        candidate_rank: int | None = None,
    ) -> dict[str, Any]:
        if candidate_rank is not None and candidate_rank < 1:
            raise StorageInvariantError("candidate_rank must be positive")
        payload_json = _audit_json(payload)
        expected = {
            "candidate_id": candidate_id,
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "symbol": symbol,
            "candidate_rank": candidate_rank,
            "eligible": int(eligible),
            "payload_json": payload_json,
            "payload_hash": _hash_json(payload_json),
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if snapshot_id is not None:
                snapshot = _required_row(
                    connection.execute(
                        "SELECT run_id, symbol FROM collection_snapshots WHERE snapshot_id=?",
                        (snapshot_id,),
                    ).fetchone(),
                    f"collection snapshot {snapshot_id}",
                )
                if snapshot["run_id"] != run_id or snapshot["symbol"] != symbol:
                    raise StorageInvariantError(
                        "candidate snapshot belongs to another run or symbol"
                    )
            existing = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if existing is not None:
                _assert_record_matches(existing, expected, "candidate")
                return _candidate_dict(existing)
            try:
                connection.execute(
                    """
                    INSERT INTO candidates(
                        candidate_id, run_id, snapshot_id, symbol, candidate_rank,
                        eligible, payload_json, payload_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*expected.values(), utc_now()),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageInvariantError("candidate identity or rank conflicts") from exc
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
        return _candidate_dict(_required_row(row, "candidate"))

    def list_candidates(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM candidates WHERE run_id=?
                ORDER BY candidate_rank IS NULL, candidate_rank, symbol, candidate_id
                """,
                (run_id,),
            ).fetchall()
        return [_candidate_dict(row) for row in rows]

    def record_gate_result(
        self,
        candidate_id: str,
        evaluation_id: str,
        gate_name: str,
        *,
        passed: bool,
        reason_code: str | None = None,
        detail: dict[str, Any] | None = None,
        evaluated_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        _require_value("evaluation_id", evaluation_id)
        _require_value("gate_name", gate_name)
        detail_json = _audit_json(detail or {})
        expected = {
            "candidate_id": candidate_id,
            "evaluation_id": evaluation_id,
            "gate_name": gate_name,
            "passed": int(passed),
            "reason_code": reason_code,
            "detail_json": detail_json,
            "evaluated_at": _timestamp(evaluated_at),
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM candidate_gate_results
                WHERE candidate_id=? AND evaluation_id=? AND gate_name=?
                """,
                (candidate_id, evaluation_id, gate_name),
            ).fetchone()
            if existing is not None:
                stable_expected = dict(expected)
                stable_expected.pop("evaluated_at")
                _assert_record_matches(existing, stable_expected, "gate result")
                return _gate_result_dict(existing)
            connection.execute(
                """
                INSERT INTO candidate_gate_results(
                    candidate_id, evaluation_id, gate_name, passed, reason_code,
                    detail_json, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(expected.values()),
            )
            row = connection.execute(
                """
                SELECT * FROM candidate_gate_results
                WHERE candidate_id=? AND evaluation_id=? AND gate_name=?
                """,
                (candidate_id, evaluation_id, gate_name),
            ).fetchone()
        return _gate_result_dict(_required_row(row, "gate result"))

    def list_gate_results(
        self, candidate_id: str, evaluation_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM candidate_gate_results WHERE candidate_id=?"
        parameters: list[Any] = [candidate_id]
        if evaluation_id is not None:
            query += " AND evaluation_id=?"
            parameters.append(evaluation_id)
        query += " ORDER BY id"
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_gate_result_dict(row) for row in rows]

    def start_agent_run(
        self,
        agent_run_id: str,
        *,
        run_id: str,
        model: str,
        prompt_hash: str,
        config_hash: str,
        candidate_id: str | None = None,
        started_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        when = _timestamp(started_at)
        expected = {
            "agent_run_id": agent_run_id,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "model": model,
            "prompt_hash": prompt_hash,
            "config_hash": config_hash,
            "started_at": when,
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if candidate_id is not None:
                candidate = _required_row(
                    connection.execute(
                        "SELECT run_id FROM candidates WHERE candidate_id=?", (candidate_id,)
                    ).fetchone(),
                    f"candidate {candidate_id}",
                )
                if candidate["run_id"] != run_id:
                    raise StorageInvariantError("agent candidate belongs to another run")
            existing = connection.execute(
                "SELECT * FROM agent_runs WHERE agent_run_id=?", (agent_run_id,)
            ).fetchone()
            if existing is not None:
                stable_expected = dict(expected)
                stable_expected.pop("started_at")
                _assert_record_matches(existing, stable_expected, "agent run")
                return _agent_run_dict(existing)
            connection.execute(
                """
                INSERT INTO agent_runs(
                    agent_run_id, run_id, candidate_id, model, status, prompt_hash,
                    config_hash, started_at
                ) VALUES (?, ?, ?, ?, 'STARTED', ?, ?, ?)
                """,
                tuple(expected.values()),
            )
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE agent_run_id=?", (agent_run_id,)
            ).fetchone()
        return _agent_run_dict(_required_row(row, "agent run"))

    def finish_agent_run(
        self,
        agent_run_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        veto_reason: str | None = None,
        error_type: str | None = None,
        ended_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        if status not in AGENT_TERMINAL_STATUSES:
            raise StorageInvariantError(f"invalid terminal agent status: {status}")
        when = _timestamp(ended_at)
        result_json = _audit_json(result) if result is not None else None
        expected_terminal = {
            "status": status,
            "result_json": result_json,
            "veto_reason": veto_reason,
            "error_type": error_type,
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = _required_row(
                connection.execute(
                    "SELECT * FROM agent_runs WHERE agent_run_id=?", (agent_run_id,)
                ).fetchone(),
                f"agent run {agent_run_id}",
            )
            if existing["status"] in AGENT_TERMINAL_STATUSES:
                _assert_record_matches(existing, expected_terminal, "finished agent run")
                return _agent_run_dict(existing)
            if existing["status"] != "STARTED":
                raise StorageInvariantError(
                    f"agent run cannot finish from status {existing['status']}"
                )
            connection.execute(
                """
                UPDATE agent_runs
                SET status=?, ended_at=?, result_json=?, veto_reason=?, error_type=?
                WHERE agent_run_id=? AND status='STARTED'
                """,
                (status, when, result_json, veto_reason, error_type, agent_run_id),
            )
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE agent_run_id=?", (agent_run_id,)
            ).fetchone()
        return _agent_run_dict(_required_row(row, "finished agent run"))

    def record_agent_tool_call(
        self,
        agent_run_id: str,
        sequence: int,
        *,
        principal: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        status: str,
        duration_ms: int,
        is_official_mcp: bool,
        called_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        if sequence < 0 or duration_ms < 0:
            raise StorageInvariantError("tool sequence and duration must be non-negative")
        expected = {
            "agent_run_id": agent_run_id,
            "sequence": sequence,
            "principal": principal,
            "tool_name": tool_name,
            "arguments_json": _audit_json(arguments),
            "result_json": _audit_json(result) if result is not None else None,
            "status": status,
            "duration_ms": duration_ms,
            "is_official_mcp": int(is_official_mcp),
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM agent_tool_trace WHERE agent_run_id=? AND sequence=?",
                (agent_run_id, sequence),
            ).fetchone()
            if existing is not None:
                _assert_record_matches(existing, expected, "agent tool trace")
                return _agent_tool_dict(existing)
            connection.execute(
                """
                INSERT INTO agent_tool_trace(
                    agent_run_id, sequence, principal, tool_name, arguments_json,
                    result_json, status, duration_ms, is_official_mcp, called_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*expected.values(), _timestamp(called_at)),
            )
            row = connection.execute(
                "SELECT * FROM agent_tool_trace WHERE agent_run_id=? AND sequence=?",
                (agent_run_id, sequence),
            ).fetchone()
        return _agent_tool_dict(_required_row(row, "agent tool trace"))

    def list_agent_tool_calls(self, agent_run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_tool_trace WHERE agent_run_id=? ORDER BY sequence",
                (agent_run_id,),
            ).fetchall()
        return [_agent_tool_dict(row) for row in rows]

    def latest_agent_run_for_strategy(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_runs
                WHERE run_id=?
                ORDER BY started_at DESC, agent_run_id DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return _agent_run_dict(row) if row is not None else None

    def start_advisory_run(
        self,
        advisory_run_id: str,
        *,
        run_id: str,
        candidate_id: str,
        model: str,
        prompt_hash: str,
        config_hash: str,
        mode: str = ADVISORY_MODE,
        started_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Start the sole non-authorizing advisory allowed for a strategy run."""

        if mode != ADVISORY_MODE:
            raise StorageInvariantError("invalid advisory mode")
        expected = {
            "advisory_run_id": advisory_run_id,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "mode": mode,
            "model": model,
            "prompt_hash": prompt_hash,
            "config_hash": config_hash,
            "started_at": _timestamp(started_at),
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidate = _required_row(
                connection.execute(
                    "SELECT run_id, eligible FROM candidates WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone(),
                f"advisory candidate {candidate_id}",
            )
            if candidate["run_id"] != run_id:
                raise StorageInvariantError(
                    "advisory candidate belongs to another strategy run"
                )
            if bool(candidate["eligible"]):
                raise StorageInvariantError(
                    "advisory can only review a deterministically rejected candidate"
                )
            existing = connection.execute(
                "SELECT * FROM advisory_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing is not None:
                stable_expected = dict(expected)
                stable_expected.pop("started_at")
                _assert_record_matches(existing, stable_expected, "advisory run")
                return _advisory_run_dict(existing)
            connection.execute(
                """
                INSERT INTO advisory_runs(
                    advisory_run_id, run_id, candidate_id, mode, model, status,
                    prompt_hash, config_hash, started_at
                ) VALUES (?, ?, ?, ?, ?, 'STARTED', ?, ?, ?)
                """,
                tuple(expected.values()),
            )
            row = connection.execute(
                "SELECT * FROM advisory_runs WHERE advisory_run_id=?",
                (advisory_run_id,),
            ).fetchone()
        return _advisory_run_dict(_required_row(row, "advisory run"))

    def finish_advisory_run(
        self,
        advisory_run_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error_type: str | None = None,
        ended_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        if status not in ADVISORY_TERMINAL_STATUSES:
            raise StorageInvariantError(f"invalid terminal advisory status: {status}")
        result_json = _audit_json(result) if result is not None else None
        expected_terminal = {
            "status": status,
            "result_json": result_json,
            "error_type": error_type,
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = _required_row(
                connection.execute(
                    "SELECT * FROM advisory_runs WHERE advisory_run_id=?",
                    (advisory_run_id,),
                ).fetchone(),
                f"advisory run {advisory_run_id}",
            )
            if existing["status"] in ADVISORY_TERMINAL_STATUSES:
                _assert_record_matches(
                    existing, expected_terminal, "finished advisory run"
                )
                return _advisory_run_dict(existing)
            if existing["status"] != "STARTED":
                raise StorageInvariantError(
                    f"advisory run cannot finish from status {existing['status']}"
                )
            connection.execute(
                """
                UPDATE advisory_runs
                SET status=?, ended_at=?, result_json=?, error_type=?
                WHERE advisory_run_id=? AND status='STARTED'
                """,
                (
                    status,
                    _timestamp(ended_at),
                    result_json,
                    error_type,
                    advisory_run_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM advisory_runs WHERE advisory_run_id=?",
                (advisory_run_id,),
            ).fetchone()
        return _advisory_run_dict(_required_row(row, "finished advisory run"))

    def advisory_run_for_strategy(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM advisory_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return _advisory_run_dict(row) if row is not None else None

    def record_advisory_tool_call(
        self,
        advisory_run_id: str,
        sequence: int,
        *,
        turn: int,
        tool_name: str,
        arguments_hash: str,
        result_hash: str | None,
        status: str,
        duration_ms: int,
        called_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        if sequence < 0 or turn < 1 or duration_ms < 0:
            raise StorageInvariantError(
                "advisory tool sequence, turn, and duration are invalid"
            )
        if tool_name not in ADVISORY_READ_TOOLS:
            raise StorageInvariantError(
                "advisory trace accepts only the fixed read-only tool set"
            )
        for label, value in (
            ("arguments_hash", arguments_hash),
            ("result_hash", result_hash),
        ):
            if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise StorageInvariantError(f"invalid advisory {label}")
        expected = {
            "advisory_run_id": advisory_run_id,
            "sequence": sequence,
            "turn": turn,
            "tool_name": tool_name,
            "arguments_hash": arguments_hash,
            "result_hash": result_hash,
            "status": status,
            "duration_ms": duration_ms,
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM advisory_tool_trace
                WHERE advisory_run_id=? AND sequence=?
                """,
                (advisory_run_id, sequence),
            ).fetchone()
            if existing is not None:
                _assert_record_matches(existing, expected, "advisory tool trace")
                return _advisory_tool_dict(existing)
            connection.execute(
                """
                INSERT INTO advisory_tool_trace(
                    advisory_run_id, sequence, turn, tool_name, arguments_hash,
                    result_hash, status, duration_ms, called_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*expected.values(), _timestamp(called_at)),
            )
            row = connection.execute(
                """
                SELECT * FROM advisory_tool_trace
                WHERE advisory_run_id=? AND sequence=?
                """,
                (advisory_run_id, sequence),
            ).fetchone()
        return _advisory_tool_dict(_required_row(row, "advisory tool trace"))

    def list_advisory_tool_calls(
        self, advisory_run_id: str
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM advisory_tool_trace
                WHERE advisory_run_id=? ORDER BY sequence
                """,
                (advisory_run_id,),
            ).fetchall()
        return [_advisory_tool_dict(row) for row in rows]

    def start_agent_smoke(
        self,
        smoke_run_id: str,
        *,
        environment: str,
        account_suffix: str,
        model: str,
        prompt_hash: str,
        config_hash: str,
        started_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        when = _timestamp(started_at)
        expected = {
            "smoke_run_id": smoke_run_id,
            "environment": environment,
            "account_suffix": account_suffix,
            "model": model,
            "prompt_hash": prompt_hash,
            "config_hash": config_hash,
        }
        for name, value in expected.items():
            _require_value(name, value)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM agent_smoke_runs WHERE smoke_run_id=?",
                (smoke_run_id,),
            ).fetchone()
            if existing is not None:
                _assert_record_matches(existing, expected, "agent smoke run")
                return _agent_smoke_run_dict(existing)
            connection.execute(
                """
                INSERT INTO agent_smoke_runs(
                    smoke_run_id, environment, account_suffix, model, status,
                    prompt_hash, config_hash, started_at
                ) VALUES (?, ?, ?, ?, 'STARTED', ?, ?, ?)
                """,
                (*expected.values(), when),
            )
            row = connection.execute(
                "SELECT * FROM agent_smoke_runs WHERE smoke_run_id=?",
                (smoke_run_id,),
            ).fetchone()
        return _agent_smoke_run_dict(_required_row(row, "agent smoke run"))

    def finish_agent_smoke(
        self,
        smoke_run_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error_type: str | None = None,
        ended_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        if status not in AGENT_SMOKE_TERMINAL_STATUSES:
            raise StorageInvariantError(f"invalid terminal agent smoke status: {status}")
        result_json = _audit_json(result) if result is not None else None
        expected_terminal = {
            "status": status,
            "result_json": result_json,
            "error_type": error_type,
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = _required_row(
                connection.execute(
                    "SELECT * FROM agent_smoke_runs WHERE smoke_run_id=?",
                    (smoke_run_id,),
                ).fetchone(),
                f"agent smoke run {smoke_run_id}",
            )
            if existing["status"] in AGENT_SMOKE_TERMINAL_STATUSES:
                _assert_record_matches(
                    existing, expected_terminal, "finished agent smoke run"
                )
                return _agent_smoke_run_dict(existing)
            if existing["status"] != "STARTED":
                raise StorageInvariantError(
                    f"agent smoke cannot finish from status {existing['status']}"
                )
            connection.execute(
                """
                UPDATE agent_smoke_runs
                SET status=?, ended_at=?, result_json=?, error_type=?
                WHERE smoke_run_id=? AND status='STARTED'
                """,
                (
                    status,
                    _timestamp(ended_at),
                    result_json,
                    error_type,
                    smoke_run_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_smoke_runs WHERE smoke_run_id=?",
                (smoke_run_id,),
            ).fetchone()
        return _agent_smoke_run_dict(_required_row(row, "finished agent smoke run"))

    def record_agent_smoke_tool(
        self,
        smoke_run_id: str,
        sequence: int,
        *,
        turn: int,
        tool_name: str,
        arguments_hash: str,
        result_hash: str | None,
        status: str,
        duration_ms: int,
        called_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        if sequence < 0 or turn < 1 or duration_ms < 0:
            raise StorageInvariantError(
                "smoke sequence/duration must be non-negative and turn must be positive"
            )
        for name, value in (
            ("smoke_run_id", smoke_run_id),
            ("tool_name", tool_name),
            ("arguments_hash", arguments_hash),
            ("status", status),
        ):
            _require_value(name, value)
        expected = {
            "smoke_run_id": smoke_run_id,
            "sequence": sequence,
            "turn": turn,
            "tool_name": tool_name,
            "arguments_hash": arguments_hash,
            "result_hash": result_hash,
            "status": status,
            "duration_ms": duration_ms,
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM agent_smoke_trace
                WHERE smoke_run_id=? AND sequence=?
                """,
                (smoke_run_id, sequence),
            ).fetchone()
            if existing is not None:
                _assert_record_matches(existing, expected, "agent smoke trace")
                return dict(existing)
            connection.execute(
                """
                INSERT INTO agent_smoke_trace(
                    smoke_run_id, sequence, turn, tool_name, arguments_hash,
                    result_hash, status, duration_ms, called_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*expected.values(), _timestamp(called_at)),
            )
            row = connection.execute(
                """
                SELECT * FROM agent_smoke_trace
                WHERE smoke_run_id=? AND sequence=?
                """,
                (smoke_run_id, sequence),
            ).fetchone()
        return dict(_required_row(row, "agent smoke trace"))

    def list_agent_smoke_tools(self, smoke_run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_smoke_trace
                WHERE smoke_run_id=? ORDER BY sequence
                """,
                (smoke_run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_order_intent(
        self,
        intent_id: str,
        *,
        run_id: str,
        purpose: str,
        client_order_id: str,
        payload: dict[str, Any],
        candidate_id: str | None = None,
    ) -> dict[str, Any]:
        _require_choice("purpose", purpose, {"entry", "exit", "recovery"})
        payload_json = _canonical_json(payload)
        expected = {
            "intent_id": intent_id,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "purpose": purpose,
            "client_order_id": client_order_id,
            "payload_json": payload_json,
            "payload_hash": _hash_json(payload_json),
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if purpose == "entry" and self._kill_switch_enabled_locked(connection):
                raise StorageInvariantError("kill switch blocks entry order intent")
            if candidate_id is not None:
                candidate = _required_row(
                    connection.execute(
                        "SELECT run_id FROM candidates WHERE candidate_id=?", (candidate_id,)
                    ).fetchone(),
                    f"candidate {candidate_id}",
                )
                if candidate["run_id"] != run_id:
                    raise StorageInvariantError("order intent candidate belongs to another run")
            existing = connection.execute(
                """
                SELECT * FROM order_intents
                WHERE intent_id=? OR client_order_id=?
                """,
                (intent_id, client_order_id),
            ).fetchone()
            if existing is not None:
                _assert_record_matches(existing, expected, "order intent")
                return _order_intent_dict(existing)
            connection.execute(
                """
                INSERT INTO order_intents(
                    intent_id, run_id, candidate_id, purpose, client_order_id,
                    payload_json, payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*expected.values(), utc_now()),
            )
            row = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
        return _order_intent_dict(_required_row(row, "order intent"))

    def get_order_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM order_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
        return _order_intent_dict(row) if row is not None else None

    def list_order_intents(
        self, run_id: str, *, purpose: str | None = None
    ) -> list[dict[str, Any]]:
        if purpose is not None:
            _require_choice("purpose", purpose, {"entry", "exit", "recovery"})
        with self.connect() as connection:
            if purpose is None:
                rows = connection.execute(
                    "SELECT * FROM order_intents WHERE run_id=? ORDER BY created_at, intent_id",
                    (run_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM order_intents
                    WHERE run_id=? AND purpose=? ORDER BY created_at, intent_id
                    """,
                    (run_id, purpose),
                ).fetchall()
        return [_order_intent_dict(row) for row in rows]

    def order_intent_matches(self, intent_id: str, payload: dict[str, Any]) -> bool:
        payload_json = _canonical_json(payload)
        payload_hash = _hash_json(payload_json)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json, payload_hash FROM order_intents WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
        return bool(
            row is not None
            and row["payload_hash"] == payload_hash
            and row["payload_json"] == payload_json
        )

    def create_order_chain(
        self,
        chain_id: str,
        *,
        run_id: str,
        intent_id: str,
        purpose: str,
    ) -> dict[str, Any]:
        _require_choice("purpose", purpose, {"entry", "exit", "recovery"})
        expected = {
            "chain_id": chain_id,
            "run_id": run_id,
            "intent_id": intent_id,
            "purpose": purpose,
        }
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            intent = _required_row(
                connection.execute(
                    "SELECT * FROM order_intents WHERE intent_id=?", (intent_id,)
                ).fetchone(),
                f"order intent {intent_id}",
            )
            if intent["run_id"] != run_id or intent["purpose"] != purpose:
                raise StorageInvariantError("order chain does not match its intent")
            existing = connection.execute(
                "SELECT * FROM order_chains WHERE chain_id=? OR intent_id=?",
                (chain_id, intent_id),
            ).fetchone()
            if existing is not None:
                _assert_record_matches(existing, expected, "order chain")
                return dict(existing)
            connection.execute(
                """
                INSERT INTO order_chains(
                    chain_id, run_id, intent_id, purpose, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'PLANNED', ?, ?)
                """,
                (chain_id, run_id, intent_id, purpose, now, now),
            )
            connection.execute(
                """
                INSERT INTO order_status_history(
                    chain_id, event_kind, from_state, to_state, detail_json, observed_at
                ) VALUES (?, 'transition', NULL, 'PLANNED', '{}', ?)
                """,
                (chain_id, now),
            )
            row = connection.execute(
                "SELECT * FROM order_chains WHERE chain_id=?", (chain_id,)
            ).fetchone()
        return dict(_required_row(row, "order chain"))

    def get_order_chain(self, chain_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM order_chains WHERE chain_id=?", (chain_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_order_chains(
        self, run_id: str, *, purpose: str | None = None
    ) -> list[dict[str, Any]]:
        if purpose is not None:
            _require_choice("purpose", purpose, {"entry", "exit", "recovery"})
        with self.connect() as connection:
            if purpose is None:
                rows = connection.execute(
                    "SELECT * FROM order_chains WHERE run_id=? ORDER BY created_at, chain_id",
                    (run_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM order_chains
                    WHERE run_id=? AND purpose=? ORDER BY created_at, chain_id
                    """,
                    (run_id, purpose),
                ).fetchall()
        return [dict(row) for row in rows]

    def transition_order_chain(
        self,
        chain_id: str,
        to_state: str,
        *,
        detail: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        broker_status: str | None = None,
        observed_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        _require_state(to_state, ORDER_CHAIN_STATES, "order chain")
        when = _timestamp(observed_at)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _required_row(
                connection.execute(
                    "SELECT * FROM order_chains WHERE chain_id=?", (chain_id,)
                ).fetchone(),
                f"order chain {chain_id}",
            )
            from_state = str(row["state"])
            if attempt_id is not None:
                attempt = _required_row(
                    connection.execute(
                        "SELECT chain_id FROM order_attempts WHERE attempt_id=?", (attempt_id,)
                    ).fetchone(),
                    f"order attempt {attempt_id}",
                )
                if attempt["chain_id"] != chain_id:
                    raise StorageInvariantError("order attempt belongs to another chain")
            if from_state == to_state:
                return dict(row)
            if to_state not in ORDER_CHAIN_TRANSITIONS[from_state]:
                raise StorageInvariantError(
                    f"invalid order chain transition {from_state} -> {to_state}"
                )
            updated = connection.execute(
                "UPDATE order_chains SET state=?, updated_at=? WHERE chain_id=? AND state=?",
                (to_state, when, chain_id, from_state),
            )
            if updated.rowcount != 1:
                raise StorageInvariantError("order chain state changed concurrently")
            connection.execute(
                """
                INSERT INTO order_status_history(
                    chain_id, attempt_id, event_kind, from_state, to_state,
                    broker_status, detail_json, observed_at
                ) VALUES (?, ?, 'transition', ?, ?, ?, ?, ?)
                """,
                (
                    chain_id,
                    attempt_id,
                    from_state,
                    to_state,
                    broker_status,
                    _audit_json(detail or {}),
                    when,
                ),
            )
            result = connection.execute(
                "SELECT * FROM order_chains WHERE chain_id=?", (chain_id,)
            ).fetchone()
        return dict(_required_row(result, "order chain"))

    def record_order_attempt(
        self,
        attempt_id: str,
        *,
        chain_id: str,
        sequence: int,
        client_order_id: str,
        request: dict[str, Any],
        broker_order_id: str | None = None,
    ) -> dict[str, Any]:
        if sequence < 0:
            raise StorageInvariantError("order attempt sequence must be non-negative")
        request_json = _canonical_json(request)
        expected = {
            "attempt_id": attempt_id,
            "chain_id": chain_id,
            "sequence": sequence,
            "client_order_id": client_order_id,
            "broker_order_id": broker_order_id,
            "request_json": request_json,
            "request_hash": _hash_json(request_json),
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM order_attempts
                WHERE attempt_id=? OR client_order_id=?
                """,
                (attempt_id, client_order_id),
            ).fetchone()
            if existing is not None:
                immutable_expected = dict(expected)
                if broker_order_id is None:
                    immutable_expected.pop("broker_order_id")
                _assert_record_matches(existing, immutable_expected, "order attempt")
                return _order_attempt_dict(existing)
            try:
                connection.execute(
                    """
                    INSERT INTO order_attempts(
                        attempt_id, chain_id, sequence, client_order_id, broker_order_id,
                        request_json, request_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*expected.values(), utc_now()),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageInvariantError("order attempt identity or sequence conflicts") from exc
            row = connection.execute(
                "SELECT * FROM order_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        return _order_attempt_dict(_required_row(row, "order attempt"))

    def bind_broker_order_id(self, attempt_id: str, broker_order_id: str) -> dict[str, Any]:
        _require_value("broker_order_id", broker_order_id)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = _required_row(
                connection.execute(
                    "SELECT * FROM order_attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone(),
                f"order attempt {attempt_id}",
            )
            existing = row["broker_order_id"]
            if existing is not None and existing != broker_order_id:
                raise StorageInvariantError("broker order ID is immutable once bound")
            if existing is None:
                try:
                    connection.execute(
                        "UPDATE order_attempts SET broker_order_id=? WHERE attempt_id=?",
                        (broker_order_id, attempt_id),
                    )
                except sqlite3.IntegrityError as exc:
                    raise StorageInvariantError("broker order ID already belongs to another attempt") from exc
            result = connection.execute(
                "SELECT * FROM order_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        return _order_attempt_dict(_required_row(result, "order attempt"))

    def latest_order_attempt(self, chain_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM order_attempts
                WHERE chain_id=? ORDER BY sequence DESC LIMIT 1
                """,
                (chain_id,),
            ).fetchone()
        return _order_attempt_dict(row) if row is not None else None

    def find_order_attempt_by_broker_id(
        self, broker_order_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM order_attempts WHERE broker_order_id=?",
                (broker_order_id,),
            ).fetchone()
        return _order_attempt_dict(row) if row is not None else None

    def record_order_status(
        self,
        chain_id: str,
        broker_status: str,
        *,
        detail: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        observed_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        _require_value("broker_status", broker_status)
        with self.connect() as connection:
            chain = _required_row(
                connection.execute(
                    "SELECT state FROM order_chains WHERE chain_id=?", (chain_id,)
                ).fetchone(),
                f"order chain {chain_id}",
            )
            if attempt_id is not None:
                attempt = _required_row(
                    connection.execute(
                        "SELECT chain_id FROM order_attempts WHERE attempt_id=?", (attempt_id,)
                    ).fetchone(),
                    f"order attempt {attempt_id}",
                )
                if attempt["chain_id"] != chain_id:
                    raise StorageInvariantError("order status attempt belongs to another chain")
            cursor = connection.execute(
                """
                INSERT INTO order_status_history(
                    chain_id, attempt_id, event_kind, from_state, to_state,
                    broker_status, detail_json, observed_at
                ) VALUES (?, ?, 'broker_observation', ?, ?, ?, ?, ?)
                """,
                (
                    chain_id,
                    attempt_id,
                    chain["state"],
                    chain["state"],
                    broker_status,
                    _audit_json(detail or {}),
                    _timestamp(observed_at),
                ),
            )
            row = connection.execute(
                "SELECT * FROM order_status_history WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        return _json_row(_required_row(row, "order status"), "detail_json")

    def list_order_status_history(self, chain_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM order_status_history WHERE chain_id=? ORDER BY id", (chain_id,)
            ).fetchall()
        return [_json_row(row, "detail_json") for row in rows]

    def record_fill(
        self,
        fill_id: str,
        *,
        chain_id: str,
        symbol: str,
        side: str,
        quantity: str,
        price: str,
        filled_at: str | datetime,
        payload: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        broker_order_id: str | None = None,
    ) -> dict[str, Any]:
        expected = {
            "fill_id": fill_id,
            "chain_id": chain_id,
            "attempt_id": attempt_id,
            "broker_order_id": broker_order_id,
            "symbol": symbol,
            "side": side,
            "quantity": str(quantity),
            "price": str(price),
            "filled_at": _timestamp(filled_at),
            "payload_json": _audit_json(payload or {}),
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if attempt_id is not None:
                attempt = _required_row(
                    connection.execute(
                        "SELECT chain_id FROM order_attempts WHERE attempt_id=?", (attempt_id,)
                    ).fetchone(),
                    f"order attempt {attempt_id}",
                )
                if attempt["chain_id"] != chain_id:
                    raise StorageInvariantError("fill attempt belongs to another chain")
            existing = connection.execute(
                "SELECT * FROM fills WHERE fill_id=?", (fill_id,)
            ).fetchone()
            if existing is not None:
                _assert_record_matches(existing, expected, "fill")
                return _json_row(existing, "payload_json")
            connection.execute(
                """
                INSERT INTO fills(
                    fill_id, chain_id, attempt_id, broker_order_id, symbol, side,
                    quantity, price, filled_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(expected.values()),
            )
            row = connection.execute("SELECT * FROM fills WHERE fill_id=?", (fill_id,)).fetchone()
        return _json_row(_required_row(row, "fill"), "payload_json")

    def record_broker_activity(
        self,
        activity_id: str,
        *,
        activity_type: str,
        occurred_at: str | datetime,
        payload: dict[str, Any],
        run_id: str | None = None,
    ) -> dict[str, Any]:
        _require_value("activity_id", activity_id)
        _require_value("activity_type", activity_type)
        payload_json = _audit_json(payload)
        expected = {
            "activity_id": activity_id,
            "run_id": run_id,
            "activity_type": activity_type,
            "occurred_at": _timestamp(occurred_at),
            "payload_json": payload_json,
            "payload_hash": _hash_json(payload_json),
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM broker_activities WHERE activity_id=?", (activity_id,)
            ).fetchone()
            if existing is not None:
                _assert_record_matches(existing, expected, "broker activity")
                return _json_row(existing, "payload_json")
            connection.execute(
                """
                INSERT INTO broker_activities(
                    activity_id, run_id, activity_type, occurred_at, payload_json,
                    payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (*expected.values(), utc_now()),
            )
            row = connection.execute(
                "SELECT * FROM broker_activities WHERE activity_id=?", (activity_id,)
            ).fetchone()
        return _json_row(_required_row(row, "broker activity"), "payload_json")

    def record_position_observation(
        self,
        observation_id: str,
        *,
        observed_at: str | datetime,
        positions: Any,
        is_flat: bool,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        payload_json = _audit_json(positions)
        expected = {
            "observation_id": observation_id,
            "run_id": run_id,
            "observed_at": _timestamp(observed_at),
            "is_flat": int(is_flat),
            "payload_json": payload_json,
            "payload_hash": _hash_json(payload_json),
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM position_observations WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            if existing is not None:
                _assert_record_matches(existing, expected, "position observation")
                return _position_observation_dict(existing)
            connection.execute(
                """
                INSERT INTO position_observations(
                    observation_id, run_id, observed_at, is_flat, payload_json, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                tuple(expected.values()),
            )
            row = connection.execute(
                "SELECT * FROM position_observations WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
        return _position_observation_dict(_required_row(row, "position observation"))

    def record_equity_observation(
        self,
        observation_id: str,
        *,
        observed_at: str | datetime,
        equity: str,
        buying_power: str | None = None,
        cash: str | None = None,
        portfolio_value: str | None = None,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        expected = {
            "observation_id": observation_id,
            "run_id": run_id,
            "observed_at": _timestamp(observed_at),
            "equity": str(equity),
            "buying_power": _optional_string(buying_power),
            "cash": _optional_string(cash),
            "portfolio_value": _optional_string(portfolio_value),
            "payload_json": _audit_json(payload or {}),
        }
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM equity_observations WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            if existing is not None:
                _assert_record_matches(existing, expected, "equity observation")
                return _json_row(existing, "payload_json")
            connection.execute(
                """
                INSERT INTO equity_observations(
                    observation_id, run_id, observed_at, equity, buying_power, cash,
                    portfolio_value, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(expected.values()),
            )
            row = connection.execute(
                "SELECT * FROM equity_observations WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
        return _json_row(_required_row(row, "equity observation"), "payload_json")

    def get_kill_switch(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_controls WHERE singleton_id=1"
            ).fetchone()
        result = dict(_required_row(row, "runtime controls"))
        result["kill_switch_enabled"] = bool(result["kill_switch_enabled"])
        return result

    def activate_kill_switch(
        self,
        reason: str,
        requested_by: str,
        *,
        run_id: str | None = None,
        evidence: dict[str, Any] | None = None,
        activated_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        _require_value("reason", reason)
        _require_value("requested_by", requested_by)
        when = _timestamp(activated_at)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            control = _required_row(
                connection.execute(
                    "SELECT * FROM runtime_controls WHERE singleton_id=1"
                ).fetchone(),
                "runtime controls",
            )
            revoked = connection.execute(
                """
                UPDATE entry_authorizations
                SET state='REVOKED', revoked_at=?, revoked_by=?, revoke_reason=?
                WHERE state='ARMED'
                """,
                (when, requested_by, f"kill switch: {reason}"),
            )
            revoked_count = int(revoked.rowcount)
            if not bool(control["kill_switch_enabled"]):
                version = int(control["version"]) + 1
                connection.execute(
                    """
                    UPDATE runtime_controls
                    SET kill_switch_enabled=1, reason=?, requested_by=?, activated_at=?,
                        updated_at=?, version=?
                    WHERE singleton_id=1 AND version=?
                    """,
                    (
                        reason,
                        requested_by,
                        when,
                        when,
                        version,
                        control["version"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO control_events(
                        control_name, enabled, reason, requested_by, detail_json,
                        version, created_at
                    ) VALUES ('kill_switch', 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        reason,
                        requested_by,
                        _audit_json(
                            {
                                **(evidence or {}),
                                "revoked_authorization_count": revoked_count,
                            }
                        ),
                        version,
                        when,
                    ),
                )
            if run_id is not None:
                run = _required_row(
                    connection.execute(
                        "SELECT * FROM strategy_runs WHERE run_id=?", (run_id,)
                    ).fetchone(),
                    f"strategy run {run_id}",
                )
                if run["state"] not in TERMINAL_STRATEGY_STATES and run["state"] != "RISK_OFF":
                    self._transition_strategy_run_locked(
                        connection,
                        run_id,
                        "RISK_OFF",
                        "KILL_SWITCH",
                        _audit_json({"reason": reason, **(evidence or {})}),
                        when,
                    )
            result = connection.execute(
                "SELECT * FROM runtime_controls WHERE singleton_id=1"
            ).fetchone()
        output = dict(_required_row(result, "runtime controls"))
        output["kill_switch_enabled"] = bool(output["kill_switch_enabled"])
        return output

    def clear_kill_switch(
        self,
        reason: str,
        requested_by: str,
        *,
        expected_version: int,
        cleared_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        _require_value("reason", reason)
        _require_value("requested_by", requested_by)
        when = _timestamp(cleared_at)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            control = _required_row(
                connection.execute(
                    "SELECT * FROM runtime_controls WHERE singleton_id=1"
                ).fetchone(),
                "runtime controls",
            )
            if int(control["version"]) != expected_version:
                raise StorageInvariantError("kill switch version changed concurrently")
            if bool(control["kill_switch_enabled"]):
                version = expected_version + 1
                connection.execute(
                    """
                    UPDATE runtime_controls
                    SET kill_switch_enabled=0, reason=?, requested_by=?, activated_at=NULL,
                        updated_at=?, version=?
                    WHERE singleton_id=1 AND version=?
                    """,
                    (reason, requested_by, when, version, expected_version),
                )
                connection.execute(
                    """
                    INSERT INTO control_events(
                        control_name, enabled, reason, requested_by, detail_json,
                        version, created_at
                    ) VALUES ('kill_switch', 0, ?, ?, '{}', ?, ?)
                    """,
                    (reason, requested_by, version, when),
                )
            result = connection.execute(
                "SELECT * FROM runtime_controls WHERE singleton_id=1"
            ).fetchone()
        output = dict(_required_row(result, "runtime controls"))
        output["kill_switch_enabled"] = bool(output["kill_switch_enabled"])
        return output

    def acquire_lease(
        self,
        lease_name: str,
        owner_id: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        _validate_lease(lease_name, owner_id, ttl_seconds)
        current = _aware_datetime(now)
        current_iso = _timestamp(current)
        expires_at = _timestamp(current + timedelta(seconds=ttl_seconds))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runtime_leases WHERE lease_name=?", (lease_name,)
            ).fetchone()
            if row is not None:
                active = _parse_timestamp(str(row["expires_at"])) > current
                if active and row["owner_id"] != owner_id:
                    return False
                acquired_at = row["acquired_at"] if row["owner_id"] == owner_id else current_iso
                connection.execute(
                    """
                    UPDATE runtime_leases
                    SET owner_id=?, acquired_at=?, heartbeat_at=?, expires_at=?
                    WHERE lease_name=?
                    """,
                    (owner_id, acquired_at, current_iso, expires_at, lease_name),
                )
                return True
            connection.execute(
                """
                INSERT INTO runtime_leases(
                    lease_name, owner_id, acquired_at, heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (lease_name, owner_id, current_iso, current_iso, expires_at),
            )
        return True

    def renew_lease(
        self,
        lease_name: str,
        owner_id: str,
        ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        _validate_lease(lease_name, owner_id, ttl_seconds)
        current = _aware_datetime(now)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runtime_leases WHERE lease_name=?", (lease_name,)
            ).fetchone()
            if (
                row is None
                or row["owner_id"] != owner_id
                or _parse_timestamp(str(row["expires_at"])) <= current
            ):
                return False
            connection.execute(
                "UPDATE runtime_leases SET heartbeat_at=?, expires_at=? WHERE lease_name=?",
                (
                    _timestamp(current),
                    _timestamp(current + timedelta(seconds=ttl_seconds)),
                    lease_name,
                ),
            )
        return True

    def release_lease(self, lease_name: str, owner_id: str) -> bool:
        _require_value("lease_name", lease_name)
        _require_value("owner_id", owner_id)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM runtime_leases WHERE lease_name=? AND owner_id=?",
                (lease_name, owner_id),
            )
        return cursor.rowcount == 1

    def get_lease(self, lease_name: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_leases WHERE lease_name=?", (lease_name,)
            ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _kill_switch_enabled_locked(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT kill_switch_enabled FROM runtime_controls WHERE singleton_id=1"
        ).fetchone()
        return bool(row and row["kill_switch_enabled"])

    @staticmethod
    def _assert_metadata_identity_locked(
        connection: sqlite3.Connection, environment: str, account_id: str
    ) -> None:
        rows = connection.execute(
            "SELECT key, value FROM metadata WHERE key IN ('environment', 'account_id')"
        ).fetchall()
        identity = {str(row["key"]): str(row["value"]) for row in rows}
        if "environment" not in identity or "account_id" not in identity:
            raise AccountIdentityError("database account identity is not bound")
        if identity["environment"] != environment:
            raise AccountIdentityError("database is bound to another environment")
        if identity["account_id"] != account_id:
            raise AccountIdentityError("database is bound to another Alpaca account")

    def latest_health(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM worker_heartbeat WHERE singleton_id=1").fetchone()
        return dict(row) if row else None


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _migrate_event_definitions(connection: sqlite3.Connection) -> None:
    """Replace the v1 exact-timestamp ledger without inventing release times."""

    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(event_definitions)").fetchall()
    }
    if "event_date" in columns:
        return
    if "event_at" not in columns:
        raise StorageInvariantError("event definition schema is not recognized")

    connection.execute(
        "ALTER TABLE event_definitions RENAME TO event_definitions_legacy_v1"
    )
    connection.execute(
        """
        CREATE TABLE event_definitions (
            symbol TEXT PRIMARY KEY,
            strategy_version TEXT NOT NULL,
            source_published_on TEXT,
            event_date TEXT NOT NULL,
            release_timing TEXT NOT NULL,
            conference_call_at TEXT,
            status TEXT NOT NULL,
            exclusion_reason TEXT,
            source_url TEXT NOT NULL,
            trade_expiration TEXT NOT NULL,
            term_expiration TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO event_definitions(
            symbol, strategy_version, source_published_on, event_date,
            release_timing, conference_call_at, status, exclusion_reason,
            source_url, trade_expiration, term_expiration, updated_at
        )
        SELECT
            symbol,
            strategy_version,
            NULL,
            substr(event_at, 1, 10),
            'legacy_unspecified',
            NULL,
            status,
            CASE
                WHEN status = 'verification_required' THEN 'RELEASE_TIME_AMBIGUOUS'
                ELSE NULL
            END,
            source_url,
            trade_expiration,
            term_expiration,
            updated_at
        FROM event_definitions_legacy_v1
        """
    )
    connection.execute("DROP TABLE event_definitions_legacy_v1")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    )


def _audit_json(value: Any) -> str:
    return _canonical_json(redact(value))


def _hash_json(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _required_row(row: sqlite3.Row | None, label: str) -> sqlite3.Row:
    if row is None:
        raise StorageInvariantError(f"missing {label}")
    return row


def _assert_record_matches(
    row: sqlite3.Row, expected: dict[str, Any], label: str
) -> None:
    mismatched = [key for key, value in expected.items() if row[key] != value]
    if mismatched:
        raise StorageInvariantError(
            f"immutable {label} conflicts on: " + ", ".join(sorted(mismatched))
        )


def _json_row(row: sqlite3.Row, *json_fields: str) -> dict[str, Any]:
    result = dict(row)
    for field in json_fields:
        raw = result.get(field)
        result[field.removesuffix("_json")] = json.loads(raw) if raw is not None else None
    return result


def _strategy_run_dict(row: sqlite3.Row) -> dict[str, Any]:
    return _json_row(row, "context_json")


def _candidate_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = _json_row(row, "payload_json")
    result["eligible"] = bool(result["eligible"])
    return result


def _gate_result_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = _json_row(row, "detail_json")
    result["passed"] = bool(result["passed"])
    return result


def _agent_run_dict(row: sqlite3.Row) -> dict[str, Any]:
    return _json_row(row, "result_json")


def _agent_tool_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = _json_row(row, "arguments_json", "result_json")
    result["is_official_mcp"] = bool(result["is_official_mcp"])
    return result


def _agent_smoke_run_dict(row: sqlite3.Row) -> dict[str, Any]:
    return _json_row(row, "result_json")


def _advisory_run_dict(row: sqlite3.Row) -> dict[str, Any]:
    return _json_row(row, "result_json")


def _advisory_tool_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _order_intent_dict(row: sqlite3.Row) -> dict[str, Any]:
    return _json_row(row, "payload_json")


def _order_attempt_dict(row: sqlite3.Row) -> dict[str, Any]:
    return _json_row(row, "request_json")


def _entry_authorization_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _position_observation_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = _json_row(row, "payload_json")
    result["is_flat"] = bool(result["is_flat"])
    return result


def _require_value(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise StorageInvariantError(f"{name} must be a non-empty string")


def _require_choice(name: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        raise StorageInvariantError(
            f"invalid {name}: {value}; expected one of {', '.join(sorted(allowed))}"
        )


def _require_state(state: str, allowed: frozenset[str], label: str) -> None:
    if state not in allowed:
        raise StorageInvariantError(f"invalid {label} state: {state}")


def _strategy_date(value: str) -> str:
    _require_value("strategy_date", value)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise StorageInvariantError(f"invalid strategy_date: {value}") from exc
    normalized = parsed.isoformat()
    if value != normalized:
        raise StorageInvariantError("strategy_date must use YYYY-MM-DD")
    return normalized


def _aware_datetime(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None or result.utcoffset() is None:
        raise StorageInvariantError("timestamps must be timezone-aware")
    return result.astimezone(UTC)


def _timestamp(value: str | datetime | None) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StorageInvariantError(f"invalid timestamp: {value}") from exc
        return _aware_datetime(parsed).isoformat(timespec="microseconds")
    return _aware_datetime(value).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    try:
        return _aware_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise StorageInvariantError(f"invalid stored timestamp: {value}") from exc


def _validate_lease(lease_name: str, owner_id: str, ttl_seconds: int) -> None:
    _require_value("lease_name", lease_name)
    _require_value("owner_id", owner_id)
    if ttl_seconds <= 0:
        raise StorageInvariantError("lease ttl_seconds must be positive")
