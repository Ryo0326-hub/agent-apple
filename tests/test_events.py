from thetatrap.events import load_events


def test_frozen_event_universe() -> None:
    config = load_events()
    verified = {event.symbol for event in config.events if event.status == "verified"}
    conditional = {
        event.symbol for event in config.events if event.status == "verification_required"
    }
    ineligible = {event.symbol for event in config.events if event.status == "ineligible"}
    assert verified == {"PANW", "MDB", "CRDO", "GTLB", "AVGO", "SNOW", "AI"}
    assert conditional == {"DELL"}
    assert ineligible == {"NTAP"}
    assert config.strategy_version == "1.2"
    assert config.trade_expiration.isoformat() == "2026-09-04"
    assert config.term_expiration.isoformat() == "2026-09-11"


def test_event_evidence_does_not_invent_an_exact_release_timestamp() -> None:
    config = load_events()
    serialized = config.model_dump(mode="json")
    assert all("event_at" not in event for event in serialized["events"])

    by_symbol = {event.symbol: event for event in config.events}
    assert by_symbol["DELL"].release_timing == "before_conference_call"
    assert by_symbol["DELL"].exclusion_reason == "RELEASE_TIME_AMBIGUOUS"
    assert (
        by_symbol["NTAP"].exclusion_reason
        == "REQUIRED_WEEKLY_EXPIRATIONS_UNAVAILABLE"
    )
    assert all(
        event.release_timing == "after_market_close"
        and event.exclusion_reason is None
        for event in config.events
        if event.status == "verified"
    )
