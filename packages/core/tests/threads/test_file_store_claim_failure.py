"""Сбой внутри ``claim`` не оставляет тред «занятым»: если запись треда
для ответа не читается (битый meta и т. п.), аренда не записана — ни
срочная, ни бессрочная (``owner_id`` в control) durable-исполнителя."""

import asyncio
from datetime import timedelta

import pytest
from gc_core.thread_store_journal import durable_owner_id
from gc_core.threads import file_layout as layout
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import LeaseRequest, ThreadKey

KEY = ThreadKey(system="cli", thread="t")
TTL = timedelta(minutes=1)


def run(coro):
    return asyncio.run(coro)


def _broken_meta(monkeypatch):
    def boom(_path):
        raise ValueError("meta не читается")

    monkeypatch.setattr(layout, "read_meta_file", boom)


def _control_bytes(tmp_path) -> bytes | None:
    found = list(tmp_path.rglob("control.json"))
    return found[0].read_bytes() if found else None


def test_failed_claim_of_new_thread_leaves_it_free(tmp_path, monkeypatch):
    store = FileThreadStore(tmp_path)
    with monkeypatch.context() as patch:
        _broken_meta(patch)
        with pytest.raises(ValueError, match="meta"):
            run(store.claim(KEY, LeaseRequest(owner="a", ttl=TTL)))
    record = run(store.claim(KEY, LeaseRequest(owner="b", ttl=TTL)))
    assert record.lease.owner == "b"
    assert record.lease.epoch == 1


def test_failed_claim_restores_previous_lease_exactly(tmp_path, monkeypatch):
    """Прежняя аренда (снятая, эпоха 1) остаётся как была: control.json не
    меняется, а следующий claim получает эпоху 2, а не 3."""
    store = FileThreadStore(tmp_path)
    first = run(store.claim(KEY, LeaseRequest(owner="a", ttl=TTL)))
    run(store.release(KEY, first.lease))
    before = _control_bytes(tmp_path)
    with monkeypatch.context() as patch:
        _broken_meta(patch)
        with pytest.raises(ValueError, match="meta"):
            run(store.claim(KEY, LeaseRequest(owner="b", ttl=TTL)))
    assert _control_bytes(tmp_path) == before
    record = run(store.claim(KEY, LeaseRequest(owner="c", ttl=TTL)))
    assert (record.lease.owner, record.lease.epoch) == ("c", 2)


def test_failed_extension_keeps_own_live_lease(tmp_path, monkeypatch):
    """Продление своей живой аренды падает — аренда та же (срок прежний)."""
    store = FileThreadStore(tmp_path)
    run(store.claim(KEY, LeaseRequest(owner="a", ttl=TTL)))
    before = _control_bytes(tmp_path)
    with monkeypatch.context() as patch:
        _broken_meta(patch)
        with pytest.raises(ValueError, match="meta"):
            run(store.claim(KEY, LeaseRequest(owner="a", ttl=TTL * 10)))
    assert _control_bytes(tmp_path) == before


def test_failed_durable_claim_leaves_no_owner(tmp_path, monkeypatch):
    """Бессрочная аренда durable-исполнителя (``ttl=None``): упавший claim
    не оставляет ни аренды, ни ``owner_id`` в control."""
    store = FileThreadStore(tmp_path)
    run(store.claim(KEY, LeaseRequest(owner="host", ttl=TTL)))
    run(store.release(KEY, run(store.get_thread(KEY)).lease))
    with monkeypatch.context() as patch:
        _broken_meta(patch)
        with pytest.raises(ValueError, match="meta"):
            run(store.claim(KEY, LeaseRequest(owner="wf-1")))
    assert durable_owner_id(str(tmp_path), "cli", "t") is None
    record = run(store.claim(KEY, LeaseRequest(owner="host", ttl=TTL)))
    assert (record.lease.owner, record.lease.epoch) == ("host", 2)


def test_failed_durable_claim_of_new_thread(tmp_path, monkeypatch):
    store = FileThreadStore(tmp_path)
    with monkeypatch.context() as patch:
        _broken_meta(patch)
        with pytest.raises(ValueError, match="meta"):
            run(store.claim(KEY, LeaseRequest(owner="wf-1")))
    assert durable_owner_id(str(tmp_path), "cli", "t") is None
    record = run(store.claim(KEY, LeaseRequest(owner="host", ttl=TTL)))
    assert record.lease.owner == "host"
