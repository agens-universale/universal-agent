"""Запись «смена модели» в файле треда: схема v1 и её разбор."""

from gc_core.persistence.model_choice_record import (
    MODEL_CHOICE_TYPE,
    MODEL_CHOICE_VERSION,
    ModelChoiceRecord,
    is_model_choice_record,
    parse_model_choice,
)


def _envelope(payload, record_type=MODEL_CHOICE_TYPE) -> dict:
    return {
        "type": record_type,
        "thread_id": "t_1",
        "system": "cli",
        "seq": 7,
        "ts": "2026-10-25T11:32:05.120Z",
        "model": payload,
    }


def test_round_trip_through_envelope():
    record = ModelChoiceRecord(choice="max", alias="big", before="light")

    assert parse_model_choice(_envelope(record.to_payload())) == record


def test_round_trip_auto_without_alias_and_before():
    record = ModelChoiceRecord(choice="auto", alias=None, before=None)

    assert parse_model_choice(_envelope(record.to_payload())) == record


def test_payload_carries_version_and_nulls():
    payload = ModelChoiceRecord("auto", None, None).to_payload()

    assert payload == {
        "v": MODEL_CHOICE_VERSION,
        "choice": "auto",
        "alias": None,
        "before": None,
    }


def test_extra_keys_are_ignored():
    payload = {**ModelChoiceRecord("mid", "mid", "auto").to_payload()}
    payload["later"] = 1

    assert parse_model_choice(_envelope(payload)) == ModelChoiceRecord(
        "mid", "mid", "auto"
    )


def test_recognizes_only_its_type():
    assert is_model_choice_record(_envelope({}))
    assert not is_model_choice_record({"type": "message", "payload": {}})
    assert not is_model_choice_record({"type": "turn_outcome"})


def test_foreign_or_broken_record_parses_to_none():
    good = ModelChoiceRecord("max", "big", "light").to_payload()

    assert parse_model_choice(_envelope(good, "turn_outcome")) is None
    assert parse_model_choice(_envelope(None)) is None
    assert parse_model_choice(_envelope({**good, "v": 2})) is None
    assert parse_model_choice(_envelope({**good, "choice": ""})) is None
    assert parse_model_choice(_envelope({**good, "alias": 3})) is None
    assert parse_model_choice(_envelope({**good, "before": []})) is None
