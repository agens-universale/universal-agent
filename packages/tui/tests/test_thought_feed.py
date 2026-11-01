"""Временные строки мыслей и записи (спека мыслей §7): пришла запись
попытки — строки прежних попыток снимаются, совпавшая становится
записанной; совпадения нет — снимаются все."""

from __future__ import annotations

from gc_tui.services.thought_feed import stale_on_record


def test_record_matches_latest_attempt_drops_older():
    assert stale_on_record(["att_1", "att_2"], "att_2") == [0]


def test_record_without_match_drops_all_closed():
    assert stale_on_record(["att_1"], None) == [0]


def test_newer_attempt_after_match_stays():
    assert stale_on_record(["att_1", "att_2", "att_3"], "att_2") == [0]


def test_record_of_unknown_attempt_drops_all():
    assert stale_on_record(["att_1", "att_2"], "att_9") == [0, 1]


def test_no_provisional_rows():
    assert stale_on_record([], "att_1") == []


def test_current_attempt_row_is_never_dropped():
    """Запись старой попытки пришла, когда уже идёт новая: живая строка
    текущей попытки остаётся."""
    assert stale_on_record(["att_1", "att_2"], "att_0", current="att_2") == [0]
    assert stale_on_record(["att_2"], None, current="att_2") == []
