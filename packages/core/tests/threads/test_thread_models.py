"""Модели хранилища тредов v2: ключ, имена потоков, лишние ключи, дайджест."""

from datetime import UTC, datetime, timedelta

import pytest
from gc_core.threads.models import (
    Lease,
    RuntimeStatus,
    ThreadChange,
    ThreadKey,
    ThreadQuery,
    Usage,
    change_digest,
    check_stream_name,
)
from pydantic import ValidationError


def _lease(minutes=1):
    return Lease(
        owner="a",
        epoch=1,
        expires_at=datetime(2026, 1, 1, tzinfo=UTC)
        + timedelta(minutes=minutes),
    )


def test_thread_key_normalizes_and_validates():
    assert ThreadKey(system=" CLI ", thread="t-1").system == "cli"
    with pytest.raises(ValidationError):
        ThreadKey(system="cli", thread="a/b")
    assert {ThreadKey(system="cli", thread="t"): 1}[
        ThreadKey(system="cli", thread="t")
    ] == 1


@pytest.mark.parametrize("name", ["messages", "inbox", "chat.responses"])
def test_stream_names_accepted(name):
    assert check_stream_name(name) == name


@pytest.mark.parametrize("name", ["chat", "Chat.x", "a.b.c", "", "edits."])
def test_stream_names_rejected(name):
    with pytest.raises(ValueError):
        check_stream_name(name)


def test_unknown_runtime_and_usage_keys_survive():
    assert RuntimeStatus(image_seq=3).model_dump()["image_seq"] == 3
    usage = Usage(by_category={"main": {"input": 1}}, currency="RUB")
    assert usage.model_dump()["currency"] == "RUB"


def test_change_digest_ignores_lease_only():
    one = ThreadChange(
        op_id="op", expected_revision=0, lease=_lease(1), append_messages=[{}]
    )
    renewed = one.model_copy(update={"lease": _lease(5)})
    other = one.model_copy(update={"append_messages": [{"id": "x"}]})
    assert change_digest(one) == change_digest(renewed)
    assert change_digest(one) != change_digest(other)


def test_query_limit_bounds():
    with pytest.raises(ValidationError):
        ThreadQuery(limit=0)
    with pytest.raises(ValidationError):
        ThreadQuery(limit=1001)


def test_models_are_frozen():
    key = ThreadKey(system="cli", thread="t")
    with pytest.raises(ValidationError):
        key.thread = "u"
