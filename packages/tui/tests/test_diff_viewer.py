"""Вьюер файла на экране изменений (`/diff` → Enter).

Тело вьюера — тот же движок, что у блоков трейса (`render_hunks`), поэтому
проверяется не раскраска, а то, что принадлежит вьюеру: границы блоков,
навигация по ним и по файлам, режим «весь файл», честность шапки и футера в
режимах, где точной базы нет.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from gc_core.agent_edits.deltas import FileEntry
from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.ladder import STEP_HEAD
from gc_core.agent_edits.snapshots import SnapshotStore, snapshots_root
from gc_core.thread_store import edits_journal_path
from gc_testkit.tui_support import (
    as_settings,
    span_style,
    tui_settings,
    wait_until,
)
from gc_tui.palette import ERR, OK
from gc_tui.screens.diff import (
    ACCEPTED_CAPTION_STEM,
    APPROX,
    FULL_SUFFIX,
    HEAD_CAPTION_STEM,
    VIEWER_NO_PREVIEW,
    VIEWER_NOTE_INEXACT,
    VIEWER_NOTE_INEXACT_SHELL,
    DiffFileViewer,
    DiffScreen,
    accepted_caption,
    build_viewer_header,
    edit_caption,
    edit_counter,
    head_caption,
    inexact_note,
    split_body,
    viewer_footer,
)
from gc_tui.services.edits_view import SOURCE_SESSION, SOURCE_TURN, EditsView
from rich.text import Text

pytest.importorskip("textual")


# --- фикстуры данных ---------------------------------------------------------


def _rec(
    path="a.py",
    turn="t1",
    call_id="c1",
    op="edit",
    add=1,
    del_=1,
    ts=1.0,
    hunks="",
    flags=None,
) -> EditRecord:
    return EditRecord(
        path=path,
        turn_id=turn,
        ts=ts,
        call_id=call_id,
        op=op,
        hunks=hunks,
        add=add,
        del_=del_,
        before_sha256=None,
        after_sha256=None,
        flags=list(flags or []),
    )


def _entry(
    path="a.py",
    add=1,
    del_=1,
    kind="modified",
    exact=True,
    countable=True,
    by_shell=False,
    external=False,
    revertable=True,
    last_ts=1.0,
) -> FileEntry:
    return FileEntry(
        path=path,
        add=add,
        del_=del_,
        kind=kind,
        exact=exact,
        countable=countable,
        by_shell=by_shell,
        external=external,
        revertable=revertable,
        last_ts=last_ts,
    )


class _Workspace:
    """Тред с журналом и снимками + файлы на диске под launch_root."""

    def __init__(self, tmp_path: Path) -> None:
        self.threads_dir = tmp_path / "threads"
        self.threads_dir.mkdir(parents=True)
        self.root = tmp_path / "proj"
        self.root.mkdir(parents=True)
        self.journal = EditsJournal(
            edits_journal_path(str(self.threads_dir), "cli", "th1")
        )
        self.store = SnapshotStore(
            snapshots_root(str(self.root)), "cli_th1", self.threads_dir
        )

    @property
    def config(self) -> dict:
        return {
            "launch_root": str(self.root),
            "agent.model": "M",
            "agent_edits": {},
        }

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def base(self, rel: str, text: str) -> None:
        """Точный снимок начала сессии — с ревизией HEAD, как пишет рекордер.

        Без ревизии признака «коммит затронул путь уже в этой сессии» нет, и
        ступень HEAD не появится ни в одном сценарии (§3.2, образец —
        `_Workspace.base` в `test_diff_screen.py`). Вне git-репозитория
        `head_rev_from_files` возвращает `None` — поведение как раньше.
        """
        from gc_core.agent_edits import git_ops

        self.store.capture_session(
            rel,
            text.encode("utf-8"),
            head_rev=git_ops.head_rev_from_files(str(self.root)),
        )

    def accept(self, rel: str, text: str) -> None:
        """Принять путь «вручную» — готовый снимок для сценариев с `.accepted`."""
        self.store.capture_accepted(rel, text.encode("utf-8"), head_oid=None)

    def edits(self) -> EditsView:
        return EditsView(
            str(self.threads_dir),
            "cli",
            "th1",
            str(self.root),
            as_settings(self.config),
        )

    def screen(self, **kwargs) -> DiffScreen:
        return DiffScreen(
            launch_root=str(self.root),
            settings=as_settings(self.config),
            threads_dir=str(self.threads_dir),
            system_id="cli",
            thread_id="th1",
            **kwargs,
        )

    def viewer(self, source: str = SOURCE_SESSION, **kwargs) -> DiffFileViewer:
        view = self.edits()
        files = [row.entry for row in view.session_view(source).active]
        return DiffFileViewer(
            view=view, source=source, files=files, index=0, **kwargs
        )


_LINES = [f"line{n:02d}" for n in range(1, 21)]


def _two_hunk_file(ws: _Workspace, rel: str = "a.py") -> None:
    """Файл с двумя далёкими правками — тело распадётся на два блока."""
    ws.base(rel, "\n".join(_LINES) + "\n")
    changed = list(_LINES)
    changed[2] = "line03 правлено"
    changed[16] = "line17 правлено"
    ws.write(rel, "\n".join(changed) + "\n")
    ws.journal.append(_rec(rel, ts=1.0, add=2, del_=2))


def _plain(widget) -> str:
    return str(widget.render())


def _body(screen: DiffFileViewer) -> str:
    return "\n".join(_plain(w) for w in screen.query("#trace > Static"))


def _screen_lines(app) -> list[str]:
    """Текстовый слепок экрана строками композитора (приём тестов блоков)."""
    return [
        "".join(seg.text for seg in strip).rstrip()
        for strip in app.screen._compositor.render_strips()
    ]


# --- чистые функции ----------------------------------------------------------


def _parts_plain(parts):
    return [
        (str(part.text) if part.text is not None else part.meta, part.edit)
        for part in parts
    ]


def test_split_body_cuts_context_edit_context():
    """Тело — участки «контекст — правка — контекст»; правка = свой участок."""
    diff = (
        "--- \n+++ \n"
        "@@ -1,4 +1,4 @@\n a\n-b\n+B\n c\n"
        "@@ -10,4 +10,4 @@\n x\n-y\n+Y\n z\n"
    )
    parts = split_body(diff, path="a.py", fill_width=None)
    edits = [part for part in parts if part.edit is not None]
    assert [part.edit for part in edits] == [0, 1]
    assert "-b" in str(edits[0].text) and "+B" in str(edits[0].text)
    assert "-y" in str(edits[1].text) and "+Y" in str(edits[1].text)
    # Контекст живёт своими участками и курсором не метится.
    context = [str(part.text) for part in parts if part.edit is None]
    assert any(" a" in text for text in context)
    assert any(" z" in text for text in context)
    assert split_body("", path="a.py", fill_width=None) == []


def test_split_body_reads_git_form_too():
    """Форма шелл-канала (`git diff`) режется так же — преамбула не в теле."""
    diff = (
        "diff --git a/x.py b/x.py\nindex 111..222 100644\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    parts = split_body(diff, path="x.py", fill_width=None)
    assert [part.edit for part in parts] == [0]
    body = str(parts[0].text)
    assert "-old" in body and "+new" in body
    assert "diff --git" not in body and "index" not in body


def test_split_body_keeps_one_gutter_width_for_the_whole_file():
    """Колонка номеров одна на всё тело: участки считаются ПОСЛЕ рендера.

    Порознь посчитанный участок у строки 9 дал бы гаттер шириной 1, а у строки
    120 — шириной 3, и тело поехало бы по вертикали на каждом стыке.
    """
    lines = [f" line{n}\n" for n in range(1, 130)]
    lines[8] = "-line9\n"
    lines[120] = "+line121\n"
    parts = split_body(
        "@@ -1,129 +1,129 @@\n" + "".join(lines), path="a.py", fill_width=None
    )
    starts = {
        line.split(" ")[0] and len(line) - len(line.lstrip(" "))
        for part in parts
        for line in str(part.text).splitlines()
    }
    # Все строки начинаются с номера, выровненного по одной ширине.
    widths = {
        len(line) - len(line.lstrip(" ")) + len(line.strip().split(" ")[0])
        for part in parts
        for line in str(part.text).splitlines()
    }
    assert len(widths) == 1, (widths, starts)


def test_split_body_chunks_long_context(tmp_path):
    """Длинный контекст режется на куски — ради него тело и разрезано."""
    body = "".join(f" line{n}\n" for n in range(1, 501))
    parts = split_body(
        "@@ -1,500 +1,500 @@\n" + body, path="a.py", fill_width=None
    )
    assert len(parts) == 5  # 500 / CONTEXT_CHUNK
    assert all(part.edit is None for part in parts)
    assert sum(len(str(part.text).splitlines()) for part in parts) == 500


def test_viewer_footer_switches_f_label_and_hides_it_when_unavailable():
    """Подпись `f` говорит, что произойдёт; без точной базы её нет вовсе."""
    diff_mode = viewer_footer(full=False, can_full=True)
    assert diff_mode == (
        "n след. правка · p пред. · ←/→ файл · f весь файл · a принять"
        " · r откат · ↑↓ PgUp PgDn · Esc"
    )
    full_mode = viewer_footer(full=True, can_full=True)
    assert "f только изменения" in full_mode
    assert "f весь файл" not in full_mode

    blocked = viewer_footer(full=False, can_full=False)
    assert "f " not in blocked
    assert "n след. правка" in blocked and "r откат" in blocked


def test_viewer_footer_drops_file_keys_for_single_file():
    """Файл в источнике один — листать нечего, сегмент уходит."""
    alone = viewer_footer(full=False, can_full=True, can_switch=False)
    assert "←/→" not in alone
    assert "n след. правка · p пред. · f весь файл" in alone
    assert "r откат · ↑↓ PgUp PgDn · Esc" in alone


def test_viewer_footer_offers_accept_but_never_the_whole_set():
    """Р20: во вьюере — действия над ЭТИМ файлом, массовых нет вовсе."""
    footer = viewer_footer(full=False, can_full=True)
    assert "a принять" in footer
    assert "Ctrl+A" not in footer and "Ctrl+R" not in footer
    # Порядок сегментов — из Р20: `a` стоит между `f` и `r`.
    assert footer.index("f весь файл") < footer.index("a принять")
    assert footer.index("a принять") < footer.index("r откат")


def test_viewer_footer_drops_accept_when_unavailable():
    """Принятие недоступно (вьюер открыт из группы) — сегмент уходит, как и `r`.

    Р9-пересмотр: в отличие от старого git add, принятие не привязано к
    репозиторию вовсе — `can_add=False` больше не значит «вне git», а значит
    «читаемый файл принимать нечем» (§4.3 плана волны 4).
    """
    blocked = viewer_footer(full=False, can_full=True, can_add=False)
    assert "принять" not in blocked
    assert "r откат · ↑↓ PgUp PgDn · Esc" in blocked
    assert blocked.startswith(
        "n след. правка · p пред. · ←/→ файл · f весь файл"
    )


def test_viewer_footer_shows_reset_only_when_applicable():
    """`u сброс` — только когда явно затребован; порядок сегментов `a → u → r`."""
    footer = viewer_footer(full=False, can_full=True, can_reset=True)
    assert "u сброс" in footer
    assert (
        footer.index("a принять")
        < footer.index("u сброс")
        < footer.index("r откат")
    )

    default = viewer_footer(full=False, can_full=True)
    assert "u сброс" not in default  # старые вызовы не рекламируют лишнего


def test_viewer_footer_keeps_np_until_the_file_has_a_single_edit():
    """Р18: `n`/`p` честны в обоих режимах и уходят только при одной правке."""
    for full in (False, True):
        many = viewer_footer(full=full, can_full=True, can_step=True)
        assert many.startswith("n след. правка · p пред.")
    lonely = viewer_footer(full=False, can_full=True, can_step=False)
    assert "n след. правка" not in lonely and "p пред." not in lonely
    assert lonely.startswith("←/→ файл")


def test_edit_counter_counts_from_one_and_does_not_hide_a_single_edit():
    """`правка 2/4`, а при единственной — `правка 1/1`, без вранья."""
    assert edit_counter(1, 4) == "правка 2/4"
    assert edit_counter(0, 1) == "правка 1/1"


def test_inexact_note_says_commands_only_when_every_edit_is_shell():
    """Нота не приписывает командам то, что правил инструмент."""
    shell_only = [_rec(call_id=None), _rec(call_id=None, ts=2.0)]
    assert inexact_note(shell_only) == VIEWER_NOTE_INEXACT_SHELL
    assert "командами" in VIEWER_NOTE_INEXACT_SHELL

    mixed = [_rec(call_id=None), _rec(call_id="c1", ts=2.0)]
    assert inexact_note(mixed) == VIEWER_NOTE_INEXACT
    assert "командами" not in VIEWER_NOTE_INEXACT
    assert inexact_note([_rec(call_id="c1")]) == VIEWER_NOTE_INEXACT


def test_footer_and_notes_never_say_hunk():
    """«Хэнк» — жаргон: в интерфейсе его нет ни в одном режиме."""
    texts = [
        viewer_footer(full=False, can_full=True),
        viewer_footer(full=True, can_full=True),
        viewer_footer(full=False, can_full=False),
        VIEWER_NOTE_INEXACT,
        VIEWER_NO_PREVIEW,
        edit_caption(1, _rec(call_id=None)),
    ]
    for text in texts:
        assert "хэнк" not in text.lower(), text


def test_viewer_header_says_source_file_and_full_mode():
    entry = _entry("src/tui/screens/diff.py", add=84, del_=12)
    left = build_viewer_header(SOURCE_SESSION, entry, full=False, width=100)
    assert left == "изменения · сессия · src/tui/screens/diff.py · +84 −12"

    full = build_viewer_header(SOURCE_SESSION, entry, full=True, width=100)
    assert full == left + f" · {FULL_SUFFIX}"


def test_viewer_header_carries_the_external_warning():
    """Предупреждение обязано быть там, где читают содержимое.

    Пометка жила только в строке списка — и исчезала ровно тогда, когда файл
    открывали и смотрели его дифф (смок 2026-09-10). Фраза одна на оба экрана:
    расходиться этим двум формулировкам не о чем.
    """
    from gc_tui.screens.diff import LEGEND_EXTERNAL

    def header(external):
        return build_viewer_header(
            SOURCE_SESSION, _entry(external=external), full=False, width=160
        )

    assert LEGEND_EXTERNAL in header(True)
    assert LEGEND_EXTERNAL not in header(False)


def test_viewer_header_gives_up_the_screen_word_before_the_external_warning():
    """Порядок жертв: слово экрана и источник уходят РАДИ предупреждения.

    А когда фраза не влезает даже с голым путём — сбрасывается она сама, и
    место тут же возвращается слову экрана: держать шапку урезанной ПОСЛЕ
    отказа от фразы незачем.
    """
    from gc_tui.screens.diff import LEGEND_EXTERNAL, SCREEN_WORD

    def header(external, width):
        return build_viewer_header(
            SOURCE_SESSION,
            _entry("a.py", external=external),
            full=False,
            width=width,
            edit=0,
            edits=1,
        )

    plain, warned = header(False, 300), header(True, 300)
    assert LEGEND_EXTERNAL in warned and len(warned) > len(plain)

    tight = header(True, len(warned) - 1)
    assert LEGEND_EXTERNAL in tight and not tight.startswith(SCREEN_WORD)

    # Ширины хватает ровно на полную шапку БЕЗ фразы — значит фраза уходит,
    # а слово экрана возвращается.
    assert header(True, len(plain)) == plain


def test_viewer_header_shows_where_the_cursor_is_among_edits():
    """Р18: счётчик правок стоит последним — и в режиме «весь файл» тоже."""
    entry = _entry("calc.py", add=27, del_=4)
    left = build_viewer_header(
        SOURCE_SESSION, entry, full=False, width=100, edit=1, edits=4
    )
    assert left == "изменения · сессия · calc.py · +27 −4 · правка 2/4"

    full = build_viewer_header(
        SOURCE_SESSION, entry, full=True, width=100, edit=0, edits=4
    )
    assert (
        full == f"изменения · сессия · calc.py · +27 −4 · {FULL_SUFFIX}"
        " · правка 1/4"
    )
    # Правок нет вовсе (бинарь, «различий нет») — счётчику неоткуда взяться.
    assert "правка" not in build_viewer_header(
        SOURCE_SESSION, entry, full=False, width=100, edit=0, edits=0
    )


def test_viewer_header_marks_inexact_numbers():
    entry = _entry("uv.lock", add=3, del_=1, exact=False)
    left = build_viewer_header(SOURCE_SESSION, entry, full=False, width=100)
    assert f"{APPROX} +3 −1" in left


def test_viewer_header_names_the_accepted_step():
    """`accepted_ts` вставляет расшифровку глифа `↺` сразу после счётчиков —
    между ними и «весь файл»/номером правки (§13 спеки); без него (`None`,
    старые вызовы) шапка не меняется вовсе."""
    entry = _entry("calc.py", add=27, del_=4)
    ts = 1723200000.0
    stamp = time.strftime("%H:%M", time.localtime(ts))

    left = build_viewer_header(
        SOURCE_SESSION, entry, full=False, width=100, accepted_ts=ts
    )
    assert left == (
        f"изменения · сессия · calc.py · +27 −4 · {ACCEPTED_CAPTION_STEM} ({stamp})"
    )
    assert accepted_caption(ts) in left

    full = build_viewer_header(
        SOURCE_SESSION,
        entry,
        full=True,
        width=100,
        accepted_ts=ts,
        edit=0,
        edits=2,
    )
    assert full == (
        f"изменения · сессия · calc.py · +27 −4 · {ACCEPTED_CAPTION_STEM}"
        f" ({stamp}) · {FULL_SUFFIX} · правка 1/2"
    )

    # Без `accepted_ts` (дефолт) — байт-в-байт прежний текст.
    assert (
        build_viewer_header(SOURCE_SESSION, entry, full=False, width=100)
        == "изменения · сессия · calc.py · +27 −4"
    )


def test_viewer_header_names_the_head_step():
    """`head_ref` вставляет расшифровку короткого хэша списка (С9) в тот же
    слот, что и `accepted_ts` (§13) — симметрично, тем же способом (С8
    смоука волны 4): расшифровка сразу после счётчиков, готовая подпись
    коммита («e5f2c1a · 14:20») берётся как есть, без пересчёта."""
    entry = _entry("calc.py", add=27, del_=4)
    ref = "e5f2c1a · 14:20"

    left = build_viewer_header(
        SOURCE_SESSION, entry, full=False, width=120, head_ref=ref
    )
    assert left == (
        f"изменения · сессия · calc.py · +27 −4 · {HEAD_CAPTION_STEM} ({ref})"
    )
    assert head_caption(ref) in left
    assert ACCEPTED_CAPTION_STEM not in left  # ступени взаимоисключающие

    full = build_viewer_header(
        SOURCE_SESSION,
        entry,
        full=True,
        width=120,
        head_ref=ref,
        edit=0,
        edits=2,
    )
    assert full == (
        f"изменения · сессия · calc.py · +27 −4 · {HEAD_CAPTION_STEM}"
        f" ({ref}) · {FULL_SUFFIX} · правка 1/2"
    )

    # Без `head_ref` (дефолт) — байт-в-байт прежний текст.
    assert (
        build_viewer_header(SOURCE_SESSION, entry, full=False, width=100)
        == "изменения · сессия · calc.py · +27 −4"
    )


def test_viewer_header_sacrifices_word_then_source_then_path():
    entry = _entry(
        "src/very/deep/nested/module/screens/engine.py", add=7, del_=2
    )
    full = "изменения · сессия · src/very/deep/nested/module/screens/engine.py · +7 −2"
    assert (
        build_viewer_header(SOURCE_SESSION, entry, full=False, width=len(full))
        == full
    )

    without_word = build_viewer_header(
        SOURCE_SESSION, entry, full=False, width=len(full) - 1
    )
    assert without_word.startswith("сессия · ")

    narrow = build_viewer_header(SOURCE_SESSION, entry, full=False, width=30)
    assert "сессия" not in narrow
    assert "engine.py · +7 −2" in narrow  # имя файла и числа целы
    assert "…" in narrow
    assert len(narrow) <= 30


def test_edit_caption_names_command_channel_only():
    """Правка командой подписана; правка инструментом — норма, её не подписывают."""
    shell = edit_caption(2, _rec(call_id=None, ts=0.0))
    assert shell.startswith("— правка 2 (") and shell.endswith(", команда) —")
    tool = edit_caption(1, _rec(call_id="c1", ts=0.0))
    assert "команда" not in tool
    assert tool.startswith("— правка 1 (") and tool.endswith(") —")


# --- живой вьюер -------------------------------------------------------------


@pytest.mark.asyncio
async def test_enter_opens_viewer_with_cumulative_diff(tmp_path):
    """Enter в списке открывает вьюер; тело — итог «база → диск», не журнал."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\nb\nc\n")
    ws.write("a.py", "a\nB\nc\nd\n")
    ws.journal.append(
        _rec("a.py", ts=1.0, hunks="@@ -1 +1 @@\n-старое\n+новое\n")
    )
    ws.journal.append(
        _rec("a.py", ts=2.0, hunks="@@ -1 +1 @@\n-новое\n+третье\n")
    )

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        viewer = pilot.app.screen
        assert isinstance(viewer, DiffFileViewer)
        body = _body(viewer)
        assert "+B" in body and "+d" in body  # кумулятив базы против диска
        assert "старое" not in body and "третье" not in body
        assert "a.py" in _plain(viewer.query_one("#header-left"))


@pytest.mark.asyncio
async def test_viewer_header_names_the_accepted_step_only_for_the_accepted_file(
    tmp_path,
):
    """Полная фраза глифа `↺` (§13 спеки) — в шапке ЖИВОГО вьюера, открытого
    через `DiffScreen._open_viewer`, и только у файла, чья ступень «Сессии»
    сейчас `.accepted` — не у соседнего активного файла без принятия.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("accepted.py", "было\n")
    ws.accept("accepted.py", "принято\n")
    ws.write("accepted.py", "принято и ещё правка\n")
    ws.journal.append(_rec("accepted.py", ts=1.0))

    ws.base("plain.py", "было\n")
    ws.write("plain.py", "стало\n")
    ws.journal.append(_rec("plain.py", ts=2.0))  # свежее — активных первым

    ts = ws.store.accepted_state("accepted.py").ts

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")  # первый активный — plain.py
        await pilot.pause()
        header = _plain(pilot.app.screen.query_one("#header-left"))
        assert "plain.py" in header
        assert ACCEPTED_CAPTION_STEM not in header

        await pilot.press("right")  # соседний файл — accepted.py
        await pilot.pause()
        header = _plain(pilot.app.screen.query_one("#header-left"))
        assert "accepted.py" in header
        assert accepted_caption(ts) in header


@pytest.mark.asyncio
async def test_viewer_header_names_the_head_step_only_for_the_head_file(
    tmp_path,
):
    """Симметрично `..._only_for_the_accepted_file` (С8): полная подпись
    ступени HEAD — в шапке ЖИВОГО вьюера, и только у файла, чья действующая
    ступень «Сессии» сейчас `head` (тай-брейк Р28: коммит поверх принятого
    состояния), не у соседнего активного файла без коммит-ступени.
    """
    import shutil
    import subprocess

    from gc_tui.app import TuiApp

    if shutil.which("git") is None:
        pytest.skip("git недоступен")

    ws = _Workspace(tmp_path)
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "test"),
    ):
        subprocess.run(
            ["git", *args], cwd=str(ws.root), check=True, capture_output=True
        )
    ws.write("head.py", "v1\n")
    subprocess.run(
        ["git", "add", "--", "head.py"],
        cwd=str(ws.root),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=str(ws.root),
        check=True,
        capture_output=True,
    )
    ws.base("head.py", "v1\n")
    ws.write("head.py", "v2\n")
    ws.journal.append(_rec("head.py", ts=1.0))

    view = EditsView(
        str(ws.threads_dir), "cli", "th1", str(ws.root), as_settings(ws.config)
    )
    view.accept(["head.py"])  # принято состояние "v2\n"
    ws.write("head.py", "v3\n")
    ws.journal.append(_rec("head.py", ts=2.0, call_id="c2"))
    subprocess.run(
        ["git", "add", "--", "head.py"],
        cwd=str(ws.root),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "commit after accept"],
        cwd=str(ws.root),
        check=True,
        capture_output=True,
    )
    ws.write("head.py", "v4\n")  # правка поверх коммита — файл активен

    ws.base("plain.py", "было\n")
    ws.write("plain.py", "стало\n")
    ws.journal.append(
        _rec("plain.py", ts=3.0, call_id="c3")
    )  # свежее — активных первым

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr._view.active[0].entry.path == "plain.py"
        assert scr._view.active[1].step is not None
        assert scr._view.active[1].step.kind == STEP_HEAD

        await pilot.press("enter")  # первый активный — plain.py
        await pilot.pause()
        header = _plain(pilot.app.screen.query_one("#header-left"))
        assert "plain.py" in header
        assert HEAD_CAPTION_STEM not in header

        await pilot.press("right")  # соседний файл — head.py
        await pilot.pause()
        header = _plain(pilot.app.screen.query_one("#header-left"))
        assert "head.py" in header
        assert HEAD_CAPTION_STEM in header


@pytest.mark.asyncio
async def test_viewer_header_counters_are_painted(tmp_path):
    """Числа шапки вьюера — того же цвета, что и в списке, и что гаттер тела."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\nb\nc\n")
    ws.write("a.py", "a\nB\nc\nd\n")  # +2 −1 от базы
    ws.journal.append(_rec("a.py", ts=1.0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        header = viewer.query_one("#header-left").content
        assert span_style(header, "+2") == OK
        assert span_style(header, "−1") == ERR


@pytest.mark.asyncio
async def test_body_frame_reserves_place_and_does_not_cut_the_body(tmp_path):
    """Р16: тело вьюера в НАСТОЯЩЕЙ рамке, и рамка не режет ни первой строки.

    `outline` места не резервирует и рисуется поверх контента — первая и
    последняя строки тела оказывались съедены. `border` место резервирует:
    рамка целая, а тело внутри неё доходит до конца.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    _two_hunk_file(ws)

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(80, 24)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()

        lines = _screen_lines(pilot.app)
        top = [i for i, line in enumerate(lines) if line.startswith("╭")]
        bottom = [i for i, line in enumerate(lines) if line.startswith("╰")]
        assert top and bottom, lines
        # Тело — ВНУТРИ рамки, шапка и футер — снаружи.
        body = [
            line for line in lines[top[0] + 1 : bottom[0]] if line.strip("│ ")
        ]
        assert all(
            line.startswith("│") and line.endswith("│") for line in body
        ), body
        assert any("line01" in line for line in body), (
            body
        )  # первая строка цела


@pytest.mark.asyncio
async def test_body_is_split_into_blocks_with_rich_text(tmp_path):
    """Каждая правка — свой Static с `rich.Text` (контракт выделения мышью)."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    _two_hunk_file(ws)

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        blocks = [widgets[0] for widgets in viewer._edits]
        assert len(blocks) == 2
        for block in blocks:
            # §16: выделять мышью можно только Static с rich.Text.
            assert isinstance(block.content, Text)
            assert block.ALLOW_SELECT is True
        assert "line03 правлено" in _plain(blocks[0])
        assert "line17 правлено" in _plain(blocks[1])


@pytest.mark.asyncio
async def test_line_numbers_present(tmp_path):
    """Номера строк даёт движок — они обязаны доехать до экрана."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    _two_hunk_file(ws)

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        first = _plain(viewer._edits[0][0]).splitlines()[0]
        assert first.lstrip().split(" ")[0].isdigit(), first
        assert "17" in _plain(viewer._edits[1][0])


def _cursor_at(viewer) -> list[int]:
    """Номера правок, помеченных кромкой курсора прямо сейчас."""
    return [
        index
        for index, widgets in enumerate(viewer._edits)
        if all(w.has_class("cursor") for w in widgets)
    ]


@pytest.mark.asyncio
async def test_n_p_move_the_cursor_in_a_circle(tmp_path):
    """Р18: `n`/`p` двигают КУРСОР по правкам, и ход круговой.

    Упор на краю снова делал бы клавишу мёртвой ровно там, где пользователь
    жмёт её чаще всего; в списке файлов `↑`/`↓` давно ходят по кругу.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    _two_hunk_file(ws)

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(80, 10)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        assert viewer._edit_idx == 0
        assert _cursor_at(viewer) == [0]  # курсор виден сразу, без нажатий
        await pilot.press("n")
        await pilot.pause()
        assert viewer._edit_idx == 1 and _cursor_at(viewer) == [1]
        await pilot.press("n")
        await pilot.pause()
        assert viewer._edit_idx == 0  # по кругу, а не упор
        await pilot.press("p")
        await pilot.pause()
        assert viewer._edit_idx == 1  # назад — тоже по кругу
        assert _cursor_at(viewer) == [1]


@pytest.mark.asyncio
async def test_cursor_marks_the_edit_even_when_everything_fits(tmp_path):
    """Р18: отклик есть и на коротком диффе, где прокручивать нечего."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    _two_hunk_file(ws)

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(80, 40)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        trace = viewer.query_one("#trace")
        assert trace.max_scroll_y == 0  # тело влезло целиком
        await pilot.press("n")
        await pilot.pause()
        assert _cursor_at(viewer) == [1]  # видимая реакция всё равно есть
        assert "правка 2/2" in _plain(viewer.query_one("#header-left"))


@pytest.mark.asyncio
async def test_cursor_is_an_accent_edge_and_diff_colours_are_untouched(
    tmp_path,
):
    """Р18: курсор — акцентная кромка слева; цвета диффа не трогаются.

    Колонку под кромку резервирует каждый участок: правка под курсором не
    имеет права съезжать вбок относительно соседних.
    """
    from gc_tui.app import TuiApp
    from gc_tui.palette import ACCENT

    ws = _Workspace(tmp_path)
    _two_hunk_file(ws)

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(80, 40)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        current, other = viewer._edits[0][0], viewer._edits[1][0]
        assert current.styles.border_left[0] == "outer"
        assert current.styles.border_left[1].hex.upper() == ACCENT.upper()
        # Кромка соседа — цвета фона: место занято, полосы не видно.
        assert other.styles.border_left[0] == "outer"
        assert other.styles.border_left[1] != current.styles.border_left[1]
        # Обе правки стоят на одной вертикали.
        assert current.content_region.x == other.content_region.x
        # Раскраска тела — движка, а не курсора: текст спанов не переписан.
        assert "-line03" in _plain(current)


@pytest.mark.asyncio
async def test_full_file_mode_still_walks_edits(tmp_path):
    """Р18: «весь файл» — не один блок; `n`/`p` прыгают по правкам и там."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    _two_hunk_file(ws)

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(80, 10)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        assert viewer.full is True
        assert len(viewer._edits) == 2  # правки те же, что и без контекста
        header = _plain(viewer.query_one("#header-left"))
        assert f"{FULL_SUFFIX} · правка 1/2" in header
        assert "n след. правка" in _plain(viewer.query_one("#footer"))

        await pilot.press("n")
        await pilot.pause()
        assert _cursor_at(viewer) == [1]
        assert "правка 2/2" in _plain(viewer.query_one("#header-left"))


@pytest.mark.asyncio
async def test_cursor_survives_the_full_file_toggle(tmp_path):
    """Переключение `f` не теряет место чтения: правки в обоих режимах те же."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    _two_hunk_file(ws)

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(80, 20)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert viewer._edit_idx == 1
        await pilot.press("f")
        await pilot.pause()
        assert viewer._edit_idx == 1 and _cursor_at(viewer) == [1]


@pytest.mark.asyncio
async def test_switching_files_recounts_the_edits_in_the_header(tmp_path):
    """Счётчик правок обязан говорить о ТЕКУЩЕМ файле, а не о предыдущем.

    Шапка собирается из уже пересобранного тела: у соседнего файла правок может
    быть другое число, и «правка 1/2» над файлом с одной правкой было бы враньём
    ровно того же рода, что счётчики от другого источника.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    _two_hunk_file(ws, "two.py")  # две правки
    ws.base("one.py", "a\nb\n")
    ws.write("one.py", "a\nB\n")  # одна правка
    ws.journal.append(_rec("one.py", ts=2.0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(80, 24)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        assert viewer.entry.path == "one.py"
        assert "правка 1/1" in _plain(viewer.query_one("#header-left"))
        assert "n след. правка" not in _plain(viewer.query_one("#footer"))

        await pilot.press("right")
        await pilot.pause()
        assert viewer.entry.path == "two.py"
        assert "правка 1/2" in _plain(viewer.query_one("#header-left"))
        assert "n след. правка" in _plain(viewer.query_one("#footer"))

        await pilot.press("left")
        await pilot.pause()
        assert "правка 1/1" in _plain(viewer.query_one("#header-left"))


@pytest.mark.asyncio
async def test_cursor_walks_journal_edits_when_there_is_no_exact_baseline(
    tmp_path,
):
    """Без точной базы правка — запись журнала: курсор метит подпись и тело."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "a\nb\nc\n")
    for number in range(2):
        ws.journal.append(
            _rec(
                "a.py",
                call_id=None,
                op="shell",
                ts=float(number + 1),
                hunks=f"@@ -{number + 1} +{number + 1} @@\n-old{number}\n+new{number}\n",
            )
        )

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(80, 24)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        assert viewer.entry.exact is False
        assert len(viewer._edits) == 2
        assert len(viewer._edits[0]) == 2  # подпись и тело — одна правка
        assert "— правка 1" in _plain(viewer._edits[0][0])
        assert "правка 1/2" in _plain(viewer.query_one("#header-left"))

        await pilot.press("n")
        await pilot.pause()
        assert _cursor_at(viewer) == [1]
        assert "правка 2/2" in _plain(viewer.query_one("#header-left"))


@pytest.mark.asyncio
async def test_single_edit_file_says_one_of_one_and_drops_np(tmp_path):
    """Правка одна — счётчик её не скрывает, а `n`/`p` из футера уходят."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\nb\n")
    ws.write("a.py", "a\nB\n")
    ws.journal.append(_rec("a.py", ts=1.0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(80, 24)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        assert len(viewer._edits) == 1
        assert "правка 1/1" in _plain(viewer.query_one("#header-left"))
        footer = _plain(viewer.query_one("#footer"))
        assert "n след. правка" not in footer
        assert _cursor_at(viewer) == [0]


@pytest.mark.asyncio
async def test_cursor_scrolls_the_frame_only_when_the_edit_is_out_of_it(
    tmp_path,
):
    """Прокрутка — следствие: кадр едет к курсору, только если правка вне его."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    _two_hunk_file(ws)

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(80, 10)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        trace = viewer.query_one("#trace")
        assert trace.scroll_offset.y == 0
        await pilot.press("n")  # вторая правка ниже сгиба
        await pilot.pause()
        await pilot.pause()
        moved = trace.scroll_offset.y
        assert moved > 0
        # Курсор на месте — кадр не дёргается повторно.
        viewer._reveal_cursor()
        await pilot.pause()
        assert trace.scroll_offset.y == moved


@pytest.mark.asyncio
async def test_left_right_switch_files_without_pushing_screen(tmp_path):
    """`←`/`→` перестраивают тот же экран, стек не растёт."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\n")
    ws.write("a.py", "a\nb\n")
    ws.write("b.py", "one\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.journal.append(_rec("b.py", ts=2.0, op="create", add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        depth = len(pilot.app.screen_stack)
        assert "b.py" in _plain(
            viewer.query_one("#header-left")
        )  # свежая первой

        await pilot.press("right")
        await pilot.pause()
        assert pilot.app.screen is viewer
        assert len(pilot.app.screen_stack) == depth
        assert viewer.index == 1
        assert "a.py" in _plain(viewer.query_one("#header-left"))
        assert "+b" in _body(viewer)

        await pilot.press("left")
        await pilot.pause()
        assert viewer.index == 0
        assert "b.py" in _plain(viewer.query_one("#header-left"))


@pytest.mark.asyncio
async def test_next_file_starts_from_the_top(tmp_path):
    """Соседний файл читают с начала, а не с чужой позиции прокрутки."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for name in ("one.py", "two.py"):
        ws.base(name, "\n".join(f"line{n:03d}" for n in range(200)) + "\n")
        ws.write(name, "\n".join(f"LINE{n:03d}" for n in range(200)) + "\n")
    ws.journal.append(_rec("one.py", ts=2.0))
    ws.journal.append(_rec("two.py", ts=1.0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(70, 20)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        trace = viewer.query_one("#trace")
        trace.scroll_end(animate=False)
        await pilot.pause()
        assert trace.scroll_offset.y > 0

        await pilot.press("right")
        await pilot.pause()
        assert viewer.entry.path == "two.py"
        assert trace.scroll_offset.y == 0


@pytest.mark.asyncio
async def test_viewer_index_returns_to_list_cursor(tmp_path):
    """Список встаёт на тот файл, на котором вьюер закрыли."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(3):
        ws.write(f"f{idx}.py", "x\n")
        ws.journal.append(
            _rec(f"f{idx}.py", ts=float(idx), op="create", add=1, del_=0)
        )

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen is scr
        assert scr._selected_idx == 1
        assert scr._rows[1].has_class("selected")


@pytest.mark.asyncio
async def test_f_toggles_full_file_in_place(tmp_path):
    """`f` — свап на месте: тело, шапка и подпись клавиши меняются вместе."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    _two_hunk_file(ws)

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        depth = len(pilot.app.screen_stack)
        assert "line10" not in _body(viewer)  # далёкая строка вне контекста
        assert FULL_SUFFIX not in _plain(viewer.query_one("#header-left"))

        await pilot.press("f")
        await pilot.pause()
        assert pilot.app.screen is viewer
        assert len(pilot.app.screen_stack) == depth
        body = _body(viewer)
        assert "line10" in body and "line20" in body
        # Р18: «весь файл» — не один блок, а участки «контекст — правка —
        # контекст»; правок в нём столько же, сколько в режиме изменений.
        assert len(viewer._edits) == 2
        assert len(viewer.query("#trace > Static")) > len(viewer._edits)
        assert FULL_SUFFIX in _plain(viewer.query_one("#header-left"))
        assert "f только изменения" in _plain(viewer.query_one("#footer"))

        await pilot.press("f")
        await pilot.pause()
        assert "line10" not in _body(viewer)
        assert FULL_SUFFIX not in _plain(viewer.query_one("#header-left"))
        assert "f весь файл" in _plain(viewer.query_one("#footer"))


@pytest.mark.asyncio
async def test_inexact_source_shows_note_and_journal_edits(tmp_path):
    """Точной базы нет — тело собрано из отдельных правок, каждая подписана."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "a\nb\n")
    ws.journal.append(
        _rec(
            "a.py",
            call_id=None,
            op="shell",
            ts=1.0,
            add=1,
            del_=0,
            hunks="diff --git a/a.py b/a.py\n@@ -1 +1,2 @@\n a\n+первая\n",
        )
    )
    ws.journal.append(
        _rec(
            "a.py",
            call_id=None,
            op="shell",
            ts=2.0,
            add=1,
            del_=0,
            hunks="@@ -2 +2,2 @@\n первая\n+вторая\n",
        )
    )

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        assert viewer.entry.exact is False
        body = _body(viewer)
        assert VIEWER_NOTE_INEXACT_SHELL in body  # обе правки — командами
        assert "— правка 1 (" in body and "— правка 2 (" in body
        assert body.index("— правка 1 (") < body.index("— правка 2 (")
        assert "команда" in body
        assert "+первая" in body and "+вторая" in body


@pytest.mark.asyncio
async def test_inexact_note_drops_commands_when_a_tool_edit_is_there(tmp_path):
    """Снимок вытеснила эвикция: правка инструментом под нотой «командами» врала бы."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "a\nb\n")
    ws.journal.append(
        _rec(
            "a.py",
            call_id=None,
            op="shell",
            ts=1.0,
            add=1,
            del_=0,
            hunks="@@ -1 +1,2 @@\n a\n+командой\n",
        )
    )
    ws.journal.append(
        _rec(  # правка тула, снимка нет
            "a.py",
            call_id="c9",
            op="edit",
            ts=2.0,
            add=1,
            del_=0,
            hunks="@@ -2 +2,2 @@\n командой\n+тулом\n",
        )
    )

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        body = _body(viewer)
        assert viewer.entry.exact is False
        assert VIEWER_NOTE_INEXACT in body
        assert VIEWER_NOTE_INEXACT_SHELL not in body
        # подпись канала осталась пофайловой правдой: команда названа, тул нет
        assert body.count(f", {'команда'})") == 1


@pytest.mark.asyncio
async def test_viewer_reads_turn_source(tmp_path):
    """Источник «Ход» — то же тело, но по записям последнего хода."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("old.py", "a\n")
    ws.write("old.py", "a\nb\n")
    ws.journal.append(_rec("old.py", turn="t1", ts=1.0))
    ws.store.capture_turn("new.py", b"one\n", "t2")
    ws.write("new.py", "one\ntwo\n")
    ws.journal.append(_rec("new.py", turn="t2", ts=2.0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        viewer = ws.viewer(SOURCE_TURN)
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        assert [entry.path for entry in viewer._files] == ["new.py"]
        header = _plain(viewer.query_one("#header-left"))
        assert "последний ход с правками" in header and "new.py" in header
        assert "+two" in _body(viewer)
        # файл в ходе один — листать некуда
        assert "←/→" not in _plain(viewer.query_one("#footer"))


@pytest.mark.asyncio
async def test_f_is_dead_without_exact_baseline(tmp_path):
    """Полного файла в записях журнала нет — `f` не врёт шапкой и не значится."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "a\nb\n")
    ws.journal.append(
        _rec(
            "a.py",
            call_id=None,
            op="shell",
            ts=1.0,
            add=1,
            del_=0,
            hunks="@@ -1 +1,2 @@\n a\n+b\n",
        )
    )

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        before = _body(viewer)
        assert "f " not in _plain(viewer.query_one("#footer"))

        await pilot.press("f")
        await pilot.pause()
        assert viewer.full is False
        assert _body(viewer) == before
        assert FULL_SUFFIX not in _plain(viewer.query_one("#header-left"))


@pytest.mark.asyncio
async def test_viewer_hides_r_for_a_file_without_a_reliable_base(tmp_path):
    """Е5: вьюер гасит `r`, когда у ОТКРЫТОГО файла нет достоверной базы —
    тот же признак, что гасит `r` в списке (Д10), а не факт подключённого
    колбэка `on_revert`. Раньше вьюер спрашивал только колбэк
    (`can_revert=self._on_revert is not None`) и рекламировал `r` даже там,
    где список его уже спрятал — нажатие отвечало «откат недоступен»."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("norev.py", "a\n")
    ws.journal.append(_rec("norev.py", ts=2.0))  # правка без снимка сессии
    ws.base("rev.py", "a\n")
    ws.write("rev.py", "a\nb\n")
    ws.journal.append(_rec("rev.py", ts=1.0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert [e.path for e in scr._entries] == ["norev.py", "rev.py"]
        assert scr._entries[0].revertable is False
        assert scr._entries[1].revertable is True

        await pilot.press("enter")  # вьюер на norev.py
        await pilot.pause()
        assert "r откат" not in _plain(pilot.app.screen.query_one("#footer"))

        await pilot.press("right")  # соседний файл — с базой
        await pilot.pause()
        assert pilot.app.screen.entry.path == "rev.py"
        assert "r откат" in _plain(pilot.app.screen.query_one("#footer"))


def test_can_revert_does_not_raise_when_the_flags_list_is_shorter_than_files():
    """Ревью фикса Е5: `_can_revert_flags` может оказаться короче `files` —
    тот же случай, что и `_reset_flags` у `_can_reset` («чужой конструктор мог
    передать короче»). Без сверки индекса с длиной списка это был бы
    необработанный `IndexError` прямо в рендере футера (`_render_chrome` →
    `viewer_footer(..., can_revert=self._can_revert())`)."""
    viewer = DiffFileViewer(
        view=None,
        source=SOURCE_SESSION,
        files=[_entry("a.py"), _entry("b.py")],
        index=1,  # вне границ одноэлементного списка
        on_revert=lambda path: None,
        can_revert=[True],
    )
    assert viewer._can_revert() is False


@pytest.mark.asyncio
async def test_uncountable_file_says_preview_is_unavailable(tmp_path):
    """Бинарь/файл сверх капа: под шапкой строка-объяснение, а не пустота."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("logo.png", "\x00\x01")
    ws.journal.append(_rec("logo.png", op="binary", ts=1.0, add=0, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        assert viewer.entry.countable is False
        assert _body(viewer) == VIEWER_NO_PREVIEW
        assert viewer._edits == []


@pytest.mark.asyncio
async def test_clipboard_source_cite_is_diff_path(tmp_path):
    """Копирование с вьюера цитирует источник: `("diff", путь)`."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\n")
    ws.write("a.py", "a\nb\n")
    ws.write("b.py", "one\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.journal.append(_rec("b.py", ts=2.0, op="create", add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        assert viewer.clipboard_source_cite() == ("diff", "b.py")
        await pilot.press("right")
        await pilot.pause()
        assert viewer.clipboard_source_cite() == ("diff", "a.py")


@pytest.mark.asyncio
async def test_resize_rebuilds_body_only_on_width_change(tmp_path):
    """Resize приходит и на смену высоты — тело зависит только от ширины."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    _two_hunk_file(ws)

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(80, 24)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()

        calls: list = []
        original = viewer._view.file_diff

        def _counted(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)

        viewer._view.file_diff = _counted

        await pilot.resize_terminal(80, 30)  # только высота
        await pilot.pause()
        assert calls == []

        await pilot.resize_terminal(60, 30)  # ширина
        await pilot.pause()
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_body_is_cached_per_file_mode_and_width(tmp_path):
    """Возврат к уже показанному телу второй раз его не считает.

    «Весь файл» на крупном файле — самая дорогая отрисовка экрана, а
    возвращаются к ней постоянно: тогл `f`, соседний файл и обратно.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    _two_hunk_file(ws)

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(80, 24)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()

        calls: list = []
        original = viewer._view.file_diff

        def _counted(*args, **kwargs):
            calls.append(kwargs.get("full"))
            return original(*args, **kwargs)

        viewer._view.file_diff = _counted

        await pilot.press("f")  # «весь файл» считается впервые
        await pilot.pause()
        assert calls == [True]

        await pilot.press("f")  # обратно — тело уже посчитано
        await pilot.pause()
        await pilot.press("f")  # и снова «весь файл»
        await pilot.pause()
        assert calls == [True]
        assert viewer.full is True
        assert "line10" in _body(viewer)  # из кэша пришло именно полное тело


@pytest.mark.asyncio
async def test_filled_lines_reach_right_edge_minus_scrollbar(tmp_path):
    """Заливка тянется до края тела, но не под колонку скроллбара.

    `size.width` колонку скроллбара не вычитает (та же ловушка, что у сетки
    списка): без резерва заливка уезжала бы под сам скроллбар. Кромку курсора
    (`EDIT_EDGE`) вычитать обязательно — на неё уходят две колонки участка.
    """
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import EDIT_EDGE, SCROLLBAR_COLUMN

    ws = _Workspace(tmp_path)
    _two_hunk_file(ws)

    def _filled(viewer: DiffFileViewer) -> set[int]:
        return {
            len(line)
            for line in _plain(viewer._edits[0][0]).splitlines()
            if line.lstrip().split(" ", 1)[-1].startswith(("+", "-"))
        }

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(70, 20)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        body = viewer.query_one("#trace")
        room = body.size.width - SCROLLBAR_COLUMN - EDIT_EDGE
        assert _filled(viewer) == {room}

        # Ширину меряем ПОСЛЕ раскладки: экран получает Resize раньше, чем её
        # примет `#trace`, и тело собиралось на предыдущую ширину.
        await pilot.resize_terminal(90, 20)
        await pilot.pause()
        assert _filled(viewer) == {
            body.size.width - SCROLLBAR_COLUMN - EDIT_EDGE
        }


@pytest.mark.asyncio
async def test_long_body_line_is_cropped_not_wrapped(tmp_path):
    """Строка кода шире тела режется, а не переносится: иначе поедут номера."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\n")
    ws.write("a.py", "a\n" + "x" * 200 + "\n")
    ws.journal.append(_rec("a.py", ts=1.0, add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(60, 24)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        block = viewer._edits[0][0]
        assert block.size.height == len(_plain(block).splitlines())


@pytest.mark.asyncio
async def test_long_line_is_reachable_by_horizontal_scrolling(tmp_path):
    """Хвост длинной строки достижим прокруткой вбок.

    Переносить здесь нельзя: на неизменной высоте участка стоит вся ленивая
    сборка (высота известна ДО отрисовки и равна числу строк). Поэтому строка
    не переносится, а полотно прокручивается — как в git-пейджере и VS Code.
    """
    from gc_tui.app import TuiApp
    from gc_tui.widgets.lazy_body import LazyBody

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\n")
    ws.write("a.py", "a\n" + "x" * 200 + "\n")
    ws.journal.append(_rec("a.py", ts=1.0, add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(60, 24)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        body = viewer.query_one("#trace", LazyBody)
        assert body.max_scroll_x > 0, "полотно не прокручивается вбок"
        # Клавиша названа, только пока жива — общее правило экрана.
        from textual.widgets import Static as _Static

        assert "Ctrl+←/→" in str(viewer.query_one("#footer", _Static).render())

        await pilot.press("ctrl+right")
        await pilot.pause()
        assert body.scroll_offset.x > 0, "Ctrl+вправо кадр не сдвинул"

        await pilot.press("ctrl+left")
        await pilot.pause()
        assert body.scroll_offset.x == 0


@pytest.mark.asyncio
async def test_double_click_row_opens_viewer(tmp_path):
    """Двойной клик по строке списка — тот же жест «открыть», что и Enter."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(2):
        ws.write(f"f{idx}.py", "x\n")
        ws.journal.append(
            _rec(f"f{idx}.py", ts=float(idx), op="create", add=1, del_=0)
        )

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = ws.screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.double_click(scr._rows[1])
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, DiffFileViewer)
        assert viewer.index == 1


# --- ленивая сборка тела (Р23) ------------------------------------------------


def _rewritten(ws: _Workspace, rel: str = "big.py", lines: int = 300) -> None:
    """Файл, переписанный ЦЕЛИКОМ: одна правка длиной во весь файл."""
    ws.base(rel, "".join(f"line{n:04d}\n" for n in range(lines)))
    ws.write(rel, "".join(f"LINE{n:04d}\n" for n in range(lines)))
    ws.journal.append(_rec(rel, add=lines, del_=lines))


def test_a_single_huge_edit_is_cut_into_parts_of_one_number():
    """Правка длиной в файл режется — но остаётся ОДНОЙ правкой (Р23).

    Файл, переписанный целиком, — это одна правка в тысячи строк: неразрезанной
    она давала бы вьюеру один `Static` на всё тело, тот самый фриз, ради
    которого тело и режется. Куски несут один её номер, поэтому курсор метит их
    разом и кромка остаётся непрерывной, а счётчик правок не превращает один
    файл в сотню «правок».
    """
    from gc_tui.screens.diff import CONTEXT_CHUNK

    body = "".join(f"-old{n}\n" for n in range(250)) + "".join(
        f"+new{n}\n" for n in range(250)
    )
    parts = split_body(
        "@@ -1,250 +1,250 @@\n" + body, path="a.py", fill_width=None
    )

    assert len(parts) == 5  # 500 строк / CONTEXT_CHUNK
    assert {part.edit for part in parts} == {0}  # и все они — одна правка
    assert all(part.lines == CONTEXT_CHUNK for part in parts)


@pytest.mark.asyncio
async def test_body_is_built_by_the_frame_not_all_at_once(tmp_path):
    """Место под тело есть всё, а собрано — окно вокруг кадра (Р23).

    Открытие большого файла не должно стоить всего файла: участки уходят в
    полотно местами известной высоты, и полоса прокрутки честна с первого
    кадра, а рисуются те, что видно.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    _rewritten(ws)

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(90, 20)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        trace = viewer.query_one("#trace")
        blocks = list(viewer.query("#trace > Static"))

        assert len(blocks) == 6  # 600 строк тела
        assert trace.virtual_size.height == 600  # место — под всё тело
        built = [w for w in blocks if _plain(w)]
        assert 0 < len(built) < len(blocks)
        assert "-line0000" in _plain(blocks[0])

        # Хвост подъезжает прокруткой (она плавная — ждём наблюдаемое, не такт).
        await pilot.press("end")
        await wait_until(
            pilot,
            lambda: "+LINE0299" in _body(viewer),
            "последняя строка файла собралась",
        )


@pytest.mark.asyncio
async def test_cursor_reaches_an_edit_that_was_not_built_yet(tmp_path):
    """`n` к далёкой правке: сначала собрать участок, потом двигать кадр (Р23)."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("big.py", "".join(f"line{n:04d}\n" for n in range(1200)))
    ws.write(
        "big.py",
        "".join(
            f"{'LINE' if n in (1, 1198) else 'line'}{n:04d}\n"
            for n in range(1200)
        ),
    )
    ws.journal.append(_rec("big.py", add=2, del_=2))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(90, 20)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        await pilot.press("f")  # весь файл: правки далеко друг от друга
        await pilot.pause()
        far = viewer._edits[1][0]
        assert not _plain(far)  # вторая правка ещё не собрана

        await pilot.press("n")
        await pilot.pause()

        assert "+LINE1198" in _plain(far)  # собрана
        assert far.has_class("cursor")  # и помечена курсором
        assert "правка 2/2" in _plain(viewer.query_one("#header-left"))


@pytest.mark.asyncio
async def test_selection_crosses_the_border_between_parts(tmp_path):
    """Протяжка мышью через границу участков берёт текст обоих (Р23).

    Тело разрезано на участки ради раскладки, а не ради пользователя: выделение
    обязано этого не замечать.
    """
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import CONTEXT_CHUNK
    from textual.events import MouseDown, MouseMove, MouseUp

    ws = _Workspace(tmp_path)
    _rewritten(ws)

    def _mouse(screen, x, y, button=0):
        return dict(
            widget=screen,
            x=x,
            y=y,
            delta_x=0,
            delta_y=0,
            button=button,
            shift=False,
            meta=False,
            ctrl=False,
            screen_x=x,
            screen_y=y,
        )

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(100, 30)) as pilot:
        viewer = ws.viewer()
        await pilot.app.push_screen(viewer)
        await pilot.pause()
        trace = viewer.query_one("#trace")
        # Граница первого и второго участка — в середине кадра.
        trace.scroll_to(y=CONTEXT_CHUNK - 5, animate=False)
        await pilot.pause()

        screen = pilot.app.screen
        screen._forward_event(MouseDown(**_mouse(screen, 6, 4)))
        await pilot.pause()
        screen._forward_event(MouseMove(**_mouse(screen, 40, 16, button=1)))
        await pilot.pause()
        screen._forward_event(MouseUp(**_mouse(screen, 40, 16)))
        await pilot.pause()

        assert len(screen.selections) == 2  # оба участка
        selected = screen.get_selected_text().splitlines()
        assert len(selected) > 10
        assert any("line0096" in line for line in selected)  # до границы
        assert any("line0100" in line for line in selected)  # и после неё
