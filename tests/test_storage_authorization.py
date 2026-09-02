from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from thetatrap.errors import AccountIdentityError
from thetatrap.storage import StorageInvariantError, Store


NOW = datetime(2026, 9, 1, 18, 50, tzinfo=UTC)
ENVIRONMENT = "development"
ACCOUNT_ID = "account-uuid-for-tests"
STRATEGY_DATE = "2026-09-01"
REQUEST = {
    "client_order_id": "tt-entry-canary",
    "limit_price": "-0.75",
    "order_class": "mleg",
    "qty": "1",
    "time_in_force": "day",
    "type": "limit",
}


def make_store(tmp_path: Path, *, bind: bool = True) -> Store:
    store = Store(tmp_path / "runtime.sqlite3")
    store.initialize()
    if bind:
        store.bind_identity(ENVIRONMENT, ACCOUNT_ID, ACCOUNT_ID)
    return store


def prepare_entry(
    store: Store,
    *,
    authorization_id: str = "authorization-1",
    run_id: str = "run-1",
    intent_id: str = "intent-1",
    chain_id: str = "chain-1",
    client_order_id: str = "tt-entry-canary",
    request: dict[str, str] | None = None,
    policy_check: bool = True,
) -> dict[str, str]:
    durable_request = request or {**REQUEST, "client_order_id": client_order_id}
    store.create_strategy_run(
        run_id,
        environment=ENVIRONMENT,
        strategy_date=STRATEGY_DATE,
        strategy_version="authorization-test-1",
        config_hash="config-hash",
    )
    store.transition_strategy_run(run_id, "SCREENING", "WINDOW_OPEN")
    store.transition_strategy_run(run_id, "AI_REVIEW", "CANDIDATE_READY")
    if policy_check:
        store.transition_strategy_run(run_id, "POLICY_CHECK", "AGENT_ALLOWED")
    store.record_order_intent(
        intent_id,
        run_id=run_id,
        purpose="entry",
        client_order_id=client_order_id,
        payload=durable_request,
    )
    store.create_order_chain(
        chain_id,
        run_id=run_id,
        intent_id=intent_id,
        purpose="entry",
    )
    store.arm_entry_authorization(
        authorization_id,
        ENVIRONMENT,
        ACCOUNT_ID,
        STRATEGY_DATE,
        NOW + timedelta(minutes=15),
        "operator@example.test",
        "single paper canary",
        NOW,
    )
    return durable_request


def begin_entry(
    store: Store,
    *,
    authorization_id: str = "authorization-1",
    run_id: str = "run-1",
    intent_id: str = "intent-1",
    chain_id: str = "chain-1",
    attempt_id: str = "attempt-1",
    client_order_id: str = "tt-entry-canary",
    request: dict[str, str] | None = None,
    observed_at: datetime = NOW + timedelta(minutes=1),
) -> dict[str, object]:
    return store.begin_authorized_entry_submission(
        authorization_id,
        ENVIRONMENT,
        ACCOUNT_ID,
        STRATEGY_DATE,
        run_id,
        intent_id,
        chain_id,
        attempt_id,
        client_order_id,
        request or {**REQUEST, "client_order_id": client_order_id},
        observed_at,
    )


def assert_submission_not_started(store: Store) -> None:
    authorization = store.get_entry_authorization(
        ENVIRONMENT, ACCOUNT_ID, STRATEGY_DATE
    )
    assert authorization is not None and authorization["state"] == "ARMED"
    assert store.get_strategy_run("run-1")["state"] == "POLICY_CHECK"  # type: ignore[index]
    assert store.get_order_chain("chain-1")["state"] == "PLANNED"  # type: ignore[index]
    assert store.latest_order_attempt("chain-1") is None


def test_authorization_is_identity_bound_unique_queryable_and_revocable(
    tmp_path: Path,
) -> None:
    unbound = make_store(tmp_path / "unbound", bind=False)
    with pytest.raises(AccountIdentityError, match="not bound"):
        unbound.arm_entry_authorization(
            "authorization-unbound",
            ENVIRONMENT,
            ACCOUNT_ID,
            STRATEGY_DATE,
            NOW + timedelta(minutes=15),
            "operator",
            "canary",
            NOW,
        )

    store = make_store(tmp_path)
    armed = store.arm_entry_authorization(
        "authorization-1",
        ENVIRONMENT,
        ACCOUNT_ID,
        STRATEGY_DATE,
        NOW + timedelta(minutes=15),
        "operator",
        "paper canary",
        NOW,
    )
    repeated = store.arm_entry_authorization(
        "authorization-1",
        ENVIRONMENT,
        ACCOUNT_ID,
        STRATEGY_DATE,
        NOW + timedelta(minutes=15),
        "operator",
        "paper canary",
        NOW,
    )
    assert repeated == armed
    assert armed["state"] == "ARMED"
    assert store.get_entry_authorization(
        ENVIRONMENT, ACCOUNT_ID, STRATEGY_DATE
    ) == armed
    assert store.latest_entry_authorization(ENVIRONMENT, ACCOUNT_ID) == armed

    with pytest.raises(StorageInvariantError, match="already exists"):
        store.arm_entry_authorization(
            "authorization-2",
            ENVIRONMENT,
            ACCOUNT_ID,
            STRATEGY_DATE,
            NOW + timedelta(minutes=20),
            "operator",
            "replacement",
            NOW,
        )

    revoked = store.revoke_entry_authorization(
        "authorization-1",
        "operator canceled canary",
        "operator",
        NOW + timedelta(minutes=2),
    )
    assert revoked["state"] == "REVOKED"
    assert revoked["revoke_reason"] == "operator canceled canary"
    assert store.revoke_entry_authorization(
        "authorization-1", "operator canceled canary", "operator"
    ) == revoked
    with pytest.raises(StorageInvariantError, match="cannot be re-armed"):
        store.arm_entry_authorization(
            "authorization-1",
            ENVIRONMENT,
            ACCOUNT_ID,
            STRATEGY_DATE,
            NOW + timedelta(minutes=15),
            "operator",
            "paper canary",
            NOW,
        )


def test_begin_authorized_submission_commits_all_entry_state_atomically(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    request = prepare_entry(store)

    consumed = begin_entry(store, request=request)

    assert consumed["state"] == "CONSUMED"
    assert consumed["consumed_run_id"] == "run-1"
    assert consumed["consumed_intent_id"] == "intent-1"
    assert consumed["consumed_chain_id"] == "chain-1"
    assert consumed["consumed_attempt_id"] == "attempt-1"
    assert store.get_strategy_run("run-1")["state"] == "SUBMITTING"  # type: ignore[index]
    assert store.get_order_chain("chain-1")["state"] == "SUBMITTING"  # type: ignore[index]

    attempt = store.latest_order_attempt("chain-1")
    assert attempt is not None
    assert attempt["attempt_id"] == "attempt-1"
    assert attempt["sequence"] == 0
    assert attempt["client_order_id"] == "tt-entry-canary"
    assert attempt["request"] == request
    assert attempt["created_at"] == "2026-09-01T18:51:00.000000+00:00"

    run_transition = store.list_strategy_transitions("run-1")[-1]
    assert run_transition["reason_code"] == "ENTRY_AUTHORIZATION_CONSUMED"
    assert run_transition["evidence"]["authorization_id"] == "[REDACTED]"
    chain_transition = store.list_order_status_history("chain-1")[-1]
    assert chain_transition["from_state"] == "PLANNED"
    assert chain_transition["to_state"] == "SUBMITTING"
    assert chain_transition["attempt_id"] == "attempt-1"
    assert chain_transition["detail"]["authorization_id"] == "[REDACTED]"

    with pytest.raises(StorageInvariantError, match="not ARMED: CONSUMED"):
        begin_entry(store, request=request)
    with pytest.raises(StorageInvariantError, match="cannot be revoked"):
        store.revoke_entry_authorization(
            "authorization-1", "too late", "operator", NOW + timedelta(minutes=2)
        )


def test_kill_switch_revokes_unused_authorization_and_clear_does_not_rearm(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    request = prepare_entry(store)

    control = store.activate_kill_switch(
        "operator emergency stop",
        "operator",
        activated_at=NOW + timedelta(minutes=1),
    )

    authorization = store.get_entry_authorization(
        ENVIRONMENT, ACCOUNT_ID, STRATEGY_DATE
    )
    assert authorization is not None
    assert authorization["state"] == "REVOKED"
    assert authorization["revoke_reason"] == "kill switch: operator emergency stop"
    with pytest.raises(StorageInvariantError, match="blocks entry authorization"):
        store.arm_entry_authorization(
            "authorization-2",
            ENVIRONMENT,
            ACCOUNT_ID,
            "2026-09-02",
            NOW + timedelta(days=1, minutes=15),
            "operator",
            "must remain blocked",
            NOW + timedelta(days=1),
        )

    cleared = store.clear_kill_switch(
        "broker state reviewed",
        "operator",
        expected_version=control["version"],
        cleared_at=NOW + timedelta(minutes=2),
    )

    assert cleared["kill_switch_enabled"] is False
    with pytest.raises(StorageInvariantError, match="not ARMED: REVOKED"):
        begin_entry(store, request=request, observed_at=NOW + timedelta(minutes=3))


@pytest.mark.parametrize(
    "failure",
    ["expired", "kill_switch", "tampered_request", "bad_run", "bad_chain"],
)
def test_failed_authorized_submission_rolls_back_every_write(
    tmp_path: Path, failure: str
) -> None:
    store = make_store(tmp_path)
    request = prepare_entry(store, policy_check=failure != "bad_run")
    if failure == "kill_switch":
        store.activate_kill_switch("operator stop", "operator", activated_at=NOW)
    elif failure == "bad_chain":
        store.transition_order_chain("chain-1", "ERROR")

    submitted_request = (
        {**request, "qty": "2"} if failure == "tampered_request" else request
    )
    observed_at = (
        NOW + timedelta(minutes=15)
        if failure == "expired"
        else NOW + timedelta(minutes=1)
    )
    expected_error = {
        "expired": "expired",
        "kill_switch": "not ARMED: REVOKED",
        "tampered_request": "durable entry intent",
        "bad_run": "POLICY_CHECK",
        "bad_chain": "PLANNED",
    }[failure]
    with pytest.raises(StorageInvariantError, match=expected_error):
        begin_entry(store, request=submitted_request, observed_at=observed_at)

    authorization = store.get_entry_authorization(
        ENVIRONMENT, ACCOUNT_ID, STRATEGY_DATE
    )
    expected_authorization_state = "REVOKED" if failure == "kill_switch" else "ARMED"
    assert authorization is not None
    assert authorization["state"] == expected_authorization_state
    assert store.latest_order_attempt("chain-1") is None
    expected_run_state = "AI_REVIEW" if failure == "bad_run" else "POLICY_CHECK"
    assert store.get_strategy_run("run-1")["state"] == expected_run_state  # type: ignore[index]
    expected_chain_state = "ERROR" if failure == "bad_chain" else "PLANNED"
    assert store.get_order_chain("chain-1")["state"] == expected_chain_state  # type: ignore[index]


def test_begin_rejects_identity_and_date_mismatches_without_consuming(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    request = prepare_entry(store)
    with pytest.raises(AccountIdentityError, match="another Alpaca account"):
        store.begin_authorized_entry_submission(
            "authorization-1",
            ENVIRONMENT,
            "different-account",
            STRATEGY_DATE,
            "run-1",
            "intent-1",
            "chain-1",
            "attempt-1",
            "tt-entry-canary",
            request,
            NOW + timedelta(minutes=1),
        )
    with pytest.raises(StorageInvariantError, match="environment/account/date"):
        store.begin_authorized_entry_submission(
            "authorization-1",
            ENVIRONMENT,
            ACCOUNT_ID,
            "2026-09-02",
            "run-1",
            "intent-1",
            "chain-1",
            "attempt-1",
            "tt-entry-canary",
            request,
            NOW + timedelta(minutes=1),
        )
    assert_submission_not_started(store)


def test_concurrent_claims_consume_authorization_once(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first_request = prepare_entry(store)
    second_request = {**REQUEST, "client_order_id": "tt-entry-alternate"}
    store.record_order_intent(
        "intent-2",
        run_id="run-1",
        purpose="entry",
        client_order_id="tt-entry-alternate",
        payload=second_request,
    )
    store.create_order_chain(
        "chain-2", run_id="run-1", intent_id="intent-2", purpose="entry"
    )
    barrier = Barrier(2)

    def claim(
        intent_id: str,
        chain_id: str,
        attempt_id: str,
        client_order_id: str,
        request: dict[str, str],
    ) -> tuple[str, str]:
        barrier.wait()
        try:
            begin_entry(
                store,
                intent_id=intent_id,
                chain_id=chain_id,
                attempt_id=attempt_id,
                client_order_id=client_order_id,
                request=request,
            )
        except StorageInvariantError:
            return ("blocked", intent_id)
        return ("consumed", intent_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda values: claim(*values),
                [
                    (
                        "intent-1",
                        "chain-1",
                        "attempt-1",
                        "tt-entry-canary",
                        first_request,
                    ),
                    (
                        "intent-2",
                        "chain-2",
                        "attempt-2",
                        "tt-entry-alternate",
                        second_request,
                    ),
                ],
            )
        )

    assert sorted(result[0] for result in results) == ["blocked", "consumed"]
    winner = next(intent_id for status, intent_id in results if status == "consumed")
    authorization = store.get_entry_authorization(
        ENVIRONMENT, ACCOUNT_ID, STRATEGY_DATE
    )
    assert authorization is not None
    assert authorization["state"] == "CONSUMED"
    assert authorization["consumed_intent_id"] == winner
    attempts = [
        store.latest_order_attempt("chain-1"),
        store.latest_order_attempt("chain-2"),
    ]
    assert sum(attempt is not None for attempt in attempts) == 1


def test_authorization_rows_reject_direct_tampering(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.arm_entry_authorization(
        "authorization-1",
        ENVIRONMENT,
        ACCOUNT_ID,
        STRATEGY_DATE,
        NOW + timedelta(minutes=15),
        "operator",
        "canary",
        NOW,
    )
    with store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE entry_authorizations
                SET expires_at='2026-09-01T20:00:00.000000+00:00'
                WHERE authorization_id='authorization-1'
                """
            )
    with store.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM entry_authorizations WHERE authorization_id='authorization-1'"
            )
