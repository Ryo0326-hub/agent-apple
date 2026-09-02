"""Timezone-safe action windows derived from the frozen event configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

from thetatrap.events import EventConfig, EventDefinition
from thetatrap.settings import StrategyProfile


NEW_YORK = ZoneInfo("America/New_York")


class ScheduleAction(StrEnum):
    OBSERVE = "observe"
    ENTRY_SCAN = "entry_scan"
    CANCEL_UNFILLED = "cancel_unfilled"
    EXIT = "exit"
    FINAL_SNAPSHOT = "final_snapshot"
    IDLE = "idle"


@dataclass(frozen=True)
class IntradaySession:
    """One bounded intraday session, expressed entirely in New York time."""

    trade_date: date
    entry_start: time
    stop_new_orders: time
    cancel_all_unfilled: time
    exit_start: time

    def __post_init__(self) -> None:
        if not (
            self.entry_start
            <= self.stop_new_orders
            < self.cancel_all_unfilled
            < self.exit_start
        ):
            raise ValueError("intraday session times must be strictly ordered")


SEP3_INTRADAY_CANARY_SESSION = IntradaySession(
    trade_date=date(2026, 9, 3),
    entry_start=time(9, 45),
    stop_new_orders=time(10, 45),
    cancel_all_unfilled=time(10, 50),
    exit_start=time(15, 15),
)


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
    strategy_profile: StrategyProfile = StrategyProfile.EARNINGS,
    intraday_session: IntradaySession = SEP3_INTRADAY_CANARY_SESSION,
) -> ScheduleAction:
    market_now = to_market_time(now)
    day = market_now.date()
    clock = market_now.timetz().replace(tzinfo=None)

    if open_trade_date is not None and day > open_trade_date:
        if has_working_entry:
            return ScheduleAction.CANCEL_UNFILLED
        if time(9, 45) <= clock:
            return ScheduleAction.EXIT
        return ScheduleAction.OBSERVE

    if strategy_profile is StrategyProfile.INTRADAY_CANARY:
        if day == intraday_session.trade_date:
            if has_working_entry:
                if clock >= intraday_session.cancel_all_unfilled:
                    return ScheduleAction.CANCEL_UNFILLED
                if (
                    intraday_session.entry_start
                    <= clock
                    <= intraday_session.stop_new_orders
                ):
                    return ScheduleAction.ENTRY_SCAN
                return ScheduleAction.OBSERVE
            if open_trade_date == day:
                if clock >= intraday_session.exit_start:
                    return ScheduleAction.EXIT
                return ScheduleAction.OBSERVE
            if (
                intraday_session.entry_start
                <= clock
                <= intraday_session.stop_new_orders
            ):
                return ScheduleAction.ENTRY_SCAN
            return ScheduleAction.OBSERVE
        if has_working_entry and day > intraday_session.trade_date:
            return ScheduleAction.CANCEL_UNFILLED
        if day == config.trade_expiration and clock >= time(9, 30):
            return ScheduleAction.FINAL_SNAPSHOT
        return ScheduleAction.IDLE

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
