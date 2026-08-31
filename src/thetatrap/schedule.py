"""Timezone-safe action windows derived from the frozen event configuration."""

from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

from thetatrap.events import EventConfig, EventDefinition


NEW_YORK = ZoneInfo("America/New_York")


class ScheduleAction(StrEnum):
    OBSERVE = "observe"
    ENTRY_SCAN = "entry_scan"
    CANCEL_UNFILLED = "cancel_unfilled"
    EXIT = "exit"
    FINAL_SNAPSHOT = "final_snapshot"
    IDLE = "idle"


def to_market_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("scheduler requires timezone-aware datetimes")
    return value.astimezone(NEW_YORK)


def verified_events_for_day(config: EventConfig, day: date) -> tuple[EventDefinition, ...]:
    return tuple(
        event
        for event in config.events
        if event.status == "verified" and event.event_date == day
    )


def action_for_time(
    *,
    now: datetime,
    config: EventConfig,
    open_trade_date: date | None,
    has_working_entry: bool,
) -> ScheduleAction:
    market_now = to_market_time(now)
    day = market_now.date()
    clock = market_now.timetz().replace(tzinfo=None)

    if open_trade_date is not None and day > open_trade_date:
        if time(9, 45) <= clock:
            return ScheduleAction.EXIT
        return ScheduleAction.OBSERVE

    events = verified_events_for_day(config, day)
    if events and config.entry_window.start <= clock <= config.entry_window.stop_new_orders:
        return ScheduleAction.ENTRY_SCAN
    if events and has_working_entry and clock >= config.entry_window.cancel_all_unfilled:
        return ScheduleAction.CANCEL_UNFILLED

    if day == config.trade_expiration and clock >= time(9, 30):
        return ScheduleAction.FINAL_SNAPSHOT
    if events:
        return ScheduleAction.OBSERVE
    return ScheduleAction.IDLE
