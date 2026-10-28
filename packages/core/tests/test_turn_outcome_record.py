import pytest
from gc_core.persistence.turn_outcome import (
    TURN_OUTCOME_TYPE,
    TURN_OUTCOME_VERSION,
    USER_STOP,
    TurnOutcome,
    is_turn_outcome_record,
    last_turn_of,
    parse_turn_outcome,
    turn_entry,
    turn_shown_in,
)

STARTED = "2026-10-22T11:30:53.004Z"
ENDED = "2026-10-22T11:32:05.118Z"


def _outcome(**overrides) -> TurnOutcome:
    fields = {
        "status": "done",
        "reason": None,
        "opened_by": "m_1",
        "started_at": STARTED,
        "ended_at": ENDED,
        "duration_sec": 63.4,
        "error": None,
    }
    fields.update(overrides)
    return TurnOutcome(**fields)


def _envelope(outcome: dict, record_type: str = TURN_OUTCOME_TYPE) -> dict:
    return {
        "type": record_type,
        "thread_id": "t_1",
        "system": "cli",
        "seq": 41,
        "ts": "2026-10-22T11:32:05.120Z",
        "outcome": outcome,
    }


def test_round_trip_through_envelope():
    outcome = _outcome(
        status="error",
        error={"type": "RuntimeError", "message": "boom"},
    )

    assert parse_turn_outcome(_envelope(outcome.to_payload())) == outcome


def test_round_trip_stopped_by_user():
    outcome = _outcome(status="stopped", reason=USER_STOP)

    assert parse_turn_outcome(_envelope(outcome.to_payload())) == outcome


def test_payload_carries_version_and_nulls():
    payload = _outcome(opened_by=None).to_payload()

    assert payload == {
        "v": TURN_OUTCOME_VERSION,
        "status": "done",
        "reason": None,
        "opened_by": None,
        "started_at": STARTED,
        "ended_at": ENDED,
        "duration_sec": 63.4,
        "error": None,
    }


def test_null_opened_by_survives_round_trip():
    outcome = _outcome(opened_by=None)

    assert parse_turn_outcome(_envelope(outcome.to_payload())) == outcome


def test_unknown_version_is_rejected():
    payload = _outcome().to_payload() | {"v": 2}

    assert parse_turn_outcome(_envelope(payload)) is None


def test_other_record_type_is_rejected():
    payload = _outcome().to_payload()

    assert parse_turn_outcome(_envelope(payload, "message")) is None


def test_live_status_is_rejected():
    payload = _outcome().to_payload() | {"status": "running"}

    assert parse_turn_outcome(_envelope(payload)) is None


def test_missing_ended_at_is_rejected():
    payload = _outcome().to_payload()
    del payload["ended_at"]

    assert parse_turn_outcome(_envelope(payload)) is None


def test_malformed_fields_are_rejected():
    base = _outcome().to_payload()
    broken = [
        base | {"duration_sec": "63"},
        base | {"duration_sec": True},
        base | {"reason": 5},
        base | {"opened_by": ["m_1"]},
        base | {"started_at": None},
        base | {"error": "boom"},
        base | {"error": {"type": "E", "message": 1}},
    ]

    assert [parse_turn_outcome(_envelope(p)) for p in broken] == [None] * 7


def test_missing_or_non_mapping_outcome_is_rejected():
    record = _envelope({})
    del record["outcome"]

    assert parse_turn_outcome(record) is None
    assert parse_turn_outcome(_envelope("done")) is None


def test_extra_key_in_outcome_is_ignored():
    outcome = _outcome()
    payload = outcome.to_payload() | {"future_field": 1}

    assert parse_turn_outcome(_envelope(payload)) == outcome


def test_integer_duration_is_accepted_as_float():
    payload = _outcome().to_payload() | {"duration_sec": 12}

    assert parse_turn_outcome(_envelope(payload)) == _outcome(
        duration_sec=12.0
    )


def test_is_turn_outcome_record():
    assert is_turn_outcome_record({"type": TURN_OUTCOME_TYPE}) is True
    assert is_turn_outcome_record({"type": "message"}) is False
    assert is_turn_outcome_record({}) is False


# ---- карта `calls`: время вызовов хода (P4) -------------------------------


def test_round_trip_with_calls():
    outcome = _outcome(calls={"c1": 1.2, "c2": 0.0})
    payload = outcome.to_payload()

    assert payload["calls"] == {"c1": 1.2, "c2": 0.0}
    assert payload["v"] == 1
    assert parse_turn_outcome(_envelope(payload)) == outcome


def test_payload_without_calls_has_no_key():
    assert "calls" not in _outcome().to_payload()
    assert "calls" not in _outcome(calls={}).to_payload()


def test_old_record_without_calls_parses():
    parsed = parse_turn_outcome(_envelope(_outcome().to_payload()))

    assert parsed is not None
    assert parsed.calls is None


@pytest.mark.parametrize(
    "calls", ["x", {"c": -1}, {"c": "1"}, {"c": True}, [1, 2], {1: 1.0}]
)
def test_broken_calls_is_dropped_record_kept(calls):
    payload = _outcome().to_payload() | {"calls": calls}
    parsed = parse_turn_outcome(_envelope(payload))

    assert parsed == _outcome()
    assert parsed.calls is None


def test_unknown_key_next_to_calls_is_ignored():
    payload = _outcome(calls={"c1": 3}).to_payload() | {"later": 1}

    assert parse_turn_outcome(_envelope(payload)).calls == {"c1": 3}


# ---- запись потока `turns` и правило сироты ---------------------------------


def test_entry_carries_anchor_and_payload():
    outcome = _outcome()

    entry = turn_entry(outcome, after="m_9")

    assert entry == {"after": "m_9", "outcome": outcome.to_payload()}


def test_envelope_of_entry_parses_back():
    outcome = _outcome()
    record = {"type": TURN_OUTCOME_TYPE, **turn_entry(outcome, after="m_9")}

    assert parse_turn_outcome(record) == outcome


def test_record_is_shown_while_its_reply_is_in_history():
    record = turn_entry(_outcome(opened_by="m_1"), after="m_9")

    assert turn_shown_in(record, {"m_1", "m_9"}) is True
    assert turn_shown_in(record, {"m_9"}) is False


def test_record_without_reply_is_anchored_by_last_message():
    record = turn_entry(_outcome(opened_by=None), after="m_9")

    assert turn_shown_in(record, {"m_9"}) is True
    assert turn_shown_in(record, set()) is False


def test_record_without_any_anchor_is_an_orphan():
    record = turn_entry(_outcome(opened_by=None), after=None)

    assert turn_shown_in(record, {"m_1"}) is False


def test_last_turn_is_status_and_reason():
    outcome = _outcome(status="stopped", reason="step_limit")

    assert last_turn_of(outcome) == {
        "status": "stopped",
        "reason": "step_limit",
    }
