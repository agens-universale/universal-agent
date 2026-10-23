"""Инлайн-дифф в ленте трейса: DiffBlock под строкой своего файлового тула."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("textual")

from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.thread_store import edits_journal_path
from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.widgets.diff_block import DiffBlock
from gc_tui.widgets.tool_call_row import ToolCallRow
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from gc_tui.palette import ERR, OK
from gc_tui.services.diff_render import copyable_hunks

SHORT_HUNKS = (
    "--- \n+++ \n@@ -1,3 +1,3 @@\n def f():\n-    return 1\n+    return 2\n"
)


def _long_hunks(count: int = 30) -> str:
    lines = [f"@@ -1,{count} +1,{count} @@"]
    lines += [f"+line {i}" for i in range(count)]
    return "\n".join(lines) + "\n"


def _call(call_id: str, name: str, args: dict) -> dict:
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(args),
    }


def _output(call_id: str, name: str) -> dict:
    return {
        "id": f"fco_{call_id}",
        "type": "function_call_output",
        "call_id": call_id,
        "name": name,
        "output": json.dumps({"tool": name, "ok": True}),
    }


def _write_thread(tmp_path, tid: str, payloads: list[dict]) -> None:
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "status": "stopped",
                "updated_at": "2026-08-10T12:00:00+00:00",
                "messages_count": len(payloads),
            }
        ),
        encoding="utf-8",
    )
    lines = [
        json.dumps(
            {
                "type": "message",
                "seq": i,
                "ts_utc": "2026-08-10T12:00:00+00:00",
                "payload": p,
            }
        )
        for i, p in enumerate(payloads, start=1)
    ]
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _record(
    call_id: str,
    *,
    path: str = "src/app.py",
    hunks: str = SHORT_HUNKS,
    add: int = 1,
    del_: int = 1,
) -> EditRecord:
    return EditRecord(
        path=path,
        turn_id="t1",
        ts=1.0,
        call_id=call_id,
        op="edit",
        hunks=hunks,
        add=add,
        del_=del_,
        before_sha256=None,
        after_sha256=None,
        flags=[],
    )


def _write_journal(tmp_path, tid: str, records: list[EditRecord]) -> None:
    journal = EditsJournal(edits_journal_path(str(tmp_path), "cli", tid))
    for rec in records:
        journal.append(rec)


@asynccontextmanager
async def _pilot(tmp_path, tid: str, config: dict | None = None):
    cfg = {"agent.model": "M"}
    if config:
        cfg.update(config)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    async with app.run_test(size=(100, 40)) as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)
        yield pilot


@asynccontextmanager
async def _screen(tmp_path, tid: str, config: dict | None = None):
    async with _pilot(tmp_path, tid, config) as pilot:
        yield pilot.app.screen


def _trace_children(scr) -> list:
    return list(scr.query_one("#trace", VerticalScroll).children)


def _body_lines(block: DiffBlock) -> int:
    return block.query_one(".block-body", Static).visual.plain.count("\n") + 1


def _capture_copies(monkeypatch) -> list[str]:
    """Что ушло в буфер через экран ленты (`on_copy_block_request`)."""
    copied: list[str] = []
    monkeypatch.setattr(
        "gc_tui.screens.main.copy_out",
        lambda _app, payload, **_kw: copied.append(payload),
    )
    return copied


def _row_content(row: ToolCallRow) -> Text:
    return row._render_content()


def _span_color(text: Text, fragment: str) -> str:
    """Цвет фрагмента строки — по спану, который начинается ровно на нём."""
    start = text.plain.index(fragment)
    span = next(s for s in text.spans if s.start == start)
    return str(span.style)


def _path(block: DiffBlock) -> str:
    """Файл блока — из записи журнала: в шапке его больше нет (он в строке тула)."""
    return block._record.path


@pytest.mark.asyncio
async def test_diff_block_mounted_right_after_its_tool_row(tmp_path):
    """Результат файлового тула с записью в журнале → блок сразу под строкой."""
    tid = "edited"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
            _output("c1", "fs_apply_file_edits"),
        ],
    )
    _write_journal(tmp_path, tid, [_record("c1")])

    async with _screen(tmp_path, tid) as scr:
        children = _trace_children(scr)
        rows = [
            i for i, w in enumerate(children) if isinstance(w, ToolCallRow)
        ]
        blocks = [
            i for i, w in enumerate(children) if isinstance(w, DiffBlock)
        ]
        assert len(rows) == 1 and len(blocks) == 1
        assert blocks[0] == rows[0] + 1  # непосредственно после своей строки
        assert not children[blocks[0]].query(".block-label")  # ветки нет


@pytest.mark.asyncio
async def test_edit_counts_live_in_call_row(tmp_path):
    """Счётчики правки — в строке вызова после объекта: `+N` цветом OK,
    `−M` — ERR; строки `└ изменено:` под вызовом больше нет. Блок правок
    (P4 §6.2): глагол по результату и счётчики в скобках `(+a −r)`."""
    tid = "counts"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
            _output("c1", "fs_apply_file_edits"),
        ],
    )
    _write_journal(tmp_path, tid, [_record("c1", add=12, del_=3)])

    async with _screen(tmp_path, tid) as scr:
        content = _row_content(scr.query_one(ToolCallRow))
        assert content.plain == "Edited src/app.py (+12 −3)"
        assert _span_color(content, "+12") == OK
        assert _span_color(content, "−3") == ERR
        assert not any("изменено:" in s for s in _screen_lines(scr))


@pytest.mark.asyncio
async def test_copy_button_ends_the_call_row(tmp_path, monkeypatch):
    """Кнопка ❐ — в конце строки вызова, справа, в той же строке экрана;
    копирует весь дифф правки (хэнки с именем файла)."""
    tid = "copybtn"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
            _output("c1", "fs_apply_file_edits"),
        ],
    )
    _write_journal(tmp_path, tid, [_record("c1")])

    async with _pilot(tmp_path, tid) as pilot:
        scr = pilot.app.screen
        copied = _capture_copies(monkeypatch)
        row = scr.query_one(ToolCallRow)
        button = row.query_one(".block-copy", Static)
        assert button.display
        assert button.region.y == row.region.y
        assert button.region.right == row.region.right
        await pilot.click(button)
        await pilot.pause()
        assert copied == [copyable_hunks(SHORT_HUNKS, path="src/app.py")]


@pytest.mark.asyncio
async def test_block_is_visually_tied_to_its_tool_row(tmp_path):
    """Блок прижат к своей строке инструмента: без отступов сверху и снизу —
    зазор до следующего события даёт верхний отступ самого события."""
    tid = "tied"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
            _output("c1", "fs_apply_file_edits"),
        ],
    )
    _write_journal(tmp_path, tid, [_record("c1")])

    async with _screen(tmp_path, tid) as scr:
        block = scr.query_one(DiffBlock)
        assert block.styles.margin.top == 0
        assert block.styles.margin.bottom == 0
        assert block.styles.border_left[0] == ""  # линии-поводка нет


def _screen_lines(scr) -> list[str]:
    """Текстовый слепок экрана: строки композитора без хвостовых пробелов."""
    return [
        "".join(seg.text for seg in strip).rstrip()
        for strip in scr.app.screen._compositor.render_strips()
    ]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


@pytest.mark.asyncio
async def test_body_right_under_tool_row(tmp_path):
    """Тело диффа — сразу под строкой инструмента, на +2 от её глифа (номер
    строки под блоком прижат вправо к третьей клетке колонки диффа, P4
    §6.2); перед следующим инструментом — пустая строка."""
    tid = "ladder"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
            _output("c1", "fs_apply_file_edits"),
            _call("c2", "run_python_code", {"code": "print(1)"}),
            _output("c2", "run_python_code"),
        ],
    )
    _write_journal(tmp_path, tid, [_record("c1")])

    async with _screen(tmp_path, tid) as scr:
        lines = _screen_lines(scr)
        tool = next(i for i, s in enumerate(lines) if "Edited src/app.py" in s)
        # Свёрнутое превью — только изменённые строки: первой идёт `-`.
        body = next(i for i, s in enumerate(lines) if "return 1" in s)
        next_tool = next(i for i, s in enumerate(lines) if "Ran  " in s)

        assert body == tool + 1
        diff = scr.query_one(DiffBlock)
        row = scr.query_one(ToolCallRow)
        assert diff.region.x - row.region.x == 2
        assert lines[body][diff.region.x : diff.region.x + 3] == "  2"
        assert lines[next_tool - 1] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [70, 120])
async def test_row_fill_reaches_the_block_edge(tmp_path, width):
    """Заливка добитых строк доходит до самого правого края подложки на любой
    ширине терминала (узкой и широкой).

    Полоса правки — ровный прямоугольник: слева она начинается с колонки
    номеров, справа доходит до края. Поле в один символ было только справа и
    читалось как недорисованность."""
    tid = f"fill{width}"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
            _output("c1", "fs_apply_file_edits"),
        ],
    )
    _write_journal(tmp_path, tid, [_record("c1")])

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(width, 30)) as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)
        block = pilot.app.screen.query_one(DiffBlock)
        rows = block.query_one(".block-body", Static).visual.plain.split("\n")
        assert len(rows[0]) == len(rows[1]) == block.size.width


@pytest.mark.asyncio
async def test_no_block_without_journal_record(tmp_path):
    """Файловый тул, но записи в журнале нет → блока нет (журнала тоже нет)."""
    tid = "nojournal"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_write_file", {"path": "src/app.py"}),
            _output("c1", "fs_write_file"),
        ],
    )

    async with _screen(tmp_path, tid) as scr:
        assert len(scr.query(ToolCallRow)) == 1
        assert not scr.query(DiffBlock)


@pytest.mark.asyncio
async def test_no_block_for_non_file_tool(tmp_path):
    """Не-файловый тул: в журнале записи нет — блок не монтируется."""
    tid = "readonly"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "покажи"},
            _call("c1", "fs_read_file", {"path": "src/app.py"}),
            _output("c1", "fs_read_file"),
        ],
    )
    _write_journal(tmp_path, tid, [_record("other")])

    async with _screen(tmp_path, tid) as scr:
        # Чтение — член блока `Explored` (P4): строка вызова одна.
        assert len(_call_rows(scr)) == 1
        assert not scr.query(DiffBlock)


def _call_rows(scr) -> list[ToolCallRow]:
    """Строки вызовов без заголовков блоков (P4)."""
    return [r for r in scr.query(ToolCallRow) if r.mode != "header"]


@pytest.mark.asyncio
async def test_two_edits_each_get_own_block(tmp_path):
    """Две правки подряд → два блока, каждый под своей строкой."""
    tid = "two_edits"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_apply_file_edits", {"path": "a.py"}),
            _output("c1", "fs_apply_file_edits"),
            _call("c2", "fs_write_file", {"path": "b.py"}),
            _output("c2", "fs_write_file"),
        ],
    )
    _write_journal(
        tmp_path,
        tid,
        [
            _record("c1", path="a.py"),
            _record("c2", path="b.py"),
        ],
    )

    async with _screen(tmp_path, tid) as scr:
        children = _trace_children(scr)
        kinds = [
            ("row", w)
            if isinstance(w, ToolCallRow) and w.mode != "header"
            else ("block", w)
            if isinstance(w, DiffBlock)
            else ("other", w)
            for w in children
        ]
        seq = [(k, w) for k, w in kinds if k != "other"]
        assert [k for k, _ in seq] == ["row", "block", "row", "block"]
        assert [_path(w) for k, w in seq if k == "block"] == ["a.py", "b.py"]


@pytest.mark.asyncio
async def test_batch_block_lands_under_own_row_not_at_the_end(tmp_path):
    """Batch (обе строки уже смонтированы) → блок встаёт между ними, не в конец."""
    tid = "batch"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_apply_file_edits", {"path": "a.py"}),
            _call("c2", "fs_write_file", {"path": "b.py"}),
            _output("c1", "fs_apply_file_edits"),
            _output("c2", "fs_write_file"),
        ],
    )
    _write_journal(
        tmp_path,
        tid,
        [
            _record("c1", path="a.py"),
            _record("c2", path="b.py"),
        ],
    )

    async with _screen(tmp_path, tid) as scr:
        seq = [
            w
            for w in _trace_children(scr)
            if isinstance(w, DiffBlock)
            or (isinstance(w, ToolCallRow) and w.mode != "header")
        ]
        assert [type(w).__name__ for w in seq] == [
            "ToolCallRow",
            "DiffBlock",
            "ToolCallRow",
            "DiffBlock",
        ]
        assert _path(seq[1]) == "a.py" and _path(seq[3]) == "b.py"


@pytest.mark.asyncio
async def test_every_call_row_has_blank_line_above(tmp_path):
    """Пустая строка перед блоком; в блоке правок (P4 §3.1) — между
    файлами, после диффа тоже, но не перед первым."""
    tid = "group"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_apply_file_edits", {"path": "a.py"}),
            _output("c1", "fs_apply_file_edits"),
            _call("c2", "fs_write_file", {"path": "b.py"}),
            _output("c2", "fs_write_file"),
        ],
    )
    _write_journal(tmp_path, tid, [_record("c1", path="a.py")])

    async with _screen(tmp_path, tid) as scr:
        rows = [w for w in _trace_children(scr) if isinstance(w, ToolCallRow)]
        assert [r.mode for r in rows] == ["header", "member", "member"]
        assert [r.styles.margin.top for r in rows] == [1, 0, 1]
        assert not scr.query(".tool-group-start")


@pytest.mark.asyncio
async def test_lookup_failure_does_not_break_trace(tmp_path, monkeypatch):
    """Сбой построения блока: лента отрисована, строка тула на месте."""
    from gc_tui.services.edits_lookup import EditsLookup

    def boom(self, call_id):
        raise RuntimeError("журнал битый")

    monkeypatch.setattr(EditsLookup, "by_call_id", boom)

    tid = "broken"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
            _output("c1", "fs_apply_file_edits"),
        ],
    )
    _write_journal(tmp_path, tid, [_record("c1")])

    async with _screen(tmp_path, tid) as scr:
        rows = scr.query(ToolCallRow)
        assert len(rows) == 1
        assert not scr.query(DiffBlock)


@pytest.mark.asyncio
async def test_collapse_threshold_comes_from_config(tmp_path):
    """Порог свёртки берётся из agent_edits.trace_inline_max_lines; под
    блоком (P4 §4.0) своего хвоста у диффа нет — одна фраза блока."""
    tid = "threshold"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_write_file", {"path": "src/app.py"}),
            _output("c1", "fs_write_file"),
        ],
    )
    _write_journal(
        tmp_path, tid, [_record("c1", hunks=_long_hunks(30), add=30, del_=0)]
    )

    async with _screen(
        tmp_path, tid, config={"agent_edits": {"trace_inline_max_lines": 3}}
    ) as scr:
        from gc_tui.widgets.tool_blocks_view import BlockAction

        block = scr.query_one(DiffBlock)
        assert _body_lines(block) == 3
        assert not block.query(".diff-more")
        (phrase,) = scr.query(BlockAction)
        assert phrase.phrase == "+ ещё 27 строк"


@pytest.mark.asyncio
async def test_collapse_threshold_defaults_without_config(tmp_path):
    """Без ключа в конфиге — дефолт 3 изменённые строки превью (P4 §6.3):
    правка на 40 строк — 3 строки.

    Строк берём заведомо больше дефолта: иначе тест не отличил бы работающий
    лимит от отсутствующего."""
    tid = "threshold_default"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_write_file", {"path": "src/app.py"}),
            _output("c1", "fs_write_file"),
        ],
    )
    _write_journal(
        tmp_path, tid, [_record("c1", hunks=_long_hunks(40), add=40, del_=0)]
    )

    async with _screen(tmp_path, tid) as scr:
        assert _body_lines(scr.query_one(DiffBlock)) == 3


# --- фокус: клик по диффу не трогает позицию Tab -----------------------------
#
# Проверять это можно ТОЛЬКО в настоящей ленте: фокус перехватывает не блок, а
# его фокусируемый предок (#trace), и в упрощённом хосте без такого предка
# перехватывать нечего — дефект там не виден.


def _focus_fixture(tmp_path, tid: str) -> None:
    """Лента с диффом посередине: строка тула → блок → ещё одна строка тула."""
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
            _output("c1", "fs_apply_file_edits"),
            _call("c2", "run_python_code", {"code": "print(1)"}),
            _output("c2", "run_python_code"),
        ],
    )
    # Строк заведомо больше лимита: тестам нужна сноска «ещё N строк» — по ней
    # они и кликают. Лимит эти тесты задают СВОЙ, маленький (`_FOCUS_CONFIG`):
    # с дефолтным лимитом блок может вырасти так, что кнопка копирования уйдёт
    # за кадр, и клик промахивается мимо экрана.
    _write_journal(
        tmp_path, tid, [_record("c1", hunks=_long_hunks(40), add=40, del_=0)]
    )


#: Лимит для тестов фокуса: блок обрезан (сноска есть) и при этом короток.
_FOCUS_CONFIG = {
    "agent.model": "M",
    "agent_edits": {"trace_inline_max_lines": 5},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [".block-body", ".block-action"])
async def test_click_on_block_keeps_current_focus(tmp_path, selector):
    """Клик по телу/хвосту фокус не двигает — ни на блок, ни на #trace."""
    tid = "keepfocus"
    _focus_fixture(tmp_path, tid)

    async with _pilot(tmp_path, tid, _FOCUS_CONFIG) as pilot:
        row = pilot.app.screen.query(ToolCallRow).first()
        row.focus()
        await pilot.pause()

        await pilot.click(selector)
        await pilot.pause()

        assert pilot.app.focused is row


@pytest.mark.asyncio
async def test_copy_button_click_keeps_focus_elsewhere(tmp_path, monkeypatch):
    """Клик по ❐ в строке вызова копирует, но строку не выделяет: фокус
    остаётся там, где был (на соседней строке)."""
    tid = "copyfocus"
    _focus_fixture(tmp_path, tid)

    async with _pilot(tmp_path, tid, _FOCUS_CONFIG) as pilot:
        scr = pilot.app.screen
        copied = _capture_copies(monkeypatch)
        first, second = list(scr.query(ToolCallRow))
        second.focus()
        await pilot.pause()

        await pilot.click(first.query_one(".block-copy"))
        await pilot.pause()

        assert pilot.app.focused is second
        assert copied == [_long_hunks(40)]


@pytest.mark.asyncio
async def test_tab_after_click_on_block_goes_where_it_would_without_click(
    tmp_path,
):
    """Tab после клика по диффу ведёт туда же, куда вёл бы без клика.

    Живой дефект: клик уводил фокус на контейнер #trace, и следующий Tab
    начинал обход ленты заново, а не продолжал его со строки инструмента.
    """
    tid = "tabafterclick"
    _focus_fixture(tmp_path, tid)

    async with _pilot(tmp_path, tid, _FOCUS_CONFIG) as pilot:
        row = pilot.app.screen.query(ToolCallRow).first()

        row.focus()
        await pilot.press("tab")
        await pilot.pause()
        expected = pilot.app.focused

        row.focus()
        await pilot.pause()
        await pilot.click(".block-body")
        await pilot.press("tab")
        await pilot.pause()

        assert pilot.app.focused is expected


@pytest.mark.asyncio
async def test_text_selection_in_block_still_works(tmp_path):
    """Протяжка мышью по телу диффа по-прежнему выделяет текст.

    Выделение начинается на MouseDown — значит глушить его нельзя, чем бы ни
    чинился фокус.
    """
    tid = "selectdrag"
    _focus_fixture(tmp_path, tid)

    async with _pilot(tmp_path, tid, _FOCUS_CONFIG) as pilot:
        await pilot.mouse_down(".block-body", offset=(0, 0))
        await pilot.hover(".block-body", offset=(12, 1))
        await pilot.mouse_up(".block-body", offset=(12, 1))
        await pilot.pause()

        assert pilot.app.screen.get_selected_text()


@pytest.mark.asyncio
async def test_tail_follows_the_width(tmp_path):
    """Под блоком ленты доля диффа — в логических строках (спека P4 §6.3).

    Строк меньше доли, но они длинные: и на узком окне, где каждая
    переносится, и на широком показаны все, а фразы раскрытия нет — скрытых
    логических строк нет. Хвост по ширине — только у диффа вне ленты
    (`test_diff_block.py`).
    """
    from gc_tui.widgets.tool_blocks_view import BlockAction

    tid = "tail_width"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_write_file", {"path": "src/app.py"}),
            _output("c1", "fs_write_file"),
        ],
    )
    long_hunks = "@@ -1,8 +1,8 @@" + "".join(
        "\n+" + "y" * 200 for _ in range(8)
    )
    _write_journal(
        tmp_path, tid, [_record("c1", hunks=long_hunks, add=8, del_=0)]
    )

    async with _pilot(
        tmp_path,
        tid,
        {"agent.model": "M", "agent_edits": {"trace_inline_max_lines": 30}},
    ) as pilot:
        scr = pilot.app.screen
        block = scr.query_one(DiffBlock)
        for width in (40, 240):
            await pilot.resize_terminal(width, 30)
            await pilot.pause()
            await pilot.pause()
            body = block.query_one(".block-body", Static).visual.plain
            numbered = [
                row for row in body.split("\n") if row.strip()[:1].isdigit()
            ]
            assert len(numbered) == 8, width
            assert not scr.query(BlockAction), width
            assert not block.query(".diff-more"), width
