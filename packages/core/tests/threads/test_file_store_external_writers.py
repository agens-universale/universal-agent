"""FileThreadStore v2 рядом со старым кодом, который пишет файлы треда
напрямую (старая версия gc на том же каталоге, журнал правок ядра):
удаление старым кодом, журнал правок, тред старого формата."""

import asyncio
import json
from datetime import timedelta

from gc_core.threads import file_layout
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import (
    LeaseRequest,
    ThreadChange,
    ThreadKey,
    ThreadQuery,
)
from gc_testkit.legacy_threads import LEGACY_THREADS, install_legacy_threads

KEY = ThreadKey(system="cli", thread="t1")


def run(coro):
    return asyncio.run(coro)


def _msgs(*ids):
    return [{"id": i, "role": "user", "content": f"текст {i}"} for i in ids]


async def _write(store, *ids, key=KEY, op="op"):
    record = await store.claim(
        key, LeaseRequest(owner="host", ttl=timedelta(minutes=1))
    )
    return await store.commit(
        key,
        ThreadChange(
            op_id=op,
            expected_revision=record.revision,
            lease=record.lease,
            append_messages=_msgs(*ids),
        ),
    )


def test_thread_deleted_by_old_code_is_gone_for_v2(tmp_path):
    store = FileThreadStore(tmp_path)
    run(_write(store, "a", "b"))
    # Старая версия удаляет тред файлами и про .store не знает.
    for name in ("data_cli_t1.jsonl", "meta_cli_t1.json"):
        (tmp_path / name).unlink()
    assert run(store.get_thread(KEY)) is None
    listed = run(store.list_threads(ThreadQuery(limit=100))).items
    assert KEY not in {r.key for r in listed}
    run(_write(store, "c", op="op2"))
    entries = run(store.read_stream(KEY, "messages"))
    assert [(e.seq, e.data["id"]) for e in entries] == [(1, "c")]


def test_caches_of_develop_thread_are_kept_after_first_read(
    tmp_path, monkeypatch
):
    fixture = LEGACY_THREADS["cli_compacted"]
    store = FileThreadStore(install_legacy_threads(tmp_path))
    key = ThreadKey(system=fixture.system, thread=fixture.thread_id)
    first = run(store.get_thread(key))
    assert len(first.archives) == fixture.archives
    scans = []
    real = file_layout.archive_suffixes
    monkeypatch.setattr(
        file_layout,
        "archive_suffixes",
        lambda *a: scans.append(a) or real(*a),
    )
    again = run(store.get_thread(key))
    assert again.archives == first.archives
    assert scans == [], "архивы пересканированы, хотя кеш уже есть"


def test_edits_appended_by_old_code_are_counted_incrementally(tmp_path):
    store = FileThreadStore(tmp_path)
    run(_write(store, "a"))
    journal = file_layout.edits_journal_path(str(tmp_path), "cli", "t1")
    for n in range(3):
        with open(journal, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"path": f"f{n}", "ts": n}) + "\n")
        record = run(store.get_thread(KEY))
        assert record.max_seq.get("edits") == n + 1


def test_discarding_sole_message_of_legacy_named_thread(tmp_path):
    line = json.dumps(
        {"type": "message", "seq": 1, "ts": "x", "payload": {"id": "only"}}
    )
    (tmp_path / "data_t1.jsonl").write_text(line + "\n", encoding="utf-8")
    store = FileThreadStore(tmp_path)
    record = run(
        store.claim(KEY, LeaseRequest(owner="host", ttl=timedelta(minutes=1)))
    )
    run(
        store.commit(
            KEY,
            ThreadChange(
                op_id="d",
                expected_revision=record.revision,
                lease=record.lease,
                discard_message_ids=["only"],
            ),
        )
    )
    assert run(store.read_stream(KEY, "messages")) == []


def test_broken_meta_of_one_thread_does_not_hide_the_others(tmp_path):
    store = FileThreadStore(tmp_path)
    other = ThreadKey(system="cli", thread="t2")
    run(_write(store, "a"))
    run(_write(store, "b", key=other, op="op2"))
    (tmp_path / "meta_cli_t2.json").write_text("{не json", encoding="utf-8")
    (tmp_path / "meta_legacy.json").write_text("{не json", encoding="utf-8")
    listed = run(store.list_threads(ThreadQuery(limit=100))).items
    assert [r.key for r in listed] == [KEY]


def test_archive_with_foreign_suffix_is_still_a_part_of_the_thread(tmp_path):
    def line(message_id):
        return json.dumps(
            {
                "type": "message",
                "seq": 1,
                "ts": "x",
                "payload": {"id": message_id},
            }
        )

    (tmp_path / "data_cli_t1__pre_summary__20260101.jsonl").write_text(
        line("old") + "\n", encoding="utf-8"
    )
    (tmp_path / "data_cli_t1.jsonl").write_text(
        line("new") + "\n", encoding="utf-8"
    )
    (tmp_path / "meta_cli_t1.json").write_text(
        json.dumps({"system": "cli", "thread_id": "t1"}), encoding="utf-8"
    )
    store = FileThreadStore(tmp_path)
    record = run(store.get_thread(KEY))
    assert [a.id for a in record.archives] == ["20260101"]
    archived = run(store.read_archive(KEY, "20260101"))
    assert [e.data["id"] for e in archived] == ["old"]
