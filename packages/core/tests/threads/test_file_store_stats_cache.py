"""Кеш статистики потока (``.store/<h>/control.json``, ``v2.stats``) не
верит одному размеру файла: develop после отката переписывает историю
(сжатие, удаление сообщения), и новый файл бывает длиннее старого. Тогда
поток читается заново, а не дочитывается «с конца кеша»."""

import asyncio
import os
from datetime import timedelta

from gc_core.threads import file_layout as layout
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import LeaseRequest, ThreadChange, ThreadKey

KEY = ThreadKey(system="cli", thread="t")


def _run(coro):
    return asyncio.run(coro)


def _append(store, *texts):
    lease = _run(
        store.claim(KEY, LeaseRequest(owner="host", ttl=timedelta(60)))
    ).lease
    record = _run(store.get_thread(KEY))
    _run(
        store.commit(
            KEY,
            ThreadChange(
                op_id=f"op-{record.revision}",
                expected_revision=record.revision,
                lease=lease,
                append_messages=[
                    {
                        "id": f"m-{record.revision}-{n}",
                        "role": "user",
                        "content": text,
                    }
                    for n, text in enumerate(texts)
                ],
            ),
        )
    )
    _run(store.release(KEY, lease))


def _develop_compacts(root, summary: str) -> None:
    """Как сжатие develop: старый файл — в архив, новый — с одной сводкой."""
    data = layout.history_file(str(root), KEY.system, KEY.thread)
    os.replace(
        data, data.replace(".jsonl", "__pre_summary__20260101_000000.jsonl")
    )
    line = layout.history_line(
        KEY.system,
        KEY.thread,
        1,
        "2026-01-01T00:00:00Z",
        {"id": "summary", "role": "user", "content": summary},
    )
    with open(data, "w", encoding="utf-8") as handle:
        handle.write(line)


async def _contents(store):
    return [
        e.data["content"] for e in await store.read_stream(KEY, "messages")
    ]


def test_rewritten_longer_history_is_read_anew(tmp_path):
    store = FileThreadStore(tmp_path)
    _append(store, "a", "b", "c")
    assert _run(_contents(store)) == ["a", "b", "c"]  # кеш заполнен
    _develop_compacts(tmp_path, "сводка " * 200)  # длиннее прежнего файла
    fresh = FileThreadStore(tmp_path)
    assert _run(_contents(fresh)) == ["сводка " * 200]
    assert _run(fresh.get_thread(KEY)).info.messages_count == 1
    _append(fresh, "d")
    assert _run(_contents(fresh))[-1] == "d"
    assert _run(fresh.get_thread(KEY)).info.messages_count == 2


def test_cursor_reader_sees_records_after_an_outside_rewrite(tmp_path):
    """Читатель с курсором (хвост TUI, ``follow``) не теряет записи, когда
    develop удалил сообщение (перезапись файла) и дописал новые так, что
    файл вырос."""
    store = FileThreadStore(tmp_path)
    _append(store, "a", "b")
    high = _run(store.read_stream(KEY, "messages"))[-1].seq
    data = layout.history_file(str(tmp_path), KEY.system, KEY.thread)
    with open(data, encoding="utf-8") as handle:
        kept = handle.readlines()[1:]  # develop удалил «a»
    fresh = [
        layout.history_line(
            KEY.system,
            KEY.thread,
            high + n,
            "2026-01-01T00:00:00Z",
            {"id": f"dev-{n}", "role": "user", "content": text},
        )
        for n, text in enumerate(["c" * 300, "d"], start=1)
    ]
    with open(data + ".tmp", "w", encoding="utf-8") as handle:
        handle.writelines([*kept, *fresh])
    os.replace(data + ".tmp", data)
    after = _run(store.read_stream(KEY, "messages", after_seq=high))
    assert [e.data["content"] for e in after] == ["c" * 300, "d"]
