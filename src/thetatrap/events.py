"""Frozen, first-party-sourced event definitions."""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, model_validator


EventStatus = Literal["verified", "verification_required", "ineligible"]
ReleaseTiming = Literal["after_market_close", "before_conference_call"]
ExclusionReason = Literal[
    "RELEASE_TIME_AMBIGUOUS",
    "REQUIRED_WEEKLY_EXPIRATIONS_UNAVAILABLE",
]


class EntryWindow(BaseModel):
    start: time
    stop_new_orders: time
    cancel_all_unfilled: time


class EventDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    source_published_on: date
    event_date: date
    release_timing: ReleaseTiming
    conference_call_at: datetime
    status: EventStatus
    exclusion_reason: ExclusionReason | None = None
    source_url: AnyHttpUrl

    @model_validator(mode="after")
    def validate_evidence_contract(self) -> "EventDefinition":
        if self.conference_call_at.tzinfo is None:
            raise ValueError("conference_call_at must include a timezone")
        if self.conference_call_at.date() != self.event_date:
            raise ValueError("conference call and event must use the same market date")
        if self.source_published_on > self.event_date:
            raise ValueError("event source cannot be published after the event date")
        if self.status == "verified":
            if self.release_timing != "after_market_close":
                raise ValueError("verified events must explicitly release after market close")
            if self.exclusion_reason is not None:
                raise ValueError("verified events cannot have an exclusion reason")
        elif self.exclusion_reason is None:
            raise ValueError("non-verified events require an exclusion reason")
        if (
            self.status == "verification_required"
            and self.exclusion_reason != "RELEASE_TIME_AMBIGUOUS"
        ):
            raise ValueError("verification_required events must identify timing ambiguity")
        if (
            self.status == "ineligible"
            and self.exclusion_reason != "REQUIRED_WEEKLY_EXPIRATIONS_UNAVAILABLE"
        ):
            raise ValueError("ineligible events must identify the contract exclusion")
        return self


class EventConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy_version: str
    timezone: Literal["America/New_York"]
    trade_expiration: date
    term_expiration: date
    entry_window: EntryWindow
    events: tuple[EventDefinition, ...]


def load_events(path: str | Path = "config/events.yaml") -> EventConfig:
    event_path = Path(path)
    raw = yaml.safe_load(event_path.read_text(encoding="utf-8"))
    return EventConfig.model_validate(raw)
