from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from thetatrap.errors import AccountIdentityError
from thetatrap.events import load_events
from thetatrap.storage import Store


def test_database_initialization_is_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.initialize()
    store.initialize()
    assert store.get_metadata("schema_version") == "2"
    with store.connect() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert journal_mode == "wal"
    assert busy_timeout == 5000


def test_database_identity_cannot_change(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.initialize()
    store.bind_identity("development", "account-a", "account-a")
    with pytest.raises(AccountIdentityError, match="another Alpaca account"):
        store.bind_identity("development", "account-b", "account-b")


def test_observed_account_must_match_expected(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.initialize()
    with pytest.raises(AccountIdentityError, match="does not match"):
        store.bind_identity("development", "expected", "observed")
    assert store.get_metadata("account_id") is None


def test_events_and_health_persist(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.initialize()
    store.upsert_events(load_events())
    store.record_heartbeat(
        status="healthy",
        environment="development",
        account_suffix="…abc123",
        mcp_schema_hash="hash",
        market_is_open=False,
        detail={"read_only": True},
    )
    with store.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM event_definitions").fetchone()[0]
        ntap = connection.execute(
            """
            SELECT status, exclusion_reason, event_date, conference_call_at
            FROM event_definitions WHERE symbol='NTAP'
            """
        ).fetchone()
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(event_definitions)").fetchall()
        }
    assert count == 9
    assert dict(ntap) == {
        "status": "ineligible",
        "exclusion_reason": "REQUIRED_WEEKLY_EXPIRATIONS_UNAVAILABLE",
        "event_date": "2026-09-02",
        "conference_call_at": "2026-09-02T17:30:00-04:00",
    }
    assert "event_at" not in columns
    assert store.latest_health()["status"] == "healthy"  # type: ignore[index]


def test_mcp_audit_redacts_secrets(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.initialize()
    store.start_mcp_session("session", "2.3.0", "Alpaca MCP", 20, "hash")
    store.record_mcp_call(
        "session",
        "system",
        "get_account_info",
        {"api_key": "never-store-me"},
        {"data": {"authorization": "never-store-me-either"}},
        "ok",
        10,
    )
    raw = (tmp_path / "state.sqlite3").read_bytes()
    assert b"never-store-me" not in raw


def test_v1_event_ledger_migrates_without_preserving_fake_release_time(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE event_definitions (
                symbol TEXT PRIMARY KEY,
                strategy_version TEXT NOT NULL,
                event_at TEXT NOT NULL,
                status TEXT NOT NULL,
                source_url TEXT NOT NULL,
                trade_expiration TEXT NOT NULL,
                term_expiration TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO event_definitions VALUES (
                'PANW', '1.1', '2026-09-01T16:05:00-04:00', 'verified',
                'https://example.test/panw', '2026-09-04', '2026-09-11',
                '2026-08-30T00:00:00+00:00'
            )
            """
        )

    store = Store(path)
    store.initialize()

    with store.connect() as connection:
        row = connection.execute(
            """
            SELECT event_date, release_timing, conference_call_at
            FROM event_definitions WHERE symbol='PANW'
            """
        ).fetchone()
        columns = {
            item["name"]
            for item in connection.execute("PRAGMA table_info(event_definitions)").fetchall()
        }
    assert dict(row) == {
        "event_date": "2026-09-01",
        "release_timing": "legacy_unspecified",
        "conference_call_at": None,
    }
    assert "event_at" not in columns

    store.upsert_events(load_events())
    with store.connect() as connection:
        current = connection.execute(
            """
            SELECT strategy_version, release_timing, conference_call_at
            FROM event_definitions WHERE symbol='PANW'
            """
        ).fetchone()
    assert dict(current) == {
        "strategy_version": "1.2",
        "release_timing": "after_market_close",
        "conference_call_at": "2026-09-01T16:30:00-04:00",
    }
