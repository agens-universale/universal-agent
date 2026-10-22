"""FileThreadStore на данных, которые писал не он (develop, оборванная
запись, откат на develop), и при гонках обслуживания."""

import asyncio
import json
from datetime import timedelta

import pytest
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


def _committed_thread(tmp_path, key):
    from datetime import timedelta

    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import LeaseRequest, ThreadChange

    store = FileThreadStore(tmp_path)
    lease = asyncio.run(
        store.claim(key, LeaseRequest(owner="h", ttl=timedelta(60)))
    ).lease
    asyncio.run(
        store.commit(
            key,
            ThreadChange(
                op_id="op",
                expected_revision=0,
                lease=lease,
                append_messages=[{"id": "m", "role": "user", "content": "x"}],
            ),
        )
    )
    asyncio.run(store.release(key, lease))
    return store


def test_bad_updated_at_does_not_break_the_list(tmp_path):
    """Одна meta с ``updated_at`` не по ISO (или без пояса) — тред в конце
    списка, остальные на месте; курсор листает дальше."""
    import json

    from gc_core.threads.models import ThreadKey, ThreadQuery

    good = ThreadKey(system="cli", thread="good")
    store = _committed_thread(tmp_path, good)
    for name, stamp in (("bad", "вчера"), ("naive", "2026-01-01T00:00:00")):
        (tmp_path / f"meta_cli_{name}.json").write_text(
            json.dumps(
                {"thread_id": name, "system": "cli", "updated_at": stamp}
            ),
            encoding="utf-8",
        )
    page = asyncio.run(store.list_threads(ThreadQuery(limit=1)))
    assert [r.key.thread for r in page.items] == ["good"]
    rest = asyncio.run(
        store.list_threads(ThreadQuery(limit=10, cursor=page.next_cursor))
    )
    assert [r.key.thread for r in rest.items] == ["naive", "bad"]


def test_corrupt_control_and_journal_do_not_lock_the_thread(tmp_path):
    """Пустой ``control.json`` и недописанный ``pending.json`` (отключение
    питания без fsync) не закрывают тред: он читается и пишется дальше."""
    from gc_core.threads.models import ThreadKey

    key = ThreadKey(system="cli", thread="t")
    store = _committed_thread(tmp_path, key)
    (base,) = [
        p
        for p in (tmp_path / ".store").iterdir()
        if (p / "control.json").exists()
    ]
    (base / "control.json").write_bytes(b"")
    (base / "pending.json").write_bytes(b'{"changes": [')
    record = asyncio.run(store.get_thread(key))
    assert record is not None and record.info.messages_count == 1
    assert not (base / "pending.json").exists()
    _committed_thread(tmp_path, key)  # ревизия — с нуля, запись проходит


def test_foreign_stream_line_is_skipped(tmp_path):
    from gc_core.threads import file_layout as layout
    from gc_core.threads.models import SideChange, ThreadKey

    key = ThreadKey(system="cli", thread="t")
    store = _committed_thread(tmp_path, key)
    asyncio.run(
        store.append_side(
            key, SideChange(op_id="s", append=[("chat.responses", [{}])])
        )
    )
    path = layout.stream_path(str(tmp_path), "cli", "t", "chat.responses")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"seq": "не число", "data": 1}\n')
    entries = asyncio.run(store.read_stream(key, "chat.responses"))
    assert len(entries) == 1


async def test_extra_cannot_override_mapped_meta_keys(tmp_path):
    """Ключи meta, которые ведут модели (секции фронтендов, tui), записью
    ``extra`` не перекрываются: иначе писатель затирал бы чужие секции."""
    from datetime import timedelta

    from gc_core.threads.facade import Threads
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import LeaseRequest, ThreadChange, ThreadKey

    key = ThreadKey(system="g", thread="t1")
    store = FileThreadStore(str(tmp_path))
    await Threads(store).update_frontend(key, "swarm", {"swarm_id": "s1"})
    record = await store.claim(
        key, LeaseRequest(owner="w", ttl=timedelta(minutes=1))
    )
    await store.commit(
        key,
        ThreadChange(
            op_id="x",
            expected_revision=record.revision,
            lease=record.lease,
            set_extra={"frontends": {}, "own_key": 1},
        ),
    )
    after = await store.get_thread(key)
    assert after.frontend["swarm"] == {"swarm_id": "s1"}
    assert after.extra.get("own_key") == 1


async def test_blob_name_cannot_leave_the_images_of_the_thread(tmp_path):
    """``get_blob`` читает только вложения своего треда: путь в имени и
    чужой префикс — ``FileNotFoundError``, а не файл с диска."""
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import BlobRef, ThreadKey

    secret = tmp_path / "secret.txt"
    secret.write_text("HOSTPW")
    store = FileThreadStore(str(tmp_path / "threads"))
    key = ThreadKey(system="generic", thread="t1")
    other = ThreadKey(system="generic", thread="t2")
    own = await store.put_blob(key, b"img", mime="image/png")
    foreign = await store.put_blob(other, b"other", mime="image/png")
    assert await store.get_blob(own) == b"img"
    for name in (str(secret), "../../secret.txt", foreign.name):
        ref = BlobRef(key=key, name=name, mime="text/plain", location="")
        with pytest.raises(FileNotFoundError):
            await store.get_blob(ref)


def test_exotic_list_cursors_are_invalid_not_store_errors():
    """Ревью, раунд 2 (M1): курсор с моментом вне диапазона (после перевода
    в UTC), до 1970 года, с NUL или негодным именем — ``InvalidCursor``
    (400), а не ошибка базы или валидации (500) и не «первая страница»."""
    from gc_core.threads.errors import InvalidCursor
    from gc_core.threads.rules import parse_list_cursor

    for cursor in (
        "0001-01-01T00:00:00+14:00|s|t",
        "1969-12-31T23:59:59+00:00|s|t",
        "2026-10-22T00:00:00+00:00|s\x00|t",
        "2026-10-22T00:00:00+00:00|s|t\x00",
    ):
        with pytest.raises(InvalidCursor):
            parse_list_cursor(cursor)
    moment, system, thread = parse_list_cursor("2026-10-22T03:00:00+03:00|s|t")
    assert (moment.isoformat(), system, thread) == (
        "2026-10-22T00:00:00+00:00",
        "s",
        "t",
    )


async def test_blob_of_a_thread_with_a_longer_name_is_not_readable(tmp_path):
    """Префикс имени — не граница треда: тред ``t`` не читает вложения
    треда ``t_x`` той же системы (их имена начинаются с ``g_t_``)."""
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import BlobRef, ThreadKey

    store = FileThreadStore(str(tmp_path / "threads"))
    mine = ThreadKey(system="g", thread="t")
    longer = ThreadKey(system="g", thread="t_x")
    foreign = await store.put_blob(longer, b"secret", mime="image/png")
    ref = BlobRef(key=mine, name=foreign.name, mime="image/png", location="")
    with pytest.raises(FileNotFoundError):
        await store.get_blob(ref)
