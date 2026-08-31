from __future__ import annotations

import json
from argparse import Namespace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from thetatrap import cli
from thetatrap.cli import (
    _entry_authorization_confirmation,
    _find_expected_error,
    _handle_entry_authorization,
    _preflight_entry_authorization,
)
from thetatrap.errors import MCPContractError
from thetatrap.settings import load_settings
from thetatrap.storage import Store


NOW = datetime(2026, 8, 30, 16, 0, tzinfo=UTC)


class FakeAuthorizationStore:
    def __init__(self) -> None:
        self.row: dict[str, Any] | None = None
        self.arm_arguments: dict[str, Any] | None = None
        self.revoke_arguments: dict[str, Any] | None = None

    def arm_entry_authorization(self, authorization_id: str, **kwargs: Any) -> dict[str, Any]:
        self.arm_arguments = {"authorization_id": authorization_id, **kwargs}
        self.row = {
            "authorization_id": authorization_id,
            "environment": kwargs["environment"],
            "account_id": kwargs["account_id"],
            "strategy_date": kwargs["strategy_date"],
            "expires_at": kwargs["expires_at"],
            "state": "ARMED",
            "reason": kwargs["reason"],
            "requested_by": kwargs["requested_by"],
            "armed_at": kwargs["armed_at"],
            "consumed_at": None,
            "revoked_at": None,
            "run_id": None,
            "intent_id": None,
            "attempt_id": None,
        }
        return dict(self.row)

    def get_entry_authorization(self, **_: Any) -> dict[str, Any] | None:
        return dict(self.row) if self.row else None

    def latest_entry_authorization(self, **_: Any) -> dict[str, Any] | None:
        return dict(self.row) if self.row else None

    def revoke_entry_authorization(
        self, authorization_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        assert self.row is not None
        self.revoke_arguments = {"authorization_id": authorization_id, **kwargs}
        self.row.update(
            {
                "state": "REVOKED",
                "revoked_at": kwargs["revoked_at"],
                "revoked_by": kwargs["requested_by"],
                "revoke_reason": kwargs["reason"],
            }
        )
        return dict(self.row)


def test_expected_error_is_recovered_from_async_exception_group() -> None:
    expected = MCPContractError("schema mismatch")
    grouped = ExceptionGroup("stdio cleanup", [ExceptionGroup("session", [expected])])
    assert _find_expected_error(grouped) is expected


def test_arm_entry_authorization_is_date_bound_expiring_and_redacted(
    monkeypatch: pytest.MonkeyPatch, valid_env_file: Path
) -> None:
    settings = load_settings(valid_env_file)
    store = FakeAuthorizationStore()
    monkeypatch.setattr(cli, "_bound_authorization_store", lambda _: store)
    strategy_day = date(2026, 9, 1)
    args = Namespace(
        action="arm",
        strategy_date=strategy_day.isoformat(),
        reason="approved dev canary",
        confirm=_entry_authorization_confirmation(settings, strategy_day),
    )

    result = _handle_entry_authorization(settings, args, now=NOW)

    assert result["status"] == "ok"
    assert result["entry_authorization"]["state"] == "ARMED"
    assert result["entry_authorization"]["account_suffix"] == "…unt-id"
    assert "account_id" not in result["entry_authorization"]
    assert store.arm_arguments is not None
    assert store.arm_arguments["strategy_date"] == "2026-09-01"
    assert store.arm_arguments["expires_at"].isoformat() == "2026-09-01T15:40:00-04:00"
    assert store.arm_arguments["authorization_id"].startswith("tt-entry-auth-")


def test_arm_requires_exact_redacted_confirmation(
    monkeypatch: pytest.MonkeyPatch, valid_env_file: Path
) -> None:
    settings = load_settings(valid_env_file)
    store = FakeAuthorizationStore()
    monkeypatch.setattr(cli, "_bound_authorization_store", lambda _: store)
    args = Namespace(
        action="arm",
        strategy_date="2026-09-01",
        reason="approved dev canary",
        confirm="ARM ONE PAPER ENTRY development 2026-09-01 wrong",
    )

    with pytest.raises(ValueError, match="use --confirm"):
        _handle_entry_authorization(settings, args, now=NOW)
    assert store.arm_arguments is None


def test_arm_rejects_date_without_verified_event(
    monkeypatch: pytest.MonkeyPatch, valid_env_file: Path
) -> None:
    settings = load_settings(valid_env_file)
    store = FakeAuthorizationStore()
    monkeypatch.setattr(cli, "_bound_authorization_store", lambda _: store)
    strategy_day = date(2026, 9, 3)
    args = Namespace(
        action="arm",
        strategy_date=strategy_day.isoformat(),
        reason="approved dev canary",
        confirm=_entry_authorization_confirmation(settings, strategy_day),
    )

    with pytest.raises(ValueError, match="no verified event"):
        _handle_entry_authorization(settings, args, now=NOW)
    assert store.arm_arguments is None


def test_status_and_revoke_never_return_full_account_id(
    monkeypatch: pytest.MonkeyPatch, valid_env_file: Path
) -> None:
    settings = load_settings(valid_env_file)
    store = FakeAuthorizationStore()
    monkeypatch.setattr(cli, "_bound_authorization_store", lambda _: store)
    strategy_day = date(2026, 9, 1)
    arm = Namespace(
        action="arm",
        strategy_date=strategy_day.isoformat(),
        reason="approved dev canary",
        confirm=_entry_authorization_confirmation(settings, strategy_day),
    )
    _handle_entry_authorization(settings, arm, now=NOW)

    status = _handle_entry_authorization(
        settings,
        Namespace(action="status", strategy_date=None, reason=None, confirm=None),
        now=NOW,
    )
    assert "account_id" not in status["entry_authorization"]
    assert status["entry_authorization"]["account_suffix"] == "…unt-id"

    revoked = _handle_entry_authorization(
        settings,
        Namespace(
            action="revoke",
            strategy_date="2026-09-01",
            reason="operator canceled canary",
            confirm=None,
        ),
        now=NOW,
    )
    assert revoked["entry_authorization"]["state"] == "REVOKED"
    assert "account_id" not in revoked["entry_authorization"]
    assert store.revoke_arguments is not None
    assert store.revoke_arguments["requested_by"] == "cli_operator"


def test_armed_worker_once_is_rejected_before_worker_start(
    tmp_path: Path, valid_env_text: str, capsys: pytest.CaptureFixture[str]
) -> None:
    armed = tmp_path / ".env.dev"
    armed.write_text(
        valid_env_text.replace("THETATRAP_READ_ONLY=true", "THETATRAP_READ_ONLY=false")
        .replace("THETATRAP_EXECUTION_ENABLED=false", "THETATRAP_EXECUTION_ENABLED=true"),
        encoding="utf-8",
    )

    result = cli.main(["--env-file", str(armed), "worker", "--once"])

    assert result == 2
    assert "worker --once is forbidden" in capsys.readouterr().out


def test_preflight_authorization_reports_effective_expiry_without_account_id() -> None:
    authorization = {
        "authorization_id": "tt-entry-auth-1",
        "environment": "competition",
        "account_id": "competition-account-uuid",
        "strategy_date": "2026-09-01",
        "expires_at": "2026-09-01T15:40:00-04:00",
        "state": "ARMED",
    }

    result = _preflight_entry_authorization(
        authorization,
        now=datetime(2026, 9, 1, 20, 0, 1, tzinfo=UTC),
    )

    assert result["effective_state"] == "EXPIRED"
    assert result["account_suffix"] == "…t-uuid"
    assert "account_id" not in result


def test_entry_authorization_cli_integrates_with_bound_store(valid_env_file: Path) -> None:
    settings = load_settings(valid_env_file)
    store = Store(settings.database_path)
    store.initialize()
    store.bind_identity(
        settings.environment,
        settings.expected_account_id,
        settings.expected_account_id,
    )
    strategy_day = date(2026, 9, 1)
    armed = _handle_entry_authorization(
        settings,
        Namespace(
            action="arm",
            strategy_date=strategy_day.isoformat(),
            reason="approved dev canary",
            confirm=_entry_authorization_confirmation(settings, strategy_day),
        ),
        now=NOW,
    )
    status = _handle_entry_authorization(
        settings,
        Namespace(
            action="status",
            strategy_date=strategy_day.isoformat(),
            reason=None,
            confirm=None,
        ),
        now=NOW,
    )

    assert armed["entry_authorization"]["state"] == "ARMED"
    assert status["entry_authorization"]["authorization_id"] == armed[
        "entry_authorization"
    ]["authorization_id"]
    assert "account_id" not in status["entry_authorization"]


def test_discover_account_cli_prints_operator_assignment_without_binding(
    monkeypatch: pytest.MonkeyPatch,
    valid_env_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_discovery(settings: Any) -> dict[str, Any]:
        assert settings.read_only is True
        return {
            "account_id": "24dd7553-1360-4d58-aee7-deadbeef9876",
            "account_suffix": "…ef9876",
            "env_assignment": (
                "THETATRAP_EXPECTED_ACCOUNT_ID="
                "24dd7553-1360-4d58-aee7-deadbeef9876"
            ),
        }

    monkeypatch.setattr(cli, "run_account_discovery", fake_discovery)
    result = cli.main(["--env-file", str(valid_env_file), "discover-account"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["account_id"].endswith("ef9876")


def test_agent_smoke_cli_reports_read_only_model_evidence(
    monkeypatch: pytest.MonkeyPatch,
    valid_env_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_smoke(settings: Any) -> dict[str, Any]:
        assert settings.execution_enabled is False
        return {
            "outcome": "PASS",
            "tool_calls": 5,
            "readiness": "READY",
            "reasons": [],
            "mutation_tools_exposed": 0,
        }

    monkeypatch.setattr(cli, "run_agent_smoke", fake_smoke)
    result = cli.main(["--env-file", str(valid_env_file), "agent-smoke"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "mutation_tools_exposed": 0,
        "outcome": "PASS",
        "readiness": "READY",
        "reasons": [],
        "status": "ok",
        "tool_calls": 5,
    }
