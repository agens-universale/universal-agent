"""FileThreadStore на данных, которые писал не он (develop, оборванная
запись, откат на develop), и при гонках обслуживания."""

import asyncio
import json
from datetime import timedelta

from gc_core.threads import file_store as file_store_module
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import (
    LeaseRequest,
    ThreadChange,
    ThreadKey,
    ThreadQuery,
)

KEY = ThreadKey(system="cli", thread="t1")


def run(coro):
    return asyncio.run(coro)


def _line(seq, message_id):
    return json.dumps(
        {
            "type": "message",
            "seq": seq,
            "ts": "x",
            "payload": {"id": message_id, "role": "user", "content": "x"},
        }
    )


async def _append(store, *ids, op="op"):
    record = await store.claim(
        KEY, LeaseRequest(owner="host", ttl=timedelta(minutes=1))
    )
    await store.commit(
        KEY,
        ThreadChange(
            op_id=op,
            expected_revision=record.revision,
            lease=record.lease,
            append_messages=[{"id": i, "role": "user"} for i in ids],
        ),
    )


def test_append_after_unterminated_last_line_keeps_both(tmp_path):
    """Последняя строка develop без перевода строки: дописывание не
    приклеивает новую запись к ней (иначе пропали бы обе)."""
    data = tmp_path / "data_cli_t1.jsonl"
    data.write_text(_line(1, "old"), encoding="utf-8")  # без \n
    store = FileThreadStore(tmp_path)
    run(_append(store, "new"))
    ids = [e.data["id"] for e in run(store.read_stream(KEY, "messages"))]
    assert ids == ["old", "new"]
    fresh = FileThreadStore(tmp_path)
    ids = [e.data["id"] for e in run(fresh.read_stream(KEY, "messages"))]
    assert ids == ["old", "new"]


def test_archive_made_by_develop_after_v2_wrote_the_thread(tmp_path):
    """Откат на develop: тот сжимает тред, который уже писал v2, — новый
    архив виден, хотя список архивов v2 закешировал раньше."""
    store = FileThreadStore(tmp_path)
    run(_append(store, "a"))
    assert run(store.get_thread(KEY)).archives == []
    suffix = "20260101_000000"
    archive = tmp_path / f"data_cli_t1__pre_summary__{suffix}.jsonl"
    archive.write_bytes((tmp_path / "data_cli_t1.jsonl").read_bytes())
    (tmp_path / "data_cli_t1.jsonl").write_text(
        _line(1, "summary") + "\n", encoding="utf-8"
    )
    meta_path = tmp_path / "meta_cli_t1.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["last_summary_archive_suffix"] = suffix
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    for reader in (store, FileThreadStore(tmp_path)):
        record = run(reader.get_thread(KEY))
        assert [a.id for a in record.archives] == [suffix]


def test_broken_identity_does_not_hide_other_threads(tmp_path):
    store = FileThreadStore(tmp_path)
    run(_append(store, "a"))
    broken = tmp_path / ".store" / "broken"
    broken.mkdir(parents=True)
    (broken / "identity.json").write_text("{обрыв", encoding="utf-8")
    listed = run(store.list_threads(ThreadQuery(limit=100))).items
    assert [r.key for r in listed] == [KEY]


def test_prune_survives_receipt_removed_concurrently(tmp_path, monkeypatch):
    store = FileThreadStore(tmp_path)
    run(_append(store, "a"))
    real = file_store_module.Path.read_bytes

    def vanished(path):
        if path.parent.name == "receipts":
            path.unlink()
            raise FileNotFoundError(str(path))
        return real(path)

    monkeypatch.setattr(file_store_module.Path, "read_bytes", vanished)
    assert run(store.prune_receipts(timedelta(0))) == 0


def test_resume_offset_without_newer_entry_reads_whole():
    cached = {"size": 30, "max": 5, "recent": [[1, 0], [2, 10]]}
    assert file_store_module._resume_offset(cached, 3) is None


def test_listing_develop_thread_does_not_read_its_history(
    tmp_path, monkeypatch
):
    """Список тредов develop — по meta, как в develop: история каждого
    треда целиком не читается (сотни тредов → секунды на /sessions)."""
    data = tmp_path / "data_cli_t1.jsonl"
    data.write_text(
        "".join(_line(n, f"m{n}") + "\n" for n in range(1, 4)),
        encoding="utf-8",
    )
    (tmp_path / "meta_cli_t1.json").write_text(
        json.dumps({"system": "cli", "thread_id": "t1", "messages_count": 3}),
        encoding="utf-8",
    )
    reads = []
    real = file_store_module._read_lines
    monkeypatch.setattr(
        file_store_module,
        "_read_lines",
        lambda path, offset: reads.append(path) or real(path, offset),
    )
    store = FileThreadStore(tmp_path)
    [listed] = run(store.list_threads(ThreadQuery(limit=10))).items
    assert listed.info.messages_count == 3
    assert reads == []
    exact = run(store.get_thread(KEY))
    assert exact.info.messages_count == 3
    assert exact.max_seq["messages"] == 3


def test_side_write_leaves_thread_stamps_alone(tmp_path):
    """Запись сбоку (заголовок TUI, статус хода) — как ``update_tui_meta``
    develop: у древнего треда с ``memory_files`` не падает (его отвергнет
    ход, а не правка заголовка), а meta без ``updated_at`` его не получает —
    тред не всплывает в списке."""
    import json

    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import SideChange, ThreadKey

    key = ThreadKey(system="cli", thread="old")
    meta_path = tmp_path / "meta_cli_old.json"
    meta_path.write_text(
        json.dumps(
            {"thread_id": "old", "system_id": "cli", "memory_files": {}}
        ),
        encoding="utf-8",
    )
    store = FileThreadStore(tmp_path)
    asyncio.run(
        store.append_side(
            key,
            SideChange(op_id="tui", patch_frontend=("tui", {"title": "x"})),
        )
    )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["tui"] == {"title": "x"}
    assert "updated_at" not in meta
    assert list(meta)[:3] == ["thread_id", "system_id", "memory_files"]
