from datetime import UTC, date, datetime

from thetatrap.events import load_events
from thetatrap.schedule import (
    ScheduleAction,
    action_for_time,
    verified_events_for_day,
)
from thetatrap.settings import StrategyProfile


def at_utc(hour: int, minute: int, day: int = 1) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def test_only_verified_events_enter_runtime_universe() -> None:
    config = load_events()
    events = verified_events_for_day(config, date(2026, 9, 1))
    assert {event.symbol for event in events} == {"PANW", "MDB", "CRDO", "GTLB"}
    next_day = verified_events_for_day(config, date(2026, 9, 2))
    assert {event.symbol for event in next_day} == {"AVGO", "SNOW", "AI"}


def test_entry_window_is_new_york_time() -> None:
    config = load_events()
    assert (
        action_for_time(
            now=at_utc(18, 50),
            config=config,
            open_trade_date=None,
            has_working_entry=False,
        )
        == ScheduleAction.ENTRY_SCAN
    )
    assert (
        action_for_time(
            now=at_utc(19, 41),
            config=config,
            open_trade_date=None,
            has_working_entry=False,
        )
        == ScheduleAction.OBSERVE
    )


def test_overdue_position_always_routes_to_exit() -> None:
    config = load_events()
    assert (
        action_for_time(
            now=at_utc(15, 0, day=2),
            config=config,
            open_trade_date=date(2026, 9, 1),
            has_working_entry=False,
        )
        == ScheduleAction.EXIT
    )


def test_working_entry_routes_to_cancel_after_cutoff() -> None:
    config = load_events()
    assert (
        action_for_time(
            now=at_utc(19, 45),
            config=config,
            open_trade_date=None,
            has_working_entry=True,
        )
        == ScheduleAction.CANCEL_UNFILLED
    )


def test_intraday_canary_scans_without_an_earnings_event() -> None:
    config = load_events()
    assert verified_events_for_day(config, date(2026, 9, 3)) == ()
    assert (
        action_for_time(
            now=at_utc(13, 45, day=3),
            config=config,
            open_trade_date=None,
            has_working_entry=False,
            strategy_profile=StrategyProfile.INTRADAY_CANARY,
        )
        == ScheduleAction.ENTRY_SCAN
    )
    assert (
        action_for_time(
            now=at_utc(14, 45, day=3),
            config=config,
            open_trade_date=None,
            has_working_entry=False,
            strategy_profile=StrategyProfile.INTRADAY_CANARY,
        )
        == ScheduleAction.ENTRY_SCAN
    )
    assert (
        action_for_time(
            now=at_utc(14, 46, day=3),
            config=config,
            open_trade_date=None,
            has_working_entry=False,
            strategy_profile=StrategyProfile.INTRADAY_CANARY,
        )
        == ScheduleAction.OBSERVE
    )


def test_intraday_canary_cancels_working_entry_at_cutoff() -> None:
    config = load_events()
    assert (
        action_for_time(
            now=at_utc(14, 50, day=3),
            config=config,
            open_trade_date=None,
            has_working_entry=True,
            strategy_profile=StrategyProfile.INTRADAY_CANARY,
        )
        == ScheduleAction.CANCEL_UNFILLED
    )


def test_intraday_working_entry_wins_over_same_day_open_trade_marker() -> None:
    config = load_events()
    assert (
        action_for_time(
            now=at_utc(14, 50, day=3),
            config=config,
            open_trade_date=date(2026, 9, 3),
            has_working_entry=True,
            strategy_profile=StrategyProfile.INTRADAY_CANARY,
        )
        == ScheduleAction.CANCEL_UNFILLED
    )


def test_intraday_canary_exits_same_day_from_1515() -> None:
    config = load_events()
    assert (
        action_for_time(
            now=at_utc(19, 14, day=3),
            config=config,
            open_trade_date=date(2026, 9, 3),
            has_working_entry=False,
            strategy_profile=StrategyProfile.INTRADAY_CANARY,
        )
        == ScheduleAction.OBSERVE
    )
    assert (
        action_for_time(
            now=at_utc(19, 15, day=3),
            config=config,
            open_trade_date=date(2026, 9, 3),
            has_working_entry=False,
            strategy_profile=StrategyProfile.INTRADAY_CANARY,
        )
        == ScheduleAction.EXIT
    )


def test_intraday_canary_retains_next_day_emergency_exit() -> None:
    config = load_events()
    assert (
        action_for_time(
            now=at_utc(13, 44, day=4),
            config=config,
            open_trade_date=date(2026, 9, 3),
            has_working_entry=False,
            strategy_profile=StrategyProfile.INTRADAY_CANARY,
        )
        == ScheduleAction.OBSERVE
    )
    assert (
        action_for_time(
            now=at_utc(13, 45, day=4),
            config=config,
            open_trade_date=date(2026, 9, 3),
            has_working_entry=False,
            strategy_profile=StrategyProfile.INTRADAY_CANARY,
        )
        == ScheduleAction.EXIT
    )
