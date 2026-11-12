"""FileThreadStore.delete убирает тред во всех формах файлов — как
``delete_thread`` develop, которым удаляли сессии TUI и уборщик сервера:
каноничные и старые имена, архивы сжатия, трассу субагентов, журнал правок
с его локом, «touched», блокировку сессии TUI, картинки треда. Соседний тред
не трогается."""

import asyncio
import os

from gc_core.threads import file_layout as layout
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey

KEY = ThreadKey(system="cli", thread="abc123")


def _touch(path) -> None:
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("x")


def _delete(root, key=KEY) -> None:
    asyncio.run(FileThreadStore(root).delete(key, op_id="delete"))


def test_delete_removes_all_shapes_and_keeps_the_neighbour(tmp_path):
    d = str(tmp_path)
    s, t = KEY.system, KEY.thread
    journal = str(layout.edits_journal_path(d, s, t))
    own = [
        f"data_{s}_{t}.jsonl",
        f"meta_{s}_{t}.json",
        f"data_{t}.jsonl",
        f"meta_{t}.json",
        f"data_{s}_{t}__pre_summary__20260101_000000.jsonl",
        f"meta_{s}_{t}__pre_summary__20260101_000000.json",
        f"data_{s}_{t}__pre_summary__20260102_000000.jsonl",
        f"meta_{s}_{t}__pre_summary__20260102_000000.json",
        f"data_subagents_{s}_{t}.jsonl",
        f"touched_{s}_{t}.json",
        f"touched_{s}_{t}__pre_summary__20260101_000000.json",
        f"edits_{s}_{t}__pre_summary__20260101_000000.jsonl",
        os.path.join("locks", f"lock_{s}_{t}.json"),
        os.path.join(layout.IMAGES_DIR, f"{s}_{t}_0123456789abcdef.png"),
    ]
    for name in own:
        _touch(tmp_path / name)
    _touch(journal)
    _touch(journal + ".lock")
    neighbour = [
        f"data_{s}_zzz999.jsonl",
        os.path.join(layout.IMAGES_DIR, f"{s}_zzz999_0123456789abcdef.png"),
    ]
    for name in neighbour:
        _touch(tmp_path / name)

    _delete(tmp_path)

    left = [n for n in own if (tmp_path / n).exists()]
    assert left == []
    assert not os.path.exists(journal)
    assert not os.path.exists(journal + ".lock")
    assert all((tmp_path / n).exists() for n in neighbour)


def test_delete_of_missing_thread_changes_nothing(tmp_path):
    _delete(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_delete_keeps_going_when_journal_lock_refuses_to_die(
    tmp_path, monkeypatch
):
    """Лок журнала правок держит открытым чужой процесс, и на Windows его
    удаление даёт ``PermissionError``: тред всё равно удаляется, лок —
    остаётся (develop вёл себя так же)."""
    d = str(tmp_path)
    journal = str(layout.edits_journal_path(d, KEY.system, KEY.thread))
    lock = journal + ".lock"
    data = tmp_path / f"data_{KEY.system}_{KEY.thread}.jsonl"
    for path in (journal, lock, data):
        _touch(path)
    real = os.unlink

    def stubborn(path, *args, **kwargs):
        if str(path) == lock:
            raise PermissionError(22, "файл занят другим процессом")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", stubborn)
    _delete(tmp_path)

    assert not data.exists()
    assert not os.path.exists(journal)
    assert os.path.exists(lock)
