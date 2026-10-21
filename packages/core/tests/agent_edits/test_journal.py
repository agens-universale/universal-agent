"""Журнал правок агента: модель EditRecord и I/O EditsJournal."""

from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.snapshots import (
    purge_thread_snapshots,
    SnapshotStore,
    snapshots_root,
)
from gc_core.thread_store import edits_journal_path
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey


def make_rec(path="a.py", turn="t1", **kw):
    base = dict(
        path=path,
        turn_id=turn,
        ts=1.0,
        call_id="c1",
        op="edit",
        hunks="@@ -1 +1 @@\n-a\n+b\n",
        add=1,
        del_=1,
        before_sha256="x",
        after_sha256="y",
        flags=[],
    )
    base.update(kw)
    return EditRecord(**base)


def test_append_and_read_roundtrip(tmp_path):
    j = EditsJournal(tmp_path / "edits_s_t.jsonl")
    j.append(make_rec())
    j.append(make_rec(turn="t2", op="write"))

    recs = j.read_all()

    assert [r.turn_id for r in recs] == ["t1", "t2"]
    assert recs[0].del_ == 1
    assert '"del": 1' in (tmp_path / "edits_s_t.jsonl").read_text(
        encoding="utf-8"
    )


def test_torn_last_line_is_dropped(tmp_path):
    p = tmp_path / "edits_s_t.jsonl"
    j = EditsJournal(p)
    j.append(make_rec())
    with open(p, "a", encoding="utf-8") as fh:
        fh.write('{"path": "obryv')

    assert len(j.read_all()) == 1


def test_torn_middle_line_is_skipped_not_fatal(tmp_path):
    p = tmp_path / "e.jsonl"
    j = EditsJournal(p)
    j.append(make_rec(turn="t1"))
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(
            '{"path": "obryv\n'
        )  # битая строка в середине файла, не последняя
    j.append(make_rec(turn="t3"))

    recs = j.read_all()

    assert [r.turn_id for r in recs] == ["t1", "t3"]


def test_last_turn_id(tmp_path):
    j = EditsJournal(tmp_path / "e.jsonl")
    j.append(make_rec(turn="t1"))
    j.append(make_rec(turn="t2", add=3, del_=0))

    assert j.last_turn_id() == "t2"


def test_last_turn_id_is_none_for_empty_journal(tmp_path):
    j = EditsJournal(tmp_path / "empty.jsonl")

    assert j.last_turn_id() is None
    assert j.read_all() == []


def test_read_all_reflects_new_append_after_cache_populated(tmp_path):
    p = tmp_path / "e.jsonl"
    j = EditsJournal(p)
    j.append(make_rec(turn="t1"))
    first = j.read_all()

    j.append(make_rec(turn="t2"))
    second = j.read_all()

    assert [r.turn_id for r in first] == ["t1"]
    assert [r.turn_id for r in second] == ["t1", "t2"]


def test_edits_journal_path_matches_touched_style(tmp_path):
    p = edits_journal_path(tmp_path, "sys", "th")

    assert p.name == "edits_sys_th.jsonl"


def _seed_two_threads(tmp_path):
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    launch_root = tmp_path / "proj"
    launch_root.mkdir()
    journal = edits_journal_path(str(threads_dir), "cli", "demo-thread")
    journal.write_text('{"path": "a.py"}\n', encoding="utf-8")
    ours = SnapshotStore(snapshots_root(str(launch_root)), "cli_demo-thread")
    ours.capture_session("a.py", b"old")
    ours.capture_accepted("a.py", b"accepted", head_oid=None)
    alien = SnapshotStore(snapshots_root(str(launch_root)), "cli_other-thread")
    alien.capture_session("a.py", b"old")
    return threads_dir, launch_root, journal, ours, alien


def test_purge_thread_snapshots_removes_only_that_thread(tmp_path):
    """Пользовательский сброс уносит снимки треда — иначе новая сессия
    считает от чужой базы.

    Ключ снимков — `system_id_thread_id`, и сброс его не меняет. А
    `capture_session` пишет ТОЛЬКО при отсутствии. Значит после `gc-cli -t X -n`
    прежние `.orig`, `.accepted` и отметки создания остались бы живыми, новая
    сессия взяла бы за «до» состояние из прошлой, а откат записал бы его поверх
    работы, которую пользователь сделал между сессиями, — молча.
    """
    threads_dir, launch_root, _journal, ours, alien = _seed_two_threads(
        tmp_path
    )

    purge_thread_snapshots(
        str(threads_dir), "cli", "demo-thread", launch_root=str(launch_root)
    )

    assert ours.session_baseline("a.py") is None
    assert ours.accepted_state("a.py") is None
    # чужой тред не задет
    assert alien.session_baseline("a.py") == b"old"


def _delete_thread(threads_dir) -> None:
    import asyncio

    key = ThreadKey(system="cli", thread="demo-thread")
    asyncio.run(FileThreadStore(threads_dir).delete(key, op_id="delete"))


def test_thread_delete_leaves_snapshots_alone(tmp_path):
    """Удаление треда ХРАНИЛИЩЕМ снимки не трогает: снос баз молча стёр бы
    право отката. Снимки убирает только пользовательский сброс
    (`purge_thread_snapshots`)."""
    threads_dir, _root, journal, ours, _alien = _seed_two_threads(tmp_path)
    (threads_dir / "data_cli_demo-thread.jsonl").write_text(
        "", encoding="utf-8"
    )

    _delete_thread(threads_dir)

    assert not journal.exists()
    assert ours.session_baseline("a.py") == b"old"


def test_thread_delete_removes_edits_journal(tmp_path):
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    (threads_dir / "data_cli_demo-thread.jsonl").write_text(
        "", encoding="utf-8"
    )
    journal_path = edits_journal_path(str(threads_dir), "cli", "demo-thread")
    EditsJournal(journal_path).append(make_rec())
    assert journal_path.exists()

    _delete_thread(threads_dir)

    assert not journal_path.exists()


def test_journal_deleted_between_stat_and_open_reads_as_empty(
    tmp_path, monkeypatch
):
    """Журнал сносят из-под читателя — это пустой набор, а не падение.

    `gc-cli -t X -n` и второе окно TUI удаляют `edits_<ключ>.jsonl` в любой
    момент; между `stat` и открытием файла окно есть всегда. Наружу это
    вылетало в `screens/main.py::_capture_edits_baseline`, где ловить некому.
    """
    import builtins
    from pathlib import Path

    p = tmp_path / "edits_s_t.jsonl"
    EditsJournal(p).append(make_rec())
    reader = EditsJournal(
        p
    )  # свежий читатель: кэша нет, чтение пойдёт на диск

    real_open = builtins.open

    def racing_open(file, *args, **kwargs):
        if Path(file) == p:
            p.unlink()  # ровно то окно: stat уже прошёл, open ещё нет
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", racing_open)

    assert reader.read_all() == []


def test_unreadable_journal_does_not_poison_the_cache(tmp_path, monkeypatch):
    """Отказ чтения — не пустота: журнал цел, прочитать не смогли СЕЙЧАС.

    Кэш ключуется размером и временем файла. Запомнив под этим ключом пустоту,
    читатель отдавал бы пустой журнал до следующей правки — хотя на диске
    всё на месте. Разница видна только если отличать «файла нет» от «файл
    занят»: первое законная пустота, второе — нет.
    """
    import builtins

    j = EditsJournal(tmp_path / "edits_s_t.jsonl")
    j.append(make_rec(path="a.py"))
    assert [r.path for r in j.read_all()] == ["a.py"]  # кэш прогрет

    j.append(make_rec(path="b.py"))  # размер и время сменились

    real_open = builtins.open

    def _busy(path, *a, **kw):
        if str(path) == str(j._path):
            raise PermissionError(13, "файл занят другим процессом")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", _busy)
    assert [r.path for r in j.read_all()] == ["a.py"], (
        "при отказе чтения отдаём последнее удачно прочитанное, а не пустоту"
    )

    monkeypatch.setattr(builtins, "open", real_open)
    assert [r.path for r in j.read_all()] == ["a.py", "b.py"], (
        "после отказа кэш не должен помнить неудачу"
    )


def test_command_paths_survives_the_roundtrip_and_old_lines_read_as_none(
    tmp_path,
):
    """`command_paths` — сколько путей тронула урезанная потолком команда.

    Экран изменений по нему признаётся, что список неполон. Строки журналов,
    записанных до поля, его не несут и читаются как «команда не урезана»."""
    path = tmp_path / "edits_s_t.jsonl"
    j = EditsJournal(path)
    j.append(make_rec(path="a.py", command_paths=23456))
    j.append(make_rec(path="b.py"))

    recs = j.read_all()

    assert [r.command_paths for r in recs] == [23456, None]
    assert (
        '"command_paths"'
        not in path.read_text(encoding="utf-8").splitlines()[1]
    )
