"""Откат правок к ТОЧНОЙ базе: восстановление, удаление созданного, отказы.

Главный страж файла — контракт безопасности: содержимое, добытое из индекса
git (счётная база), на диск не попадает ни одним путём.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from gc_core.agent_edits.deltas import FileEntry
from gc_core.agent_edits.revert import (
    REASON_NO_BASELINE,
    REASON_NOTHING_TO_DELETE,
    REASON_NOT_REVERTABLE,
    REASON_WRITE_FAILED,
    revert_paths,
)


def _entry(
    path="a.py", *, kind="modified", revertable=True,
    add=1, del_=1, exact=True, countable=True, by_shell=False, external=False,
) -> FileEntry:
    return FileEntry(
        path=path, add=add, del_=del_, kind=kind, exact=exact,
        countable=countable, by_shell=by_shell, external=external,
        revertable=revertable, last_ts=1.0,
    )


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --- восстановление содержимого ---------------------------------------------


def test_restores_file_content_from_baseline(tmp_path):
    disk = _write(tmp_path / "a.py", b"new\n")

    out = revert_paths(["a.py"], {"a.py": _entry()}, lambda k: b"old\n", str(tmp_path))

    assert out.restored == ["a.py"]
    assert (out.deleted, out.skipped) == ([], [])
    assert disk.read_bytes() == b"old\n"


def test_recreates_file_that_was_deleted_by_agent(tmp_path):
    key = "pkg/gone.py"

    out = revert_paths(
        [key], {key: _entry(key, kind="deleted")}, lambda k: b"body\n", str(tmp_path)
    )

    assert out.restored == [key]
    assert (tmp_path / "pkg" / "gone.py").read_bytes() == b"body\n"


def test_text_file_overwritten_by_binary_is_restorable(tmp_path):
    """Доступность отката — по наличию базы, а не по ``op`` последней записи."""
    disk = _write(tmp_path / "a.txt", b"\x00\x01\x02\xff")

    baseline = "текст\n".encode("utf-8")

    out = revert_paths(
        ["a.txt"], {"a.txt": _entry("a.txt")}, lambda k: baseline, str(tmp_path)
    )

    assert out.restored == ["a.txt"]
    assert disk.read_bytes() == "текст\n".encode("utf-8")


def test_absolute_path_key_is_restored_outside_launch_root(tmp_path):
    outside = _write(tmp_path / "outside" / "cfg.ini", b"new\n")
    root = tmp_path / "proj"
    root.mkdir()

    out = revert_paths(
        [str(outside)], {str(outside): _entry(str(outside))},
        lambda k: b"old\n", str(root),
    )

    assert out.restored == [str(outside)]
    assert outside.read_bytes() == b"old\n"


# --- созданные агентом файлы ------------------------------------------------


def test_deletes_file_created_by_agent_without_baseline(tmp_path):
    disk = _write(tmp_path / "new.py", b"invented\n")
    calls: list[str] = []

    out = revert_paths(
        ["new.py"], {"new.py": _entry("new.py", kind="created")},
        lambda k: calls.append(k) or "ЛЕВАЯ БАЗА".encode("utf-8"), str(tmp_path),
    )

    assert out.deleted == ["new.py"]
    assert out.restored == []
    assert not disk.exists()
    # База у созданного файла не спрашивается вовсе — защита в глубину.
    assert calls == []


def test_created_file_already_gone_is_skipped_as_noop(tmp_path):
    out = revert_paths(
        ["new.py"], {"new.py": _entry("new.py", kind="created")},
        lambda k: "ЛЕВАЯ БАЗА".encode("utf-8"), str(tmp_path),
    )

    assert (out.deleted, out.restored) == ([], [])
    assert out.skipped == [("new.py", REASON_NOTHING_TO_DELETE)]


def test_created_then_accepted_is_restored_not_deleted(tmp_path):
    """Страж: путь с ``kind="modified"`` и непустой базой восстанавливается,

    а не удаляется. У созданного и принятого файла сервис обязан отдать
    именно такую форму (``kind="modified"``, не ``"created"``) — иначе откат
    удалил бы файл вместо восстановления принятого содержимого.
    """
    (tmp_path / "new.py").write_text("after\n", encoding="utf-8")

    out = revert_paths(["new.py"],
                       {"new.py": _entry(kind="modified", revertable=True)},
                       lambda k: b"accepted\n", str(tmp_path))

    assert out.restored == ["new.py"]
    assert (tmp_path / "new.py").read_text() == "accepted\n"   # НЕ удалён


def test_created_and_deleted_is_noop(tmp_path):
    """Файл создан и удалён внутри набора: ``revertable=False``, диск не трогаем."""
    entry = _entry("gone.py", kind="deleted", revertable=False)

    out = revert_paths(["gone.py"], {"gone.py": entry}, lambda k: b"x", str(tmp_path))

    assert (out.restored, out.deleted) == ([], [])
    assert out.skipped == [("gone.py", REASON_NOT_REVERTABLE)]
    assert not (tmp_path / "gone.py").exists()


# --- отказы -----------------------------------------------------------------


def test_skips_shell_record_marked_not_revertable(tmp_path):
    disk = _write(tmp_path / "a.py", b"disk\n")
    entry = _entry("a.py", revertable=False, by_shell=True)
    from_index = "ИНДЕКС".encode("utf-8")

    out = revert_paths(["a.py"], {"a.py": entry}, lambda k: from_index, str(tmp_path))

    assert out.restored == []
    assert out.skipped == [("a.py", REASON_NOT_REVERTABLE)]
    assert disk.read_bytes() == b"disk\n"   # счётная база на диск не попала


def test_binary_file_without_baseline_is_skipped(tmp_path):
    disk = _write(tmp_path / "img.png", b"\x89PNG\r\n")

    out = revert_paths(["img.png"], {"img.png": _entry("img.png")},
                       lambda k: None, str(tmp_path))

    assert out.restored == []
    assert out.skipped == [("img.png", REASON_NO_BASELINE)]
    assert disk.read_bytes() == b"\x89PNG\r\n"


def test_write_failure_lands_in_skipped_not_raises(tmp_path):
    (tmp_path / "busy.py").mkdir()   # целевой путь занят каталогом

    out = revert_paths(["busy.py"], {"busy.py": _entry("busy.py")},
                       lambda k: b"old\n", str(tmp_path))

    assert out.restored == []
    assert out.skipped == [("busy.py", REASON_WRITE_FAILED)]
    assert (tmp_path / "busy.py").is_dir()
    assert list((tmp_path / "busy.py").iterdir()) == []
    # временный файл за собой не оставили
    assert [p.name for p in tmp_path.iterdir()] == ["busy.py"]


def test_baseline_read_failure_is_skipped_not_raises(tmp_path):
    _write(tmp_path / "a.py", b"disk\n")

    def boom(key: str):
        raise OSError("снимок унесла конкурентная сессия")

    out = revert_paths(["a.py"], {"a.py": _entry()}, boom, str(tmp_path))

    assert out.skipped == [("a.py", REASON_NO_BASELINE)]
    assert (tmp_path / "a.py").read_bytes() == b"disk\n"


def test_path_without_entry_is_skipped(tmp_path):
    out = revert_paths(["ghost.py"], {}, lambda k: b"x", str(tmp_path))

    assert out.restored == [] and out.deleted == []
    assert out.skipped and out.skipped[0][0] == "ghost.py"
    assert not (tmp_path / "ghost.py").exists()


def test_mixed_batch_reports_each_path_once(tmp_path):
    _write(tmp_path / "keep.py", b"new\n")
    _write(tmp_path / "made.py", b"invented\n")
    entries = {
        "keep.py": _entry("keep.py"),
        "made.py": _entry("made.py", kind="created"),
        "shell.py": _entry("shell.py", revertable=False, by_shell=True),
    }

    out = revert_paths(
        ["keep.py", "made.py", "shell.py"], entries, lambda k: b"old\n", str(tmp_path)
    )

    assert out.restored == ["keep.py"]
    assert out.deleted == ["made.py"]
    assert out.skipped == [("shell.py", REASON_NOT_REVERTABLE)]


# --- права восстановленного файла -------------------------------------------


@pytest.mark.skipif(
    os.name == "nt", reason="на Windows биты доступа файла ничего не решают"
)
def test_restore_keeps_file_mode(tmp_path):
    """Откат `scripts/deploy.sh` не смеет снимать бит исполнения.

    Восстановленный файл пишется заново, и без переноса режима он получает
    права по umask: содержимое верное, ошибки нет, ломается позже при запуске.
    """
    disk = _write(tmp_path / "deploy.sh", b"new\n")
    os.chmod(disk, 0o755)

    out = revert_paths(
        ["deploy.sh"], {"deploy.sh": _entry("deploy.sh")},
        lambda k: b"old\n", str(tmp_path),
    )

    assert out.restored == ["deploy.sh"]
    assert disk.read_bytes() == b"old\n"
    assert stat.S_IMODE(disk.stat().st_mode) == 0o755
