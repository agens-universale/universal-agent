"""EditsView: данные экрана правок — источники, кумулятив, откат, принятие.

Тесты идут на настоящих файлах, снимках и (для принятия) на настоящем
временном репозитории: подменять слой, ради которого сервис и существует,
смысла нет.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

from gc_core.agent_edits import deltas, git_ops
from gc_core.agent_edits.git_ops import show_index_bytes
from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.ladder import STEP_ACCEPTED, STEP_HEAD
from gc_core.agent_edits.revert import REASON_NOT_REVERTABLE
from gc_core.agent_edits.snapshots import SnapshotStore, snapshots_root
from gc_core.thread_store import edits_journal_path
from gc_tui.services.edits_view import (
    ACCEPT_EXTERNAL,
    ACCEPT_READ_FAILED,
    ACCEPT_TOO_LARGE,
    ACCEPT_WRITE_FAILED,
    RESET_NOT_ACCEPTED,
    RESET_WRITE_FAILED,
    SETTLED_COMMIT,
    SETTLED_MANUAL,
    SOURCE_SESSION,
    SOURCE_TURN,
    TARGET_SESSION_START,
    EditsView,
    RowStep,
    _LadderGitFacts,
    diff_limits,
)


def _rec(
    path="a.py", turn="t1", call_id="c1", op="edit", add=1, del_=1,
    ts=1.0, hunks="", flags=None, before_sha256=None, after_sha256=None,
):
    return EditRecord(
        path=path, turn_id=turn, ts=ts, call_id=call_id, op=op, hunks=hunks,
        add=add, del_=del_, before_sha256=before_sha256, after_sha256=after_sha256,
        flags=list(flags or []),
    )


class _Workspace:
    """Тред + журнал, снимки обеих пород и файлы на диске под launch_root."""

    def __init__(self, tmp_path: Path, config=None, git: bool = False) -> None:
        self.threads_dir = tmp_path / "threads"
        self.threads_dir.mkdir(parents=True)
        self.root = tmp_path / "proj"
        self.root.mkdir(parents=True)
        if git:
            _git_init(self.root)
        journal_path = edits_journal_path(str(self.threads_dir), "cli", "th1")
        self.journal = EditsJournal(journal_path)
        self.store = SnapshotStore(
            snapshots_root(str(self.root)), "cli_th1", self.threads_dir
        )
        self.view = EditsView(
            str(self.threads_dir), "cli", "th1", str(self.root), config or {}
        )

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def session_base(self, rel: str, text: str) -> None:
        """Точный снимок сессии (`.orig`) — годится и счёту, и откату.

        Ревизия HEAD пишется рядом, как это делает рекордер (§3.2, фикс C1):
        по ней потом видно, затронул ли путь коммит уже ПОСЛЕ снимка. Снять
        снимок и не записать ревизию значит смоделировать снимок старого
        формата — для этого есть ``session_base_without_head_rev``.
        """
        self.store.capture_session(
            rel, text.encode("utf-8"), head_rev=git_ops.head_rev_from_files(str(self.root)),
        )

    def session_base_without_head_rev(self, rel: str, text: str) -> None:
        """Снимок БЕЗ признака — снятый версией до фикса C1 либо вне git."""
        self.store.capture_session(rel, text.encode("utf-8"))

    def created_mark(self, rel: str) -> None:
        """Отметка «создан здесь при ревизии X» — то, что пишут оба канала на
        записи о СОЗДАНИИ файла (Д1). Снимка у такого пути нет и быть не должно
        («пустая база не хранится»), и ревизию ступени HEAD несёт только она.
        """
        self.store.mark_created(
            rel, head_rev=git_ops.head_rev_from_files(str(self.root)),
        )

    def turn_base(self, rel: str, text: str, turn: str = "t1") -> None:
        self.store.capture_turn(rel, text.encode("utf-8"), turn)

    def shell_session_base(self, rel: str, text: str) -> None:
        """Счётная база сессии (`.gitorig`) — откату не подаётся никогда."""
        self.store.capture_shell_session(rel, text.encode("utf-8"))

    def shell_turn_base(self, rel: str, text: str, turn: str = "t1") -> None:
        self.store.capture_shell_turn(rel, text.encode("utf-8"), turn)

    def snapshot_path(self, suffix: str) -> Path:
        return next(snapshots_root(str(self.root)).glob(f"*{suffix}"))


def _git_init(root: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git недоступен")
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "test"),
        ("config", "core.autocrlf", "false"),
    ):
        subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


# --- источники --------------------------------------------------------------


def test_session_source_lists_all_files_with_cumulative_counters(tmp_path):
    """Кумулятив: база сессии против диска, а не сумма записей журнала."""
    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "a\nb\nc\n")
    ws.write("a.py", "a\nB\nc\nd\n")               # +2 −1 от базы
    ws.write("new.py", "one\ntwo\nthree\n")        # создан в наборе: +3
    ws.journal.append(_rec("a.py", turn="t1", ts=1.0, add=1, del_=1))
    ws.journal.append(_rec("a.py", turn="t2", ts=2.0, add=1, del_=0))
    ws.journal.append(_rec("new.py", turn="t2", ts=3.0, op="create", add=3, del_=0))

    view = ws.view.session_view(SOURCE_SESSION)

    assert view.source == SOURCE_SESSION and view.turn_id is None
    assert [row.entry.path for row in view.active] == ["new.py", "a.py"]   # по последней правке
    assert (view.total_add, view.total_del) == (5, 1)
    a_entry = view.active[1].entry
    assert (a_entry.add, a_entry.del_, a_entry.exact) == (2, 1, True)
    assert view.active[0].entry.kind == "created"


def test_turn_source_lists_only_last_turn_with_edits(tmp_path):
    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "a\n")
    ws.write("a.py", "a\nb\n")
    ws.turn_base("b.py", "one\n", "t2")
    ws.write("b.py", "one\ntwo\nthree\n")
    ws.journal.append(_rec("a.py", turn="t1", ts=1.0))
    ws.journal.append(_rec("b.py", turn="t2", ts=2.0, call_id="c2"))

    view = ws.view.session_view(SOURCE_TURN)

    assert view.turn_id == "t2"
    assert [row.entry.path for row in view.active] == ["b.py"]
    assert (view.total_add, view.total_del) == (2, 0)


def test_turn_source_skips_a_turn_with_only_external_edits(tmp_path):
    """Ход, где записана только внешняя правка, не становится «последним
    ходом с правками»: у внешнего ключа базы нет, `file_entries` его
    выбросит, а источник обязан остаться на предыдущем ходе с настоящей
    правкой — не объявить несуществующие правки отменёнными (находка
    финального ревью ветки).
    """
    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "a\n")
    ws.write("a.py", "a\nb\n")
    ws.journal.append(_rec("a.py", turn="t1", ts=1.0))
    outside = tmp_path / "outside.txt"
    outside.write_text("y\n", encoding="utf-8", newline="\n")
    ws.journal.append(_rec(str(outside), turn="t2", ts=2.0, call_id="c2"))

    view = ws.view.session_view(SOURCE_TURN)

    assert view.turn_id == "t1"
    assert [row.entry.path for row in view.active] == ["a.py"]


def test_turn_source_is_empty_when_no_edits(tmp_path):
    ws = _Workspace(tmp_path)

    view = ws.view.session_view(SOURCE_TURN)

    assert view.turn_id is None
    assert view.active == []
    assert (view.total_add, view.total_del) == (0, 0)


def test_old_turn_without_snapshot_falls_back_to_journal_counters(tmp_path):
    """Р12: точная база есть только у последнего хода.

    Снимок `.turn` принадлежит ходу t1, правки — в t2: базы хода нет, и
    сервис обязан сказать это вслух (`exact=False`), а числа взять из журнала,
    а не показать молча неверную дельту от чужой базы.
    """
    ws = _Workspace(tmp_path)
    ws.turn_base("a.py", "a\nb\nc\n", "t1")
    ws.write("a.py", "a\nB\nc\nd\ne\nf\n")
    ws.journal.append(_rec("a.py", turn="t2", ts=2.0, add=3, del_=1))

    [row] = ws.view.session_view(SOURCE_TURN).active
    entry = row.entry

    assert entry.exact is False
    assert (entry.add, entry.del_) == (3, 1)


def test_turn_source_counts_shell_edit_via_counting_base(tmp_path):
    """Ход, где файл правила только команда: счётная база из индекса работает."""
    ws = _Workspace(tmp_path)
    ws.shell_turn_base("a.py", "a\nb\nc\n", "t1")
    ws.write("a.py", "a\nB\nc\n")
    ws.journal.append(_rec("a.py", turn="t1", call_id=None, op="shell",
                           add=9, del_=9))

    [row] = ws.view.session_view(SOURCE_TURN).active
    entry = row.entry

    assert (entry.exact, entry.add, entry.del_) == (True, 1, 1)
    assert entry.by_shell is True
    assert entry.revertable is False        # счётная база откату не годится


# --- тело диффа -------------------------------------------------------------


def test_file_diff_full_shows_whole_file(tmp_path):
    ws = _Workspace(tmp_path)
    before = "".join(f"line{i}\n" for i in range(20))
    ws.session_base("a.py", before)
    ws.write("a.py", before.replace("line10", "СТРОКА10"))
    ws.journal.append(_rec("a.py"))

    short, exact = ws.view.file_diff(SOURCE_SESSION, "a.py")
    full, full_exact = ws.view.file_diff(SOURCE_SESSION, "a.py", full=True)

    assert exact is True and full_exact is True
    assert "line0\n" not in short and "line19\n" not in short
    assert " line0\n" in full and " line19\n" in full


def test_file_diff_falls_back_to_journal_hunks_when_base_missing(tmp_path):
    ws = _Workspace(tmp_path)
    ws.write("a.py", "a\nb\n")
    ws.journal.append(_rec("a.py", ts=1.0, call_id=None, op="shell",
                           hunks="@@ -1 +1 @@\n-a\n+A\n"))
    ws.journal.append(_rec("a.py", ts=2.0, call_id=None, op="shell",
                           hunks="@@ -2 +2 @@\n-b\n+B\n"))

    hunks, exact = ws.view.file_diff(SOURCE_TURN, "a.py")

    assert exact is False and hunks.count("@@") >= 2


def test_file_diff_of_created_file_never_uses_stale_snapshot(tmp_path):
    """У созданного здесь файла база набора пуста ВСЕГДА.

    Снимок такого пути физически бывает (рекордер снимает его со ВТОРОЙ
    правки), но он — середина набора: победи он пустоту, во вьюере осталась бы
    одна дописанная строка вместо всего сочинённого файла.
    """
    ws = _Workspace(tmp_path)
    ws.write("new.py", "one\ntwo\n")
    ws.session_base("new.py", "one\n")            # снимок «после первой правки»
    ws.journal.append(_rec("new.py", ts=1.0, op="create", add=1, del_=0))
    ws.journal.append(_rec("new.py", ts=2.0, call_id="c2", add=1, del_=0))

    hunks, exact = ws.view.file_diff(SOURCE_SESSION, "new.py")

    assert exact is True
    assert "+one\n" in hunks and "+two\n" in hunks


def test_file_diff_of_created_then_accepted_file_uses_step_not_whole_file(tmp_path):
    """Создан -> принят (+40) -> дописан (+10): тело диффа — дельта от
    принятого (10 добавленных строк), а не весь файл (50) целиком.

    Страж находки гейта волны 4: `file_diff` проверял `created_here` в обход
    поправки `_stepped`/`stepped_over_created` — файл со ступенью выше (её
    уже верно видит строка списка, `test_active_after_accept_counts_from_
    accepted`) во вьюере всё равно показывался бы созданным с нуля.
    """
    ws = _Workspace(tmp_path)
    ws.write("a.py", "".join(f"line{i}\n" for i in range(40)))
    ws.journal.append(_rec("a.py", ts=1.0, op="create", add=40, del_=0))
    ws.view.accept(["a.py"])
    with open(ws.root / "a.py", "a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(f"extra{i}\n" for i in range(10)))
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2", add=10, del_=0))

    hunks, exact = ws.view.file_diff(SOURCE_SESSION, "a.py")

    assert exact is True
    assert "line0\n" not in hunks             # принятые 40 строк — не в дельте
    assert "+extra0\n" in hunks and "+extra9\n" in hunks


def test_file_diff_of_settled_path_is_session_cumulative_not_record_sum(tmp_path):
    """Enter на строке группы показывает КУМУЛЯТИВ СЕССИИ (§12/Р31), а не сумму
    записей журнала — и не помечает тело неточным при живом `.orig`.

    Тот самый случай «+293 при реальных +133»: три записи журнала с раздутыми
    хэнками против одной строки разницы «начало сессии → диск».
    """
    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "a\n")
    ws.write("a.py", "a\nb\n")
    ws.journal.append(_rec("a.py", ts=1.0, hunks="@@ ЛИШНЕЕ-1 @@\n+b\n"))
    ws.write("a.py", "a\nb\nc\n")
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2", hunks="@@ ЛИШНЕЕ-2 @@\n+c\n"))
    ws.write("a.py", "a\nb\n")
    ws.journal.append(_rec("a.py", ts=3.0, call_id="c3", hunks="@@ ЛИШНЕЕ-3 @@\n-c\n"))
    ws.view.accept(["a.py"])                 # принят и чист → уходит в группу

    assert [row.path for row in ws.view.session_view(SOURCE_SESSION).settled] == ["a.py"]

    hunks, exact = ws.view.file_diff(SOURCE_SESSION, "a.py")

    assert exact is True
    assert "ЛИШНЕЕ" not in hunks                        # не склейка записей журнала
    assert "+b\n" in hunks and "+c\n" not in hunks      # ровно разница «до → сейчас»


def test_file_diff_of_settled_created_file_shows_the_whole_file(tmp_path):
    """У созданного в сессии файла база кумулятива пуста и в группе — иначе
    снимок «после первой правки» съел бы из тела весь сочинённый файл."""
    ws = _Workspace(tmp_path)
    ws.write("new.py", "one\n")
    ws.journal.append(_rec("new.py", ts=1.0, op="create"))
    ws.session_base("new.py", "one\n")        # снимок появился со ВТОРОЙ правки
    ws.write("new.py", "one\ntwo\n")
    ws.journal.append(_rec("new.py", ts=2.0, call_id="c2"))
    ws.view.accept(["new.py"])

    hunks, exact = ws.view.file_diff(SOURCE_SESSION, "new.py")

    assert exact is True
    assert "+one\n" in hunks and "+two\n" in hunks


def test_file_diff_of_settled_path_without_any_base_stays_journal(tmp_path):
    """Регресс I1: «базы нет вовсе» — это НЕ «база пуста».

    Файл разобран коммитом, а снимков в сторе нет ни одной породы (каталог
    вычищен, журнал треда жив). Числа строки группы приходят из журнала, и
    тело обязано приходить оттуда же: дельта от пустоты объявила бы весь файл
    написанным агентом.

    Коммит обязан случиться ВНУТРИ сессии: «разобран коммитом» опирается на
    записанный факт, и файл, закоммиченный ДО неё, разобранным не является.
    Отметка ревизии живёт в `index.json`, поэтому переживает вычистку снимков.
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "line1\nline2\nline3\nline4\nline5\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "line1\nline2\nline3\nline4\nline5\n")
    for snap in snapshots_root(str(ws.root)).glob("*.orig"):
        snap.unlink()                                    # снимки вычищены, отметка жива
    ws.write("a.py", "line1\nline2\nline3x\nline4\nline5\n")
    ws.journal.append(_rec("a.py", ts=1.0, add=1, del_=1, hunks="@@ из журнала @@\n+line3\n"))
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "коммит внутри сессии"], cwd=str(ws.root),
                   check=True, capture_output=True)
    ws.view.invalidate()
    assert list(snapshots_root(str(ws.root)).glob("*.orig")) == []   # снимков нет

    [row] = ws.view.session_view(SOURCE_SESSION).settled
    assert (row.add, row.del_, row.exact) == (1, 1, False)   # числа из журнала

    hunks, exact = ws.view.file_diff(SOURCE_SESSION, "a.py")

    assert exact is False
    assert "из журнала" in hunks


def test_file_diff_of_accepted_path_the_recorder_never_snapshotted(tmp_path):
    """Тот же регресс со стороны `a`: путь, у которого снимка не было никогда.

    Untracked-файл, тронутый shell-командой (записи `partial` — снимок при них
    не снимается): базы в сторе нет, а принятие делает путь разобранным.
    """
    ws = _Workspace(tmp_path)
    ws.write("a.py", "line1\nline2\nline3\nline4\nline5\n")
    ws.journal.append(_rec("a.py", ts=1.0, call_id=None, op="shell", add=1, del_=1,
                           hunks="@@ из журнала @@\n+line3\n", flags=["partial"]))
    ws.view.accept(["a.py"])
    assert ws.store.counting_session_baseline("a.py") is None    # базы нет ни одной породы

    [row] = ws.view.session_view(SOURCE_SESSION).settled
    assert (row.add, row.del_, row.exact) == (1, 1, False)

    hunks, exact = ws.view.file_diff(SOURCE_SESSION, "a.py")

    assert exact is False
    assert "из журнала" in hunks


def test_file_diff_of_settled_uncountable_path_falls_back_to_journal(tmp_path):
    """Сравнить нечем (кап) — тело честно из журнала и с `exact=False`:
    `SettledRow` признака не несёт, и выясняется это только здесь."""
    ws = _Workspace(tmp_path, {"agent_edits": {"max_diff_source_bytes": 5}})
    ws.write("a.py", "ab\n")
    ws.journal.append(_rec("a.py", ts=1.0, hunks="@@ из журнала @@\n+ab\n"))
    ws.view.accept(["a.py"])
    ws.write("a.py", "".join(f"line{i}\n" for i in range(20)))   # диск сверх капа

    hunks, exact = ws.view.file_diff(SOURCE_SESSION, "a.py")

    assert exact is False
    assert "из журнала" in hunks


def test_file_diff_of_unknown_path_is_empty(tmp_path):
    ws = _Workspace(tmp_path)

    assert ws.view.file_diff(SOURCE_SESSION, "ghost.py") == ("", False)


# --- что именно отменит откат -----------------------------------------------


def test_revert_delta_counts_from_precise_baseline_not_counting_base(tmp_path):
    """Подтверждение обязано считать той базой, которой откат ПИШЕТ.

    Опасная форма: `.gitorig` записан раньше и совпадает с диском (команда
    удалила строку, инструмент вернул её обратно) — счётная база отдаёт «нет
    различий», а откат к точному снимку строку уничтожит.
    """
    ws = _Workspace(tmp_path)
    ws.write("a.py", "A\nB\nC\n")
    # Точная база — первой: поверх счётной API её больше не пишет. Пара «счётная
    # старше, точная моложе» — наследие хранилищ до этого правила; времена ниже явные.
    ws.session_base("a.py", "A\nC\n")
    ws.shell_session_base("a.py", "A\nB\nC\n")
    now = time.time()
    os.utime(ws.snapshot_path(".gitorig"), (now - 30, now - 30))
    os.utime(ws.snapshot_path(".orig"), (now - 10, now - 10))
    ws.journal.append(_rec("a.py", call_id=None, op="shell", ts=1.0))
    ws.journal.append(_rec("a.py", ts=2.0))

    assert ws.view.file_diff(SOURCE_SESSION, "a.py") == ("", True)   # счётная база

    delta = ws.view.revert_delta(SOURCE_SESSION, "a.py")

    assert (delta.add, delta.del_) == (1, 0)
    assert "+B" in delta.hunks
    assert delta.countable is True


def test_revert_delta_of_created_file_is_the_whole_file(tmp_path):
    """У созданного здесь файла база пуста ВСЕГДА — откат удалит его целиком.

    Снимок у такого пути существовать может (рекордер снимает его со ВТОРОЙ
    правки), но он середина набора: подтверждение, собранное от него, обещало
    бы исчезновение одной дописанной строки вместо всего файла.
    """
    ws = _Workspace(tmp_path)
    ws.write("new.py", "one\ntwo\n")
    ws.session_base("new.py", "one\n")            # снимок после первой правки
    ws.journal.append(_rec("new.py", op="create", ts=1.0, add=1, del_=0))
    ws.journal.append(_rec("new.py", ts=2.0, add=1, del_=0))

    delta = ws.view.revert_delta(SOURCE_SESSION, "new.py")

    assert (delta.add, delta.del_) == (2, 0)
    assert "+one" in delta.hunks and "+two" in delta.hunks


def test_revert_delta_of_created_then_accepted_file_uses_step_not_whole_file(tmp_path):
    """Тот же сценарий для превью отката: числа и хэнки — дельта от принятого
    (10/0), а не весь файл (50/0) — иначе подтверждение отката лжёт о
    масштабе перед разрушающим действием, хотя сам `revert()` уже писал
    верно (страж находки гейта волны 4).
    """
    ws = _Workspace(tmp_path)
    ws.write("a.py", "".join(f"line{i}\n" for i in range(40)))
    ws.journal.append(_rec("a.py", ts=1.0, op="create", add=40, del_=0))
    ws.view.accept(["a.py"])
    with open(ws.root / "a.py", "a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(f"extra{i}\n" for i in range(10)))
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2", add=10, del_=0))

    delta = ws.view.revert_delta(SOURCE_SESSION, "a.py")

    assert (delta.add, delta.del_) == (10, 0)
    assert "line0\n" not in delta.hunks


def test_revert_delta_of_uncountable_path_has_no_numbers(tmp_path):
    """Сравнить нечем — ни диффа, ни чисел; нули тут значат «не считали»."""
    ws = _Workspace(tmp_path)
    ws.session_base("a.bin", "текст\n")
    (ws.root / "a.bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00")
    ws.journal.append(_rec("a.bin", add=1, del_=1))

    delta = ws.view.revert_delta(SOURCE_SESSION, "a.bin")

    assert delta.countable is False
    assert (delta.add, delta.del_, delta.hunks) == (0, 0, "")


def test_revert_delta_of_turn_source_uses_turn_snapshot(tmp_path):
    """Источник задаёт базу и здесь: «Ход» обещает откат к состоянию хода."""
    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "сессия\n")
    ws.turn_base("a.py", "ход\n", "t1")
    ws.write("a.py", "сейчас\n")
    ws.journal.append(_rec("a.py", turn="t1"))

    delta = ws.view.revert_delta(SOURCE_TURN, "a.py")

    assert "-ход" in delta.hunks and "+сейчас" in delta.hunks
    assert "сессия" not in delta.hunks


def test_revert_delta_of_unknown_path_is_empty(tmp_path):
    ws = _Workspace(tmp_path)
    ws.journal.append(_rec("a.py"))

    delta = ws.view.revert_delta(SOURCE_SESSION, "ghost.py")

    assert (delta.add, delta.del_, delta.hunks) == (0, 0, "")
    assert delta.countable is True


# --- откат ------------------------------------------------------------------


def test_revert_uses_precise_baseline_not_counting_base(tmp_path):
    """Страж контракта безопасности в ОПАСНОЙ форме.

    `.gitorig` с ЧУЖИМ содержимым записан РАНЬШЕ точного `.orig`, поэтому
    счётная база отдаёт именно его, а путь при этом откатываемый. Откат
    ПРОИСХОДИТ — и видно, чем именно: на диске обязано лежать содержимое
    точного снимка, маркера «ИНДЕКС» в файле быть не должно.
    """
    ws = _Workspace(tmp_path)
    ws.write("a.py", "АГЕНТ\n")
    # Точная база — первой: поверх счётной API её больше не пишет. Пара «счётная
    # старше, точная моложе» — наследие хранилищ до этого правила; времена ниже явные.
    ws.session_base("a.py", "ТОЧНАЯ\n")
    ws.shell_session_base("a.py", "ИНДЕКС\n")
    now = time.time()
    os.utime(ws.snapshot_path(".gitorig"), (now - 30, now - 30))
    os.utime(ws.snapshot_path(".orig"), (now - 10, now - 10))
    ws.journal.append(_rec("a.py"))

    [row] = ws.view.session_view(SOURCE_SESSION).active
    assert row.entry.revertable is True
    assert ws.store.counting_session_baseline("a.py") == "ИНДЕКС\n".encode("utf-8")

    outcome = ws.view.revert(SOURCE_SESSION, ["a.py"])

    assert outcome.restored == ["a.py"]
    text = (ws.root / "a.py").read_text(encoding="utf-8")
    assert text == "ТОЧНАЯ\n"
    assert "ИНДЕКС" not in text


def test_revert_in_turn_source_uses_precise_baseline_not_counting_base(tmp_path):
    """Зеркало стража для «Хода»: у источника своя пара баз, и своя опасность.

    Форма та же — `.gitturn` с маркером «ИНДЕКС» записан РАНЬШЕ точного
    `.turn`, откат ПРОИСХОДИТ и видно, чем именно. Без зеркала подмена базы
    именно в ветке «Хода» осталась бы невидимой: сессионный страж её не задевает.
    """
    ws = _Workspace(tmp_path)
    ws.write("a.py", "АГЕНТ\n")
    # Точная база — первой: поверх счётной API её больше не пишет. Пара «счётная
    # старше, точная моложе» — наследие хранилищ до этого правила; времена ниже явные.
    ws.turn_base("a.py", "ТОЧНАЯ\n", "t1")
    ws.shell_turn_base("a.py", "ИНДЕКС\n", "t1")
    now = time.time()
    os.utime(ws.snapshot_path(".gitturn"), (now - 30, now - 30))
    os.utime(ws.snapshot_path(".turn"), (now - 10, now - 10))
    ws.journal.append(_rec("a.py", turn="t1"))

    [row] = ws.view.session_view(SOURCE_TURN).active
    assert row.entry.revertable is True
    assert ws.store.counting_turn_baseline("a.py", "t1") == "ИНДЕКС\n".encode("utf-8")

    outcome = ws.view.revert(SOURCE_TURN, ["a.py"])

    assert outcome.restored == ["a.py"]
    text = (ws.root / "a.py").read_text(encoding="utf-8")
    assert text == "ТОЧНАЯ\n"
    assert "ИНДЕКС" not in text


def test_revert_in_turn_source_uses_turn_baseline(tmp_path):
    """Р8: источник базы отката задаёт представление, а не сервис молча."""
    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "СЕССИЯ\n")
    ws.turn_base("a.py", "ХОД\n", "t1")
    ws.write("a.py", "АГЕНТ\n")
    ws.journal.append(_rec("a.py", turn="t1"))

    ws.view.revert(SOURCE_TURN, ["a.py"])

    assert (ws.root / "a.py").read_text(encoding="utf-8") == "ХОД\n"


def test_revert_skips_path_without_precise_snapshot(tmp_path):
    ws = _Workspace(tmp_path)
    ws.shell_session_base("a.py", "ИНДЕКС\n")
    ws.write("a.py", "АГЕНТ\n")
    ws.journal.append(_rec("a.py", call_id=None, op="shell"))

    outcome = ws.view.revert(SOURCE_SESSION, ["a.py"])

    assert outcome.restored == []
    assert outcome.skipped == [("a.py", REASON_NOT_REVERTABLE)]
    assert (ws.root / "a.py").read_text(encoding="utf-8") == "АГЕНТ\n"


def test_revert_refreshes_the_view(tmp_path):
    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "a\n")
    ws.write("a.py", "a\nb\nc\n")
    ws.journal.append(_rec("a.py"))
    assert ws.view.session_view(SOURCE_SESSION).total_add == 2

    ws.view.revert(SOURCE_SESSION, ["a.py"])

    assert ws.view.session_view(SOURCE_SESSION).total_add == 0


def test_view_of_unknown_source_raises(tmp_path):
    ws = _Workspace(tmp_path)

    with pytest.raises(ValueError):
        ws.view.session_view("оба")


# --- кэш и лимиты -----------------------------------------------------------


def test_cache_hit_spawns_no_git_subprocess(tmp_path, monkeypatch):
    """Повторный вызов вида не запускает git. Это не оптимизация, а отзывчивость.

    Ключ кэша считается ДО проверки кэша — иначе нельзя, ключ и есть вопрос «не
    устарело ли». Но в ключ входила ревизия HEAD, а брали её подпроцессом. И
    `revert_delta` зовёт вид НА КАЖДЫЙ путь: массовый откат шестидесяти файлов
    платил шестьдесят с лишним запусков git синхронно в потоке интерфейса, ещё
    до окна подтверждения — замер дал 2,2 секунды (пятый раунд ревью, находка 9).

    Ревизия теперь читается из файлов `.git` тем же `head_rev_from_files`,
    которым её пишет рекордер, — значит сравнивать их между собой стало
    осмысленно, а на попадании в кэш подпроцессов не остаётся вовсе.
    """
    from gc_core.agent_edits import git_ops

    ws = _Workspace(tmp_path, git=True)
    ws.session_base("a.py", "a\n")
    ws.write("a.py", "a\nb\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.view.session_view(SOURCE_SESSION)          # первый вызов строит вид

    spawned = []
    real = git_ops.run_git

    def counting(*args, **kwargs):
        spawned.append(args[1] if len(args) > 1 else kwargs.get("args"))
        return real(*args, **kwargs)

    monkeypatch.setattr(git_ops, "run_git", counting)

    ws.view.session_view(SOURCE_SESSION)

    assert spawned == []


def test_view_cache_invalidated_by_journal_append(tmp_path, monkeypatch):
    calls = []
    real = deltas.delta_vs_disk

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(deltas, "delta_vs_disk", counting)

    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "a\n")
    ws.write("a.py", "a\nb\n")
    ws.journal.append(_rec("a.py", ts=1.0))

    assert ws.view.session_view(SOURCE_SESSION).total_add == 1
    assert ws.view.session_view(SOURCE_SESSION).total_add == 1
    assert len(calls) == 1

    ws.write("b.py", "x\n")
    ws.journal.append(_rec("b.py", ts=2.0, call_id="c2", op="create"))

    assert ws.view.session_view(SOURCE_SESSION).total_add == 2
    assert len(calls) == 3          # пересчитаны оба пути


def test_invalidate_forces_recompute(tmp_path, monkeypatch):
    calls = []
    real = deltas.delta_vs_disk
    monkeypatch.setattr(
        deltas, "delta_vs_disk",
        lambda *a, **k: (calls.append(1), real(*a, **k))[1],
    )

    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "a\n")
    ws.write("a.py", "a\nb\n")
    ws.journal.append(_rec("a.py"))

    ws.view.session_view(SOURCE_SESSION)
    ws.view.invalidate()
    ws.view.session_view(SOURCE_SESSION)

    assert len(calls) == 2


def test_limits_come_from_config_not_defaults(tmp_path):
    """`hunk_context_lines: 0` → контекстных строк в теле диффа нет."""
    before = "".join(f"line{i}\n" for i in range(9))
    after = before.replace("line4", "СТРОКА4")

    default_ws = _Workspace(tmp_path / "d")
    default_ws.session_base("a.py", before)
    default_ws.write("a.py", after)
    default_ws.journal.append(_rec("a.py"))

    tight_ws = _Workspace(tmp_path / "t", {"agent_edits": {"hunk_context_lines": 0}})
    tight_ws.session_base("a.py", before)
    tight_ws.write("a.py", after)
    tight_ws.journal.append(_rec("a.py"))

    default_hunks, _ = default_ws.view.file_diff(SOURCE_SESSION, "a.py")
    tight_hunks, _ = tight_ws.view.file_diff(SOURCE_SESSION, "a.py")

    assert " line3\n" in default_hunks
    assert [line for line in tight_hunks.splitlines() if line.startswith(" ")] == []


def test_source_bytes_cap_comes_from_config(tmp_path):
    ws = _Workspace(tmp_path, {"agent_edits": {"max_diff_source_bytes": 10}})
    ws.session_base("a.py", "a\n")
    ws.write("a.py", "".join(f"line{i}\n" for i in range(50)))
    ws.journal.append(_rec("a.py"))

    [row] = ws.view.session_view(SOURCE_SESSION).active
    entry = row.entry

    assert entry.countable is False
    assert (entry.add, entry.del_) == (1, 1)      # счётчики из журнала


def test_diff_limits_ignore_broken_config_values(tmp_path):
    from gc_core.config_modules.agent_edits import (
        DEFAULT_HUNK_CONTEXT_LINES,
        DEFAULT_MAX_DIFF_SOURCE_BYTES,
    )

    assert diff_limits({}) == (DEFAULT_MAX_DIFF_SOURCE_BYTES,
                               DEFAULT_HUNK_CONTEXT_LINES)
    assert diff_limits({"agent_edits": {"hunk_context_lines": True}})[1] == (
        DEFAULT_HUNK_CONTEXT_LINES
    )
    assert diff_limits({"agent_edits": {"hunk_context_lines": -1}})[1] == (
        DEFAULT_HUNK_CONTEXT_LINES
    )
    assert diff_limits({"agent_edits": {"max_diff_source_bytes": "мусор"}})[0] == (
        DEFAULT_MAX_DIFF_SOURCE_BYTES
    )
    assert diff_limits({"agent_edits": {"hunk_context_lines": 0}})[1] == 0


# --- принятие и сброс принятия ------------------------------------------------


def test_accept_shifts_base_and_stages(tmp_path):
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "hello\n")

    outcome = ws.view.accept(["a.py"])

    assert outcome.accepted == ["a.py"]
    assert (outcome.staged, outcome.stage_error, outcome.skipped) == (1, None, [])
    assert ws.store.accepted_state("a.py").content == b"hello\n"
    assert show_index_bytes(str(ws.root), "a.py") == b"hello\n"


def test_accept_head_oid_uses_one_batch_call(tmp_path, monkeypatch):
    """``head_oid`` — один батч-вызов на все принимаемые пути, не по одному."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "v1\n")
    ws.write("b.py", "v1\n")
    for rel in ("a.py", "b.py"):
        subprocess.run(["git", "add", "--", rel], cwd=str(ws.root), check=True,
                       capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    calls = []
    real = git_ops.head_blob_oids
    monkeypatch.setattr(
        git_ops, "head_blob_oids",
        lambda root, rels: (calls.append(list(rels)), real(root, rels))[1],
    )

    ws.view.accept(["a.py", "b.py"])

    assert len(calls) == 1
    assert sorted(calls[0]) == ["a.py", "b.py"]
    head_oid = ws.store.accepted_state("a.py").head_oid
    _, out = git_ops.run_git(str(ws.root), ["rev-parse", "HEAD:a.py"])
    assert head_oid == out.decode().strip()


def test_accept_deleted_file_stages_deletion(tmp_path):
    """Файл удалён агентом → accept: маркер + git add фиксирует удаление в индексе."""
    ws = _Workspace(tmp_path, git=True)
    path = ws.write("a.py", "hello\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    path.unlink()

    outcome = ws.view.accept(["a.py"])

    assert outcome.accepted == ["a.py"]
    assert (outcome.staged, outcome.stage_error) == (1, None)
    assert ws.store.accepted_state("a.py").content is None
    assert show_index_bytes(str(ws.root), "a.py") is None    # удаление застейджено


def test_accept_batch_survives_a_path_git_never_heard_of(tmp_path):
    """I3: путь, которого нет ни на диске, ни у git (создан и удалён агентом),
    ронял ВЕСЬ батч `git add` — `fatal: pathspec … did not match any files`,
    код 128, не застейжено ничего. Такой путь попадает в `Ctrl+A` штатно.
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("real.py", "hello\n")
    ws.journal.append(_rec("gone.py", ts=1.0, op="create"))     # создан и удалён агентом
    ws.journal.append(_rec("real.py", ts=2.0, call_id="c2"))

    outcome = ws.view.accept(["gone.py", "real.py"])

    assert outcome.accepted == ["gone.py", "real.py"]     # база сдвинута у обоих
    assert (outcome.staged, outcome.stage_error) == (1, None)
    assert show_index_bytes(str(ws.root), "real.py") == b"hello\n"


def test_accept_of_only_unknown_paths_makes_no_git_claim(tmp_path):
    """Стейджить нечего вовсе — `git add` не зовётся, и плашке нечего сказать
    про индекс (`staged=0`, `stage_error=None`)."""
    ws = _Workspace(tmp_path, git=True)
    ws.journal.append(_rec("gone.py", ts=1.0, op="create"))

    outcome = ws.view.accept(["gone.py"])

    assert outcome.accepted == ["gone.py"]
    assert (outcome.staged, outcome.stage_error) == (0, None)


def test_accept_store_failure_is_skipped_not_raised(tmp_path, monkeypatch):
    """I4: стор отказал (занятый `index.json.lock`, полный диск) — путь уходит
    в `skipped`, метод не бросает: экран зовёт `accept` без try/except."""
    ws = _Workspace(tmp_path)
    ws.write("a.py", "x\n")
    ws.write("b.py", "y\n")
    real = SnapshotStore.capture_accepted

    def flaky(self, path_key, content, *, head_oid):
        if path_key == "a.py":
            raise TimeoutError("лок индекса занят")
        return real(self, path_key, content, head_oid=head_oid)

    monkeypatch.setattr(SnapshotStore, "capture_accepted", flaky)

    outcome = ws.view.accept(["a.py", "b.py"])

    assert outcome.accepted == ["b.py"]
    assert outcome.skipped == [("a.py", ACCEPT_WRITE_FAILED)]
    assert ws.store.accepted_state("a.py") is None
    assert ws.store.accepted_state("b.py").content == b"y\n"


def test_reset_store_failure_is_skipped_not_raised(tmp_path, monkeypatch):
    """Тот же класс отказов на сбросе — исход, а не исключение наружу."""
    ws = _Workspace(tmp_path)
    ws.write("a.py", "x\n")
    ws.view.accept(["a.py"])

    def boom(self, path_key):
        raise OSError("снимок не отдаётся")

    monkeypatch.setattr(SnapshotStore, "clear_accepted", boom)

    outcome = ws.view.reset_accept(["a.py"])

    assert (outcome.reset, outcome.to_head) == ([], [])
    assert outcome.skipped == [("a.py", RESET_WRITE_FAILED)]


def test_reset_targets_assume_a_lost_point_when_the_store_refuses(tmp_path, monkeypatch):
    """Прочитать принятую базу не удалось — безопасная сторона: считаем точку
    отката теряемой. Времени принятия при этом нет — строка тела обязана
    прожить без него, а не соврать про час."""
    ws = _Workspace(tmp_path)
    ws.write("a.py", "x\n")
    ws.view.accept(["a.py"])

    def boom(self, path_key):
        raise TimeoutError("лок индекса занят")

    monkeypatch.setattr(SnapshotStore, "accepted_state", boom)

    [reset] = ws.view.reset_targets(["a.py"])

    assert (reset.loses_point, reset.accepted_ts) == (True, None)


def test_accept_oversize_is_skipped_with_reason(tmp_path):
    ws = _Workspace(tmp_path, {"agent_edits": {"max_diff_source_bytes": 10}})
    ws.write("a.py", "".join(f"line{i}\n" for i in range(50)))

    outcome = ws.view.accept(["a.py"])

    assert outcome.accepted == []
    assert outcome.skipped == [("a.py", ACCEPT_TOO_LARGE)]
    assert ws.store.accepted_state("a.py") is None


def test_accept_read_failure_is_skipped_not_raised(tmp_path, monkeypatch):
    """Гонка: файл исчез/залочен между `is_file()` и `read_bytes()` — skip, не исключение."""
    ws = _Workspace(tmp_path)
    ws.write("a.py", "x\n")
    ws.write("b.py", "y\n")
    real_read_bytes = Path.read_bytes

    def flaky_read_bytes(self):
        if self.name == "a.py":
            raise OSError("вымышленная гонка")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky_read_bytes)

    outcome = ws.view.accept(["a.py", "b.py"])

    assert outcome.accepted == ["b.py"]
    assert outcome.skipped == [("a.py", ACCEPT_READ_FAILED)]
    assert ws.store.accepted_state("a.py") is None
    assert ws.store.accepted_state("b.py").content == b"y\n"


def test_accept_plain_folder_no_git_tail(tmp_path):
    ws = _Workspace(tmp_path)
    ws.write("a.py", "x\n")

    out = ws.view.accept(["a.py"])

    assert out.staged == 0 and out.stage_error is None
    assert out.accepted == ["a.py"]
    assert ws.store.accepted_state("a.py").content == b"x\n"


def test_accept_reports_external_files(tmp_path):
    ws = _Workspace(tmp_path)
    ws.write("a.py", "v2\n")
    ws.write("b.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0, after_sha256="h1"))
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2",
                           before_sha256="ЧУЖОЙ", after_sha256="h2"))
    ws.journal.append(_rec("b.py", ts=1.0, call_id="c3"))

    outcome = ws.view.accept(["a.py", "b.py"])

    assert outcome.accepted == ["a.py", "b.py"]
    assert outcome.with_external == ["a.py"]


def test_stage_error_keeps_base_shifted(tmp_path):
    """``add_paths`` вернул ошибку → ``stage_error`` заполнен, но ``accepted_state`` жив."""
    ws = _Workspace(tmp_path, git=True)
    ws.write(".gitignore", "ign.txt\n")
    ws.write("ign.txt", "x\n")

    outcome = ws.view.accept(["ign.txt"])

    assert outcome.accepted == ["ign.txt"]
    assert outcome.staged == 0
    assert outcome.stage_error is not None and "ign.txt" in outcome.stage_error
    assert ws.store.accepted_state("ign.txt").content == b"x\n"


def test_accept_skips_external_key_without_a_baseline(tmp_path):
    """Абсолютный ключ (файл вне проекта) — гвард `accept`: базу не сдвигаем.

    Латентная дыра: сегодня до `accept` внешний ключ не доезжает никаким
    путём экрана (`self._entries` уже просеян `file_entries`), но метод
    обязан отказывать сам — иначе вызывающий в обход экрана получил бы право
    положить байты чужого файла в `.gc/agent_edits` и назначить ему точку
    отсчёта.
    """
    ws = _Workspace(tmp_path, git=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("y\n", encoding="utf-8", newline="\n")

    outcome = ws.view.accept([str(outside)])

    assert outcome.accepted == []
    assert outcome.skipped == [(str(outside), ACCEPT_EXTERNAL)]
    assert ws.store.accepted_state(str(outside)) is None


def test_reset_returns_to_head_flag(tmp_path):
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "v1\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "v1\n")
    ws.write("a.py", "v2\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "agent commit"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.view.accept(["a.py"])

    outcome = ws.view.reset_accept(["a.py"])

    assert (outcome.reset, outcome.to_head, outcome.skipped) == (["a.py"], ["a.py"], [])
    assert ws.store.accepted_state("a.py") is None


def test_reset_without_committed_change_has_no_head_step(tmp_path):
    """Без коммита после начала сессии ступени HEAD нет — счёт падает на начало сессии."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "v1\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "v1\n")
    ws.write("a.py", "v2\n")
    ws.view.accept(["a.py"])

    outcome = ws.view.reset_accept(["a.py"])

    assert outcome.to_head == []


def test_reset_targets_names_the_committed_state_when_head_survives(tmp_path):
    """Цель сброса — до самого сброса (задача 4 доп.): тот же сетап, что у
    `test_reset_returns_to_head_flag` (ступень HEAD переживает снятие
    принятия), но спрошено ДО действия — превью не вправе разойтись с тем,
    что сброс решит пост-фактум (`ResetOutcome.to_head`)."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "v1\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "v1\n")
    ws.write("a.py", "v2\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "agent commit"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.view.accept(["a.py"])

    [reset] = ws.view.reset_targets(["a.py"])

    expected_ref = git_ops.head_commit_label(str(ws.root), "a.py")
    assert (reset.path, reset.target.kind) == ("a.py", STEP_HEAD)
    assert reset.target.label == f"закоммиченное состояние ({expected_ref})"
    assert (reset.target.skip_reason, reset.target.warns) == (None, ())
    # Диск равен принятому — терять нечего, а время принятия известно и без потери.
    assert reset.loses_point is False
    assert reset.accepted_ts == ws.store.accepted_state("a.py").ts

    # Превью не расходится с исходом самого сброса.
    outcome = ws.view.reset_accept(["a.py"])
    assert outcome.to_head == ["a.py"]


def test_reset_targets_names_session_start_without_a_surviving_commit(tmp_path):
    """Без коммита ПОСЛЕ начала сессии ступени HEAD нет — цель сброса
    называет начало сессии, тот же сетап, что у
    `test_reset_without_committed_change_has_no_head_step`, только ДО сброса.
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "v1\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "v1\n")
    ws.write("a.py", "v2\n")
    ws.view.accept(["a.py"])

    [reset] = ws.view.reset_targets(["a.py"])

    assert (reset.path, reset.target.kind, reset.target.label) == (
        "a.py", "session", TARGET_SESSION_START,
    )
    assert (reset.target.skip_reason, reset.target.warns) == (None, ())


def test_reset_skips_path_without_accepted_state(tmp_path):
    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")

    outcome = ws.view.reset_accept(["a.py"])

    assert (outcome.reset, outcome.to_head) == ([], [])
    assert outcome.skipped == [("a.py", RESET_NOT_ACCEPTED)]


def test_reset_targets_lose_the_point_only_when_the_delta_is_nonempty(tmp_path):
    """Дельта от принятой базы пуста → терять нечего; непуста → точка отката
    теряется (Д5 второго смоука: и гейт подтверждения, и строка тела читают
    ОДИН этот признак). Путь без принятой базы не теряет ничего и времени
    принятия не имеет вовсе."""
    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.write("b.py", "v1\n")
    ws.write("c.py", "v1\n")
    ws.view.accept(["a.py", "b.py"])
    ws.write("b.py", "v2\n")

    result = ws.view.reset_targets(["a.py", "b.py", "c.py"])

    assert [t.path for t in result] == ["a.py", "b.py", "c.py"]
    targets = {t.path: t for t in result}
    assert targets["a.py"].loses_point is False
    assert targets["b.py"].loses_point is True
    assert targets["b.py"].accepted_ts == ws.store.accepted_state("b.py").ts
    assert (targets["c.py"].loses_point, targets["c.py"].accepted_ts) == (False, None)


def test_reset_targets_treat_uncountable_delta_as_a_lost_point(tmp_path):
    """Сравнить нечем (кап на диске) — безопасная сторона: точка отката
    считается теряемой, подтверждение спрашивается."""
    ws = _Workspace(tmp_path, {"agent_edits": {"max_diff_source_bytes": 5}})
    ws.write("a.py", "ab\n")
    ws.view.accept(["a.py"])
    ws.write("a.py", "".join(f"line{i}\n" for i in range(20)))

    [reset] = ws.view.reset_targets(["a.py"])

    assert reset.loses_point is True


# --- вид сессии: активные от лестницы, разобранные группой (волна 4) --------


def test_accepted_clean_file_moves_to_settled_manual(tmp_path):
    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.view.accept(["a.py"])

    view = ws.view.session_view(SOURCE_SESSION)

    assert view.active == []
    [row] = view.settled
    assert (row.path, row.kind, row.reason) == ("a.py", "modified", SETTLED_MANUAL)


def test_committed_clean_file_moves_to_settled_commit(tmp_path):
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "v1\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "v1\n")
    ws.write("a.py", "v2\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "agent commit"], cwd=str(ws.root), check=True,
                   capture_output=True)

    view = ws.view.session_view(SOURCE_SESSION)

    assert view.active == []
    [row] = view.settled
    assert (row.path, row.add, row.del_, row.kind, row.reason) == (
        "a.py", 1, 1, "modified", SETTLED_COMMIT,
    )


def test_active_after_accept_counts_from_accepted(tmp_path):
    """Принят (+40 разобраны), дописан (+10) → в active с (10, 0) и ступенью accepted."""
    ws = _Workspace(tmp_path)
    ws.write("a.py", "".join(f"line{i}\n" for i in range(40)))
    ws.journal.append(_rec("a.py", ts=1.0, add=40, del_=0))
    ws.view.accept(["a.py"])
    with open(ws.root / "a.py", "a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(f"extra{i}\n" for i in range(10)))
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2", add=10, del_=0))

    view = ws.view.session_view(SOURCE_SESSION)

    assert view.settled == []
    [row] = view.active
    assert row.step is not None and row.step.kind == STEP_ACCEPTED
    assert (row.entry.add, row.entry.del_, row.entry.exact) == (10, 0, True)


def test_partially_committed_counts_from_head(tmp_path):
    """Закоммичено +40, поверх +10 → active (10, 0); откатная цель — HEAD (Задача 9)."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "orig\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "orig\n")
    ws.write("a.py", "orig\n" + "".join(f"line{i}\n" for i in range(40)))
    ws.journal.append(_rec("a.py", ts=1.0, add=40, del_=0))
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit +40"], cwd=str(ws.root), check=True,
                   capture_output=True)
    with open(ws.root / "a.py", "a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(f"more{i}\n" for i in range(10)))
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2", add=10, del_=0))

    view = ws.view.session_view(SOURCE_SESSION)

    assert view.settled == []
    [row] = view.active
    assert (row.entry.add, row.entry.del_, row.entry.exact) == (10, 0, True)


def test_untracked_never_settles_by_git_rule(tmp_path):
    """Ignored/untracked путь чист «по porcelain», но не в ls-files → активен."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("committed.py", "seed\n")
    subprocess.run(["git", "add", "--", "committed.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.write(".gitignore", "ignored.py\n")
    ws.write("ignored.py", "x\n")
    ws.journal.append(_rec("ignored.py", ts=1.0))

    view = ws.view.session_view(SOURCE_SESSION)

    assert view.settled == []
    assert [row.entry.path for row in view.active] == ["ignored.py"]


def test_settled_cumulative_not_record_sum(tmp_path):
    """Файл правлен трижды и принят: SettledRow.add/del_ — итог, не сумма шагов."""
    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "a\n")
    ws.write("a.py", "a\nb\n")
    ws.journal.append(_rec("a.py", ts=1.0, add=9, del_=9))
    ws.write("a.py", "a\nb\nc\n")
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2", add=9, del_=9))
    ws.write("a.py", "a\nb\nc\nd\n")
    ws.journal.append(_rec("a.py", ts=3.0, call_id="c3", add=9, del_=9))
    ws.view.accept(["a.py"])

    [row] = ws.view.session_view(SOURCE_SESSION).settled

    assert (row.add, row.del_) == (3, 0)   # от начала сессии до диска, не 27/27


def test_header_totals_are_active_only(tmp_path):
    ws = _Workspace(tmp_path)
    ws.write("a.py", "x\n")
    ws.journal.append(_rec("a.py", ts=1.0, add=1, del_=0))
    ws.view.accept(["a.py"])                # a.py уходит в settled
    ws.write("b.py", "y\n")
    ws.journal.append(_rec("b.py", ts=2.0, call_id="c2", add=1, del_=0))

    view = ws.view.session_view(SOURCE_SESSION)

    assert [row.path for row in view.settled] == ["a.py"]
    assert [row.entry.path for row in view.active] == ["b.py"]
    assert (view.total_add, view.total_del) == (1, 0)


def test_commit_invalidates_done_cache(tmp_path):
    """Git commit при живом кэше → файл уезжает в settled без изменения журнала."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "v1\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "v1\n")
    ws.write("a.py", "v2\n")
    ws.journal.append(_rec("a.py", ts=1.0))

    before = ws.view.session_view(SOURCE_SESSION)
    assert [row.entry.path for row in before.active] == ["a.py"]
    assert before.settled == []

    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit outside"], cwd=str(ws.root), check=True,
                   capture_output=True)

    after = ws.view.session_view(SOURCE_SESSION)

    assert after.active == []
    [row] = after.settled
    assert (row.path, row.reason) == ("a.py", SETTLED_COMMIT)


def test_turn_source_has_no_settled_and_no_ladder(tmp_path):
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "v1\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.turn_base("a.py", "v1\n", "t1")
    ws.write("a.py", "v2\n")
    ws.journal.append(_rec("a.py", turn="t1"))
    # Закоммичено сразу — чисто относительно HEAD, но «Ход» разобранность не видит.
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit v2"], cwd=str(ws.root), check=True,
                   capture_output=True)

    view = ws.view.session_view(SOURCE_TURN)

    assert view.settled == []
    assert [row.entry.path for row in view.active] == ["a.py"]
    assert (view.total_add, view.total_del) == (1, 1)   # "v1\n" -> "v2\n": строка иная целиком


def test_dirty_pre_session_user_edits_not_counted_as_head_step(tmp_path):
    """HEAD == снимок начала сессии → ступени HEAD нет (до-сессионные правки не искажаются)."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "line1\nline2\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "pre-session history"], cwd=str(ws.root),
                   check=True, capture_output=True)
    ws.session_base("a.py", "line1\nline2\n")     # .orig == HEAD: ступени нет
    ws.write("a.py", "line1\nline2\nline3\n")
    ws.journal.append(_rec("a.py", ts=1.0, add=1, del_=0))

    [row] = ws.view.session_view(SOURCE_SESSION).active

    assert row.entry.exact is True
    assert (row.entry.add, row.entry.del_) == (1, 0)   # от начала сессии, не от HEAD
    assert row.step is None


def test_settled_reason_follows_accepted_state_liveness_not_which_rule_matched(tmp_path):
    """`reason` = «вручную» при живом ``accepted_state`` — даже если settled сработал по (б).

    Живой сценарий из §4.2 спеки: файл принят, а ПОВЕРХ принятого закоммичен
    ДРУГОЙ вариант (диск ушёл дальше принятого, но чист относительно HEAD).
    Разобранность здесь ловит правило (б) («коммитом»), но у строки должен
    остаться `u` — а он привязан к пометке «вручную», не к тому, какое именно
    правило сработало.
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "x\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "x\n")     # снимок начала сессии: в проде он есть всегда
    ws.write("a.py", "x-accepted\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.view.accept(["a.py"])
    ws.write("a.py", "y-committed\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "further commit"], cwd=str(ws.root), check=True,
                   capture_output=True)

    [row] = ws.view.session_view(SOURCE_SESSION).settled

    assert row.reason == SETTLED_MANUAL


def test_ladder_head_bytes_not_refetched_across_rebuilds_without_commit(tmp_path, monkeypatch):
    """Кэш ступени по (rel, HEAD): дозапись журнала пересобирает вид, но
    `cat-file --filters` на неизменившийся файл повторно не зовётся (§3.2 спеки).
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "orig\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "orig\n")
    # Ступень HEAD заводит только коммит ПОСЛЕ снимка (фикс C1); правка поверх
    # коммита оставляет файл активным.
    ws.write("a.py", "orig\ncommitted\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit inside session"], cwd=str(ws.root),
                   check=True, capture_output=True)
    ws.write("a.py", "orig\ncommitted\ndirty\n")
    ws.view.session_view(SOURCE_SESSION)      # первая сборка — ступень HEAD посчитана

    calls = []
    real = git_ops.cat_file_filters

    def tracking(root, rel, **kwargs):
        if rel == "a.py":
            calls.append(1)
        return real(root, rel, **kwargs)

    monkeypatch.setattr(git_ops, "cat_file_filters", tracking)

    ws.write("b.py", "new\n")
    ws.journal.append(_rec("b.py", ts=2.0, call_id="c2", op="create"))
    view = ws.view.session_view(SOURCE_SESSION)   # вторая сборка — журнал ДРУГОГО файла

    assert calls == []
    [a_row] = [row for row in view.active if row.entry.path == "a.py"]
    assert a_row.entry.exact is True
    assert (a_row.entry.add, a_row.entry.del_) == (1, 0)   # от ступени HEAD, не от начала


def test_dirty_before_session_is_not_a_head_step_and_revert_keeps_user_work(tmp_path):
    """C1: файл закоммичен, пользователь правил его РУКАМИ и не коммитил, потом
    его правит агент — коммита в этой сессии не было.

    `head_bytes != .orig` здесь истинно (в снимок попала несохранённая работа
    пользователя), и по старому правилу объявлялась ступень HEAD: счёт шёл от
    коммита, а `r` записывал содержимое коммита поверх работы, которой нет ни в
    индексе, ни в истории. Признак теперь записанный — ступени нет, откат
    возвращает `.orig`.
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "committed\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    user_work = "committed\nuser-work\n"
    ws.write("a.py", user_work)          # правка пользователя вне агента, без коммита
    ws.session_base("a.py", user_work)   # снимок агента снят с диска — с этой правкой
    ws.write("a.py", user_work + "agent\n")
    ws.journal.append(_rec("a.py", ts=1.0, add=1, del_=0))

    [row] = ws.view.session_view(SOURCE_SESSION).active
    assert (row.entry.add, row.entry.del_) == (1, 0)   # от начала сессии, не от HEAD

    [target] = ws.view.revert_targets(SOURCE_SESSION, ["a.py"])
    assert target.kind == "session"

    outcome = ws.view.revert(SOURCE_SESSION, ["a.py"])

    assert outcome.restored == ["a.py"]
    assert (ws.root / "a.py").read_text(encoding="utf-8") == user_work


def test_snapshot_without_recorded_rev_never_gets_a_head_step(tmp_path):
    """Снимок без признака (снят версией до фикса C1) деградирует безопасно:
    коммит в сессии был, но ступени HEAD у пути нет — счёт от начала сессии."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "orig\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base_without_head_rev("a.py", "orig\n")
    ws.write("a.py", "orig\ncommitted\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit inside session"], cwd=str(ws.root),
                   check=True, capture_output=True)
    ws.write("a.py", "orig\ncommitted\ndirty\n")

    [row] = ws.view.session_view(SOURCE_SESSION).active

    assert (row.entry.add, row.entry.del_) == (2, 0)   # от `.orig`, а ступень HEAD дала бы (1, 0)
    [target] = ws.view.revert_targets(SOURCE_SESSION, ["a.py"])
    assert target.kind == "session"


def test_created_then_committed_then_edited_counts_from_head(tmp_path):
    """Д1: агент СОЗДАЛ файл, файл закоммитили, агент дописал → счёт от коммита.

    У созданного пути снимка нет («пустая база не хранится»), а `.orig` второй
    правки снят уже ПОСЛЕ коммита и несёт пост-коммитную ревизию — по нему
    ступени HEAD не видно. Ревизию момента создания несёт отдельная отметка
    (R2), и без неё путь считался бы от пустоты (весь файл), а `r` удалял бы
    файл вместо возврата к закоммиченному.
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("seed.txt", "seed\n")
    subprocess.run(["git", "add", "--", "seed.txt"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)

    committed = "".join(f"line{i}\n" for i in range(40))
    ws.created_mark("new.py")                  # запись create: снимка нет, есть отметка
    ws.write("new.py", committed)
    ws.journal.append(_rec("new.py", ts=1.0, op="create", add=40, del_=0))
    subprocess.run(["git", "add", "--", "new.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "add new.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("new.py", committed)       # вторая правка: `.orig` уже пост-коммитный
    with open(ws.root / "new.py", "a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(f"more{i}\n" for i in range(10)))
    ws.journal.append(_rec("new.py", ts=2.0, call_id="c2", add=10, del_=0))

    view = ws.view.session_view(SOURCE_SESSION)

    assert view.settled == []
    [row] = view.active
    assert row.step is not None and row.step.kind == STEP_HEAD
    assert (row.entry.kind, row.entry.exact) == ("modified", True)
    assert (row.entry.add, row.entry.del_) == (10, 0)   # не весь файл

    outcome = ws.view.revert(SOURCE_SESSION, ["new.py"])

    assert (outcome.restored, outcome.deleted) == (["new.py"], [])
    assert (ws.root / "new.py").read_text(encoding="utf-8") == committed


def test_created_then_committed_counts_from_head_with_lf_disk_under_autocrlf(tmp_path):
    """Е1: репозиторий с ``core.autocrlf=true``, файл на диске с LF.

    Ступень коммита читалась в форме выкладки (CRLF), и на дописанной строке
    счёт выходил «весь файл заменён», а откат переписывал файлу переводы строк
    — после чего git считал файл изменённым. База обязана следовать диску.
    """
    ws = _Workspace(tmp_path, git=True)
    subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=str(ws.root),
                   check=True, capture_output=True)
    ws.write("seed.txt", "seed\n")
    subprocess.run(["git", "add", "--", "seed.txt"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)

    ws.created_mark("tools.md")                # запись create: снимка нет, есть отметка
    ws.write("tools.md", "# t\n\nline\n")      # LF, как пишет агент
    ws.journal.append(_rec("tools.md", ts=1.0, op="create", add=3, del_=0))
    subprocess.run(["git", "add", "--", "tools.md"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "smoke: tools.md"], cwd=str(ws.root),
                   check=True, capture_output=True)
    ws.write("tools.md", "# t\n\nline\nappended\n")
    ws.journal.append(_rec("tools.md", ts=2.0, call_id="c2", add=1, del_=0))

    view = ws.view.session_view(SOURCE_SESSION)

    [row] = view.active
    assert row.step is not None and row.step.kind == STEP_HEAD
    assert (row.entry.add, row.entry.del_) == (1, 0)      # не «весь файл заменён»

    outcome = ws.view.revert(SOURCE_SESSION, ["tools.md"])

    assert outcome.restored == ["tools.md"]
    assert (ws.root / "tools.md").read_bytes() == b"# t\n\nline\n"    # LF сохранён
    status = subprocess.run(["git", "status", "--porcelain", "--", "tools.md"],
                            cwd=str(ws.root), check=True, capture_output=True, text=True)
    assert status.stdout == ""                            # git считает файл чистым


def test_ladder_head_bytes_skips_reading_oversized_file(tmp_path):
    """Кап источника — НАСТРОЕННЫЙ (`agent_edits.max_diff_source_bytes`), тот же,
    с которым считает `deltas.delta_vs_disk`. Файл сверх лимита целиком не
    читается: стиля диска тогда взять неоткуда, и база остаётся в форме
    выкладки, как было до Е1. Поднятый в конфиге кап обязан пускать к диску
    файл, который на дефолтном лимите не прошёл бы, — иначе «весь файл заменён»
    возвращается через настройку.
    """
    def _facts(root: Path, cap: int):
        view = EditsView(
            str(tmp_path / "threads"), "cli", "th1", str(root),
            {"agent_edits": {"max_diff_source_bytes": cap}},
        )
        facts = view._ladder_facts
        facts.prime(git_ops.rev_parse_head(str(root)), {})
        return facts

    ws = _Workspace(tmp_path, git=True)
    subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=str(ws.root),
                   check=True, capture_output=True)
    ws.write("a.txt", "one\ntwo\n")
    subprocess.run(["git", "add", "--", "a.txt"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)

    # Кап опущен ниже размера файла — читать нечего, база в форме выкладки.
    assert _facts(ws.root, 4).head_bytes("a.txt") == b"one\r\ntwo\r\n"
    # Кап поднят — база следует диску (LF), как и счёт на том же лимите.
    assert _facts(ws.root, 10_000_000).head_bytes("a.txt") == b"one\ntwo\n"


def test_ladder_git_facts_prime_empty_oids_keeps_prepared_batch(tmp_path):
    """`prime(head_rev, {})` на ТОЙ ЖЕ ревизии не стирает батч, который
    подготовил предыдущий `prime()` — так зовёт `_head_step` (ему нужен
    только `head_rev` для ключей кэша, свежий батч он не считает, см. его
    докстринг). До фикса это било по свойству, а не по видимому поведению
    напрямую: `_compute_session_steps` готовит батч, `_head_step` в той же
    сборке вида звал `prime(head_rev, {})` поверх него и обнулял `_oids` —
    находка финального ревью ветки.
    """
    facts = _LadderGitFacts(str(tmp_path), 10_000_000)
    facts.prime("deadbeef", {"a.py": "oid-a"})

    facts.prime("deadbeef", {})    # `_head_step`: та же ревизия, батч не свой

    assert facts.head_oid("a.py") == "oid-a"


def test_ladder_git_facts_prime_new_revision_clears_batch(tmp_path):
    """Смена ревизии обязана сбросить батч, даже если новых oid'ов нет:
    старые относятся к прежнему HEAD и не могут выдаваться под видом новой
    ревизии (`head_oid` ключа ревизии в кэше не несёт)."""
    facts = _LadderGitFacts(str(tmp_path), 10_000_000)
    facts.prime("rev1", {"a.py": "oid-a"})

    facts.prime("rev2", {})

    assert facts.head_oid("a.py") is None


def test_created_uncommitted_keeps_empty_base(tmp_path):
    """Коммита не было — у созданного файла всё по-прежнему: база ПУСТА, в
    счёте весь файл, `r` его удаляет. Отметка создания сама по себе ступени не
    даёт: ступень объявляется только по факту коммита ПОСЛЕ создания."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("seed.txt", "seed\n")
    subprocess.run(["git", "add", "--", "seed.txt"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)

    created = "".join(f"line{i}\n" for i in range(3))
    ws.created_mark("new.py")
    ws.write("new.py", created)
    ws.journal.append(_rec("new.py", ts=1.0, op="create", add=3, del_=0))
    ws.session_base("new.py", created)         # вторая правка тем же инструментом
    ws.write("new.py", created + "extra\n")
    ws.journal.append(_rec("new.py", ts=2.0, call_id="c2", add=1, del_=0))

    view = ws.view.session_view(SOURCE_SESSION)

    [row] = view.active
    assert row.step is None
    assert (row.entry.kind, row.entry.add, row.entry.del_) == ("created", 4, 0)

    [target] = ws.view.revert_targets(SOURCE_SESSION, ["new.py"])
    assert target.kind == "delete"


def test_snapshot_taken_earlier_beats_a_later_creation_mark(tmp_path):
    """Обратный к Д1 порядок: снимок лёг ПЕРВЫМ — его ревизия и есть честная.

    Так приходит щель `append_to_file` (финальное ревью ветки, I2):
    существующему файлу НУЛЕВОГО размера тул ставит `op="create"`, и путь,
    усечённый шелл-командой раньше в этой же сессии, получал бы отметку
    создания с поздней ревизией — поверх `.orig`, снятого до коммита. Коммит,
    случившийся МЕЖДУ ними, по такой отметке «не виден»: ступень пропадает, и
    файл считается от начала сессии вместо ближайшей ступени — коммита.
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("notes.md", "user work\n")
    subprocess.run(["git", "add", "--", "notes.md"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)

    ws.session_base("notes.md", "user work\n")   # шелл усёк файл: «до» снято честно
    ws.write("notes.md", "")
    ws.journal.append(_rec("notes.md", ts=1.0, op="shell", add=0, del_=1))
    subprocess.run(["git", "commit", "-am", "truncate notes.md"], cwd=str(ws.root),
                   check=True, capture_output=True)
    ws.created_mark("notes.md")                  # append_to_file: `size == 0` → create
    ws.write("notes.md", "appended\n")
    ws.journal.append(_rec("notes.md", ts=2.0, call_id="c2", op="create", add=1, del_=0))

    [row] = ws.view.session_view(SOURCE_SESSION).active

    assert row.step is not None and row.step.kind == STEP_HEAD
    assert (row.entry.kind, row.entry.add, row.entry.del_) == ("modified", 1, 0)
    [target] = ws.view.revert_targets(SOURCE_SESSION, ["notes.md"])
    assert target.kind == "head"


# --- цели отката по ступеням и предупреждения хода (задача 9) ---------------


def test_target_accepted_with_label(tmp_path):
    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0, op="create"))
    ws.view.accept(["a.py"])
    ws.write("a.py", "v2\n")

    [target] = ws.view.revert_targets(SOURCE_SESSION, ["a.py"])

    accepted_ts = ws.store.accepted_state("a.py").ts
    expected_hhmm = f"{datetime.fromtimestamp(accepted_ts):%H:%M}"
    assert (target.path, target.kind) == ("a.py", "accepted")
    assert target.label == f"принятое состояние ({expected_hhmm})"
    assert (target.skip_reason, target.warns) == (None, ())


def test_target_head_for_partially_committed(tmp_path):
    """Закоммичено +40, поверх +10 → откатная цель — HEAD, не начало сессии."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "orig\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "orig\n")
    ws.write("a.py", "orig\n" + "".join(f"line{i}\n" for i in range(40)))
    ws.journal.append(_rec("a.py", ts=1.0, add=40, del_=0))
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit +40"], cwd=str(ws.root), check=True,
                   capture_output=True)
    with open(ws.root / "a.py", "a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(f"more{i}\n" for i in range(10)))
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2", add=10, del_=0))

    [target] = ws.view.revert_targets(SOURCE_SESSION, ["a.py"])

    expected_ref = git_ops.head_commit_label(str(ws.root), "a.py")
    assert target.kind == "head"
    assert target.label == f"закоммиченное состояние ({expected_ref})"
    assert (target.skip_reason, target.warns) == (None, ())


def test_revert_restores_to_head_bytes(tmp_path):
    """Закоммичено X, агент правил → откат → на диске байты HEAD (форма worktree)."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "orig\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "orig\n")
    committed = "orig\n" + "".join(f"line{i}\n" for i in range(40))
    ws.write("a.py", committed)
    ws.journal.append(_rec("a.py", ts=1.0, add=40, del_=0))
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit +40"], cwd=str(ws.root), check=True,
                   capture_output=True)
    with open(ws.root / "a.py", "a", encoding="utf-8", newline="\n") as fh:
        fh.write("dirty\n")
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2"))

    outcome = ws.view.revert(SOURCE_SESSION, ["a.py"])

    assert outcome.restored == ["a.py"]
    assert (ws.root / "a.py").read_text(encoding="utf-8") == committed


def test_revert_writes_accepted_bytes_for_ordinary_edit(tmp_path):
    """Обычная (не created) правка с физическим `.orig` на диске → accept() →
    ещё правка → откат пишет ИМЕННО принятые байты.

    Отличается от ``test_target_accepted_with_label``/
    ``test_revert_accepted_absent_deletes_file``: там путь либо не пишет байты
    вовсе (маркер отсутствия — ветка удаления), либо доходит до ступени через
    `stepped_over_created` (`revertable` там истинен БЕЗ обращения к
    `_session_precise_baseline`). Здесь `.orig` физически существует и
    `revertable` истинен по ОБЫЧНОМУ правилу (`has_session_baseline`) — значит
    запись на диск обязана пройти именно через `_session_precise_baseline`,
    и именно её нужно ловить, если она случайно завяжется на `STEP_HEAD`.
    """
    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "before\n")
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.view.accept(["a.py"])
    ws.write("a.py", "v2\n")
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2"))

    outcome = ws.view.revert(SOURCE_SESSION, ["a.py"])

    assert outcome.restored == ["a.py"]
    assert (ws.root / "a.py").read_text(encoding="utf-8") == "v1\n"


def test_revert_accepted_absent_deletes_file(tmp_path):
    ws = _Workspace(tmp_path)
    path = ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0, op="create"))
    path.unlink()
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2", op="delete"))
    ws.view.accept(["a.py"])
    assert ws.store.accepted_state("a.py").content is None
    ws.write("a.py", "recreated\n")   # файл возвращается на диск помимо принятого
    ws.journal.append(_rec("a.py", ts=3.0, call_id="c3", op="create"))

    [target] = ws.view.revert_targets(SOURCE_SESSION, ["a.py"])
    assert (target.kind, target.label) == (
        "delete", "будет удалён (принято отсутствие файла)",
    )

    outcome = ws.view.revert(SOURCE_SESSION, ["a.py"])

    assert outcome.deleted == ["a.py"]
    assert not (ws.root / "a.py").exists()


def test_accepted_absent_then_recreated_counts_from_emptiness(tmp_path):
    """I2: у ступени «принято отсутствие» база не отсутствует, а ПУСТА.

    Файл существовал до сессии, агент его удалил, пользователь принял
    отсутствие, агент пересоздал файл и дописал. Ступень есть — значит §12
    требует точных чисел «что отменит `r`», а строка до фикса брала счётчики
    из журнала (`exact=False`) и вид `modified`, хотя `r` файл УДАЛИТ.
    """
    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "до сессии\n")
    ws.write("a.py", "правка агента\n")
    ws.journal.append(_rec("a.py", ts=1.0, add=7, del_=7))     # числа журнала — не про диск
    (ws.root / "a.py").unlink()
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2", op="delete", add=7, del_=7))
    ws.view.accept(["a.py"])
    assert ws.store.accepted_state("a.py").content is None
    ws.write("a.py", "снова\nсоздан\nагентом\n")
    ws.journal.append(_rec("a.py", ts=3.0, call_id="c3", op="create", add=7, del_=7))

    [row] = ws.view.session_view(SOURCE_SESSION).active

    assert (row.entry.kind, row.entry.exact) == ("created", True)
    assert (row.entry.add, row.entry.del_) == (3, 0)    # от пустоты, а не 21/21 из журнала
    assert row.entry.revertable is True

    # Числа отвечают на вопрос «что отменит `r`»: превью и сам откат — удаление.
    [target] = ws.view.revert_targets(SOURCE_SESSION, ["a.py"])
    assert (target.kind, target.label) == (
        "delete", "будет удалён (принято отсутствие файла)",
    )
    assert ws.view.revert(SOURCE_SESSION, ["a.py"]).deleted == ["a.py"]


def test_accepted_absent_without_file_on_disk_stays_settled(tmp_path):
    """Принято отсутствие, файла нет — дельта от ступени пуста: разобран, не активен."""
    ws = _Workspace(tmp_path)
    path = ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0, op="create"))
    path.unlink()
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2", op="delete"))
    ws.view.accept(["a.py"])

    view = ws.view.session_view(SOURCE_SESSION)

    assert view.active == []
    assert [row.path for row in view.settled] == ["a.py"]


def test_turn_warns_on_post_start_accept_and_commit(tmp_path):
    """Позитив (a.py, b.py) и НЕГАТИВ (c.py) в одном тесте — иначе временной
    гейт `%ct >= ts начала хода` непроверяем: коммиты в тесте выполняются за
    миллисекунды, и если ``ts`` записи хода не подставлен реальным временем
    (сравнимым с `%ct`), сравнение истинно ВСЕГДА и не отличает «коммит до
    хода» от «коммит после хода». Даты коммитов заданы явно
    (``GIT_AUTHOR_DATE``/``GIT_COMMITTER_DATE``) — без этого пришлось бы
    спать между шагами теста, чтобы развести коммиты по разным секундам
    `%ct` (git его усекает).
    """
    before_turn = "2020-01-01T00:00:00"
    turn_start_dt = "2020-06-01T00:00:00"
    after_turn = "2021-01-01T00:00:00"

    ws = _Workspace(tmp_path, git=True)

    def commit_at(msg: str, when: str) -> None:
        env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        subprocess.run(["git", "commit", "-m", msg], cwd=str(ws.root), check=True,
                       capture_output=True, env=env)

    # a.py: коммит, задевающий путь, случится ПОСЛЕ первой записи хода — предупреждение есть.
    ws.write("a.py", "orig\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    commit_at("seed a", before_turn)
    ws.session_base("a.py", "orig\n")
    ws.turn_base("a.py", "orig\n", "t1")

    # c.py: коммит, задевающий путь, случился ДО начала хода — предупреждать не о чем,
    # хотя ступень HEAD у пути жива (HEAD отличается от начала сессии).
    ws.write("c.py", "c0\n")
    subprocess.run(["git", "add", "--", "c.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    commit_at("seed c", before_turn)
    ws.session_base("c.py", "c0\n")
    ws.write("c.py", "c1\n")
    subprocess.run(["git", "add", "--", "c.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    commit_at("commit before turn", before_turn)
    ws.turn_base("c.py", "c1\n", "t1")

    # b.py: принятие случится ПОСЛЕ первой записи хода.
    ws.write("b.py", "b1\n")
    ws.turn_base("b.py", "b1\n", "t1")

    turn_start_ts = datetime.fromisoformat(turn_start_dt).timestamp()
    ws.journal.append(_rec("a.py", turn="t1", ts=turn_start_ts))
    ws.journal.append(_rec("b.py", turn="t1", ts=turn_start_ts, call_id="c2"))
    ws.journal.append(_rec("c.py", turn="t1", ts=turn_start_ts, call_id="c3"))
    ws.write("c.py", "c1\nagent\n")   # правка внутри хода, без нового коммита

    ws.write("a.py", "orig\nagent\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    commit_at("commit inside turn", after_turn)
    ws.write("b.py", "b2\n")
    ws.view.accept(["b.py"])

    [a_target, b_target, c_target] = ws.view.revert_targets(
        SOURCE_TURN, ["a.py", "b.py", "c.py"],
    )

    assert any("заденет коммит" in w and "начала хода" in w for w in a_target.warns)
    accepted_ts = ws.store.accepted_state("b.py").ts
    expected_hhmm = f"{datetime.fromtimestamp(accepted_ts):%H:%M}"
    assert b_target.warns == (
        f"заденет принятое вами состояние (принято в {expected_hhmm})",
    )
    assert c_target.warns == ()   # коммит раньше начала хода — предупреждать не о чем


def test_turn_revert_can_go_below_head(tmp_path):
    """Ход: правка → коммит командой → правка; откат хода возвращает к началу хода."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "orig\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.turn_base("a.py", "orig\n", "t1")
    ws.write("a.py", "orig\nagent1\n")
    ws.journal.append(_rec("a.py", turn="t1", ts=1.0))
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit inside turn"], cwd=str(ws.root),
                   check=True, capture_output=True)
    ws.write("a.py", "orig\nagent1\nagent2\n")
    ws.journal.append(_rec("a.py", turn="t1", ts=2.0, call_id="c2"))

    outcome = ws.view.revert(SOURCE_TURN, ["a.py"])

    assert outcome.restored == ["a.py"]
    assert (ws.root / "a.py").read_text(encoding="utf-8") == "orig\n"


def test_settled_paths_are_skip_targets(tmp_path):
    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.view.accept(["a.py"])   # a.py уходит в разобранные (чист относительно принятого)

    [target] = ws.view.revert_targets(SOURCE_SESSION, ["a.py"])

    assert (target.kind, target.skip_reason) == ("skip", "изменений нет")
    assert target.warns == ()


def test_counting_base_never_reaches_disk_via_ladder(tmp_path):
    """У пути только `.gitorig` (счётная) → цель "сессия" недоступна: revertable=False,
    kind="skip" — страж §12: счётная база не должна доехать до отката."""
    ws = _Workspace(tmp_path)
    ws.shell_session_base("a.py", "ИНДЕКС\n")
    ws.write("a.py", "АГЕНТ\n")
    ws.journal.append(_rec("a.py", call_id=None, op="shell"))

    [target] = ws.view.revert_targets(SOURCE_SESSION, ["a.py"])

    assert target.kind == "skip"
    assert target.skip_reason == "достоверной базы для отката нет"
    assert target.warns == ()


# --- ActiveRow.step: действующая ступень наружу (задача 1 волны 4 смоука) ---


def test_turn_rows_never_carry_a_step(tmp_path):
    """С1: глиф `↺` — только когда ступень «принято» реально действует. В
    источнике «Ход» счёт идёт от начала хода, и принятие на него не влияет —
    `row.step` обязан быть `None` всегда, даже когда путь принят.
    """
    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.view.accept(["a.py"])
    ws.write("a.py", "v2\n")
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2"))

    [row] = ws.view.session_view(SOURCE_TURN).active

    assert row.step is None


def test_turn_row_carries_accepted_base_flag_even_without_a_step(tmp_path):
    """Финальный фикс волны 4 (Р27-ревью владельца на смоуке): «Ход» не
    считает от принятой базы (`step` остаётся `None`, С1), но факт её
    наличия обязан долетать до строки — иначе `u`/`Ctrl+U` не поняли бы, что
    там есть что снимать, хотя владелец явно оставил обе клавиши рабочими и
    в «Ходе» (эффект просто виден в «Сессии»)."""
    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.view.accept(["a.py"])
    ws.write("a.py", "v2\n")
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2"))

    [row] = ws.view.session_view(SOURCE_TURN).active

    assert row.step is None
    assert row.has_accepted_base is True


def test_session_row_carries_accepted_step_with_ts(tmp_path):
    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.view.accept(["a.py"])
    ws.write("a.py", "v2\n")
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2"))

    [row] = ws.view.session_view(SOURCE_SESSION).active

    accepted_ts = ws.store.accepted_state("a.py").ts
    assert row.step == RowStep(kind=STEP_ACCEPTED, ts=accepted_ts, ref=None)


def test_session_row_carries_head_step_with_ref(tmp_path):
    """Частично закоммиченный файл: ступень HEAD несёт короткую подпись
    коммита («<хэш> · HH:MM»), собранную существующим `git_ops.head_commit_label`.
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "orig\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "orig\n")
    ws.write("a.py", "orig\n" + "".join(f"line{i}\n" for i in range(40)))
    ws.journal.append(_rec("a.py", ts=1.0, add=40, del_=0))
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit +40"], cwd=str(ws.root), check=True,
                   capture_output=True)
    with open(ws.root / "a.py", "a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(f"more{i}\n" for i in range(10)))
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2", add=10, del_=0))

    [row] = ws.view.session_view(SOURCE_SESSION).active

    expected_ref = git_ops.head_commit_label(str(ws.root), "a.py")
    assert row.step == RowStep(kind=STEP_HEAD, ts=None, ref=expected_ref)
    short_hash, sep, hhmm = row.step.ref.partition(" · ")
    assert sep == " · " and short_hash and len(hhmm) == 5 and hhmm[2] == ":"


def test_row_without_step_is_counted_from_session_start(tmp_path):
    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "a\nb\nc\n")
    ws.write("a.py", "a\nB\nc\nd\n")
    ws.journal.append(_rec("a.py", ts=1.0, add=2, del_=1))

    [row] = ws.view.session_view(SOURCE_SESSION).active

    assert row.step is None
    assert (row.entry.add, row.entry.del_) == (2, 1)


def test_active_row_has_no_accepted_base_without_any_accept(tmp_path):
    """Обратная сторона факта: файл, который никогда не принимали, не должен
    считаться «есть с чего снимать `u`» просто потому, что поле существует."""
    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "a\nb\nc\n")
    ws.write("a.py", "a\nB\nc\nd\n")
    ws.journal.append(_rec("a.py", ts=1.0, add=2, del_=1))

    [row] = ws.view.session_view(SOURCE_SESSION).active

    assert row.has_accepted_base is False


def test_head_commit_label_called_once_per_view_build(tmp_path, monkeypatch):
    """`ref` собирается батчем на сборку вида — пачкой (`git_ops.last_commits`),
    без пофайлового `head_commit_label` вовсе, и не по разу на каждое чтение
    строки. Пофайловые вызовы стоили секунд уже на десятке файлов, а после
    коммита правок команды на 2000 путей — 26 с открытия экрана.
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "orig\n")
    ws.write("b.py", "orig\n")
    subprocess.run(["git", "add", "--", "a.py", "b.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    for rel in ("a.py", "b.py"):
        ws.session_base(rel, "orig\n")
        ws.write(rel, "orig\n" + "".join(f"line{i}\n" for i in range(40)))
    ws.journal.append(_rec("a.py", ts=1.0, add=40, del_=0))
    ws.journal.append(_rec("b.py", ts=1.0, call_id="c2", add=40, del_=0))
    subprocess.run(["git", "add", "--", "a.py", "b.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit +40"], cwd=str(ws.root), check=True,
                   capture_output=True)
    for rel in ("a.py", "b.py"):
        with open(ws.root / rel, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("more\n")
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c3", add=1, del_=0))
    ws.journal.append(_rec("b.py", ts=2.0, call_id="c4", add=1, del_=0))

    calls: list[str] = []
    real = git_ops.head_commit_label
    monkeypatch.setattr(
        git_ops, "head_commit_label",
        lambda root, rel: (calls.append(rel), real(root, rel))[1],
    )

    view = ws.view.session_view(SOURCE_SESSION)
    for row in view.active:
        assert row.step is not None and row.step.ref     # чтение — не новый вызов

    assert calls == []


# --- дефект диагностики: равенство байтов до декодирования -------------------


def test_accepted_undecodable_file_settles(tmp_path):
    """Правка командой без точной базы → `≈` (`exact=False`) → `accept` без
    дальнейших правок обязан увести файл в разобранные — раньше неудача
    декодирования (chardet ниже порога на коротком кириллическом тексте)
    держала такой файл активным вечно (`_settled_reasons` не считал дельту
    от принятой базы пустой, хотя байты побайтово совпадали).
    """
    ws = _Workspace(tmp_path)
    content = "привет\n".encode("cp1251")
    (ws.root / "a.py").write_bytes(content)
    ws.journal.append(_rec("a.py", ts=1.0, call_id=None, op="shell", add=1, del_=1,
                           flags=["partial"]))

    [row] = ws.view.session_view(SOURCE_SESSION).active
    assert row.entry.exact is False

    ws.view.accept(["a.py"])
    view = ws.view.session_view(SOURCE_SESSION)

    assert view.active == []
    [row] = view.settled
    assert (row.path, row.reason) == ("a.py", SETTLED_MANUAL)


def test_reset_of_unchanged_undecodable_file_loses_nothing(tmp_path):
    """§4.3: диск не разошёлся с принятым — терять нечего, подтверждение не
    спрашивается, даже когда байты не декодируются уверенно (тот же дефект
    диагностики, только со стороны `reset_targets`)."""
    ws = _Workspace(tmp_path)
    (ws.root / "a.py").write_bytes("привет\n".encode("cp1251"))
    ws.view.accept(["a.py"])

    [reset] = ws.view.reset_targets(["a.py"])

    assert reset.loses_point is False


def test_path_returned_to_the_session_baseline_leaves_the_screen(tmp_path):
    """Правки не осталось — строке не место ни в активных, ни в группе.

    Такой путь уезжал в «закоммичено»: правило членства ловит «известен git и
    чист относительно HEAD», а после отката в чистом репозитории это верно
    ВСЕГДА — и экран утверждал коммит, которого не было. Тот же случай, когда
    агент сам вернул правку обратно.
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "v1\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "v1\n")
    ws.write("a.py", "v2\n")
    ws.journal.append(_rec("a.py", ts=1.0))

    view = ws.view.session_view(SOURCE_SESSION)
    assert [row.entry.path for row in view.active] == ["a.py"]      # правка живая

    ws.write("a.py", "v1\n")            # вернули к началу сессии — как делает откат
    ws.view.invalidate()
    view = ws.view.session_view(SOURCE_SESSION)

    assert [row.entry.path for row in view.active] == []
    assert [row.path for row in view.settled] == []


def test_a_really_committed_path_still_shows_as_committed(tmp_path):
    """Обратная сторона: настоящий коммит по-прежнему «закоммичено».

    Скрывается только путь, вернувшийся к началу сессии; у закоммиченного
    правка на диске жива, ступень — HEAD, и порода остаётся.
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "v1\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "v1\n")
    ws.write("a.py", "v2\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "agent commit"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.view.invalidate()

    view = ws.view.session_view(SOURCE_SESSION)

    assert [row.entry.path for row in view.active] == []
    assert [(row.path, row.reason) for row in view.settled] == [("a.py", SETTLED_COMMIT)]


def test_created_empty_path_returned_to_its_snapshot_is_not_hidden(tmp_path):
    """Созданный путь скрывать нельзя: откат его УДАЛЯЕТ, а не пишет базу.

    Счётная дельта у созданного считается от ПУСТОТЫ, поэтому обычно она не
    нулевая и такой путь до сверки не доходит. Пустой созданный файл — доходит:
    и счётный ноль, и совпадение с `.orig` налицо. А `r` по нему всё равно
    удалит файл (`revert._delete_created`), и спрятанная строка отняла бы у
    пользователя единственный способ это сделать.
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("new.txt", "")                       # агент создал файл пустым
    ws.journal.append(_rec("new.txt", op="create", ts=1.0, add=0, del_=0))
    ws.session_base("new.txt", "")                # `.orig` лёг со ВТОРОЙ правкой
    ws.write("new.txt", "нечто\n")
    ws.journal.append(_rec("new.txt", ts=2.0, call_id="c2"))
    ws.write("new.txt", "")                       # вернули к `.orig`
    ws.view.invalidate()

    view = ws.view.session_view(SOURCE_SESSION)

    assert [row.entry.path for row in view.active] == ["new.txt"]
    assert view.active[0].entry.kind == "created"      # откат = удаление


def test_deleted_path_with_an_empty_baseline_is_not_hidden(tmp_path):
    """Удалённый путь скрывать нельзя: откат его ВЕРНЁТ.

    `delta_vs_disk` читает отсутствующий файл как пустой текст, поэтому у
    пути с базой `b""` (снимок пустого, но существовавшего файла) удаление
    даёт счётный ноль.
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("empty.txt", "")
    ws.session_base("empty.txt", "")
    ws.write("empty.txt", "стало непусто\n")
    ws.journal.append(_rec("empty.txt", ts=1.0))
    (ws.root / "empty.txt").unlink()              # путь удалён
    ws.view.invalidate()

    view = ws.view.session_view(SOURCE_SESSION)

    assert [row.entry.path for row in view.active] == ["empty.txt"]


def test_path_with_a_nonzero_counting_delta_is_not_hidden(tmp_path):
    """Скрывать по одной точной базе нельзя: экран считает СЧЁТНОЙ.

    Обратная сторона того же расхождения баз: `.gitorig` лёг раньше (шелл
    тронул путь до инструмента), точная совпала с диском, а счётная — нет.
    Строка со своими `+1` исчезла бы вместе с числами из итогов хода.
    """
    ws = _Workspace(tmp_path)
    ws.write("a.py", "A\nB\n")
    # Точная база — первой: поверх счётной API её больше не пишет. Пара «счётная
    # старше, точная моложе» — наследие хранилищ до этого правила; времена ниже явные.
    ws.session_base("a.py", "A\nB\n")         # точная: совпала с диском
    ws.shell_session_base("a.py", "A\n")        # счётная: диск от неё отличается
    now = time.time()
    os.utime(ws.snapshot_path(".gitorig"), (now - 30, now - 30))
    os.utime(ws.snapshot_path(".orig"), (now - 10, now - 10))
    ws.journal.append(_rec("a.py", call_id=None, op="shell", ts=1.0))
    ws.journal.append(_rec("a.py", ts=2.0))

    view = ws.view.session_view(SOURCE_SESSION)

    assert [row.entry.path for row in view.active] == ["a.py"]
    assert (view.total_add, view.total_del) == (1, 0)   # числа на месте


def test_agent_writing_the_committed_version_over_dirty_work_stays_revertable(tmp_path):
    """Агент затёр несохранённую работу версией из коммита — строка обязана жить.

    Файл совпал с HEAD, но коммита за сессию НЕ БЫЛО: совпадение получилось
    потому, что агент сам записал туда содержимое коммита. Старое правило
    членства читало это как «закоммичен» и убирало строку в группу разобранного,
    где откат её ПРОПУСКАЕТ, — несохранённая работа пользователя оказывалась с
    экрана недостижима, хотя точный снимок с ней цел.
    """
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "закоммиченное\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.write("a.py", "работа пользователя\n")          # несохранённая работа
    ws.session_base("a.py", "работа пользователя\n")   # с неё началась сессия
    ws.write("a.py", "закоммиченное\n")                # агент затёр её версией из коммита
    ws.journal.append(_rec("a.py", ts=1.0))

    view = ws.view.session_view(SOURCE_SESSION)

    assert [row.entry.path for row in view.active] == ["a.py"]
    assert [row.path for row in view.settled] == []

    ws.view.revert(SOURCE_SESSION, ["a.py"])

    assert (ws.root / "a.py").read_text(encoding="utf-8") == "работа пользователя\n"


def test_turn_hides_a_row_whose_turn_left_nothing(tmp_path):
    """«Пусто — значит нет строки», и живое принятие исключением не делает.

    Сценарий одного хода: `a`, затем `r`. Диск вернулся к началу хода, правки
    не осталось. Исключение ради достижимости `u` держало строку видимой — но
    `f21cf7bbc` перевёл пометку «принят» на ТЕКУЩЕЕ состояние, а содержимое
    после отката принятому уже не равно. Строка `+0 −0` осталась без пометки и
    перестала объяснять, почему она на экране (пятый раунд ревью, находка 5):
    видимость говорила о факте из прошлого, пометка — о состоянии.

    Отнятого не пропало. Путь виден и снимается в «Сессии»: там дельта идёт от
    ПРИНЯТОЙ базы и после отката не пуста. А `Ctrl+U` уходит из «Хода» вместе
    со строкой — `mass_reset` считается по видимым строкам, — так что
    молчаливого действия по невидимому пути не будет.
    """
    ws = _Workspace(tmp_path)
    ws.write("a.py", "было\n")
    ws.session_base("a.py", "было\n")
    ws.store.capture_turn("a.py", "было\n".encode(), "t1")
    ws.write("a.py", "стало\n")
    ws.journal.append(_rec("a.py", turn="t1", ts=1.0))
    ws.view.accept(["a.py"])
    ws.view.revert(SOURCE_TURN, ["a.py"])
    ws.view.invalidate()

    turn = ws.view.session_view(SOURCE_TURN)
    assert turn.active == []

    session = ws.view.session_view(SOURCE_SESSION)
    assert [(r.entry.path, r.has_accepted_base) for r in session.active] == [("a.py", True)]


def test_a_command_cut_by_the_cap_leaves_its_real_size_in_the_view(tmp_path):
    """Команда, урезанная потолком записи, оставляет в виде настоящее число путей.

    Пути сверх потолка шелл-канал не записывает: их нет в списке и откатить их
    нельзя. Экрану нужно, чем в этом признаться (`cut_commands`), и нужно знать,
    КАКАЯ команда урезана: скрывают предупреждение про конкретную команду. «Ход»
    видит только свой ход: урезанная команда прошлого хода к нему не относится."""
    from dataclasses import replace

    ws = _Workspace(tmp_path)
    ws.write("a.py", "a\n")
    ws.write("b.py", "b\n")
    ws.journal.append(replace(_rec("a.py", turn="t1", call_id=None, op="shell"),
                              command_paths=23456))
    ws.journal.append(_rec("b.py", turn="t2", call_id=None, op="shell", ts=2.0))

    assert ws.view.session_view(SOURCE_SESSION).cut_commands == (("t1", 23456),)
    assert ws.view.session_view(SOURCE_TURN).cut_commands == ()


def _committed_workspace(tmp_path, n: int) -> "_Workspace":
    """`n` путей со снимком сессии, затем правки, закоммиченные поверх снимков."""
    ws = _Workspace(tmp_path, git=True)
    for i in range(n):
        ws.write(f"p{i}.py", "orig\n")
    subprocess.run(["git", "add", "-A"], cwd=str(ws.root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    for i in range(n):
        ws.session_base(f"p{i}.py", "orig\n")
        ws.turn_base(f"p{i}.py", "orig\n", "t1")
        ws.write(f"p{i}.py", "orig\nnew\n")
        ws.journal.append(_rec(f"p{i}.py", ts=float(i), call_id=f"c{i}", add=1, del_=0))
    subprocess.run(["git", "commit", "-qam", "commit edits"], cwd=str(ws.root),
                   check=True, capture_output=True)
    return ws


def _count_git(monkeypatch) -> list:
    """Каждый запуск процесса git — и одиночный, и из параллельной пачки (`run_git_many`)."""
    calls = []
    real = git_ops._run_unguarded
    monkeypatch.setattr(git_ops, "_run_unguarded",
                        lambda *a, **k: (calls.append(a[1]), real(*a, **k))[1])
    return calls


def _busy_snapshot_index(monkeypatch) -> None:
    """Замок индекса снимков отвечает таймаутом — как при конкурирующем писателе.

    Подменяется чтение индекса СТОРА, а не метод вида: занятым бывает стор, и
    страж покрывает каждое чтение индекса в проверяемом вызове, а не одно."""
    from gc_core.agent_edits.snapshots import SnapshotStore

    def busy(self):
        raise TimeoutError("индекс снимков занят")

    monkeypatch.setattr(SnapshotStore, "_index_snapshot", busy)


def test_view_after_committing_many_paths_costs_a_constant_number_of_git_calls(
    tmp_path, monkeypatch
):
    """После коммита правок лестница спрашивает git пачками, а не по три раза на путь.

    Атрибуты, прежняя версия из HEAD и подпись коммита спрашивались по каждому
    закоммиченному пути, синхронно в потоке UI: 2000 путей — 26 с открытия F3."""
    ws = _committed_workspace(tmp_path, 12)
    calls = _count_git(monkeypatch)

    view = ws.view.session_view(SOURCE_SESSION)

    assert len(view.settled) == 12                       # всё ушло в коммит — разобрано
    assert len(calls) <= 8, [c[:4] for c in calls]


def test_turn_revert_targets_of_many_committed_paths_cost_a_constant_number_of_git_calls(
    tmp_path, monkeypatch
):
    """Цели отката «Хода» по многим закоммиченным путям — тоже пачками.

    Признак «коммит был» и факты лестницы спрашивались по одному на путь."""
    ws = _committed_workspace(tmp_path, 12)
    keys = [f"p{i}.py" for i in range(12)]
    calls = _count_git(monkeypatch)

    targets = ws.view.revert_targets(SOURCE_TURN, keys)

    assert len(targets) == 12
    assert len(calls) <= 8, [c[:4] for c in calls]


def test_prefetch_does_not_read_blobs_the_ladder_never_looks_at(tmp_path, monkeypatch):
    """Блоб HEAD файла сверх `max_diff_source_bytes` пачкой не читается.

    Лестница такой файл с диска не читает (`head_bytes` берёт форму выкладки),
    и его блоб в пачке — чистый балласт памяти."""
    from gc_tui.services.edits_view import _LadderGitFacts

    ws = _committed_workspace(tmp_path, 2)
    ws.write("p1.py", "x" * 5000)
    facts = _LadderGitFacts(str(ws.root), 100)
    facts.prime(git_ops.head_rev_from_files(str(ws.root)), {})
    asked = []
    real = git_ops.blobs_at
    monkeypatch.setattr(git_ops, "blobs_at",
                        lambda root, rels, **kw: (asked.extend(rels), real(root, rels, **kw))[1])
    since = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=str(ws.root),
                           capture_output=True, text=True, check=True).stdout.strip()

    facts.prefetch({since: ["p0.py", "p1.py"]})

    assert asked == ["p0.py"]


def test_reset_targets_survive_a_busy_snapshot_index(tmp_path, monkeypatch):
    """Занятый индекс снимков не роняет превью снятия принятия — цель деградирует.

    Признак коммита читает индекс под замком; при конкуренции (субагенты, swarm
    в одном проекте) замок отвечает таймаутом. Пофайловый расчёт гасил его и
    отдавал цель «начало сессии»; пачка, вынесенная из-под обработки, роняла бы
    экран на `u`."""
    ws = _committed_workspace(tmp_path, 3)
    keys = [f"p{i}.py" for i in range(3)]
    _busy_snapshot_index(monkeypatch)                     # вид не собираем: превью `u` его не зовёт

    assert len(ws.view.reset_targets(keys)) == 3


def test_resetting_many_accepted_committed_paths_costs_a_constant_number_of_git_calls(
    tmp_path, monkeypatch
):
    """Само снятие принятия — пачкой, как и его превью.

    Превью (`reset_targets`) считало признак коммита батчем, а применение
    (`reset_accept`) — по `git diff` на путь: замороженный поток UI возвращался
    на шаге, который пользователь уже подтвердил."""
    ws = _committed_workspace(tmp_path, 12)
    keys = [f"p{i}.py" for i in range(12)]
    for key in keys:
        ws.store.capture_accepted(key, (ws.root / key).read_bytes(), head_oid=None)
    calls = _count_git(monkeypatch)

    outcome = ws.view.reset_accept(keys)

    assert len(outcome.reset) == 12
    assert len(calls) <= 8, [c[:4] for c in calls]


def test_turn_revert_targets_survive_a_busy_snapshot_index(tmp_path, monkeypatch):
    """При СОБРАННОМ виде превью отката «Хода» на занятом индексе деградирует по путям.

    Без предупреждений «заденет коммит» и «заденет принятое», но с целью на
    каждый путь. Отказ самой сборки вида — другой случай: превью без вида не
    построить, и его ловит граница действий экрана
    (`test_diff_screen.test_actions_on_a_busy_snapshot_index_warn_instead_of_crashing`)."""
    ws = _committed_workspace(tmp_path, 3)
    keys = [f"p{i}.py" for i in range(3)]
    ws.view.session_view(SOURCE_TURN)                     # вид собран до занятого замка
    _busy_snapshot_index(monkeypatch)

    assert len(ws.view.revert_targets(SOURCE_TURN, keys)) == 3


def test_a_failed_warm_up_keeps_the_committed_set(tmp_path, monkeypatch):
    """Сбой прогрева фактов лестницы не выбрасывает уже посчитанное множество закоммиченных.

    Иначе цикл пересчитывал бы признак коммита по пути — ещё раз дёргая индекс
    снимков, который только что ответил."""
    ws = _committed_workspace(tmp_path, 3)
    keys = [f"p{i}.py" for i in range(3)]
    head_rev = git_ops.head_rev_from_files(str(ws.root))

    def broken(*_a, **_k):
        raise OSError("прогрев не удался")

    monkeypatch.setattr(ws.view._ladder_facts, "prefetch", broken)

    assert ws.view._prepare_head_steps(keys, head_rev) == set(keys)


def test_revert_preview_of_many_paths_builds_the_view_once(tmp_path, monkeypatch):
    """Дельты превью массового отката — пачкой: вид и подпись диска считаются один раз.

    Превью звало `revert_delta` на каждый путь, а тот перебирал все записи
    журнала и пересобирал ключ кэша вида — `stat` по всем путям набора. На
    10 000 файлов это ~10^8 системных вызовов: смок 2026-09-24 повесил
    интерфейс на `Ctrl+R` дольше, чем его ждали."""
    from gc_tui.services import edits_view as edits_view_mod

    ws = _Workspace(tmp_path)
    keys = [f"f{i}.py" for i in range(20)]
    for i, key in enumerate(keys):
        ws.session_base(key, "old\n")
        ws.write(key, "new\n")
        ws.journal.append(_rec(key, ts=float(i), call_id=f"c{i}"))
    expected = [ws.view.revert_delta(SOURCE_SESSION, key) for key in keys]
    ws.view.invalidate()
    signatures = []
    real = edits_view_mod.disk_signature
    monkeypatch.setattr(edits_view_mod, "disk_signature",
                        lambda *a, **k: (signatures.append(1), real(*a, **k))[1])

    assert ws.view.revert_deltas(SOURCE_SESSION, keys) == expected
    assert len(signatures) == 1


def test_a_created_then_reverted_binary_leaves_no_row(tmp_path):
    """Созданный и откаченный БИНАРЬ тоже строки не оставляет.

    У бинаря и файла сверх капа числа неточные (`exact=False`), и проверка
    «создан и его нет» стояла за гейтом точного нуля — до неё такой путь не
    доходил. Но отсутствующему созданному файлу неважно, считается ли он:
    откату делать нечего. После отката сотни сгенерированных бинарей (сборка,
    `.pyc`) иначе висели бы сотней мёртвых строк «удалён» (ревью `3e684cab3`)."""
    ws = _Workspace(tmp_path)
    ws.write("keep.py", "new\n")
    ws.session_base("keep.py", "old\n")
    ws.journal.append(_rec("keep.py", ts=1.0, call_id="c1"))
    ws.journal.append(_rec("gone.bin", ts=2.0, call_id=None, op="create",
                           add=0, del_=0, flags=["no_diff"]))
    # gone.bin создан командой и уже удалён откатом — на диске его нет

    for source in (SOURCE_SESSION, SOURCE_TURN):
        view = ws.view.session_view(source)
        assert [row.entry.path for row in view.active] == ["keep.py"], source


def test_a_created_then_reverted_file_leaves_no_row(tmp_path):
    """Файл, созданный в наборе и удалённый откатом, строки не оставляет.

    Исходное состояние такого пути — «файла нет», и откату больше нечего
    делать. Правило «пусто — значит нет строки» требовало файла на диске и
    оставляло «удалён +0 −0»: смок 2026-09-24 после отката 10 000 созданных
    файлов показал 10 000 мёртвых строк."""
    ws = _Workspace(tmp_path)
    ws.write("keep.py", "new\n")
    ws.session_base("keep.py", "old\n")
    ws.journal.append(_rec("keep.py", ts=1.0, call_id="c1"))
    ws.journal.append(_rec("gone.py", ts=2.0, call_id=None, op="create", add=1, del_=0))
    # gone.py создан командой и уже удалён откатом — на диске его нет

    for source in (SOURCE_SESSION, SOURCE_TURN):
        view = ws.view.session_view(source)
        assert [row.entry.path for row in view.active] == ["keep.py"], source


def test_an_accept_by_another_instance_invalidates_the_cached_view(tmp_path):
    """Принятие во втором экземпляре TUI видно первому — кэш вида не устаревает.

    Принятие не трогает ни журнал, ни диск, ни HEAD — а только из них и
    собирался ключ кэша. Первый экземпляр продолжал считать файл активным, и
    его откат вёл к началу сессии вместо принятого состояния (ревью PR,
    находка 8). Ключ теперь включает подпись индекса снимков."""
    ws = _Workspace(tmp_path)
    ws.session_base("a.py", "original\n")
    ws.write("a.py", "agent\n")
    ws.journal.append(_rec("a.py"))
    assert [row.entry.path for row in ws.view.session_view(SOURCE_SESSION).active] == ["a.py"]

    other = EditsView(str(ws.threads_dir), "cli", "th1", str(ws.root), {})
    other.accept(["a.py"])

    view = ws.view.session_view(SOURCE_SESSION)
    assert view.active == []
    assert [row.path for row in view.settled] == ["a.py"]


def test_revert_state_catches_an_edit_that_kept_the_modification_time(tmp_path):
    """Правка того же размера с сохранённым временем изменения — подпись другая.

    Время изменения выставляет кто угодно (`cp -p`, `rsync -a`, `os.utime`), а
    на части файловых систем оно грубое. Подпись несёт ещё номер файла —
    атомарная замена его меняет — и `ctime`, который программе не выставить
    (ревью починок, находка 2)."""
    import os

    ws = _Workspace(tmp_path)
    path = ws.write("a.py", "aaaa\n")
    before = ws.view.revert_state(["a.py"])
    st = path.stat()

    replacement = ws.write("a.py.new", "bbbb\n")
    os.replace(replacement, path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

    assert ws.view.revert_state(["a.py"]) != before


def _deleted_and_committed(tmp_path) -> "_Workspace":
    """Файл правили в сессии, затем удалили и закоммитили удаление."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "orig\n")
    subprocess.run(["git", "add", "-A"], cwd=str(ws.root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("a.py", "orig\n")
    ws.write("a.py", "orig\nnew\n")
    ws.journal.append(_rec("a.py", ts=1.0, add=1, del_=0))
    subprocess.run(["git", "rm", "-qf", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-qm", "drop a"], cwd=str(ws.root), check=True,
                   capture_output=True)
    return ws


def test_a_committed_deletion_settles_the_file_instead_of_offering_its_old_copy(tmp_path):
    """Удаление закоммичено — откатывать сессии нечего: файл уходит в разобранные.

    Прежде откат сессии восстанавливал старую копию — уходил ниже коммита
    (ревью PR, второй коллега, находка 1)."""
    ws = _deleted_and_committed(tmp_path)

    view = ws.view.session_view(SOURCE_SESSION)

    assert view.active == []
    assert [row.path for row in view.settled] == ["a.py"]


def test_a_file_back_on_disk_after_its_committed_deletion_is_never_deleted(tmp_path):
    """Файл снова на диске после закоммиченного удаления — откат его не удаляет.

    Ступень «удаление закоммичено» строится, только если файла нет и на диске:
    путь отсутствует в HEAD и тогда, когда файл пересоздан, снят с учёта или
    переименован регистром, — и во всех этих случаях удаление стёрло бы файл
    пользователя (ревью починок). Откат возвращает содержимое, ничего не удаляя."""
    ws = _deleted_and_committed(tmp_path)
    ws.write("a.py", "again\n")
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2", op="create", add=1, del_=0))

    ws.view.session_view(SOURCE_SESSION)
    assert all(t.kind != "delete" for t in ws.view.revert_targets(SOURCE_SESSION, ["a.py"]))

    ws.view.revert(SOURCE_SESSION, ["a.py"])
    assert (ws.root / "a.py").exists()


def test_an_untracked_but_kept_file_is_never_deleted_by_revert(tmp_path):
    """`git rm --cached` и коммит: файла нет в HEAD, но он остался на диске — откат
    его не удаляет (ревью починок, находка 2)."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("config.local", "orig\n")
    subprocess.run(["git", "add", "-A"], cwd=str(ws.root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("config.local", "orig\n")
    ws.write("config.local", "orig\nmine\n")
    ws.journal.append(_rec("config.local", ts=1.0, add=1, del_=0))
    subprocess.run(["git", "rm", "-q", "--cached", "config.local"], cwd=str(ws.root),
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "untrack"], cwd=str(ws.root), check=True,
                   capture_output=True)

    ws.view.session_view(SOURCE_SESSION)
    assert all(t.kind != "delete"
               for t in ws.view.revert_targets(SOURCE_SESSION, ["config.local"]))
    ws.view.revert(SOURCE_SESSION, ["config.local"])
    assert (ws.root / "config.local").exists()


def test_a_committed_case_only_directory_rename_is_not_a_deletion(tmp_path):
    """Каталог переименован только регистром (`Docs` → `docs`) — не удаление.

    Проверка имени файла это не ловила: на macOS и Windows `Docs/a.md` открывается
    как прежде, а `ls-tree` его не находит (ревью починок, находка 1)."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("Docs/a.md", "orig\n")
    subprocess.run(["git", "add", "-A"], cwd=str(ws.root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("Docs/a.md", "orig\n")
    ws.write("Docs/a.md", "orig\nnew\n")
    ws.journal.append(_rec("Docs/a.md", ts=1.0, add=1, del_=0))
    # В два шага: на файловой системе без различия регистра `git mv Docs docs` отказывает.
    for src, dst in (("Docs", "docs-tmp"), ("docs-tmp", "docs")):
        subprocess.run(["git", "mv", src, dst], cwd=str(ws.root), check=True,
                       capture_output=True)
    subprocess.run(["git", "commit", "-qm", "rename dir"], cwd=str(ws.root), check=True,
                   capture_output=True)

    ws.view.session_view(SOURCE_SESSION)
    assert all(t.kind != "delete" for t in ws.view.revert_targets(SOURCE_SESSION, ["Docs/a.md"]))
    ws.view.revert(SOURCE_SESSION, ["Docs/a.md"])
    assert (ws.root / "docs" / "a.md").exists()


def test_a_committed_case_only_rename_is_not_a_deletion(tmp_path):
    """Закоммиченное переименование, меняющее только регистр, — не удаление.

    `ls-tree` сравнивает регистр строго и не находит `Readme.md` после
    `git mv Readme.md README.md`, а файловая система macOS и Windows регистр не
    различает: файл «на месте», в разобранные не уходит, и `r` по ступени
    «файла нет» УДАЛЯЛ закоммиченный `README.md` (ревью починок, находка 2)."""
    ws = _Workspace(tmp_path, git=True)
    ws.write("Readme.md", "orig\n")
    subprocess.run(["git", "add", "-A"], cwd=str(ws.root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.session_base("Readme.md", "orig\n")
    ws.write("Readme.md", "orig\nnew\n")
    ws.journal.append(_rec("Readme.md", ts=1.0, add=1, del_=0))
    subprocess.run(["git", "mv", "Readme.md", "README.md"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-qm", "rename"], cwd=str(ws.root), check=True,
                   capture_output=True)

    ws.view.session_view(SOURCE_SESSION)
    targets = ws.view.revert_targets(SOURCE_SESSION, ["Readme.md"])

    assert all(target.kind != "delete" for target in targets)
