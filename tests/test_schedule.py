from datetime import UTC, date, datetime

from thetatrap.events import load_events
from thetatrap.schedule import ScheduleAction, action_for_time, verified_events_for_day


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
