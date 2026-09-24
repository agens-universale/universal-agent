"""Экран изменений агента (`/diff`, F3), этап A — список источников.

Слой презентации проверяется двумя способами и оба нужны:

* жёсткая сетка строки и шапка — чистыми функциями (`build_rows`,
  `build_header`): колонки считаются по максимуму ПО ВСЕМУ списку, и увидеть
  расхождение можно только сравнив строки между собой;
* поведение (курсор, источники, клики, пустые состояния) — живым pilot'ом.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from gc_core.config_modules import env_vars
from gc_core.runtime_notices import clear_runtime_notices
from gc_core.agent_edits.deltas import FileEntry
from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.ladder import STEP_ACCEPTED, STEP_HEAD
from gc_core.agent_edits.snapshots import SnapshotStore, snapshots_root
from gc_core.thread_store import edits_journal_path
from gc_tui.palette import ACCENT, BRIGHT, DEEP_DIM, ERR, INK, MUTED, OK
from gc_tui.screens.diff import (
    ACCEPTED_GLYPH,
    MARK_ACCEPTED,
    APPROX,
    EMPTY_SESSION,
    EMPTY_TURN,
    MARK_EXTERNAL,
    MARK_SHELL,
    NO_THREAD,
    ROW_ACCEPTED,
    ROW_GROUP,
    ROW_GROUP_FILE,
    ROW_PLAIN,
    VIEWER_NOTE_INEXACT,
    DiffScreen,
    accept_ack,
    accept_tone,
    build_group_row,
    build_header,
    build_rows,
    build_settled_rows,
    footer_text,
    group_summary,
    mass_tail,
    norm_key,
    paint_counters,
    reset_ack,
    reset_tone,
    viewer_footer,
)
from gc_tui.services.edits_view import (
    ACCEPT_READ_FAILED,
    ACCEPT_TOO_LARGE,
    RESET_NOT_ACCEPTED,
    SETTLED_COMMIT,
    SETTLED_MANUAL,
    SOURCE_SESSION,
    SOURCE_TURN,
    AcceptOutcome,
    EditsView,
    ResetOutcome,
    RowStep,
    SettledRow,
)
from gc_testkit.tui_support import span_style


# --- фикстуры данных ---------------------------------------------------------


def _entry(
    path="a.py", add=1, del_=1, kind="modified", exact=True, countable=True,
    by_shell=False, external=False, revertable=True, last_ts=1.0,
) -> FileEntry:
    return FileEntry(
        path=path, add=add, del_=del_, kind=kind, exact=exact,
        countable=countable, by_shell=by_shell, external=external,
        revertable=revertable, last_ts=last_ts,
    )


def _rec(path="a.py", turn="t1", call_id="c1", op="edit", add=1, del_=1, ts=1.0):
    return EditRecord(
        path=path, turn_id=turn, ts=ts, call_id=call_id, op=op, hunks="",
        add=add, del_=del_, before_sha256=None, after_sha256=None, flags=[],
    )


def _settled(
    path="a.py", add=1, del_=1, kind="modified", reason=SETTLED_MANUAL, exact=True,
) -> SettledRow:
    return SettledRow(
        path=path, add=add, del_=del_, kind=kind, reason=reason, exact=exact,
    )


class _Workspace:
    """Тред с журналом и снимками + файлы на диске под launch_root."""

    def __init__(self, tmp_path: Path, *, git: bool = False) -> None:
        self.threads_dir = tmp_path / "threads"
        self.threads_dir.mkdir(parents=True)
        self.root = tmp_path / "proj"
        self.root.mkdir(parents=True)
        if git:
            (self.root / ".git").mkdir()
        self.journal = EditsJournal(
            edits_journal_path(str(self.threads_dir), "cli", "th1")
        )
        self.store = SnapshotStore(
            snapshots_root(str(self.root)), "cli_th1", self.threads_dir
        )

    @property
    def config(self) -> dict:
        return {"launch_root": str(self.root), "model": "M", "diff_tui": {}}

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def base(self, rel: str, text: str) -> None:
        """Точный снимок начала сессии — с ревизией HEAD, как пишет рекордер.

        Без ревизии признака «коммит затронул путь уже в этой сессии» нет, и
        ступень HEAD не появится ни в одном сценарии (§3.2). У пустышки
        `.git` (git=True) ревизии не существует — там аргумент просто `None`.
        """
        from gc_core.agent_edits import git_ops

        self.store.capture_session(
            rel,
            text.encode("utf-8"),
            head_rev=git_ops.head_rev_from_files(str(self.root)),
        )

    def accept(self, rel: str, text: str) -> None:
        """Принять путь «вручную» — готовый снимок для settled-сценариев экрана."""
        self.store.capture_accepted(rel, text.encode("utf-8"), head_oid=None)

    def screen(self, **kwargs) -> DiffScreen:
        return DiffScreen(
            launch_root=str(self.root),
            config=self.config,
            threads_dir=str(self.threads_dir),
            system_id="cli",
            thread_id="th1",
            **kwargs,
        )


def _plain(widget) -> str:
    return str(widget.render())


def _rows_text(screen: DiffScreen) -> list[str]:
    return [_plain(row) for row in screen._rows]


def _screen_lines(app) -> list[str]:
    """Текстовый слепок экрана строками композитора (приём тестов блоков диффа)."""
    return ["".join(seg.text for seg in strip).rstrip()
            for strip in app.screen._compositor.render_strips()]


# --- сетка строки: жёсткое требование дизайн-гейта ---------------------------


_GRID = [
    _entry("src/tui/screens/diff.py", add=84, del_=12, last_ts=6.0),
    _entry("src/agent_edits/revert.py", add=96, del_=0, kind="created", last_ts=5.0),
    _entry("src/agent_edits/deltas.py", add=9, del_=3, by_shell=True, last_ts=4.0),
    _entry("src/tui/app.py", add=7, del_=2, external=True, last_ts=3.0),
    _entry("src/config.py", add=5, del_=1, by_shell=True, external=True, last_ts=2.0),
    _entry("uv.lock", add=3, del_=1, by_shell=True, exact=False, last_ts=1.0),
]


def test_counter_columns_are_identical_for_rows_with_and_without_marks():
    """Колонки `+`/`−` занимают одни и те же позиции — с пометками и без.

    Иначе счётчики «пляшут» по вертикали ровно там, где глаз их и сверяет.
    Числа внутри колонки выровнены ВПРАВО (в макете гейта `+84` стоит над
    ` +9`), поэтому общая у всех строк — граница колонки, а не позиция знака.
    """
    rows = [str(t) for t in build_rows(_GRID, selected=0, width=100)]
    marked = [row for row in rows if MARK_SHELL in row or MARK_EXTERNAL in row]
    plain = [row for row in rows if row not in marked]
    assert marked and plain, rows          # обе породы строк в наборе есть

    plus = [re.search(r"\+\d+", row) for row in rows]
    minus = [re.search(r"−\d+", row) for row in rows]
    plus_w = max(len(m.group()) for m in plus)
    minus_w = max(len(m.group()) for m in minus)
    # правый край колонки общий, левый = правый минус ширина самого широкого
    # числа: пометки не сдвигают ни ту границу, ни другую
    assert len({m.end() for m in plus}) == 1, rows
    assert len({m.end() for m in minus}) == 1, rows
    assert all(m.end() - plus_w <= m.start() for m in plus), rows
    assert all(m.end() - minus_w <= m.start() for m in minus), rows
    assert len({len(row.rstrip()) for row in rows}) == 1, rows


def test_marks_column_is_right_aligned():
    """Все пометки заканчиваются на одной вертикали, независимо от длины."""
    rows = [str(t) for t in build_rows(_GRID, selected=0, width=100)]
    ends = set()
    for row in rows:
        if MARK_EXTERNAL in row:
            ends.add(row.index(MARK_EXTERNAL) + len(MARK_EXTERNAL))
        elif MARK_SHELL in row:
            ends.add(row.index(MARK_SHELL) + len(MARK_SHELL))
    assert len(ends) == 1, rows


def test_approx_sign_has_its_own_slot_before_plus():
    """`≈` — про точность чисел; стоит в своём слоте перед `+N`, не вместо."""
    rows = [str(t) for t in build_rows(_GRID, selected=0, width=100)]
    inexact = next(row for row in rows if "uv.lock" in row)
    assert APPROX in inexact
    assert inexact.index(APPROX) < inexact.index("+")
    assert "+3" in inexact and "−1" in inexact
    exact = next(row for row in rows if "app.py" in row)
    assert APPROX not in exact


def test_approx_survives_narrow_width_while_marks_shrink():
    """При нехватке ширины жертвуются пометки, но не `≈` и не счётчики.

    Сначала уходит вторая пометка (приоритет «извне» > «командой»: она
    предупреждает о риске отката, «командой» — только об авторстве), потом
    колонка пометок целиком. Ужимать после этого нечего.
    """
    entries = [
        _entry("src/config.py", add=5, del_=1, by_shell=True, external=True),
        _entry("uv.lock", add=3, del_=1, by_shell=True, exact=False, last_ts=0.5),
    ]
    rows = [str(t) for t in build_rows(entries, selected=0, width=48)]

    both = next(row for row in rows if "config.py" in row)
    assert MARK_EXTERNAL in both and MARK_SHELL not in both
    assert APPROX in next(row for row in rows if "uv.lock" in row)

    tight = [str(t) for t in build_rows(entries, selected=0, width=40)]
    for row in tight:
        assert MARK_EXTERNAL not in row and MARK_SHELL not in row
        assert len(row) <= 40, row
    assert APPROX in next(row for row in tight if "uv.lock" in row)
    assert "+5" in tight[0] and "−1" in tight[0]


def test_truncated_path_does_not_touch_neighbours():
    """Обрезанный путь занимает свою колонку до конца — зазор обязателен.

    Живьём без зазора выходило `…screens/engine.pyкомандой · извне`, а на узкой
    ширине, где колонка пометок пожертвована, — `…screens/engine.py≈+1284`.
    """
    entries = [
        _entry("src/very/deep/nested/package/module/screens/engine.py",
               add=1284, del_=96, by_shell=True, external=True, exact=False),
        _entry("src/config.py", add=5, del_=1, by_shell=True, last_ts=0.5),
    ]
    for width in (80, 60, 45):
        rows = [str(t) for t in build_rows(entries, selected=0, width=width)]
        assert "…" in rows[0], (width, rows)        # путь и правда обрезан
        for row in rows:
            right = [row.index(m) for m in (MARK_SHELL, MARK_EXTERNAL) if m in row]
            right += [row.index(APPROX)] if APPROX in row else []
            if right:
                assert row[min(right) - 1] == " ", (width, row)


def test_agent_tool_edits_carry_no_marks():
    """Правки инструментами агента — норма, её не подписывают."""
    row = str(build_rows([_entry("a.py")], selected=0, width=80)[0])
    assert MARK_SHELL not in row and MARK_EXTERNAL not in row


def test_kind_words_and_cursor_glyph():
    entries = [
        _entry("a.py", kind="modified"),
        _entry("b.py", kind="created", last_ts=0.9),
        _entry("c.py", kind="deleted", last_ts=0.8),
    ]
    rows = [str(t) for t in build_rows(entries, selected=1, width=80)]
    assert "изменён" in rows[0] and "создан" in rows[1] and "удалён" in rows[2]
    assert rows[1].startswith("▸ ")
    assert rows[0].startswith("  ") and not rows[0].startswith("▸")


def test_long_path_is_truncated_from_the_left():
    """Режем слева: имя файла различает строки, общий корень — нет."""
    long_path = "src/very/deep/nested/package/module/screens/diff.py"
    row = str(build_rows([_entry(long_path, add=1, del_=1)], selected=0, width=40)[0])
    assert "diff.py" in row
    assert long_path not in row
    assert "…" in row


# --- глиф принятой базы и хэш коммит-ступени (§13, С9 смоука волны 4) --------


def test_active_row_has_glyph_when_accepted():
    """↺ — сразу после счётчиков, цветом ACCENT, только у ступени `accepted`."""
    entries = [_entry("a.py", add=10, del_=0), _entry("b.py", add=5, del_=1, last_ts=0.5)]
    steps = [RowStep(kind=STEP_ACCEPTED, ts=1.0, ref=None), None]
    rows = build_rows(entries, selected=0, width=100, steps=steps)

    assert ACCEPTED_GLYPH in rows[0].plain
    assert ACCEPTED_GLYPH not in rows[1].plain
    assert rows[0].plain.index("−0") < rows[0].plain.index(ACCEPTED_GLYPH)
    assert span_style(rows[0], ACCEPTED_GLYPH) == ACCENT


def test_glyph_is_absent_without_steps_argument():
    """Как раньше: без `steps` глифа (и хэша) нет ни у кого — старые вызовы не врут."""
    row = str(build_rows([_entry("a.py")], selected=0, width=80)[0])
    assert ACCEPTED_GLYPH not in row


def test_glyph_reserves_room_for_every_row_so_none_overflows():
    """Место под глиф резервируется у ВСЕХ строк набора, если он есть хоть у одной."""
    entries = [_entry("src/config.py", add=5, del_=1),
               _entry("uv.lock", add=3, del_=1, last_ts=0.5)]
    steps = [RowStep(kind=STEP_ACCEPTED, ts=1.0, ref=None), None]
    for width in (40, 48, 60, 80):
        rows = [str(t) for t in build_rows(entries, selected=0, width=width,
                                           steps=steps)]
        for row in rows:
            assert len(row) <= width, (width, row)
    rows = [str(t) for t in build_rows(entries, selected=0, width=60, steps=steps)]
    assert ACCEPTED_GLYPH in rows[0] and ACCEPTED_GLYPH not in rows[1]


def test_active_row_shows_short_hash_for_head_step():
    """Хэш — приглушённо, в слоте глифа, только у ступени `head` (С9)."""
    entries = [_entry("a.py", add=10, del_=0), _entry("b.py", add=5, del_=1, last_ts=0.5)]
    steps = [RowStep(kind=STEP_HEAD, ts=None, ref="e5f2c1a · 14:20"), None]
    rows = build_rows(entries, selected=0, width=100, steps=steps)

    assert "e5f2c1a" in rows[0].plain
    assert "14:20" not in rows[0].plain          # только хэш, время — дело шапки вьюера
    assert ACCEPTED_GLYPH not in rows[0].plain
    assert rows[0].plain.index("−0") < rows[0].plain.index("e5f2c1a")
    assert span_style(rows[0], "e5f2c1a") == DEEP_DIM
    assert "e5f2c1a" not in rows[1].plain


def test_glyph_and_hash_are_mutually_exclusive_on_a_row_but_coexist_in_a_set():
    """Слот на двоих: `↺` у принятого файла, хэш — у файла со ступенью `head`,
    в одном наборе разом, без коллизии (два знака в одном слоте не появляются)."""
    entries = [_entry("a.py"), _entry("b.py", last_ts=0.5)]
    steps = [
        RowStep(kind=STEP_ACCEPTED, ts=1.0, ref=None),
        RowStep(kind=STEP_HEAD, ts=None, ref="abc1234 · 09:00"),
    ]
    rows = build_rows(entries, selected=0, width=100, steps=steps)
    assert ACCEPTED_GLYPH in rows[0].plain and "abc1234" not in rows[0].plain
    assert "abc1234" in rows[1].plain and ACCEPTED_GLYPH not in rows[1].plain


def test_hash_is_sacrificed_before_marks_on_a_narrow_window():
    """Порядок жертв (задача 2 брифа): хэш уходит ПЕРВЫМ, ещё до второй
    пометки и колонки пометок целиком — счётчики не жертвуются никогда.
    60 колонок — контрольная ширина брифа: хэш уже уступил, пометки ещё целы.
    """
    entries = [
        _entry("src/config.py", add=5, del_=1, by_shell=True, external=True),
        _entry("uv.lock", add=3, del_=1, by_shell=True, exact=False, last_ts=0.5),
    ]
    steps = [RowStep(kind=STEP_HEAD, ts=None, ref="e5f2c1a · 14:20"), None]

    full = [str(t) for t in build_rows(entries, selected=0, width=65, steps=steps)]
    config_full = next(r for r in full if "config.py" in r)
    assert "e5f2c1a" in config_full
    assert MARK_SHELL in config_full and MARK_EXTERNAL in config_full

    at_60 = [str(t) for t in build_rows(entries, selected=0, width=60, steps=steps)]
    config_60 = next(r for r in at_60 if "config.py" in r)
    assert "e5f2c1a" not in config_60                          # хэш уступил первым
    assert MARK_SHELL in config_60 and MARK_EXTERNAL in config_60   # пометки ещё целы
    assert "+5" in config_60 and "−1" in config_60                  # счётчики целы

    shrunk = [str(t) for t in build_rows(entries, selected=0, width=48, steps=steps)]
    config_shrunk = next(r for r in shrunk if "config.py" in r)
    assert "e5f2c1a" not in config_shrunk
    assert MARK_EXTERNAL in config_shrunk and MARK_SHELL not in config_shrunk

    bare = [str(t) for t in build_rows(entries, selected=0, width=40, steps=steps)]
    for row in bare:
        assert "e5f2c1a" not in row
        assert MARK_EXTERNAL not in row and MARK_SHELL not in row
        assert len(row) <= 40, row
    assert "+5" in bare[0] and "−1" in bare[0]


# --- колонка состояния индекса снята (§13) ------------------------------------


def test_index_column_is_gone():
    """Р21 отменена для списка: у `build_rows` нет ни параметра, ни следа колонки.

    Роль колонки забрали группа «принято» и глиф ↺ (§13); шапка вьюера слово
    состояния индекса тоже больше не несёт (слой снят Задачей 14).
    """
    assert "git_states" not in inspect.signature(build_rows).parameters
    row = str(build_rows([_entry("a.py")], selected=0, width=80)[0])
    assert "в индексе" not in row and "частично" not in row


# --- группа «принято» (§4.2, §13) ----------------------------------------------


def test_group_row_collapsed_expanded_and_zero_segments():
    """`▸ принято N · закоммичено M` — нулевые сегменты опущены, общего
    слова-зонтика нет (С3 смоука волны 4)."""
    manual_only = [_settled("a.py", reason=SETTLED_MANUAL)]
    assert group_summary(manual_only) == "принято 1"
    assert build_group_row(manual_only, expanded=False).plain == "▸ принято 1"
    assert build_group_row(manual_only, expanded=True).plain == "▾ принято 1"

    mixed = [
        _settled("a.py", reason=SETTLED_MANUAL),
        _settled("b.py", reason=SETTLED_COMMIT),
        _settled("c.py", reason=SETTLED_COMMIT),
    ]
    assert group_summary(mixed) == "принято 1 · закоммичено 2"

    commit_only = [_settled("b.py", reason=SETTLED_COMMIT)]
    assert group_summary(commit_only) == "закоммичено 1"    # «принято 0» опущен


def test_group_row_is_muted_as_a_whole():
    """Строка группы приглушена целиком — включая её собственный глиф."""
    row = build_group_row([_settled("a.py")], expanded=False)
    assert span_style(row, "▸") == DEEP_DIM
    assert span_style(row, "принято") == DEEP_DIM


# --- строки разобранных файлов внутри группы (§4.2, §13) ----------------------


def test_settled_rows_reason_in_kind_column():
    """Причина стоит там, где у активных «изменён/создан» — сразу после
    маркера, словами `принят`/`закоммичен` (С3 смоука волны 4), не сырыми
    кодами сервиса `SETTLED_MANUAL`/`SETTLED_COMMIT` (`вручную`/`коммитом`)."""
    rows_data = [
        _settled("a.py", add=22, del_=3, reason=SETTLED_MANUAL),
        _settled("verylongname/pkg/b.py", add=8, del_=1, reason=SETTLED_COMMIT),
    ]
    rows = [str(t) for t in build_settled_rows(rows_data, selected=-1, width=100)]

    assert rows[0].index("принят") < rows[0].index("a.py")
    assert "+22" in rows[0] and "−3" in rows[0]
    assert "закоммичен" in rows[1] and "b.py" in rows[1]
    assert SETTLED_MANUAL not in rows[0] and SETTLED_COMMIT not in rows[1]


def test_settled_rows_have_no_marks_column():
    """`SettledRow` не несёт `by_shell`/`external` — колонок для них нет; у
    ТОЧНОЙ строки пуст и слот `≈` (сам слот при этом занят — соседний тест)."""
    row = str(build_settled_rows([_settled("a.py")], selected=-1, width=100)[0])
    assert MARK_SHELL not in row and MARK_EXTERNAL not in row and APPROX not in row


def test_settled_rows_carry_approx_from_the_row_itself():
    """Д6 второго смоука: точность у строки группы своя (`SettledRow.exact`),
    и кумулятив, собранный из журнала, метится `≈` тем же знаком и в том же
    слоте, что у активных строк, — слот занят ВСЕГДА, иначе счётчики точной
    и неточной строк разъехались бы по вертикали."""
    rows = [str(t) for t in build_settled_rows(
        [_settled("a.py", add=22, del_=3),
         _settled("b.py", add=8, del_=1, exact=False)],
        selected=-1, width=100,
    )]
    exact, inexact = rows
    assert APPROX not in exact
    assert APPROX in inexact
    slot = inexact.index(APPROX)
    assert slot < inexact.index("+8")             # знак стоит ПЕРЕД счётчиками
    assert exact[slot] == " "                     # у точной строки слот занят пробелом
    assert exact.index("−3") == inexact.index("−1")   # счётчики на одной вертикали


def test_settled_rows_paint_approx_deep_dim_like_active_rows():
    """`≈` — про достоверность чисел, а не про сами прибавленное и убранное:
    тот же тусклый токен, что у активных строк, не цвет счётчиков."""
    [row] = build_settled_rows(
        [_settled("a.py", exact=False)], selected=-1, width=100
    )
    assert span_style(row, APPROX) == DEEP_DIM


def test_settled_rows_path_and_kind_stay_muted():
    """Путь и причина (колонка вида) остаются приглушёнными — существующий
    muted-токен, не новый оттенок (задача 2 брифа: «путь и колонка вида
    остаются приглушёнными»)."""
    [row] = build_settled_rows([_settled("a.py", add=1, del_=1)], selected=-1, width=100)
    assert span_style(row, "a.py") == MUTED
    assert span_style(row, "принят") == MUTED


def test_settled_rows_counters_are_dim_green_and_red_not_grey():
    """С2 смоука волны 4: строка группы серая, но `+`/`−` красятся притушённым
    вариантом смысловых цветов, не сливаются с приглушённым путём/причиной.
    Не новый оттенок — существующие `OK`/`ERR` с рендер-атрибутом Rich `dim`.
    """
    [row] = build_settled_rows([_settled("a.py", add=1, del_=1)], selected=-1, width=100)
    assert span_style(row, "+1") == f"{OK} dim"
    assert span_style(row, "−1") == f"{ERR} dim"
    assert span_style(row, "+1") != MUTED
    assert span_style(row, "−1") != MUTED


def test_settled_row_cursor_glyph():
    """Курсор внутри группы — тот же приём, что у активных: `▸`/два пробела."""
    rows = [str(t) for t in build_settled_rows(
        [_settled("a.py"), _settled("b.py")], selected=1, width=100,
    )]
    assert rows[1].startswith("▸ ")
    assert rows[0].startswith("  ") and not rows[0].startswith("▸")


# --- цвет счётчиков ----------------------------------------------------------


def test_counters_are_painted_by_meaning_not_grey():
    """`+N` — зелёный `OK`, `−M` — красный `ERR` (макет дизайн-гейта).

    Те же семантические цвета носит гаттер тела диффа: один смысл — один цвет,
    и колонка счётчиков читается цветом, а не чтением чисел.
    """
    rows = build_rows(_GRID, selected=0, width=100)
    for entry, row in zip(_GRID, rows):
        assert span_style(row, f"+{entry.add}") == OK, row.plain
        assert span_style(row, f"−{entry.del_}") == ERR, row.plain


def test_zero_counter_keeps_the_colour_of_its_column():
    """`−0` у созданного файла — тот же красный: колонку читают цветом."""
    row = next(row for row in build_rows(_GRID, selected=0, width=100)
               if "revert.py" in row.plain)
    assert span_style(row, "−0") == ERR


def test_approx_sign_stays_dim():
    """`≈` — про достоверность чисел, а не про них самих; цвет счётчиков не его."""
    row = next(row for row in build_rows(_GRID, selected=0, width=100)
               if APPROX in row.plain)
    assert span_style(row, APPROX) == DEEP_DIM


def test_cursor_brightens_the_path_but_not_the_counters():
    """Курсор — про строку, а не про числа: путь ярче, счётчики остаются собой."""
    rows = build_rows(_GRID, selected=0, width=100)
    assert span_style(rows[0], "diff.py") == BRIGHT      # строка под курсором
    assert span_style(rows[1], "revert.py") == INK       # соседняя
    assert span_style(rows[0], "+84") == OK
    assert span_style(rows[0], "−12") == ERR


# --- шапка -------------------------------------------------------------------


def test_header_counters_are_painted_like_the_rows():
    """Шапка красится тем же правилом: её числа сверяют глазом так же."""
    left, _right = build_header(
        SOURCE_SESSION, files=3, add=120, del_=15, mode="git-mode", width=100
    )
    painted = paint_counters(left)
    assert painted.plain == left           # текст тот же, добавлен только цвет
    assert span_style(painted, "+120") == OK
    assert span_style(painted, "−15") == ERR


def test_header_without_numbers_is_not_painted_at_all():
    """Красить нечего — и не красим: «изменений нет» цветом не рассказывают."""
    left, _right = build_header(
        SOURCE_TURN, files=0, add=0, del_=0, mode="cache-mode", width=100
    )
    painted = paint_counters(left)
    assert painted.plain == left
    assert not painted.spans


def test_header_paints_the_counters_not_a_plus_inside_the_path():
    """Красится последняя пара строки: счётчики всегда в хвосте, путь — нет."""
    painted = paint_counters("изменения · сессия · src/c++/a+1 −2.py · +9 −4")
    assert span_style(painted, "+9") == OK
    assert span_style(painted, "−4") == ERR
    assert span_style(painted, "+1") == ""      # пара внутри пути не тронута


def test_header_says_izmeneniya_and_marks_git_mode_only():
    left, right = build_header(
        SOURCE_SESSION, files=3, add=120, del_=15, mode="git-mode", width=100
    )
    assert left == "изменения · сессия · 3 файла · +120 −15"
    assert right == "git"

    _left, right_cache = build_header(
        SOURCE_SESSION, files=3, add=120, del_=15, mode="cache-mode", width=100
    )
    assert right_cache == ""          # слова «cache» в интерфейсе нет


def test_header_sacrifices_git_then_word_then_files_count():
    kwargs = dict(files=3, add=120, del_=15, mode="git-mode")
    full = "изменения · сессия · 3 файла · +120 −15"

    left, right = build_header(SOURCE_SESSION, width=len(full) + 2, **kwargs)
    assert (left, right) == (full, "")                       # первым уходит `git`

    left, _ = build_header(SOURCE_SESSION, width=len(full) - 1, **kwargs)
    assert left == "сессия · 3 файла · +120 −15"              # затем слово экрана

    left, _ = build_header(SOURCE_SESSION, width=20, **kwargs)
    assert left == "сессия · +120 −15"                        # затем «N файлов»


def test_header_of_empty_source_says_no_changes():
    left, _right = build_header(
        SOURCE_TURN, files=0, add=0, del_=0, mode="cache-mode", width=100
    )
    assert left == "изменения · последний ход с правками · от начала хода · изменений нет"


def test_header_says_no_new_changes_while_the_group_lives():
    """Д7 второго смоука: активных файлов нет, но группа жива — «изменений
    нет» было бы неправдой, изменения есть и они разобраны. Голое «изменений
    нет» остаётся только там, где нет ни активных, ни группы."""
    kwargs = dict(files=0, add=0, del_=0, mode="cache-mode", width=100)

    with_group, _ = build_header(SOURCE_SESSION, has_group=True, **kwargs)
    assert with_group == "изменения · сессия · новых изменений нет"

    without_group, _ = build_header(SOURCE_SESSION, has_group=False, **kwargs)
    assert without_group == "изменения · сессия · изменений нет"


def test_header_names_the_turn_base_only_for_turn_source():
    """С5 смоука волны 4: шапка «Хода» называет базу счёта — там она одна на
    весь список; шапка «Сессии» её не называет вовсе, потому что базы там
    разные у каждого файла (своя пометка на строке — глиф `↺` или короткий
    хэш коммит-ступени, С8/С9), и общая фраза была бы неправдой."""
    kwargs = dict(files=3, add=120, del_=15, mode="cache-mode", width=100)

    turn_left, _ = build_header(SOURCE_TURN, **kwargs)
    assert turn_left == "изменения · последний ход с правками · от начала хода · 3 файла · +120 −15"

    session_left, _ = build_header(SOURCE_SESSION, **kwargs)
    assert session_left == "изменения · сессия · 3 файла · +120 −15"
    assert "от начала хода" not in session_left


def test_header_turn_base_phrase_sacrificed_before_files_count_but_after_word():
    """Порядок жертв (задача 3 брифа): «от начала хода» уходит раньше «N
    файлов», но не раньше слова экрана «изменения»; счётчики не жертвуются
    никогда. 70 колонок — контрольная ширина: слово экрана уже уступило,
    а фраза и «N файлов» ещё целы (ширины сдвинулись вместе с подписью
    источника — жертвуется по-прежнему в том же порядке).
    """
    kwargs = dict(files=3, add=120, del_=15, mode="cache-mode")
    full = "изменения · последний ход с правками · от начала хода · 3 файла · +120 −15"

    left, _ = build_header(SOURCE_TURN, width=len(full) + 2, **kwargs)
    assert left == full

    left, _ = build_header(SOURCE_TURN, width=70, **kwargs)
    assert left == "последний ход с правками · от начала хода · 3 файла · +120 −15"

    left, _ = build_header(SOURCE_TURN, width=50, **kwargs)
    assert left == "последний ход с правками · 3 файла · +120 −15"          # фраза ушла

    left, _ = build_header(SOURCE_TURN, width=35, **kwargs)
    assert left == "последний ход с правками · +120 −15"                    # «N файлов» тоже ушёл


def test_files_word_declension():
    for files, word in ((1, "файл"), (2, "файла"), (5, "файлов"), (11, "файлов")):
        left, _ = build_header(
            SOURCE_SESSION, files=files, add=0, del_=0, mode="cache-mode", width=100
        )
        assert f"{files} {word}" in left


# --- футер v3: навигация + контекстные действия (§13) -------------------------
#
# `mode` (git/cache) больше не решает содержимое футера (Р9 revision — «принять»
# доступно и без git): решает СТРОКА ПОД КУРСОРОМ — четыре состояния
# (`ROW_PLAIN`, `ROW_ACCEPTED`, `ROW_GROUP`, `ROW_GROUP_FILE`). Тесты ниже
# покрывают каждое дословно (§13 плана волны 4), плюс перенос на узком окне и
# пустой источник.

_FOOTER_ROWS = (ROW_PLAIN, ROW_ACCEPTED, ROW_GROUP, ROW_GROUP_FILE)


def test_footer_nav_line_never_changes_with_cursor_state():
    """Первая строка — навигация — одна на все состояния курсора."""
    nav = "↑↓ файл · Shift+Tab сессия/ход · Esc назад"
    for row in _FOOTER_ROWS:
        assert footer_text(row=row).splitlines()[0] == nav


def test_footer_nav_advertises_tab_only_when_group_exists():
    """`Tab` переключает области (С4 смоука волны 4) — рекламируется в
    навигации, только когда в списке реально есть строка группы; без неё
    клавиша молчит, и подсказка о ней не появляется ни в одном состоянии
    курсора."""
    nav_without_group = "↑↓ файл · Shift+Tab сессия/ход · Esc назад"
    nav_with_group = (
        "↑↓ файл · Shift+Tab сессия/ход · Tab активные/группа · Esc назад"
    )
    for row in _FOOTER_ROWS:
        assert footer_text(row=row).splitlines()[0] == nav_without_group
        assert footer_text(row=row, has_group=True).splitlines()[0] == nav_with_group


def test_footer_actions_for_a_plain_active_file():
    footer = footer_text(row=ROW_PLAIN)
    assert footer.splitlines()[1] == (
        "Enter дифф · a принять — текущее состояние станет точкой отсчёта · "
        "r откат — вернуть файл к началу сессии"
    )


def test_footer_action_target_follows_the_source():
    """Цель `r` в строке действий — по источнику: «Ход» откатывает к началу
    ХОДА, и футер обязан повторять слова превью, а не сочинять своё."""
    assert "вернуть файл к началу сессии" in footer_text(
        row=ROW_PLAIN, source=SOURCE_SESSION,
    )
    assert "вернуть файл к началу хода" in footer_text(
        row=ROW_PLAIN, source=SOURCE_TURN,
    )


def test_footer_action_target_names_the_commit_step():
    """Д4 второго смоука: действующая ступень строки — коммит (`STEP_HEAD`), и
    цель `r` в футере — закоммиченное состояние, а не начало сессии. Строкой
    `ROW_ACCEPTED` этот случай не покрыть: та про ступень «принято», а здесь
    тай-брейк Р28 отдал счёт коммиту."""
    footer = footer_text(row=ROW_PLAIN, step=STEP_HEAD)
    assert footer.splitlines()[1] == (
        "Enter дифф · a принять — текущее состояние станет точкой отсчёта · "
        "r откат — вернуть файл к закоммиченному"
    )


def test_footer_hides_revert_for_a_file_without_a_reliable_base():
    """Д10 второго смоука: у файла нет достоверной базы отката
    (`FileEntry.revertable`) — `r` неприменим, и канон экрана его не
    рекламирует; остальные сегменты строки остаются на месте.

    Массовый хвост от `can_revert` больше не зависит вовсе (Е4 — массовые
    клавиши стали свойством ПРЕДСТАВЛЕНИЯ, у него свои параметры `mass_*`).
    """
    assert footer_text(row=ROW_PLAIN, can_revert=False).splitlines()[1] == (
        "Enter дифф · a принять — текущее состояние станет точкой отсчёта"
    )
    assert footer_text(
        row=ROW_PLAIN, can_reset=True, can_revert=False
    ).splitlines()[1] == (
        "Enter дифф · a принять — текущее состояние станет точкой отсчёта · "
        "u сброс — отменить принятие"
    )
    assert footer_text(row=ROW_ACCEPTED, can_revert=False).splitlines()[1] == (
        "Enter дифф · a принять — зачесть новые правки · u сброс — отменить "
        "принятие, считать с начала сессии"
    )


def test_footer_actions_for_an_accepted_active_file():
    """У файла с `↺` `a`/`r` объясняются иначе, и появляется `u`."""
    footer = footer_text(row=ROW_ACCEPTED)
    assert footer.splitlines()[1] == (
        "Enter дифф · a принять — зачесть новые правки · u сброс — отменить "
        "принятие, считать с начала сессии · r откат — вернуть к принятому"
    )


def test_footer_actions_for_a_plain_file_with_an_accepted_base_names_u():
    """Финальный фикс волны 4: строка не `ROW_ACCEPTED` (тай-брейк Р28 отдал
    ступень HEAD, либо источник — «Ход», Р27), но принятая база у файла есть
    — `can_reset` обязан включить `u` в действия: канон экрана не молчит про
    рабочую клавишу."""
    footer = footer_text(row=ROW_PLAIN, can_reset=True)
    assert footer.splitlines()[1] == (
        "Enter дифф · a принять — текущее состояние станет точкой отсчёта · "
        "u сброс — отменить принятие · r откат — вернуть файл к началу сессии"
    )


def test_footer_actions_for_group_show_no_mass_tail_when_nothing_is_live():
    header = footer_text(row=ROW_GROUP)
    assert header.splitlines()[1] == "Enter развернуть"
    assert "Ctrl+U" not in header

    in_group = footer_text(row=ROW_GROUP_FILE, can_reset=True)
    assert in_group.splitlines()[1] == (
        "Enter — что агент сделал с файлом за сессию · u сброс — отменить "
        "принятие, файл вернётся в активные"
    )
    assert "Ctrl+U" not in in_group


def test_footer_of_empty_source_offers_only_working_keys():
    """Строк нет — открывать, откатывать и листать нечего; не рекламируем.

    Курсор в этом случае ни на что не указывает — `row` роли не играет,
    пустота решает раньше (как и массовые `mass_*`).
    """
    assert footer_text(empty=True) == "Shift+Tab сессия/ход · Esc"
    assert footer_text(
        row=ROW_ACCEPTED, empty=True, mass_accept=True, mass_revert=True,
        mass_reset=True,
    ) == "Shift+Tab сессия/ход · Esc"


# --- `Enter` по строке под курсором, `u` только живой (Ж3/Ж4 батча 4) ---------
#
# Правило экрана: что меняется от КУРСОРА — в строке действий, что от
# СОСТОЯНИЯ экрана — в строке навигации. `Enter` делает разное на разных
# строках (дифф / развернуть / кумулятив сессии), поэтому ушёл вниз; `Tab`
# зависит от наличия группы, а не от курсора, и остался наверху.


def test_nav_line_no_longer_names_enter():
    """`Enter` больше не живёт в навигации: его смысл решает строка."""
    nav = footer_text(row=ROW_PLAIN).split("\n")[0]
    assert "Enter" not in nav
    assert nav.startswith("↑↓ файл · Shift+Tab сессия/ход")


def test_enter_is_named_by_the_row_under_the_cursor():
    """Четыре строки — четыре текста `Enter`, и заголовок группы называет
    клавишу по ТЕКУЩЕМУ состоянию разворота."""
    plain = footer_text(row=ROW_PLAIN).split("\n")[1]
    assert plain.startswith("Enter дифф · a принять")

    collapsed = footer_text(row=ROW_GROUP, group_expanded=False).split("\n")[1]
    assert collapsed.startswith("Enter развернуть")
    expanded = footer_text(row=ROW_GROUP, group_expanded=True).split("\n")[1]
    assert expanded.startswith("Enter свернуть")

    in_group = footer_text(row=ROW_GROUP_FILE, can_reset=True).split("\n")[1]
    assert in_group.startswith("Enter — что агент сделал с файлом за сессию")


def test_group_header_does_not_advertise_a_dead_reset():
    """Ж4: на самом заголовке группы снимать нечего — `_reset_target` отдаёт
    по нему `None`, и футер обязан молчать."""
    text = footer_text(row=ROW_GROUP, group_expanded=True)
    assert "u сброс" not in text          # `u` на заголовке группы мёртв


def test_settled_file_advertises_reset_only_when_it_is_alive():
    """Ж4: у файла внутри группы `u` жив только при причине «вручную» —
    гейт тот же расчёт, что исполняет клавишу."""
    manual = footer_text(row=ROW_GROUP_FILE, can_reset=True)
    assert "u сброс — отменить принятие, файл вернётся в активные" in manual
    by_commit = footer_text(row=ROW_GROUP_FILE, can_reset=False)
    assert "u сброс" not in by_commit


# --- массовые клавиши: свойство ПРЕДСТАВЛЕНИЯ, не строки (Е4) -----------------
#
# Раньше `Ctrl+A/Ctrl+R`/`Ctrl+U` собирались в тексте строки под курсором
# (`_FooterActions.mass`, `_FOOTER_GROUP_MASS_UNDO`) — и `Ctrl+U` был виден
# ровно в одном месте: на строке группы, и только когда в ней есть файл,
# принятый ВРУЧНУЮ. Курсор на активном файле с принятой базой (сцена смоука 3)
# о рабочей клавише не узнавал ничего. Хвост — общий на все строки, и решает
# его `DiffScreen` по представлению целиком, а не по строке под курсором.


def test_mass_tail_names_every_live_key_with_a_verb():
    assert mass_tail(accept=True, revert=True, reset=True) == (
        "Ctrl+A принять все · Ctrl+R откатить все · Ctrl+U сбросить все принятия")
    assert mass_tail(accept=True, revert=False, reset=False) == "Ctrl+A принять все"
    assert mass_tail(accept=False, revert=False, reset=False) == ""


def test_footer_mass_tail_is_the_same_on_every_row():
    kw = dict(mass_accept=True, mass_revert=True, mass_reset=True)
    for row in _FOOTER_ROWS:
        text = footer_text(row=row, **kw)
        assert text.endswith(
            "Ctrl+A принять все · Ctrl+R откатить все · Ctrl+U сбросить все принятия")
        assert "то же для всех" not in text and "остальные" not in text


def test_footer_names_ctrl_u_on_a_plain_row_when_any_accepted_base_exists():
    """Смоук 3, шаг 7: файл под курсором сам без принятой базы, но она есть
    ГДЕ-ТО в представлении — `Ctrl+U` рекламируется и здесь (Е4)."""
    text = footer_text(
        row=ROW_PLAIN, can_revert=False, mass_accept=True, mass_revert=True,
        mass_reset=True,
    )
    assert "Ctrl+U сбросить все принятия" in text and " r откат" not in text


@pytest.mark.asyncio
async def test_live_screen_advertises_ctrl_u_from_an_active_accepted_row(tmp_path):
    """Сцена смоука 3, шаг 7: принятый файл с новой правкой стоит в активных
    (`ROW_ACCEPTED`), второй принятый файл — в группе. Раньше `Ctrl+U`
    рекламировался только со строки группы (Р35) — курсор на активном о
    клавише не узнавал ничего, хотя она рабочая."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("active.py", "a\n")
    ws.write("active.py", "a\nb\n")
    ws.journal.append(_rec("active.py", ts=1.0))
    ws.accept("active.py", "a\nb\n")
    ws.write("active.py", "a\nb\nc\n")       # новая правка поверх принятого — снова активен
    ws.journal.append(_rec("active.py", ts=2.0, call_id="c2"))
    ws.write("settled.py", "v1\n")
    ws.journal.append(_rec("settled.py", ts=0.5))
    ws.accept("settled.py", "v1\n")          # чисто относительно принятого — уходит в группу

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert [e.path for e in scr._entries] == ["active.py"]
        assert scr._selected_idx == 0                    # курсор на активном

        footer = _plain(scr.query_one("#footer"))
        assert "Ctrl+U сбросить все принятия" in footer


@pytest.mark.asyncio
async def test_footer_wraps_not_clips_at_narrow_widths(tmp_path):
    """Узкая ширина переносит футер, а не режет — контракт §13 (ревью задачи 11).

    Число строк — СЛЕДСТВИЕ утверждённых дословных текстов, не отдельная
    гарантия: «до трёх строк» было оценкой, написанной ДО этих текстов, а
    измеренная реальность — до шести на 60 колонках (резать утверждённые
    формулировки ради числа было бы неправильно, поэтому спека сужена до
    «переносится, не обрезается»). Главная проверка ниже — контракт: ничего
    не обрезано и ни один смысловой сегмент строки действий не потерян, в
    частности `u сброс` в состоянии `↺` и хвост массовых клавиш (Е4) — теперь
    ОДИН на все три строки, а не только на группе. Числа — регрессионный
    якорь на измеренное поведение, явно по (ширина, состояние курсора): 60
    колонок — шесть строк на обычном файле и на файле с `↺`, четыре на
    заголовке группы (сценарий несёт группу, и строка навигации везде несёт
    сегмент `Tab активные/группа` — задача 2 брифа, оба конца переключателя
    названы явно по ревью задачи 2); на 100 колонках — три, четыре и две.
    Заголовок группы стал коротким после Ж3 батча 4: `Enter` там называется
    одним словом («развернуть»), а `u` ушёл вовсе — на самом заголовке он
    мёртв.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    # Снимки начала сессии — как у живого рекордера: без них у файлов не было
    # бы достоверной базы, и `r` из футера ушёл бы вовсе (Д10).
    ws.base("plain.py", "a\n")
    ws.base("accepted.py", "a\n")
    ws.write("plain.py", "a\nb\n")
    ws.journal.append(_rec("plain.py", ts=3.0))
    ws.write("accepted.py", "a\nb\n")
    ws.journal.append(_rec("accepted.py", ts=1.0))
    ws.accept("accepted.py", "a\nb\n")
    ws.write("accepted.py", "a\nb\nc\n")           # принят и снова изменён — строка с ↺
    ws.journal.append(_rec("accepted.py", ts=2.0, call_id="c2"))
    ws.write("group.py", "v1\n")
    ws.journal.append(_rec("group.py", ts=0.5))
    ws.accept("group.py", "v1\n")                  # чисто относительно принятого — в группе

    app = TuiApp(threads_dir=str(ws.threads_dir), system="cli", config=ws.config)
    async with app.run_test(size=(100, 24)) as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert len(scr._entries) == 2                  # plain.py + accepted.py активны
        plain_idx = next(i for i, e in enumerate(scr._entries) if e.path == "plain.py")
        accepted_idx = next(
            i for i, e in enumerate(scr._entries) if e.path == "accepted.py"
        )
        group_idx = len(scr._entries)                  # строка группы — сразу после активных

        # Хвост массовых клавиш — свойство представления (Е4): в этом сценарии
        # он одинаков на ВСЕХ трёх строках (принят есть `accepted.py`, база
        # отката есть у обоих активных) — «Ctrl+U» видно и вне группы.
        #
        # (индекс строки, {ширина: измеренная высота футера}, обязательные сегменты)
        cases = [
            (plain_idx, {60: 6, 100: 3},
             ("Shift+Tab сессия/ход", "Tab активные/группа", "Enter дифф",
              "a принять", "r откат", "Ctrl+A принять все", "Ctrl+R откатить все",
              "Ctrl+U сбросить все принятия")),
            (accepted_idx, {60: 6, 100: 4},
             ("Shift+Tab сессия/ход", "Tab активные/группа", "Enter дифф",
              "a принять", "u сброс", "r откат", "Ctrl+A принять все",
              "Ctrl+R откатить все", "Ctrl+U сбросить все принятия")),
            (group_idx, {60: 4, 100: 2},
             ("Shift+Tab сессия/ход", "Tab активные/группа", "Enter развернуть",
              "Ctrl+U сбросить все принятия")),
        ]
        for idx, widths, chunks in cases:
            scr._move(idx)
            await pilot.pause()
            for width, expected_lines in widths.items():
                await pilot.resize_terminal(width, 24)
                await pilot.pause()
                await pilot.pause()
                footer = scr.query_one("#footer")
                needed = footer.get_content_height(
                    footer.container_size, footer.container_size, footer.size.width
                )
                assert needed <= footer.size.height, (idx, width)     # без обрезки
                rendered = _plain(footer)
                for chunk in chunks:
                    assert chunk in rendered, (idx, width, rendered)  # сегмент цел
                assert footer.size.height == expected_lines, (
                    idx, width, footer.size.height, rendered
                )


# --- плашки принятия и сброса (§13) --------------------------------------------
#
# Пять случаев §13 (успех без ручных правок / с ними / без git-хвоста / отказ
# git add с причиной / условная формулировка сброса по `ResetOutcome.to_head`)
# плюс отказ капа (Задача 11 брифа) — тем же приёмом, что `revert_ack`:
# чистые функции над сконструированным Outcome.


def test_accept_ack_plain_success_in_a_repo():
    outcome = AcceptOutcome(
        accepted=["src/app.py"], staged=1, stage_error=None, skipped=[],
        with_external=[],
    )
    assert accept_ack(outcome, is_git=True) == (
        "принято: src/app.py — новая точка отсчёта; файл добавлен в индекс git"
    )
    assert accept_tone(outcome, is_git=True) == "ok"


def test_accept_ack_mentions_manual_edits_only_when_marked_external():
    outcome = AcceptOutcome(
        accepted=["src/app.py"], staged=1, stage_error=None, skipped=[],
        with_external=["src/app.py"],
    )
    assert accept_ack(outcome, is_git=True) == (
        "принято: src/app.py — новая точка отсчёта; в индекс git вошли и "
        "ваши правки, сделанные вне агента"
    )


def test_accept_ack_has_no_git_tail_outside_a_repository():
    outcome = AcceptOutcome(
        accepted=["src/app.py"], staged=0, stage_error=None, skipped=[],
        with_external=[],
    )
    ack = accept_ack(outcome, is_git=False)
    assert ack == "принято: src/app.py — новая точка отсчёта"
    assert "индекс" not in ack


def test_accept_ack_reports_git_add_failure_with_reason_and_action():
    """Плашка называет СОБЫТИЕ (git отказал), а не результат «в индекс не попал».

    Отказ батча бывает частичным: на ignored-путь `git add` отвечает кодом 1,
    остальные пути при этом стейджит, — и утверждение о результате оказалось
    бы ложью (§7, финальное ревью ветки, I3). Причина и действие остаются.
    """
    outcome = AcceptOutcome(
        accepted=["src/app.py"], staged=0,
        stage_error="fatal: Unable to create '.git/index.lock': File exists.",
        skipped=[], with_external=[],
    )
    assert accept_ack(outcome, is_git=True) == (
        "принято: src/app.py, но git add отказал (fatal: Unable to "
        "create '.git/index.lock': File exists.) — повторите a позже или "
        "выполните git add вручную"
    )
    assert "не попал" not in accept_ack(outcome, is_git=True)
    assert accept_tone(outcome, is_git=True) == "warn"


def test_accept_ack_names_the_count_for_a_mass_accept():
    """`Ctrl+A` — плашка с числом (§4.3), как у отката и git add."""
    outcome = AcceptOutcome(
        accepted=["a.py", "b.py", "c.py"], staged=3, stage_error=None,
        skipped=[], with_external=[],
    )
    assert accept_ack(outcome, is_git=True).startswith(
        "принято: 3 файла — новая точка отсчёта"
    )


def test_accept_ack_reports_the_cap_refusal_with_a_reason():
    outcome = AcceptOutcome(
        accepted=[], staged=0, stage_error=None,
        skipped=[("huge.bin", ACCEPT_TOO_LARGE)], with_external=[],
    )
    ack = accept_ack(outcome, is_git=True)
    assert "huge.bin" in ack and "файл больше лимита" in ack
    assert accept_tone(outcome, is_git=True) == "warn"


def test_accept_ack_reports_the_read_failure_reason():
    outcome = AcceptOutcome(
        accepted=[], staged=0, stage_error=None,
        skipped=[("a.py", ACCEPT_READ_FAILED)], with_external=[],
    )
    assert "прочитать не удалось" in accept_ack(outcome, is_git=True)


def test_accept_ack_combines_success_and_skip_in_one_message():
    outcome = AcceptOutcome(
        accepted=["a.py"], staged=1, stage_error=None,
        skipped=[("huge.bin", ACCEPT_TOO_LARGE)], with_external=[],
    )
    ack = accept_ack(outcome, is_git=True)
    assert ack.startswith("принято: a.py — новая точка отсчёта")
    assert "huge.bin" in ack and "файл больше лимита" in ack
    assert accept_tone(outcome, is_git=True) == "warn"     # неполный успех — не ok


def test_accept_ack_in_turn_source_notes_where_the_effect_is_visible():
    """Задача 4 доп. (С6): в «Ходе» принятие двигает базу «Сессии», но сам
    ход её не видит — файл в его списке остаётся на месте. Плашка обязана
    назвать источник, где сдвиг виден; в «Сессии» текст не меняется."""
    outcome = AcceptOutcome(
        accepted=["a.py"], staged=0, stage_error=None, skipped=[], with_external=[],
    )
    session_ack = accept_ack(outcome, is_git=False, source=SOURCE_SESSION)
    turn_ack = accept_ack(outcome, is_git=False, source=SOURCE_TURN)

    assert session_ack == accept_ack(outcome, is_git=False)   # источник по умолчанию — прежний
    assert turn_ack != session_ack
    assert turn_ack.startswith(session_ack)
    assert "Сесси" in turn_ack


def test_reset_ack_from_session_start_when_no_head_step():
    outcome = ResetOutcome(reset=["src/app.py"], to_head=[], skipped=[])
    assert reset_ack(outcome) == (
        "принятие отменено: src/app.py — счёт для файла снова с начала сессии"
    )
    assert reset_tone(outcome) == "ok"


def test_reset_ack_from_head_when_head_step_lives():
    """Условная формулировка (Р29, §13): ступень HEAD жива — не «с начала сессии»."""
    outcome = ResetOutcome(reset=["src/app.py"], to_head=["src/app.py"], skipped=[])
    assert reset_ack(outcome) == (
        "принятие отменено: src/app.py — счёт от закоммиченного состояния"
    )


def test_reset_ack_names_the_count_for_a_uniform_mass_reset():
    outcome = ResetOutcome(reset=["a.py", "b.py"], to_head=[], skipped=[])
    assert reset_ack(outcome) == (
        "принятие отменено: 2 файла — счёт для файла снова с начала сессии"
    )


def test_reset_ack_splits_mixed_targets_by_head_step():
    outcome = ResetOutcome(reset=["a.py", "b.py"], to_head=["b.py"], skipped=[])
    ack = reset_ack(outcome)
    assert "принятие отменено: a.py — счёт для файла снова с начала сессии" in ack
    assert "принятие отменено: b.py — счёт от закоммиченного состояния" in ack


def test_reset_ack_when_nothing_had_an_accepted_base():
    outcome = ResetOutcome(reset=[], to_head=[], skipped=[("a.py", RESET_NOT_ACCEPTED)])
    ack = reset_ack(outcome)
    assert "принятие не отменено" in ack and "a.py" in ack
    assert reset_tone(outcome) == "warn"


def test_reset_ack_in_turn_source_notes_where_the_effect_is_visible():
    """Финальный фикс волны 4: из «Хода» сброс тоже двигает базу «Сессии»
    (принятая база — глобальный маркер), но сам «Ход» её не видит (Р27) —
    файл в его списке остаётся на месте. Плашка обязана назвать источник, где
    сдвиг реально виден; в «Сессии» текст не меняется (тот же приём, что у
    `accept_ack`/`ACCEPT_TURN_NOTE`)."""
    outcome = ResetOutcome(reset=["a.py"], to_head=[], skipped=[])
    session_ack = reset_ack(outcome, source=SOURCE_SESSION)
    turn_ack = reset_ack(outcome, source=SOURCE_TURN)

    assert session_ack == reset_ack(outcome)          # источник по умолчанию — прежний
    assert turn_ack != session_ack
    assert turn_ack.startswith(session_ack)
    assert "Сесси" in turn_ack


# --- вьюер группы: футер не рекламирует ни `a`, ни `r` (ревью задачи 10) ------


def test_viewer_footer_drops_accept_and_revert_when_opened_from_the_group():
    """§4.3: у вьюера из группы дельта от ступени пуста — обе клавиши мертвы."""
    footer = viewer_footer(full=False, can_full=False, can_add=False, can_revert=False)
    assert "a принять" not in footer
    assert "r откат" not in footer
    assert "↑↓ PgUp PgDn · Esc" in footer


def test_viewer_footer_keeps_revert_by_default():
    """Старые вызовы без `can_revert` не меняют поведение (обратная совместимость)."""
    footer = viewer_footer(full=False, can_full=True)
    assert "r откат" in footer


def test_viewer_footer_names_the_revert_with_the_same_verb_as_the_list():
    """Д10 второго смоука: глагол один на список и вьюер — `откат`. Две формы
    у одной клавиши читались как два разных действия."""
    footer = viewer_footer(full=False, can_full=True)
    assert "r откат · " in footer
    assert "откатить" not in footer


def test_hotkeys_are_case_and_layout_insensitive():
    for raw in ("a", "A", "ф", "Ф"):
        assert norm_key(raw) == "a"
    for raw in ("r", "R", "к", "К"):
        assert norm_key(raw) == "r"
    for raw in ("u", "U", "г", "Г"):
        assert norm_key(raw) == "u"
    for raw in ("f", "F", "а", "А"):
        assert norm_key(raw) == "f"
    for raw in ("n", "N", "т", "Т"):
        assert norm_key(raw) == "n"
    for raw in ("p", "P", "з", "З"):
        assert norm_key(raw) == "p"
    for raw in ("ctrl+a", "ctrl+A", "ctrl+ф", "ctrl+Ф"):
        assert norm_key(raw) == "ctrl+a"
    for raw in ("ctrl+r", "ctrl+R", "ctrl+к", "ctrl+К"):
        assert norm_key(raw) == "ctrl+r"
    for raw in ("ctrl+u", "ctrl+U", "ctrl+г", "ctrl+Г"):
        assert norm_key(raw) == "ctrl+u"


# --- стражи ------------------------------------------------------------------


def test_screen_module_does_not_import_diff_tui():
    """Экран живёт на `agent_edits` — умирающий пакет ему не нужен ни строкой."""
    import gc_tui.screens.diff as mod

    assert "diff_tui" not in inspect.getsource(mod)


def test_no_approve_wording_anywhere():
    """Р1: слова похороненной pending/approve-модели в экране не живут.

    Запрет «приня*» снят волной 4 (§7 спеки): «принять»/«принято» —
    легитимные слова нового смысла (Р26 — принятие как действие над точкой
    отсчёта, не как состояние правки). «pending»/«одобрить»/«ожидает
    принятия» остаются под запретом.
    """
    import gc_tui.screens.diff as mod

    source = inspect.getsource(mod).lower()
    for dead in ("approve", "pending", "одобрить", "ожидает принятия"):
        assert dead not in source


# --- живой экран -------------------------------------------------------------


@pytest.mark.asyncio
async def test_diff_command_opens_screen(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen
    from gc_tui.screens.main import TraceScreen
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen

    ws = _Workspace(tmp_path)
    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        assert isinstance(pilot.app.screen, TraceScreen)
        await pilot.app.screen._handle_command("diff")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffScreen)


@pytest.mark.asyncio
async def test_header_shows_source_and_cumulative_counters(tmp_path):
    """Числа шапки — итог по файлам источника, а не сумма шагов журнала."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\nb\nc\n")
    ws.write("a.py", "a\nB\nc\nd\n")            # +2 −1 от базы
    ws.write("new.py", "one\ntwo\n")            # создан в наборе: +2
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.journal.append(_rec("a.py", ts=2.0, turn="t2"))
    ws.journal.append(_rec("new.py", ts=3.0, turn="t2", op="create", add=2, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        header = _plain(scr.query_one("#header-left"))
        assert header == "изменения · сессия · 2 файла · +4 −1"
        # цвет доезжает до живой шапки, а не остаётся в чистой функции
        content = scr.query_one("#header-left").content
        assert span_style(content, "+4") == OK
        assert span_style(content, "−1") == ERR


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(40, 24), (50, 24), (80, 24), (120, 30)])
async def test_body_frame_holds_full_height_and_keeps_rows_whole(tmp_path, size):
    """Р16: тело списка в рамке на всю доступную высоту, строки целы.

    `outline` в Textual места не резервирует: движок затирает им первую и
    последнюю строки виджета, а дети-строки закрашивают середину рамки собой —
    живьём выходила разорванная рамка, съедавшая первую строку списка.
    Настоящая рамка место резервирует; высоту она занимает ВСЮ между шапкой и
    футером, чтобы не прыгать по содержимому при смене источника.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(3):
        ws.write(f"f{idx}.py", "x\n")
        ws.journal.append(_rec(f"f{idx}.py", ts=float(idx), op="create",
                               add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test(size=size) as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()

        lines = _screen_lines(pilot.app)
        top = next(i for i, line in enumerate(lines) if line.startswith("╭"))
        bottom = next(i for i, line in enumerate(lines) if line.startswith("╰"))
        # Рамка стоит между шапкой (2 строки) и футером, занимая весь остаток.
        assert top == 2, lines
        footer_h = scr.query_one("#footer").outer_size.height
        # Ещё минус строка — зарезервированный зазор под плашку (Д8): он есть
        # всегда, и рамка кончается на строку выше полосы подсказок.
        assert bottom == len(lines) - footer_h - 2, lines
        # Строки списка целы и лежат ВНУТРИ рамки.
        for row in (scr._rows[0], scr._rows[-1]):
            text = _plain(row).strip()
            inside = [line for line in lines[top + 1: bottom] if text in line]
            assert inside, (text, lines)
            assert inside[0].startswith("│") and inside[0].endswith("│"), inside


@pytest.mark.asyncio
async def test_shift_tab_switches_source_in_place_without_pushing_screen(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\n")
    ws.write("a.py", "a\nb\n")
    ws.write("b.py", "one\n")
    ws.journal.append(_rec("a.py", turn="t1", ts=1.0))
    ws.journal.append(_rec("b.py", turn="t2", ts=2.0, op="create", add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        depth = len(pilot.app.screen_stack)
        assert [e.path for e in scr._entries] == ["b.py", "a.py"]

        await pilot.press("shift+tab")
        await pilot.pause()
        assert pilot.app.screen is scr                    # in-place, без push
        assert len(pilot.app.screen_stack) == depth
        assert scr.source == SOURCE_TURN
        assert [e.path for e in scr._entries] == ["b.py"]
        assert "последний ход с правками" in _plain(scr.query_one("#header-left"))

        await pilot.press("shift+tab")
        await pilot.pause()
        assert scr.source == SOURCE_SESSION


@pytest.mark.asyncio
async def test_tab_switches_between_active_part_and_group_remembering_position(tmp_path):
    """`Tab` — переключатель областей (С4 смоука волны 4): активная часть ↔
    строка группы, с запоминанием позиции в активной части."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(3):
        ws.write(f"f{idx}.py", "x\n")
        ws.journal.append(_rec(f"f{idx}.py", ts=float(idx), op="create", add=1, del_=0))
    ws.write("done.py", "v1\n")
    ws.journal.append(_rec("done.py", ts=9.0))
    ws.accept("done.py", "v1\n")                # чисто относительно принятого → группа

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert len(scr._entries) == 3
        assert scr._settled                            # группа реально есть
        group_idx = len(scr._entries)
        scr._move(1)                                    # курсор на вторую активную строку
        await pilot.pause()

        await pilot.press("tab")
        await pilot.pause()
        assert scr._selected_idx == group_idx           # ушли на строку группы

        await pilot.press("tab")
        await pilot.pause()
        assert scr._selected_idx == 1                   # вернулись туда, откуда ушли

        # Второй круг с другой позиции — запоминается заново, не залипает на 1.
        scr._move(2)
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert scr._selected_idx == group_idx
        await pilot.press("tab")
        await pilot.pause()
        assert scr._selected_idx == 2


@pytest.mark.asyncio
async def test_tab_is_silent_without_a_group(tmp_path):
    """Группы нет — `Tab` не двигает курсор (С4): клавише нечем управлять."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(2):
        ws.write(f"f{idx}.py", "x\n")
        ws.journal.append(_rec(f"f{idx}.py", ts=float(idx), op="create", add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert not scr._settled                        # группы нет
        scr._move(1)
        await pilot.pause()

        await pilot.press("tab")
        await pilot.pause()
        assert scr._selected_idx == 1                  # курсор не сдвинулся
        assert "Tab активные/группа" not in _plain(scr.query_one("#footer"))


def _group_workspace(tmp_path) -> _Workspace:
    """Один активный файл и группа из двух разобранных — сетап тестов `Tab` (Д3)."""
    ws = _Workspace(tmp_path)
    ws.write("act.py", "x\n")
    ws.journal.append(_rec("act.py", ts=5.0, op="create", add=1, del_=0))
    for idx, name in enumerate(("one.py", "two.py")):
        ws.write(name, "v1\n")
        ws.journal.append(_rec(name, ts=float(idx), call_id=f"c{idx}"))
        ws.accept(name, "v1\n")           # чисто относительно принятого → в группу
    return ws


@pytest.mark.asyncio
async def test_tab_returns_to_the_remembered_row_inside_the_group(tmp_path):
    """Д3 второго смоука: `Tab` помнит позицию в ОБЕИХ областях. Уход из
    группы запоминает строку внутри неё, возврат приземляется на неё, а не на
    заголовок группы."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _group_workspace(tmp_path)
    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert len(scr._entries) == 1 and len(scr._settled) == 2
        group_idx = 1

        scr._move(group_idx)
        await pilot.press("enter")                  # раскрыть группу
        await pilot.pause()
        assert scr._group_expanded is True
        scr._move(group_idx + 2)                    # вторая строка внутри группы
        await pilot.pause()

        await pilot.press("tab")
        await pilot.pause()
        assert scr._selected_idx == 0                # ушли в активную часть

        await pilot.press("tab")
        await pilot.pause()
        assert scr._selected_idx == group_idx + 2    # вернулись на ту же строку


@pytest.mark.asyncio
async def test_tab_returns_to_the_group_row_when_the_group_is_collapsed(tmp_path):
    """Запомненной строки на экране больше нет — возвращаться некуда, кроме
    заголовка группы (Д3)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _group_workspace(tmp_path)
    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        group_idx = 1

        scr._move(group_idx)
        await pilot.press("enter")
        await pilot.pause()
        scr._move(group_idx + 1)
        await pilot.pause()
        await pilot.press("tab")                    # запомнили строку в группе
        await pilot.pause()
        assert scr._selected_idx == 0

        scr._toggle_group()                          # свернули, курсор не трогали
        await pilot.pause()
        assert scr._group_expanded is False

        await pilot.press("tab")
        await pilot.pause()
        assert scr._selected_idx == group_idx


@pytest.mark.asyncio
async def test_tab_returns_to_the_group_row_when_the_remembered_file_is_gone(tmp_path):
    """Память держит ПУТЬ файла, а не индекс: между двумя `Tab` список
    перечитывается, и запомненная строка могла уйти из группы вовсе — тогда
    возврат идёт на заголовок (Д3)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _group_workspace(tmp_path)
    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()

        scr._move(1)
        await pilot.press("enter")
        await pilot.pause()
        scr._move(3)                                 # вторая строка группы — two.py
        await pilot.pause()
        remembered = scr._settled[1].path
        await pilot.press("tab")
        await pilot.pause()

        scr._edits.reset_accept([remembered])        # файл вернулся в активные
        scr._reload()
        await pilot.pause()
        assert remembered not in [row.path for row in scr._settled]

        await pilot.press("tab")
        await pilot.pause()
        assert scr._selected_idx == len(scr._entries)   # заголовок группы


@pytest.mark.asyncio
async def test_tab_is_silent_when_the_active_part_is_empty(tmp_path):
    """Активных файлов нет — переключать области нечем, и `Tab` молчит; в
    футере его тоже нет (Д3: немая клавиша не рекламируется)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "v1\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr._entries == [] and scr._settled     # только группа
        assert "Tab активные/группа" not in _plain(scr.query_one("#footer"))

        await pilot.press("enter")                     # раскрыть
        await pilot.pause()
        scr._move(1)                                    # строка внутри группы
        await pilot.pause()

        await pilot.press("tab")
        await pilot.pause()
        assert scr._selected_idx == 1                   # курсор не сдвинулся


@pytest.mark.asyncio
async def test_tab_without_a_group_still_stops_and_prevents_default(tmp_path):
    """Ловушка Textual (`Tab` — перемещение фокуса, Global Constraints): даже
    когда клавише нечем управлять (группы нет), `on_key` обязан снять и
    всплытие, и штатное действие клавиши САМ — иначе фокус уехал бы в
    контейнер. `test_tab_is_silent_without_a_group` этого не ловит: на этом
    DOM необработанный `Tab` тоже не двигает `_selected_idx` (он не завязан
    на фокус Textual) — тест зелёный при любой реализации ветки `tab`.

    Здесь проверяется не поведение, а ФАКТ: `event.stop()`/
    `event.prevent_default()` — обычные методы события, их можно застабить
    поверх `pilot.press("tab")` и убедиться, что код их правда вызвал.
    """
    pytest.importorskip("textual")
    from unittest.mock import patch

    from textual import events
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(2):
        ws.write(f"f{idx}.py", "x\n")
        ws.journal.append(_rec(f"f{idx}.py", ts=float(idx), op="create", add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert not scr._settled                        # группы нет

        with patch.object(events.Key, "stop") as stop_spy, \
                patch.object(events.Key, "prevent_default") as prevent_spy:
            await pilot.press("tab")
            await pilot.pause()

        assert stop_spy.called
        assert prevent_spy.called


@pytest.mark.asyncio
async def test_selected_row_has_caret_and_selected_class(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(3):
        ws.write(f"f{idx}.py", "x\n")
        ws.journal.append(_rec(f"f{idx}.py", ts=float(idx), op="create", add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr._selected_idx == 0
        rows = scr._rows
        assert rows[0].has_class("diff-row") and rows[0].has_class("selected")
        assert not rows[1].has_class("selected")
        assert _plain(rows[0]).startswith("▸ ")
        assert _plain(rows[1]).startswith("  ")


@pytest.mark.asyncio
async def test_arrows_wrap_and_home_end(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(3):
        ws.write(f"f{idx}.py", "x\n")
        ws.journal.append(_rec(f"f{idx}.py", ts=float(idx), op="create", add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert scr._selected_idx == 2          # круговая навигация вверх
        await pilot.press("down")
        await pilot.pause()
        assert scr._selected_idx == 0
        await pilot.press("end")
        await pilot.pause()
        assert scr._selected_idx == 2
        await pilot.press("home")
        await pilot.pause()
        assert scr._selected_idx == 0


@pytest.mark.asyncio
async def test_pgdn_moves_by_page_and_clamps(tmp_path):
    """Страница — не круг: на краях курсор упирается, а не перескакивает."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(40):
        ws.write(f"f{idx:02d}.py", "x\n")
        ws.journal.append(
            _rec(f"f{idx:02d}.py", ts=float(40 - idx), op="create", add=1, del_=0)
        )

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("pagedown")
        await pilot.pause()
        first_page = scr._selected_idx
        assert 0 < first_page < 39
        for _ in range(10):
            await pilot.press("pagedown")
        await pilot.pause()
        assert scr._selected_idx == 39         # уперлись, а не ушли по кругу
        for _ in range(10):
            await pilot.press("pageup")
        await pilot.pause()
        assert scr._selected_idx == 0


@pytest.mark.asyncio
async def test_long_list_keeps_rows_one_line_high(tmp_path):
    """Скроллбар длинного списка не должен переносить строку во вторую.

    Он сужает тело на колонку уже после того, как строки посчитаны: строка
    перестаёт влезать, уезжает во вторую — и разъезжаются разом и колонки
    счётчиков, и шаг PgUp/PgDn (он меряется строками экрана).
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(40):
        ws.write(f"f{idx:02d}.py", "x\n")
        ws.journal.append(
            _rec(f"f{idx:02d}.py", ts=float(40 - idx), op="create", add=1, del_=0)
        )

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr.query_one("#trace").scrollbar_size_vertical > 0   # список длинный
        assert {row.size.height for row in scr._rows} == {1}


@pytest.mark.asyncio
async def test_row_wider_than_body_is_cropped_not_wrapped(tmp_path):
    """Промах ширины обязан стоить хвоста строки, а не второй строки.

    Держат это правила `.diff-row` в styles.tcss: Textual рендерит `Static`
    через свой `Content`, и `overflow` самого `rich.Text` до экрана не доезжает.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "x\n")
    ws.journal.append(_rec("a.py", ts=1.0, op="create", add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test(size=(50, 24)) as pilot:
        scr = ws.screen()
        scr._row_width = lambda: 70            # намеренно шире тела
        await pilot.app.push_screen(scr)
        await pilot.pause()
        row = scr._rows[0]
        assert len(_plain(row)) > row.size.width       # строка правда не влезла
        assert row.size.height == 1


@pytest.mark.asyncio
async def test_narrow_terminal_keeps_rows_one_line_high(tmp_path):
    """Узкий терминал — тоже одна строка на файл: жертвуются пометки.

    Порядок жертв гейта: счётчики и `≈` не отдаём никогда, поэтому при нехватке
    места уходит колонка пометок целиком. Раньше путь упирался в свой минимум,
    строка вылезала за тело и переносилась второй — сетка колонок рассыпалась.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    deep = "src/very/deep/nested/package/module/screens/engine.py"
    ws.write(deep, "x\n")
    ws.journal.append(_rec(deep, ts=9.0, op="create", add=1284, del_=0))
    for idx in range(6):
        ws.write(f"f{idx}.py", "x\n")
        ws.journal.append(
            _rec(f"f{idx}.py", call_id=None, ts=float(idx), op="create", add=1, del_=0)
        )

    for width in (50, 40):
        async with TuiApp(
            threads_dir=str(ws.threads_dir), system="cli", config=ws.config
        ).run_test(size=(width, 24)) as pilot:
            scr = ws.screen()
            await pilot.app.push_screen(scr)
            await pilot.pause()
            body = scr._rows[0].size.width
            lengths = {len(_plain(row)) for row in scr._rows}
            assert {row.size.height for row in scr._rows} == {1}, width
            assert len(lengths) == 1, (width, lengths)      # сетка общая
            assert lengths.pop() <= body, (width, body)     # и влезает в тело
            for row in scr._rows:                 # счётчики уцелели у всех
                assert re.search(r"\+\d+ +−\d+", _plain(row)), (width, _plain(row))


@pytest.mark.asyncio
async def test_cursor_index_is_clamped_to_the_list(tmp_path):
    """Индекс приходит и снаружи — из вьюера, закрытого на исчезнувшем файле.

    Без клампа курсор уехал бы за диапазон, и ни одна строка не осталась бы
    выбранной: список выглядел бы мёртвым.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(3):
        ws.write(f"f{idx}.py", "x\n")
        ws.journal.append(_rec(f"f{idx}.py", ts=float(idx), op="create", add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._move(99)
        await pilot.pause()
        assert scr._selected_idx == 2
        assert scr._rows[2].has_class("selected")
        scr._move(-5)
        await pilot.pause()
        assert scr._selected_idx == 0
        assert scr._rows[0].has_class("selected")


@pytest.mark.asyncio
async def test_click_selects_row(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(3):
        ws.write(f"f{idx}.py", "x\n")
        ws.journal.append(_rec(f"f{idx}.py", ts=float(idx), op="create", add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.click(scr._rows[2])
        await pilot.pause()
        assert scr._selected_idx == 2
        assert scr._rows[2].has_class("selected")
        assert not scr._rows[0].has_class("selected")


@pytest.mark.asyncio
async def test_empty_states_per_source(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        body = "\n".join(_plain(w) for w in scr.query("#trace > Static"))
        assert body == EMPTY_SESSION
        assert "изменений нет" in _plain(scr.query_one("#header-left"))
        # действовать не над чем — футер предлагает только рабочие клавиши
        assert _plain(scr.query_one("#footer")) == "Shift+Tab сессия/ход · Esc"

        await pilot.press("shift+tab")
        await pilot.pause()
        body = "\n".join(_plain(w) for w in scr.query("#trace > Static"))
        assert body == EMPTY_TURN


@pytest.mark.asyncio
async def test_incomplete_thread_identity_shows_no_data_not_crash(tmp_path):
    """Тред не выбран — экран открывается с объяснением, а не падает."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = DiffScreen(
            launch_root=str(ws.root),
            config=ws.config,
            threads_dir=str(ws.threads_dir),
            system_id="cli",
            thread_id=None,
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        body = "\n".join(_plain(w) for w in scr.query("#trace > Static"))
        assert body == NO_THREAD
        assert _plain(scr.query_one("#header-left")) == ""
        assert _plain(scr.query_one("#header-right")) == ""


@pytest.mark.asyncio
async def test_footer_offers_accept_regardless_of_git_mode(tmp_path):
    """Р26: «принять» не привязано к git — footer одинаков в обоих режимах."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    footers = {}
    for git in (False, True):
        ws = _Workspace(tmp_path / ("git" if git else "cache"), git=git)
        ws.write("a.py", "a\n")
        ws.journal.append(_rec("a.py", ts=1.0, op="create", add=1, del_=0))
        async with TuiApp(
            threads_dir=str(ws.threads_dir), system="cli", config=ws.config
        ).run_test() as pilot:
            scr = ws.screen()
            await pilot.app.push_screen(scr)
            await pilot.pause()
            assert scr._entries                    # список не пуст
            footers[git] = _plain(scr.query_one("#footer"))

    assert footers[False] == footers[True]          # режим не решает содержимое
    assert "a принять" in footers[False]
    assert "r откат" in footers[False]


@pytest.mark.asyncio
async def test_footer_shows_accepted_wording_on_a_marked_row(tmp_path):
    """Курсор на файле с `↺` — вторая строка футера объясняет `a`/`u`/`r` иначе."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\n")                 # снимок начала сессии — откат доступен
    ws.write("a.py", "a\nb\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "a\nb\n")
    ws.write("a.py", "a\nb\nc\n")          # новая правка поверх принятого — снова активен
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert len(scr._entries) == 1
        row = scr._view.active[0]
        assert row.step is not None and row.step.kind == STEP_ACCEPTED

        footer = _plain(scr.query_one("#footer"))
        assert "a принять — зачесть новые правки" in footer
        assert "u сброс" in footer
        assert "r откат — вернуть к принятому" in footer


@pytest.mark.asyncio
async def test_footer_shows_group_wording_and_ctrl_u_on_the_group_row(tmp_path):
    """На заголовке группы `Enter` называется разворотом (Ж3 батча 4), а
    массовый `Ctrl+U` жив: принятый вручную файл в группе есть."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "v1\n")               # принят вручную — снимать есть с кого

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr._entries == []
        assert scr._selected_idx == 0             # курсор сразу на группе

        footer = _plain(scr.query_one("#footer"))
        assert "Enter развернуть" in footer
        assert "Ctrl+U сбросить все принятия" in footer


@pytest.mark.asyncio
async def test_footer_on_group_rows_advertises_u_only_where_it_lives(tmp_path):
    """Ж3/Ж4 батча 4 на живом экране: заголовок группы и файл ВНУТРИ неё —
    разные строки футера.

    На заголовке `Enter` только разворачивает группу, а `u` мёртв
    (`_reset_target` отдаёт по нему `None`) — и не рекламируется. На файле,
    принятом вручную, `Enter` открывает кумулятив сессии и `u` жив. На файле,
    попавшем в группу коммитом, снимать нечего — `u` снова молчит; причина
    подставляется в данные, которые читает клавиша (реальный коммит для неё
    роли не играет — тот же приём, что у стража `u` на settled-строке).
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("act.py", "x\n")
    ws.journal.append(_rec("act.py", ts=2.0))
    ws.write("manual.py", "v1\n")
    ws.journal.append(_rec("manual.py", ts=1.0, call_id="c2"))
    ws.accept("manual.py", "v1\n")          # чисто относительно принятого — в группе

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert [entry.path for entry in scr._entries] == ["act.py"]
        assert [row.path for row in scr._settled] == ["manual.py"]

        await pilot.press("down")                    # заголовок группы
        await pilot.pause()
        footer = _plain(scr.query_one("#footer"))
        assert "Enter развернуть" in footer
        assert "u сброс" not in footer

        await pilot.press("enter")                   # развернули группу
        await pilot.pause()
        assert scr._group_expanded is True
        assert "Enter свернуть" in _plain(scr.query_one("#footer"))

        await pilot.press("down")                    # файл внутри группы
        await pilot.pause()
        footer = _plain(scr.query_one("#footer"))
        assert "Enter — что агент сделал с файлом за сессию" in footer
        assert "u сброс — отменить принятие, файл вернётся в активные" in footer

        scr._settled = [_settled(path="commit.py", reason=SETTLED_COMMIT)]
        scr._render_screen()
        await pilot.pause()
        footer = _plain(scr.query_one("#footer"))
        assert "Enter — что агент сделал с файлом за сессию" in footer
        assert "u сброс" not in footer


@pytest.mark.asyncio
async def test_footer_hides_revert_when_the_row_has_no_reliable_base(tmp_path):
    """Д10 на живом экране: правка без снимка начала сессии откатывать нечем
    (`FileEntry.revertable is False`) — сегмент `r` из футера уходит, всё
    остальное остаётся; массовый `Ctrl+R` тоже мёртв — базы отката нет ни у
    кого в представлении (единственный файл)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "a\nb\n")
    ws.journal.append(_rec("a.py", ts=1.0))       # правка есть, снимка сессии нет

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert len(scr._entries) == 1
        assert scr._entries[0].revertable is False

        footer = _plain(scr.query_one("#footer"))
        assert "r откат" not in footer
        assert "a принять" in footer
        assert "Ctrl+A принять все" in footer
        assert "Ctrl+R" not in footer and "Ctrl+U" not in footer


@pytest.mark.asyncio
async def test_a_reserved_row_separates_the_flash_from_the_footer(tmp_path):
    """Д8 второго смоука: между плашкой и футером — пустая строка, и она
    зарезервирована ВСЕГДА. Без резерва плашка липла к подсказкам вплотную, а
    её появление и снятие двигали бы полосу под рукой пользователя."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\n")
    ws.write("a.py", "a\nb\n")
    ws.journal.append(_rec("a.py", ts=1.0))

    app = TuiApp(threads_dir=str(ws.threads_dir), system="cli", config=ws.config)
    async with app.run_test(size=(160, 24)) as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        footer = scr.query_one("#footer")
        quiet_y = footer.region.y
        assert _screen_lines(pilot.app)[quiet_y - 1] == ""     # резерв есть и без плашки

        scr._flash("принято: 1 файл — новая точка отсчёта")
        await pilot.pause()
        flash = scr.query_one("#diff-flash")

        assert footer.region.y == quiet_y                       # футер не сдвинулся
        assert _screen_lines(pilot.app)[quiet_y - 1] == ""
        # ровно одна пустая строка между плашкой и полосой подсказок
        assert flash.region.y + flash.region.height == quiet_y - 1


@pytest.mark.asyncio
async def test_footer_actions_follow_the_cursor_between_rows(tmp_path):
    """Курсор ушёл на другую строку без полной пересборки — футер обязан
    обновиться тоже (`_repaint_rows`, а не только `_render_screen`)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "a\nb\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "a\nb\n")
    ws.write("a.py", "a\nb\nc\n")
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2"))
    ws.write("b.py", "b\n")
    ws.journal.append(_rec("b.py", ts=3.0, call_id="c3", op="create", add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert len(scr._entries) == 2
        accepted_idx = next(
            i for i, row in enumerate(scr._view.active)
            if row.step is not None and row.step.kind == STEP_ACCEPTED
        )
        plain_idx = 1 - accepted_idx

        scr._move(plain_idx)
        await pilot.pause()
        assert "текущее состояние станет точкой отсчёта" in _plain(
            scr.query_one("#footer")
        )

        scr._move(accepted_idx)
        await pilot.pause()
        assert "зачесть новые правки" in _plain(scr.query_one("#footer"))


@pytest.mark.asyncio
async def test_group_viewer_body_is_the_session_cumulative_not_journal_records(tmp_path):
    """Enter на разобранном файле показывает кумулятив сессии (§12/Р31, I1).

    Тело собирается от базы начала сессии — тем же расчётом, что числа строки
    группы; ноты «точный снимок недоступен» при живом `.orig` быть не должно.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "было\n")
    ws.write("a.py", "было\nстало\n")
    ws.journal.append(_rec("a.py", ts=1.0))     # хэнков в записи нет вовсе
    ws.accept("a.py", "было\nстало\n")          # принят и чист → уходит в группу

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr._entries == []
        await pilot.press("enter")             # раскрыть группу
        await pilot.pause()
        await pilot.press("down")              # settled-строка
        await pilot.press("enter")             # вьюер группы
        await pilot.pause()

        body = "\n".join(_screen_lines(pilot.app))
        assert "+стало" in body                       # реальная разница «до → сейчас»
        assert VIEWER_NOTE_INEXACT not in body        # `.orig` жив — нота была бы ложью


@pytest.mark.asyncio
async def test_group_viewer_keeps_approx_when_group_numbers_came_from_the_journal(tmp_path):
    """Регресс I1: у разобранного пути без базы в сторе числа приходят из
    журнала — шапка вьюера обязана сохранить `≈`, а тело остаться историей
    правок. Заглушка строки берёт `exact` У СТРОКИ, а не назначает его."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "line1\nline2\nline3\n")
    ws.journal.append(_rec("a.py", ts=1.0))     # снимка у пути нет вовсе
    ws.accept("a.py", "line1\nline2\nline3\n")  # принят и чист → уходит в группу

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr._settled[0].exact is False
        await pilot.press("enter")             # раскрыть группу
        await pilot.pause()
        await pilot.press("down")              # settled-строка
        await pilot.press("enter")             # вьюер группы
        await pilot.pause()

        header = _plain(pilot.app.screen.query_one("#header-left"))
        assert APPROX in header


@pytest.mark.asyncio
async def test_group_viewer_footer_advertises_neither_accept_nor_revert(tmp_path):
    """Вьюер, открытый из группы, не рекламирует ни `a`, ни `r` (ревью задачи 10)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "v1\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")             # раскрыть группу
        await pilot.pause()
        await pilot.press("down")              # settled-строка
        await pilot.press("enter")             # вьюер группы
        await pilot.pause()

        footer = _plain(pilot.app.screen.query_one("#footer"))
        assert "a git add" not in footer
        assert "r откат" not in footer
        assert "↑↓ PgUp PgDn · Esc" in footer


@pytest.mark.asyncio
async def test_row_shows_counters_next_to_marks_not_instead(tmp_path):
    """Счётчики есть у КАЖДОЙ строки, включая помеченные."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\n")
    ws.write("a.py", "a\nb\n")
    ws.journal.append(_rec("a.py", call_id=None, ts=1.0, add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        row = _rows_text(scr)[0]
        assert MARK_SHELL in row
        assert "+1" in row and "−0" in row


# --- группа «принято» на живом экране (§4.1, §4.2, §13) -----------------------


@pytest.mark.asyncio
async def test_header_counts_active_only_and_empty_state_rule(tmp_path):
    """Шапка считает только активные; полноэкранная пустота — лишь когда нет и группы."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "v1\n")             # a.py уходит в settled — активных нет

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()

        assert scr._entries == []                        # активных нет
        # Группа жива — шапка говорит про НОВЫЕ изменения (Д7 второго смоука).
        assert "новых изменений нет" in _plain(scr.query_one("#header-left"))
        body = "\n".join(_plain(w) for w in scr.query("#trace > Static"))
        assert EMPTY_SESSION not in body                  # но группа даёт видимое содержимое
        assert "принято 1" in body


@pytest.mark.asyncio
async def test_cursor_walks_into_expanded_group(tmp_path):
    """Курсор идёт по одному плоскому списку: активные → группа → её файлы (канон /sessions)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "v1\n")
    ws.write("c.py", "v1\n")
    ws.journal.append(_rec("c.py", ts=2.0, call_id="c2"))
    ws.accept("c.py", "v1\n")
    ws.write("b.py", "x\n")
    ws.journal.append(_rec("b.py", ts=3.0, call_id="c3", op="create", add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()

        assert len(scr._entries) == 1                     # только b.py активен
        assert scr._selected_idx == 0

        await pilot.press("down")                          # b.py → строка группы
        await pilot.pause()
        assert scr._selected_idx == 1
        assert scr._rows[1].has_class("selected")

        await pilot.press("enter")                         # разворот, курсор остаётся тут
        await pilot.pause()
        assert scr._group_expanded is True
        assert scr._selected_idx == 1
        assert len(scr._rows) == 4                         # 1 активный + группа + 2 settled

        await pilot.press("down")
        await pilot.pause()
        assert scr._selected_idx == 2                       # первый settled

        await pilot.press("down")
        await pilot.pause()
        assert scr._selected_idx == 3                       # второй settled

        await pilot.press("down")                            # по кругу — назад к активному
        await pilot.pause()
        assert scr._selected_idx == 0


@pytest.mark.asyncio
async def test_enter_toggles_group_and_opens_settled_viewer(tmp_path):
    """Enter на строке группы — только toggle; Enter на settled-строке — вьюер."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffFileViewer

    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "v1\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr._entries == []
        assert scr._selected_idx == 0                       # курсор сразу на группе

        await pilot.press("enter")
        await pilot.pause()
        assert scr._group_expanded is True
        assert pilot.app.screen is scr                       # не открылся вьюер

        await pilot.press("down")                            # на settled-строку
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(pilot.app.screen, DiffFileViewer)
        assert pilot.app.screen.entry.path == "a.py"


@pytest.mark.asyncio
async def test_click_on_group_row_toggles_expansion(tmp_path):
    """Клик (не только Enter) раскрывает группу — «Enter/клик» из §4.2 спеки."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "v1\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()

        await pilot.click(scr._rows[0])
        await pilot.pause()
        assert scr._group_expanded is True

        await pilot.click(scr._rows[0])
        await pilot.pause()
        assert scr._group_expanded is False


@pytest.mark.asyncio
async def test_revert_and_git_add_keys_are_noop_on_group_row(tmp_path):
    """`r` в группе не работает ни у кого (§4.3) — и не падает индексом за списком."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "v1\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr._selected_idx == 0                        # единственная строка — группа

        await pilot.press("r")
        await pilot.press("a")
        await pilot.pause()

        assert pilot.app.screen is scr                        # ни подтверждения, ни вьюера
        assert not scr.query("#diff-flash")                   # и действие не отчиталось


# --- принятие/сброс из вьюера группы (§4.3) -----------------------------------


@pytest.mark.asyncio
async def test_group_viewer_hides_dead_keys(tmp_path):
    """Вьюер из группы: `a`/`r` не срабатывают вовсе (не только не рекламируются,
    ревью задачи 10); `u` работает у файла, принятого вручную."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffFileViewer

    ws = _Workspace(tmp_path, git=True)
    ws.write("manual.py", "v1\n")
    ws.journal.append(_rec("manual.py", ts=1.0))
    ws.accept("manual.py", "v1\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")            # раскрыть группу
        await pilot.pause()
        await pilot.press("down")             # settled-строка manual.py
        await pilot.press("enter")            # вьюер группы
        await pilot.pause()

        viewer = pilot.app.screen
        assert isinstance(viewer, DiffFileViewer)
        assert viewer.entry.path == "manual.py"

        await pilot.press("a")
        await pilot.press("r")
        await pilot.pause()

        assert pilot.app.screen is viewer            # ни подтверждения, ни принятия
        assert not viewer.query("#diff-flash")
        assert ws.store.accepted_state("manual.py") is not None    # база цела

        await pilot.press("u")
        await pilot.pause()

        assert pilot.app.screen is scr                # успешный сброс закрыл вьюер
        assert ws.store.accepted_state("manual.py") is None


@pytest.mark.asyncio
async def test_u_is_dead_on_a_settled_row_accepted_only_by_commit(tmp_path):
    """`u` работает только у `вручную` (§4.2) — «коммитом»-строка снимать нечего.

    Реальный коммит тут не нужен — только причина `SETTLED_COMMIT` в данных,
    которые читает клавиша (реальный git-сценарий для неё — Задача 12
    вьюера/списка не про то, откуда взялась причина, а про то, что она значит).
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._settled = [_settled(path="a.py", reason=SETTLED_COMMIT)]
        scr._group_expanded = True
        scr._render_screen()
        await pilot.pause()
        # `scr._entries` всё ещё держит настоящую активную запись a.py (idx=0)
        # — курсор реальной навигацией, а не `_move(N)` наугад: idx=1 — сама
        # строка группы, idx=2 — settled-строка a.py с причиной «коммитом».
        await pilot.press("down")
        await pilot.press("down")
        await pilot.pause()

        await pilot.press("u")
        await pilot.pause()

        assert pilot.app.screen is scr
        assert not scr.query("#diff-flash")


@pytest.mark.asyncio
async def test_double_click_group_row_single_toggle(tmp_path):
    """Быстрый двойной клик по строке группы разворачивает её РОВНО один раз."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "v1\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()

        await pilot.double_click(scr._rows[0])
        await pilot.pause()

        assert scr._group_expanded is True


# --- `u`/`Ctrl+U` в источнике «Ход» (финальный фикс волны 4) ------------------
#
# Владелец на смоуке решил явно: `a`/`u` остаются рабочими в «Ходе», а плашка
# называет источник, где виден эффект (Р27-ревью). Правка задачи 1, переведшая
# строку списка с булева признака «есть принятая база» на действующую ступень,
# по ошибке погасила и доступность клавиш вместе с показом — `row.step` в
# «Ходе» всегда `None` (С1), а `_reset_target`/`_manual_accept_keys` до этого
# фикса проверяли именно ступень.


@pytest.mark.asyncio
async def test_u_resets_from_the_turn_source_when_the_file_has_an_accepted_base(
    tmp_path,
):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff_revert_confirm import RevertConfirmScreen

    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "v1\n")
    ws.write("a.py", "v2\n")                 # дельта от принятого непуста
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen(source=SOURCE_TURN)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr.source == SOURCE_TURN
        row = scr._view.active[0]
        assert row.step is None                              # «Ход» ступеней не видит (Р27)
        assert row.has_accepted_base is True
        assert ACCEPTED_GLYPH not in _rows_text(scr)[0]       # глиф не про «Ход»
        # Пометки «принят» тут НЕТ: диск ушёл дальше принятого (`v2` против
        # `v1`), а пометка говорит про СОСТОЯНИЕ — «файл и есть то, что вы
        # приняли», — а не про факт из прошлого. Иначе она висела бы вечно и
        # утверждала бы принятость новой, непринятой правки.
        assert MARK_ACCEPTED not in _rows_text(scr)[0]

        footer = _plain(scr.query_one("#footer"))
        assert "u сброс" in footer                            # клавиша названа

        await pilot.press("u")
        await pilot.pause()
        confirm = pilot.app.screen
        assert isinstance(confirm, RevertConfirmScreen)       # дошла, не смолчала
        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.screen is scr
        assert ws.store.accepted_state("a.py") is None


@pytest.mark.asyncio
async def test_ctrl_u_finds_accepted_files_from_the_turn_source(tmp_path):
    """Та же регрессия, что у одиночного `u`, но в `_manual_accept_keys`:
    `Ctrl+U` обязан найти файл с принятой базой и не задеть файл без неё."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff_revert_confirm import RevertConfirmScreen

    ws = _Workspace(tmp_path)
    ws.write("accepted.py", "v1\n")
    ws.journal.append(_rec("accepted.py", ts=1.0))
    ws.accept("accepted.py", "v1\n")
    ws.write("accepted.py", "v2\n")
    ws.journal.append(_rec("accepted.py", ts=1.5, call_id="c1b"))
    ws.write("plain.py", "p1\n")
    ws.journal.append(_rec("plain.py", ts=1.2, call_id="c2"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen(source=SOURCE_TURN)
        await pilot.app.push_screen(scr)
        await pilot.pause()

        await pilot.press("ctrl+u")
        await pilot.pause()
        confirm = pilot.app.screen
        assert isinstance(confirm, RevertConfirmScreen)
        body = "\n".join(_plain(w) for w in confirm.query("#trace > Static"))
        assert "accepted.py" in body and "plain.py" not in body

        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.screen is scr
        assert ws.store.accepted_state("accepted.py") is None


# --- гейт подтверждения сброса (Д5 второго смоука) ---------------------------


@pytest.mark.asyncio
async def test_ctrl_u_confirm_counts_the_file_that_risks_nothing_too(tmp_path):
    """Заголовок считает ВСЕХ, у кого снимается принятие (Д5/R5).

    Гейт «спрашивать вообще» прежний — хоть один теряющий точку отката, — но
    показывается после этого весь набор: старое «у N файлов» по одним лишь
    теряющим занижало число, а сброс всё равно снимал принятие у всех.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff_revert_confirm import RevertConfirmScreen

    ws = _Workspace(tmp_path)
    ws.write("lost.py", "v1\n")
    ws.journal.append(_rec("lost.py", ts=2.0))
    ws.accept("lost.py", "v1\n")
    ws.write("lost.py", "v2\n")           # дельта непуста — точка отката теряется
    ws.write("kept.py", "v1\n")
    ws.journal.append(_rec("kept.py", ts=1.0, call_id="c2"))
    ws.accept("kept.py", "v1\n")          # диск равен принятому — терять нечего

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()

        await pilot.press("ctrl+u")
        await pilot.pause()

        confirm = pilot.app.screen
        assert isinstance(confirm, RevertConfirmScreen)
        assert _plain(confirm.query_one("#header-left")) == "Сброс принятия у 2 файлов"

        await pilot.press("enter")
        await pilot.pause()

        assert ws.store.accepted_state("lost.py") is None
        assert ws.store.accepted_state("kept.py") is None


@pytest.mark.asyncio
async def test_ctrl_u_without_a_single_loss_resets_at_once(tmp_path):
    """Терять нечего никому — спрашивать не о чем (Д5/R5): сброс применяется
    сразу, с плашкой, и подтверждения не открывается вовсе."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for name, ts in (("a.py", 2.0), ("b.py", 1.0)):
        ws.write(name, "v1\n")
        ws.journal.append(_rec(name, ts=ts, call_id=f"c_{name}"))
        ws.accept(name, "v1\n")           # диск равен принятому у обоих

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()

        await pilot.press("ctrl+u")
        await pilot.pause()

        assert pilot.app.screen is scr                   # экрана подтверждения нет
        assert "принятие отменено" in _plain(scr.query_one("#diff-flash"))
        assert ws.store.accepted_state("a.py") is None
        assert ws.store.accepted_state("b.py") is None


@pytest.mark.asyncio
async def test_ctrl_u_cyrillic_lower_resets_all(tmp_path):
    """Ctrl+г (русская раскладка) — `on_key`/`norm_key`, не `BINDINGS` (фикс
    ревью Задачи 12): живое нажатие, образец — `test_sessions_screen.py`
    (`test_r10_ctrl_cyrillic_k_lower_renames`)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        called = []
        scr._reset_all = lambda: called.append(1)  # type: ignore[assignment]

        await pilot.press("ctrl+г")

        assert called == [1], called


@pytest.mark.asyncio
async def test_footer_does_not_promise_accepted_target_outside_the_session_ladder(tmp_path):
    """Ревью ветки, Minor 1: у файла есть `.accepted`, но `r` вернёт НЕ к нему.

    Два случая в одном сценарии — оба про одно: состояние футера обязано
    следовать ДЕЙСТВУЮЩЕЙ ступени, а не факту наличия принятой базы.

    * источник «Ход» ступеней не видит вовсе (Р27) — `r` вернёт к началу хода;
    * в «Сессии» тай-брейк Р28 отдаёт ступень HEAD коммиту, сделанному ПОСЛЕ
      принятия, — `r` вернёт к закоммиченному состоянию.

    Превью в обоих случаях говорит правду; сверяем именно футер.
    """
    pytest.importorskip("textual")
    import shutil
    import subprocess

    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen
    from gc_tui.app import TuiApp

    if shutil.which("git") is None:
        pytest.skip("git недоступен")

    ws = _Workspace(tmp_path)
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "test"),
    ):
        subprocess.run(["git", *args], cwd=str(ws.root), check=True, capture_output=True)
    ws.write("a.py", "v1\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.base("a.py", "v1\n")
    # Снимок начала ХОДА — как у живого рекордера: без него у «Хода» нет
    # достоверной базы, и `r` из его футера ушёл бы вовсе (Д10).
    ws.store.capture_turn("a.py", b"v1\n", "t1")
    ws.write("a.py", "v2\n")
    ws.journal.append(_rec("a.py", ts=1.0))

    view = EditsView(str(ws.threads_dir), "cli", "th1", str(ws.root), ws.config)
    view.accept(["a.py"])                      # принято состояние "v2\n"
    ws.write("a.py", "v3\n")
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2"))
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit after accept"], cwd=str(ws.root),
                   check=True, capture_output=True)
    ws.write("a.py", "v4\n")                   # правка поверх коммита — файл активен
    ws.journal.append(_rec("a.py", ts=3.0, call_id="c3"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert ws.store.accepted_state("a.py") is not None    # `.accepted` жив
        row = scr._view.active[0]
        assert row.step is not None and row.step.kind == STEP_HEAD  # но действует ступень HEAD
        [target] = scr._edits.revert_targets(SOURCE_SESSION, ["a.py"])
        assert target.kind == "head"                          # а `r` вернёт к коммиту

        footer = _plain(scr.query_one("#footer"))
        assert "вернуть к принятому" not in footer
        # Д4 второго смоука: цель `r` называет ДЕЙСТВУЮЩУЮ ступень — коммит.
        assert "вернуть файл к закоммиченному" in footer

        scr._toggle_source()
        await pilot.pause()
        assert scr.source == SOURCE_TURN
        assert ws.store.accepted_state("a.py") is not None    # `.accepted` по-прежнему жив
        assert scr._view.active[0].step is None                # но в «Ходе» ступени нет вовсе (С1)

        turn_footer = _plain(scr.query_one("#footer"))
        assert "вернуть к принятому" not in turn_footer
        assert "вернуть файл к началу хода" in turn_footer


@pytest.mark.asyncio
async def test_u_resets_in_session_source_when_a_later_commit_wins_the_tiebreak(
    tmp_path,
):
    """Финальный фикс волны 4: тот же сетап, что у предыдущего теста (тай-брейк
    Р28 отдаёт действующую ступень HEAD, принятая база при этом ещё жива), но
    здесь проверяем `u`, а не `r`. Регрессия батча: `_reset_target` проверял
    ДЕЙСТВУЮЩУЮ ступень (`_is_accepted_step`) вместо факта принятой базы и
    молча гасил клавишу вместе с глифом `↺` везде, где тай-брейк отдавал счёт
    коммиту, — хотя принятие никуда не делось и снимать его можно.
    """
    pytest.importorskip("textual")
    import shutil
    import subprocess

    from gc_tui.app import TuiApp
    from gc_tui.screens.diff_revert_confirm import RevertConfirmScreen

    if shutil.which("git") is None:
        pytest.skip("git недоступен")

    ws = _Workspace(tmp_path)
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "test"),
    ):
        subprocess.run(["git", *args], cwd=str(ws.root), check=True, capture_output=True)
    ws.write("a.py", "v1\n")
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(ws.root), check=True,
                   capture_output=True)
    ws.base("a.py", "v1\n")
    ws.write("a.py", "v2\n")
    ws.journal.append(_rec("a.py", ts=1.0))

    view = EditsView(str(ws.threads_dir), "cli", "th1", str(ws.root), ws.config)
    view.accept(["a.py"])                      # принято состояние "v2\n"
    ws.write("a.py", "v3\n")
    ws.journal.append(_rec("a.py", ts=2.0, call_id="c2"))
    subprocess.run(["git", "add", "--", "a.py"], cwd=str(ws.root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit after accept"], cwd=str(ws.root),
                   check=True, capture_output=True)
    ws.write("a.py", "v4\n")                   # правка поверх коммита — файл активен
    ws.journal.append(_rec("a.py", ts=3.0, call_id="c3"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()

        row = scr._view.active[0]
        assert row.step is not None and row.step.kind == STEP_HEAD   # тай-брейк отдал HEAD
        assert row.has_accepted_base is True                          # принятая база жива
        assert ACCEPTED_GLYPH not in _rows_text(scr)[0]                # глиф не про эту строку

        footer = _plain(scr.query_one("#footer"))
        assert "u сброс" in footer            # клавиша названа, хоть строка и не ROW_ACCEPTED

        await pilot.press("u")
        await pilot.pause()

        confirm = pilot.app.screen
        assert isinstance(confirm, RevertConfirmScreen)     # клавиша сработала, не смолчала
        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.screen is scr
        assert ws.store.accepted_state("a.py") is None


@pytest.mark.asyncio
async def test_turn_with_all_edits_undone_says_so(tmp_path):
    """Ход выбран, но показывать нечего — экран обязан назвать причину.

    Прежняя формулировка «в этом ходе изменений не было» спорила с шапкой: та
    обещает «последний ход С ПРАВКАМИ» — он и выбран по записи в журнале. Вместе
    это читалось как противоречие, хотя случилось простое: правки хода откачены.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import EMPTY_TURN_UNDONE

    ws = _Workspace(tmp_path)
    ws.write("a.py", "было\n")
    ws.store.capture_turn("a.py", "было\n".encode(), "t1")   # база начала хода
    ws.journal.append(_rec("a.py", turn="t1", ts=1.0))
    # диск совпал с началом хода: правки этого хода не осталось

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen(source=SOURCE_TURN)
        await pilot.app.push_screen(scr)
        await pilot.pause()

        assert scr._entries == []
        body = "\n".join(_plain(w) for w in scr.query("#trace > Static"))
        assert body == EMPTY_TURN_UNDONE


def test_header_legend_explains_only_the_marks_that_are_on_screen():
    """Легенда пометок — про то, что реально видно, и уходит первой на узком.

    В «Сессии» база счёта у каждого файла своя, поэтому в шапке объясняется
    ЗНАК, а не одна база на всех. Без легенды знак приходилось знать: полная
    фраза жила только во вьюере и в превью отката, а вопрос возникает в списке.
    """
    from gc_tui.screens.diff import (LEGEND_ACCEPTED, LEGEND_COMMIT,
                                     LEGEND_EXTERNAL)

    def head(width=160, **kw):
        return build_header(SOURCE_SESSION, files=2, add=2, del_=2,
                            mode="git-mode", width=width, **kw)[0]

    plain = head()
    assert LEGEND_ACCEPTED not in plain and LEGEND_COMMIT not in plain
    assert LEGEND_EXTERNAL not in plain

    only_external = head(legend_external=True)
    assert LEGEND_EXTERNAL in only_external
    assert LEGEND_ACCEPTED not in only_external and LEGEND_COMMIT not in only_external

    only_accepted = head(legend_accepted=True)
    assert LEGEND_ACCEPTED in only_accepted and LEGEND_COMMIT not in only_accepted

    only_commit = head(legend_commit=True)
    assert LEGEND_COMMIT in only_commit and LEGEND_ACCEPTED not in only_commit

    both = head(legend_accepted=True, legend_commit=True)
    assert LEGEND_ACCEPTED in both and LEGEND_COMMIT in both

    # Узкая ширина: легенда жертвуется ПЕРВОЙ — знак без расшифровки хуже, чем
    # числа без знака, но числа и источник обязаны выжить.
    narrow = head(width=len(plain), legend_accepted=True, legend_commit=True,
                  legend_external=True)
    assert narrow == plain


def test_external_legend_says_nothing_about_how_much_of_the_file_is_foreign():
    """Расшифровка про АВТОРСТВО, не про объём: объёма мы не знаем.

    «Весь файл чужой» на этом экране не бывает по построению: путь попадает в
    набор только из журнала, то есть агент его правил — иначе строки бы не
    было. А какие именно строки чужие, сказать нечем: в журнале лежат хэши и
    пошаговые хэнки, содержимого «каким агент оставил файл» мы не храним.
    Голое слово `извне` давало прочитать пометку как объём (смок 2026-09-10).
    """
    from gc_tui.screens.diff import LEGEND_EXTERNAL, MARK_EXTERNAL

    assert LEGEND_EXTERNAL == f"{MARK_EXTERNAL} — есть правки не от агента"
    for scope in ("весь", "все строки", "целиком", "полностью", "часть"):
        assert scope not in LEGEND_EXTERNAL


@pytest.mark.asyncio
async def test_turn_marks_a_file_whose_content_is_the_accepted_one(tmp_path):
    """«принят» в «Ходе» — про состояние: файл и есть то, что приняли.

    После `a` пометка появляется — видно, что жест сработал. Изменится файл
    дальше — пометка уйдёт: она не расписка о прошлом, а признак текущего
    состояния. Тот же смысл, что у группы «принято» в «Сессии», где группы
    нет — там его несёт строка.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "v1\n")          # принято ровно то, что на диске

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen(source=SOURCE_TURN)
        await pilot.app.push_screen(scr)
        await pilot.pause()

        assert MARK_ACCEPTED in _rows_text(scr)[0]


@pytest.mark.asyncio
async def test_screen_admits_the_list_is_incomplete_after_a_command_cut_by_the_cap(tmp_path):
    """Команда тронула больше путей, чем пишет журнал, — экран говорит об этом прямо.

    Без строки список выглядел бы полным: «откатить всё» откатило бы видимое, а
    остальное осталось бы изменённым молча. Строка — не строка списка: курсор на
    неё не встаёт, и жесты над файлами её не касаются."""
    pytest.importorskip("textual")
    from dataclasses import replace

    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "a\n")
    ws.journal.append(replace(_rec("a.py", turn="t1", call_id=None, op="shell"),
                              command_paths=10001))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()

        body = [_plain(w) for w in scr.query("#trace > Static")]
        assert "одна из команд этой сессии изменила 10001 файл " in body[0].lower()
        assert len(scr._rows) == 1 and "a.py" in _plain(scr._rows[0])


@pytest.mark.asyncio
async def test_x_hides_the_cut_command_note_until_the_app_exits(tmp_path):
    """`x` («ч») скрывает строку про урезанную команду — до выхода из приложения.

    Строка построена по истории журнала, а не по диску: пути сверх потолка не
    записаны, и погаснуть сама она не может. Скрытие живёт в приложении, а не в
    экране — иначе каждое новое открытие F3 возвращало бы прочитанное. Скрывается
    КОНКРЕТНАЯ команда: новая урезанная команда показывается снова."""
    pytest.importorskip("textual")
    from dataclasses import replace

    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "a\n")
    ws.write("b.py", "b\n")
    ws.journal.append(replace(_rec("a.py", turn="t1", call_id=None, op="shell"),
                              command_paths=10001))

    def notes(scr):
        return [t for t in (_plain(w) for w in scr.query("#trace > Static"))
                if "одна из команд" in t.lower()]

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert notes(scr) and "x — скрыть" in notes(scr)[0]

        await pilot.press("ч")
        await pilot.pause()
        assert notes(scr) == []
        assert len(scr._rows) == 1 and "a.py" in _plain(scr._rows[0])

        await pilot.press("escape")
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert notes(scr) == []

        await pilot.press("escape")
        ws.journal.append(replace(_rec("b.py", turn="t2", call_id=None, op="shell", ts=2.0),
                                  command_paths=20002))
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert len(notes(scr)) == 1 and "20002" in notes(scr)[0]


@pytest.mark.asyncio
async def test_x_removes_only_the_note_without_rebuilding_the_list(tmp_path):
    """`x` снимает одну строку, а не пересобирает список.

    Строка появляется, только когда команда задела больше путей, чем пишет
    журнал (10 000), — список под ней почти всегда огромный, и полная
    пересборка на каждое нажатие стоила бы секунды (ревью `b51c34400`)."""
    pytest.importorskip("textual")
    from dataclasses import replace

    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "a\n")
    ws.journal.append(replace(_rec("a.py", turn="t1", call_id=None, op="shell"),
                              command_paths=10001))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        rows_before = list(scr._rows)
        rebuilds = []
        scr._render_screen = lambda: rebuilds.append(1)

        await pilot.press("x")
        await pilot.pause()

        assert rebuilds == []
        assert not any("одна из команд" in _plain(w).lower()
                       for w in scr.query("#trace > Static"))
        assert scr._rows == rows_before and all(row.is_attached for row in scr._rows)


@pytest.mark.asyncio
async def test_list_rows_are_mounted_in_one_call(tmp_path, monkeypatch):
    """Строки списка монтируются ОДНИМ вызовом, а не по одной.

    Textual на каждом монтировании перебирает уже смонтированных детей: по одной
    строке это квадрат — 10 000 файлов открывались 96 секунд (замер, после
    подъёма потолка записи шелл-канала до 10 000 путей)."""
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll

    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for i in range(30):
        ws.write(f"f{i}.py", "x\n")
        ws.journal.append(_rec(f"f{i}.py", turn="t1", ts=float(i)))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        trace = scr.query_one("#trace", VerticalScroll)
        calls = []
        real_mount = trace.mount
        monkeypatch.setattr(trace, "mount", lambda *w, **k: (calls.append(len(w)), real_mount(*w, **k))[1])
        scr._render_screen()
        await pilot.pause()

        assert calls == [30]
        assert len(scr._rows) == 30


@pytest.mark.asyncio
async def test_cursor_step_repaints_only_the_rows_that_changed(tmp_path, monkeypatch):
    """Шаг курсора перерисовывает две строки — ушедшую и пришедшую, а не весь список.

    Каждое обновление строки просит Textual переложить список: перерисовка всех
    строк на каждую стрелку стоила 1,3 с на нажатие при 10 000 файлов."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for i in range(30):
        ws.write(f"f{i}.py", "x\n")
        ws.journal.append(_rec(f"f{i}.py", turn="t1", ts=float(i)))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        updated = []
        for idx, row in enumerate(scr._rows):
            real = row.update
            monkeypatch.setattr(row, "update", lambda *a, _i=idx, _r=real, **k: (updated.append(_i), _r(*a, **k))[1])
        await pilot.press("down")
        await pilot.pause()

        assert sorted(updated) == [0, 1]
        assert [r.has_class("selected") for r in scr._rows[:3]] == [False, True, False]


@pytest.mark.asyncio
async def test_actions_on_a_busy_snapshot_index_warn_instead_of_crashing(tmp_path, monkeypatch):
    """Занятый индекс снимков не роняет экран ни на одном действии — только предупреждает.

    Индекс читает почти каждое действие (превью и применение отката, принятие,
    снятие принятия, тело вьюера), а его замок держит конкурирующий писатель —
    второе окно, CLI, субагент, — сбивая заодно кэш вида. Защита стоит на границе
    действий экрана, а не в точке очередной трассы: вида здесь НЕ греем, и
    чтение индекса ломается везде, где случится."""
    pytest.importorskip("textual")
    from gc_core.agent_edits.snapshots import SnapshotStore

    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import EDITS_UNREADABLE

    ws = _Workspace(tmp_path)
    ws.base("a.py", "old\n")
    ws.write("a.py", "new\n")
    ws.journal.append(_rec("a.py", turn="t1", ts=1.0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        notes = []
        monkeypatch.setattr(pilot.app, "notify", lambda msg, **kw: notes.append(msg))

        def busy(self):
            raise TimeoutError("индекс снимков занят")

        monkeypatch.setattr(SnapshotStore, "_index_snapshot", busy)
        for key in ("r", "a", "u", "ctrl+r", "ctrl+a", "enter"):
            scr._edits.invalidate()                   # конкурент сбил кэш вида
            await pilot.press(key)
            await pilot.pause()
            if pilot.app.screen is not scr:
                await pilot.press("escape")           # вьюер или подтверждение — назад
                await pilot.pause()

        assert pilot.app.screen is scr               # экран жив
        assert EDITS_UNREADABLE in notes


@pytest.mark.asyncio
async def test_viewer_body_on_a_busy_snapshot_index_says_so_instead_of_crashing(
    tmp_path, monkeypatch
):
    """Тело вьюера на занятом индексе — строка «повторите», а не падение экрана.

    Тело строится лениво, при отрисовке, и до границы действий экрана отказ не
    доходит. База «Хода» читается из индекса: переключение режима (`f`)
    пересобирает тело уже под занятым замком."""
    pytest.importorskip("textual")
    from gc_core.agent_edits.snapshots import SnapshotStore

    from textual.widgets import Static

    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import EDITS_UNREADABLE

    ws = _Workspace(tmp_path)
    ws.write("a.py", "new\n")
    ws.store.capture_turn("a.py", b"old\n", "t1")
    ws.journal.append(_rec("a.py", turn="t1", ts=1.0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen(source=SOURCE_TURN)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        viewer = pilot.app.screen
        assert viewer is not scr, "предусловие: вьюер открыт"

        def busy(self):
            raise TimeoutError("индекс снимков занят")

        monkeypatch.setattr(SnapshotStore, "_index_snapshot", busy)
        await pilot.press("f")
        await pilot.pause()
        await pilot.pause()

        assert pilot.app.screen is viewer
        assert EDITS_UNREADABLE in "\n".join(_plain(w) for w in viewer.query(Static))


@pytest.mark.asyncio
async def test_revert_that_cannot_be_reported_still_reloads_and_says_so(
    tmp_path, monkeypatch
):
    """Отказ ПОСЛЕ отката — не «правки не прочитать»: файлы уже изменены.

    Хвост действия рассказывает об откате модели (`on_undo_feedback` пишет в
    файлы треда), и отказ там граница чтения глотать не вправе: она советует
    повтор, которого делать нельзя, и оставляет список со старыми дельтами.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import EDITS_CHANGED_UNREPORTED, EDITS_UNREADABLE

    ws = _Workspace(tmp_path)
    ws.base("a.py", "old\n")
    ws.write("a.py", "new\n")
    ws.journal.append(_rec("a.py", turn="t1", ts=1.0))

    def cannot_tell(restored, deleted):
        raise OSError("файл треда занят")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = ws.screen(on_undo_feedback=cannot_tell)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        notes = []
        monkeypatch.setattr(pilot.app, "notify", lambda msg, **kw: notes.append(msg))
        reloads = []
        real_reload = scr._reload
        monkeypatch.setattr(
            scr, "_reload", lambda: (reloads.append(1), real_reload())[1]
        )

        scr._apply_revert([scr._entries[0].path], from_viewer=False)
        await pilot.pause()

        assert pilot.app.screen is scr
        assert (ws.root / "a.py").read_text() == "old\n"
        assert reloads == [1]
        assert notes == [EDITS_CHANGED_UNREPORTED]
        assert EDITS_UNREADABLE not in notes
