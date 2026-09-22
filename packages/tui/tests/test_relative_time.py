"""format_relative_local — Rule A, локальное время. now инъектируется.
Времена — середина суток, чтобы сдвиг локали не флипал календарную дату."""
from datetime import UTC, datetime, timedelta

from gc_tui.services.relative_time import format_relative_local

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC).astimezone()


def test_just_now():
    ts = (NOW - timedelta(seconds=30)).astimezone(UTC).isoformat()
    assert format_relative_local(ts, now=NOW) == "только что"


def test_minutes_ago():
    ts = (NOW - timedelta(minutes=12)).astimezone(UTC).isoformat()
    assert format_relative_local(ts, now=NOW) == "12 мин назад"


def test_hours_ago():
    ts = (NOW - timedelta(hours=5)).astimezone(UTC).isoformat()
    assert format_relative_local(ts, now=NOW) == "5 ч назад"


def test_yesterday():
    ts = (NOW - timedelta(days=1)).astimezone(UTC).isoformat()
    out = format_relative_local(ts, now=NOW)
    assert out.startswith("вчера, ")


def test_days_ago():
    ts = (NOW - timedelta(days=5)).astimezone(UTC).isoformat()
    assert format_relative_local(ts, now=NOW) == "5 дн назад"


def test_this_year_date():
    ts = datetime(2026, 3, 10, 12, 0, tzinfo=UTC).isoformat()
    out = format_relative_local(ts, now=NOW)
    assert out.startswith("10 марта, ")


def test_older_year_date():
    ts = datetime(2025, 5, 29, 12, 0, tzinfo=UTC).isoformat()
    out = format_relative_local(ts, now=NOW)
    assert out == "29 мая 2025"


def test_garbage_returns_input():
    assert format_relative_local("not-a-date", now=NOW) == "not-a-date"
    assert format_relative_local("", now=NOW) == ""
