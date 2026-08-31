"""ThetaTrap setup, operations, replay, and autonomous-worker CLI."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from thetatrap.errors import ThetaTrapError
from thetatrap.events import load_events
from thetatrap.log import configure_logging
from thetatrap.mcp.client import open_alpaca_mcp
from thetatrap.execution import ExecutionService
from thetatrap.replay import run_replay_suite
from thetatrap.report import generate_report
from thetatrap.schedule import verified_events_for_day
from thetatrap.settings import (
    account_suffix,
    load_settings,
    validate_environment_pair,
)
from thetatrap.storage import Store
from thetatrap.worker import (
    prepare_foundation,
    run_account_discovery,
    run_agent_smoke,
    run_mcp_smoke,
    run_worker,
)


ENTRY_AUTHORIZATION_CONFIRM_PREFIX = "ARM ONE PAPER ENTRY"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="thetatrap")
    parser.add_argument(
        "--env-file",
        help="explicit role file, such as .env.dev or .env.competition",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check-config", help="validate configuration without network calls")
    subparsers.add_parser("init-db", help="initialize SQLite and load frozen events")

    capture = subparsers.add_parser(
        "capture-mcp-schema", help="discover and write the official MCP tool contract"
    )
    capture.add_argument("--output", required=True)

    subparsers.add_parser(
        "discover-account",
        help="read the Alpaca paper-account UUID without binding the database",
    )
    subparsers.add_parser(
        "agent-smoke",
        help="require the hosted model to call the five read-only MCP tools",
    )
    subparsers.add_parser("mcp-smoke", help="run one credentialed read-only MCP cycle")
    subparsers.add_parser(
        "preflight", help="read broker state and report entry-admission gates without mutation"
    )
    worker = subparsers.add_parser("worker", help="run the autonomous paper-options worker")
    worker.add_argument("--once", action="store_true")

    replay = subparsers.add_parser(
        "replay", help="run the five broker-isolated acceptance scenarios"
    )
    replay.add_argument("--output-db")

    report = subparsers.add_parser("report", help="export a credential-free JSON/Markdown report")
    report.add_argument("--output", required=True)

    kill = subparsers.add_parser("kill-switch", help="inspect or change the durable kill switch")
    kill.add_argument("action", choices=("status", "on", "off"))
    kill.add_argument("--reason")
    kill.add_argument("--confirm")

    authorization = subparsers.add_parser(
        "entry-authorization",
        help="inspect, arm, or revoke one durable paper-entry authorization",
    )
    authorization.add_argument("action", choices=("status", "arm", "revoke"))
    authorization.add_argument("--strategy-date")
    authorization.add_argument("--reason")
    authorization.add_argument("--confirm")

    pair = subparsers.add_parser(
        "validate-env-pair", help="prove development and competition identities differ"
    )
    pair.add_argument("--dev", required=True)
    pair.add_argument("--competition", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-env-pair":
            dev, competition = validate_environment_pair(args.dev, args.competition)
            _print_json(
                {
                    "status": "ok",
                    "development_account": account_suffix(dev.expected_account_id),
                    "competition_account": account_suffix(competition.expected_account_id),
                    "databases_are_separate": True,
                    "credentials_are_separate": True,
                }
            )
            return 0

        settings = load_settings(args.env_file)
        configure_logging(settings.log_level)

        if args.command == "check-config":
            _print_json({"status": "ok", **settings.redacted_summary()})
            return 0

        if args.command == "init-db":
            store = prepare_foundation(settings)
            _print_json({"status": "ok", "database": str(store.path)})
            return 0

        if args.command == "capture-mcp-schema":
            settings.require_mcp_credentials()
            snapshot = asyncio.run(_capture_schema(settings))
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            _print_json(
                {
                    "status": "ok",
                    "output": str(output),
                    "required_schema_hash": snapshot["required_schema_hash"],
                    "tool_count": len(snapshot["all_tool_names"]),
                }
            )
            return 0

        if args.command == "discover-account":
            report = asyncio.run(run_account_discovery(settings))
            _print_json({"status": "ok", **report})
            return 0

        if args.command == "agent-smoke":
            report = asyncio.run(run_agent_smoke(settings))
            _print_json({"status": "ok", **report})
            return 0

        if args.command == "mcp-smoke":
            report = asyncio.run(run_mcp_smoke(settings))
            _print_json({"status": "ok", **(report or {})})
            return 0

        if args.command == "preflight":
            settings.require_mcp_credentials()
            report = asyncio.run(_preflight(settings))
            _print_json({"status": "ok", **report})
            return 0

        if args.command == "worker":
            if args.once and settings.execution_enabled:
                raise ValueError(
                    "worker --once is forbidden while execution is enabled; "
                    "run the long-lived worker so it can reconcile and exit exposure"
                )
            report = asyncio.run(run_worker(settings, once=args.once))
            if args.once:
                _print_json({"status": "ok", **(report or {})})
            return 0

        if args.command == "replay":
            replay_report = run_replay_suite(args.output_db)
            _print_json(replay_report)
            return 0 if replay_report["passed"] else 2

        if args.command == "report":
            report_payload = generate_report(
                settings.database_path,
                args.output,
                execution_enabled=settings.execution_enabled,
                read_only=settings.read_only,
                environment=settings.environment,
            )
            _print_json(
                {
                    "status": "ok",
                    "output": str(Path(args.output).expanduser().resolve()),
                    "report_digest": report_payload["report_digest"],
                }
            )
            return 0

        if args.command == "kill-switch":
            store = prepare_foundation(settings)
            current = store.get_kill_switch()
            if args.action == "status":
                _print_json({"status": "ok", **current})
                return 0
            if not args.reason or not args.reason.strip():
                raise ValueError("--reason is required when changing the kill switch")
            if args.action == "on":
                active = store.find_active_strategy_run(environment=settings.environment)
                result = store.activate_kill_switch(
                    args.reason.strip(),
                    "cli_operator",
                    run_id=active["run_id"] if active else None,
                    evidence={"source": "local_cli"},
                )
            else:
                if args.confirm != "CLEAR KILL SWITCH":
                    raise ValueError('use --confirm "CLEAR KILL SWITCH" to clear')
                result = store.clear_kill_switch(
                    args.reason.strip(),
                    "cli_operator",
                    expected_version=int(current["version"]),
                )
            _print_json({"status": "ok", **result})
            return 0

        if args.command == "entry-authorization":
            _print_json(_handle_entry_authorization(settings, args))
            return 0
        raise AssertionError(f"unhandled command: {args.command}")
    except (ThetaTrapError, ValueError) as exc:
        _print_json({"status": "error", "error": type(exc).__name__, "message": str(exc)})
        return 2
    except Exception as exc:
        expected = _find_expected_error(exc)
        if expected is not None:
            _print_json(
                {
                    "status": "error",
                    "error": type(expected).__name__,
                    "message": str(expected),
                }
            )
            return 2
        raise
    except KeyboardInterrupt:
        return 130


async def _capture_schema(settings: Any) -> dict[str, Any]:
    store = prepare_foundation(settings)
    async with open_alpaca_mcp(settings, store) as connection:
        return connection.registry.snapshot()


async def _preflight(settings: Any) -> dict[str, Any]:
    store = prepare_foundation(settings)
    async with open_alpaca_mcp(settings, store) as connection:
        service = ExecutionService(settings, store, connection)
        snapshot = await service.read_broker_snapshot()
        current_strategy_date = snapshot.observed_at.astimezone(
            ZoneInfo(settings.timezone)
        ).date().isoformat()
        admission = service.entry_admission(
            snapshot, strategy_date=current_strategy_date
        )
        latest_authorization = store.latest_entry_authorization(
            environment=settings.environment,
            account_id=settings.expected_account_id,
        )
        non_arm_reasons = tuple(
            reason
            for reason in admission.reasons
            if reason not in {"EXECUTION_DISARMED", "MARKET_CLOSED"}
            and not reason.startswith("ENTRY_AUTHORIZATION_")
        )
        return {
            "environment": settings.environment,
            "current_strategy_date": current_strategy_date,
            "paper_mode": True,
            "execution_enabled": settings.execution_enabled,
            "account_suffix": account_suffix(str(snapshot.account["id"])),
            "account_status": snapshot.account.get("status"),
            "options_level": snapshot.options_level,
            "market_is_open": snapshot.market_is_open,
            "equity": str(snapshot.equity),
            "buying_power": str(snapshot.buying_power),
            "open_order_count": len(snapshot.open_orders),
            "position_count": len(snapshot.positions),
            "entry_allowed_now": admission.allowed,
            "entry_admission_reasons": list(admission.reasons),
            "broker_ready_except_arm_and_schedule": not non_arm_reasons,
            "entry_authorization": _preflight_entry_authorization(
                latest_authorization,
                now=datetime.now(UTC),
            ),
            "required_schema_hash": connection.registry.required_schema_hash,
            "mcp_tool_count": connection.registry.tool_count,
        }


def _handle_entry_authorization(
    settings: Any,
    args: argparse.Namespace,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Operate the durable one-shot entry authorization without broker calls."""

    store = _bound_authorization_store(settings)
    strategy_day = _optional_strategy_date(args.strategy_date)
    current = (now or datetime.now(UTC)).astimezone(UTC)

    if args.action == "status":
        authorization = (
            store.get_entry_authorization(
                environment=settings.environment,
                account_id=settings.expected_account_id,
                strategy_date=strategy_day.isoformat(),
            )
            if strategy_day is not None
            else store.latest_entry_authorization(
                environment=settings.environment,
                account_id=settings.expected_account_id,
            )
        )
        return {
            "status": "ok",
            "action": "status",
            "entry_authorization": _preflight_entry_authorization(
                authorization,
                now=current,
            ),
        }

    reason = str(args.reason or "").strip()
    if not reason:
        raise ValueError("--reason is required when arming or revoking entry authorization")

    if args.action == "arm":
        if strategy_day is None:
            raise ValueError("--strategy-date is required when arming entry authorization")
        events = load_events()
        if not verified_events_for_day(events, strategy_day):
            raise ValueError("strategy date has no verified event in the frozen configuration")
        expires_at = datetime.combine(
            strategy_day,
            events.entry_window.stop_new_orders,
            tzinfo=ZoneInfo(events.timezone),
        )
        if expires_at.astimezone(UTC) <= current:
            raise ValueError("entry authorization expiry is not in the future")
        expected_confirmation = _entry_authorization_confirmation(settings, strategy_day)
        if args.confirm != expected_confirmation:
            raise ValueError(f'use --confirm "{expected_confirmation}" to arm one paper entry')
        authorization = store.arm_entry_authorization(
            _entry_authorization_id(
                environment=settings.environment,
                account_id=settings.expected_account_id,
                strategy_day=strategy_day,
                strategy_version=events.strategy_version,
            ),
            environment=settings.environment,
            account_id=settings.expected_account_id,
            strategy_date=strategy_day.isoformat(),
            expires_at=expires_at,
            requested_by="cli_operator",
            reason=reason,
            armed_at=current,
        )
        return {
            "status": "ok",
            "action": "arm",
            "entry_authorization": _preflight_entry_authorization(
                authorization,
                now=current,
            ),
        }

    authorization = (
        store.get_entry_authorization(
            environment=settings.environment,
            account_id=settings.expected_account_id,
            strategy_date=strategy_day.isoformat(),
        )
        if strategy_day is not None
        else store.latest_entry_authorization(
            environment=settings.environment,
            account_id=settings.expected_account_id,
        )
    )
    if authorization is None:
        raise ValueError("entry authorization was not found")
    revoked = store.revoke_entry_authorization(
        str(authorization["authorization_id"]),
        reason=reason,
        requested_by="cli_operator",
        revoked_at=current,
    )
    return {
        "status": "ok",
        "action": "revoke",
        "entry_authorization": _preflight_entry_authorization(
            revoked,
            now=current,
        ),
    }


def _bound_authorization_store(settings: Any) -> Store:
    path = Path(settings.database_path)
    if not path.is_file():
        raise ValueError(
            "database must already be initialized and broker-bound before entry authorization"
        )
    store = Store(path)
    try:
        environment = store.get_metadata("environment")
        account_id = store.get_metadata("account_id")
    except sqlite3.DatabaseError as exc:
        raise ValueError(
            "database must already be initialized and broker-bound before entry authorization"
        ) from exc
    if environment != settings.environment or account_id != settings.expected_account_id:
        raise ValueError("database identity does not match the configured role and account")
    return store


def _optional_strategy_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("--strategy-date must use YYYY-MM-DD") from exc


def _entry_authorization_confirmation(settings: Any, strategy_day: date) -> str:
    return " ".join(
        (
            ENTRY_AUTHORIZATION_CONFIRM_PREFIX,
            str(settings.environment),
            strategy_day.isoformat(),
            account_suffix(str(settings.expected_account_id)),
        )
    )


def _entry_authorization_id(
    *,
    environment: str,
    account_id: str,
    strategy_day: date,
    strategy_version: str,
) -> str:
    material = "|".join(
        (
            "entry-authorization",
            environment,
            account_id,
            strategy_day.isoformat(),
            strategy_version,
        )
    )
    return "tt-entry-auth-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _redacted_entry_authorization(
    authorization: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if authorization is None:
        return None
    result = dict(authorization)
    account_id = str(result.pop("account_id", ""))
    result["account_suffix"] = account_suffix(account_id)
    return result


def _preflight_entry_authorization(
    authorization: dict[str, Any] | None,
    *,
    now: datetime,
) -> dict[str, Any]:
    redacted = _redacted_entry_authorization(authorization)
    if redacted is None:
        return {"effective_state": "MISSING"}
    effective_state = str(redacted.get("state") or "UNKNOWN").upper()
    if effective_state == "ARMED":
        expires_at = datetime.fromisoformat(str(redacted["expires_at"]))
        if expires_at.tzinfo is None:
            raise ValueError("entry authorization expiry must include a timezone")
        if expires_at.astimezone(UTC) <= now.astimezone(UTC):
            effective_state = "EXPIRED"
    redacted["effective_state"] = effective_state
    return redacted


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _find_expected_error(exc: BaseException) -> BaseException | None:
    if isinstance(exc, (ThetaTrapError, ValueError)):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            found = _find_expected_error(nested)
            if found is not None:
                return found
    return None


if __name__ == "__main__":
    sys.exit(main())
