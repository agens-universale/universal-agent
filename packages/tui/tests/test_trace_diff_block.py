"""Инлайн-дифф в ленте трейса: DiffBlock под строкой своего файлового тула."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("textual")

from textual.containers import VerticalScroll
from textual.widgets import Static

from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.thread_store import edits_journal_path
from gc_tui.app import TuiApp
from gc_tui.widgets.diff_block import DiffBlock
from gc_tui.widgets.tool_call_row import ToolCallRow

SHORT_HUNKS = (
    "--- \n"
    "+++ \n"
    "@@ -1,3 +1,3 @@\n"
    " def f():\n"
    "-    return 1\n"
    "+    return 2\n"
)


def _long_hunks(count: int = 30) -> str:
    lines = [f"@@ -1,{count} +1,{count} @@"]
    lines += [f"+line {i}" for i in range(count)]
    return "\n".join(lines) + "\n"


def _call(call_id: str, name: str, args: dict) -> dict:
    return {"id": f"fc_{call_id}", "type": "function_call", "call_id": call_id,
            "name": name, "arguments": json.dumps(args)}


def _output(call_id: str, name: str) -> dict:
    return {"id": f"fco_{call_id}", "type": "function_call_output", "call_id": call_id,
            "name": name, "output": json.dumps({"tool": name, "ok": True})}


def _write_thread(tmp_path, tid: str, payloads: list[dict]) -> None:
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "status": "stopped",
                    "updated_at": "2026-08-10T12:00:00+00:00",
                    "messages_count": len(payloads)}),
        encoding="utf-8")
    lines = [
        json.dumps({"type": "message", "seq": i,
                    "ts_utc": "2026-08-10T12:00:00+00:00", "payload": p})
        for i, p in enumerate(payloads, start=1)
    ]
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def _record(call_id: str, *, path: str = "src/app.py", hunks: str = SHORT_HUNKS,
            add: int = 1, del_: int = 1) -> EditRecord:
    return EditRecord(path=path, turn_id="t1", ts=1.0, call_id=call_id, op="edit",
                      hunks=hunks, add=add, del_=del_, before_sha256=None,
                      after_sha256=None, flags=[])


def _write_journal(tmp_path, tid: str, records: list[EditRecord]) -> None:
    journal = EditsJournal(edits_journal_path(str(tmp_path), "cli", tid))
    for rec in records:
        journal.append(rec)


@asynccontextmanager
async def _pilot(tmp_path, tid: str, config: dict | None = None):
    cfg = {"model": "M"}
    if config:
        cfg.update(config)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=cfg)
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


def _label(block: DiffBlock) -> str:
    return block.query_one(".block-label", Static).visual.plain


def _path(block: DiffBlock) -> str:
    """Файл блока — из записи журнала: в шапке его больше нет (он в строке тула)."""
    return block._record.path


@pytest.mark.asyncio
async def test_diff_block_mounted_right_after_its_tool_row(tmp_path):
    """Результат файлового тула с записью в журнале → блок сразу под строкой."""
    tid = "edited"
    _write_thread(tmp_path, tid, [
        {"id": "u1", "role": "user", "content": "правь"},
        _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
        _output("c1", "fs_apply_file_edits"),
    ])
    _write_journal(tmp_path, tid, [_record("c1")])

    async with _screen(tmp_path, tid) as scr:
        children = _trace_children(scr)
        rows = [i for i, w in enumerate(children) if isinstance(w, ToolCallRow)]
        blocks = [i for i, w in enumerate(children) if isinstance(w, DiffBlock)]
        assert len(rows) == 1 and len(blocks) == 1
        assert blocks[0] == rows[0] + 1   # непосредственно после своей строки
        assert _label(children[blocks[0]]) == "└ изменено: +1 −1"


@pytest.mark.asyncio
async def test_block_is_visually_tied_to_its_tool_row(tmp_path):
    """Блок примыкает к своей строке инструмента сверху (без верхнего отступа) и
    отбит от следующей снизу — пустой строкой. Связку рисует ветка `└`, поэтому
    вертикальной линии-поводка у блока больше нет."""
    tid = "tied"
    _write_thread(tmp_path, tid, [
        {"id": "u1", "role": "user", "content": "правь"},
        _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
        _output("c1", "fs_apply_file_edits"),
    ])
    _write_journal(tmp_path, tid, [_record("c1")])

    async with _screen(tmp_path, tid) as scr:
        block = scr.query_one(DiffBlock)
        assert block.styles.margin.top == 0
        assert block.styles.margin.bottom == 1
        assert block.styles.border_left[0] == ""   # линии-поводка больше нет


def _screen_lines(scr) -> list[str]:
    """Текстовый слепок экрана: строки композитора без хвостовых пробелов."""
    return ["".join(seg.text for seg in strip).rstrip()
            for strip in scr.app.screen._compositor.render_strips()]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


@pytest.mark.asyncio
async def test_indent_ladder_tool_branch_body(tmp_path):
    """Лесенка: строка инструмента → ветка (+2) → тело диффа (+2 от ветки)."""
    tid = "ladder"
    _write_thread(tmp_path, tid, [
        {"id": "u1", "role": "user", "content": "правь"},
        _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
        _output("c1", "fs_apply_file_edits"),
        _call("c2", "run_python_code", {"code": "print(1)"}),
        _output("c2", "run_python_code"),
    ])
    _write_journal(tmp_path, tid, [_record("c1")])

    async with _screen(tmp_path, tid) as scr:
        lines = _screen_lines(scr)
        tool = next(i for i, s in enumerate(lines) if "fs_apply_file_edits" in s)
        branch = next(i for i, s in enumerate(lines) if "изменено:" in s)
        body = next(i for i, s in enumerate(lines) if "def f():" in s)
        next_tool = next(i for i, s in enumerate(lines) if "run_python_code" in s)

        assert _indent(lines[branch]) - _indent(lines[tool]) == 2
        assert _indent(lines[body]) - _indent(lines[branch]) == 2
        # ветка сразу под своей строкой, между блоком и следующим тулом — пусто
        assert branch == tool + 1
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
    _write_thread(tmp_path, tid, [
        {"id": "u1", "role": "user", "content": "правь"},
        _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
        _output("c1", "fs_apply_file_edits"),
    ])
    _write_journal(tmp_path, tid, [_record("c1")])

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test(size=(width, 30)) as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)
        block = pilot.app.screen.query_one(DiffBlock)
        rows = block.query_one(".block-body", Static).visual.plain.split("\n")
        assert len(rows[1]) == len(rows[2]) == block.size.width - 2


@pytest.mark.asyncio
async def test_no_block_without_journal_record(tmp_path):
    """Файловый тул, но записи в журнале нет → блока нет (журнала тоже нет)."""
    tid = "nojournal"
    _write_thread(tmp_path, tid, [
        {"id": "u1", "role": "user", "content": "правь"},
        _call("c1", "fs_write_file", {"path": "src/app.py"}),
        _output("c1", "fs_write_file"),
    ])

    async with _screen(tmp_path, tid) as scr:
        assert len(scr.query(ToolCallRow)) == 1
        assert not scr.query(DiffBlock)


@pytest.mark.asyncio
async def test_no_block_for_non_file_tool(tmp_path):
    """Не-файловый тул: в журнале записи нет — блок не монтируется."""
    tid = "readonly"
    _write_thread(tmp_path, tid, [
        {"id": "u1", "role": "user", "content": "покажи"},
        _call("c1", "fs_read_file", {"path": "src/app.py"}),
        _output("c1", "fs_read_file"),
    ])
    _write_journal(tmp_path, tid, [_record("other")])

    async with _screen(tmp_path, tid) as scr:
        assert len(scr.query(ToolCallRow)) == 1
        assert not scr.query(DiffBlock)


@pytest.mark.asyncio
async def test_two_edits_each_get_own_block(tmp_path):
    """Две правки подряд → два блока, каждый под своей строкой."""
    tid = "two_edits"
    _write_thread(tmp_path, tid, [
        {"id": "u1", "role": "user", "content": "правь"},
        _call("c1", "fs_apply_file_edits", {"path": "a.py"}),
        _output("c1", "fs_apply_file_edits"),
        _call("c2", "fs_write_file", {"path": "b.py"}),
        _output("c2", "fs_write_file"),
    ])
    _write_journal(tmp_path, tid, [
        _record("c1", path="a.py"),
        _record("c2", path="b.py"),
    ])

    async with _screen(tmp_path, tid) as scr:
        children = _trace_children(scr)
        kinds = [
            ("row", w) if isinstance(w, ToolCallRow)
            else ("block", w) if isinstance(w, DiffBlock)
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
    _write_thread(tmp_path, tid, [
        {"id": "u1", "role": "user", "content": "правь"},
        _call("c1", "fs_apply_file_edits", {"path": "a.py"}),
        _call("c2", "fs_write_file", {"path": "b.py"}),
        _output("c1", "fs_apply_file_edits"),
        _output("c2", "fs_write_file"),
    ])
    _write_journal(tmp_path, tid, [
        _record("c1", path="a.py"),
        _record("c2", path="b.py"),
    ])

    async with _screen(tmp_path, tid) as scr:
        seq = [w for w in _trace_children(scr)
               if isinstance(w, (ToolCallRow, DiffBlock))]
        assert [type(w).__name__ for w in seq] == [
            "ToolCallRow", "DiffBlock", "ToolCallRow", "DiffBlock"]
        assert _path(seq[1]) == "a.py" and _path(seq[3]) == "b.py"


@pytest.mark.asyncio
async def test_block_between_tool_rows_keeps_group_indent(tmp_path):
    """Блок между строками тулов не делает следующую строку началом группы."""
    tid = "group"
    _write_thread(tmp_path, tid, [
        {"id": "u1", "role": "user", "content": "правь"},
        _call("c1", "fs_apply_file_edits", {"path": "a.py"}),
        _output("c1", "fs_apply_file_edits"),
        _call("c2", "fs_write_file", {"path": "b.py"}),
        _output("c2", "fs_write_file"),
    ])
    _write_journal(tmp_path, tid, [_record("c1", path="a.py")])

    async with _screen(tmp_path, tid) as scr:
        rows = [w for w in _trace_children(scr) if isinstance(w, ToolCallRow)]
        assert len(rows) == 2
        assert rows[0].has_class("tool-group-start")      # первый в группе
        assert not rows[1].has_class("tool-group-start")  # блок группу не рвёт


@pytest.mark.asyncio
async def test_lookup_failure_does_not_break_trace(tmp_path, monkeypatch):
    """Сбой построения блока: лента отрисована, строка тула на месте."""
    from gc_tui.services.edits_lookup import EditsLookup

    def boom(self, call_id):
        raise RuntimeError("журнал битый")

    monkeypatch.setattr(EditsLookup, "by_call_id", boom)

    tid = "broken"
    _write_thread(tmp_path, tid, [
        {"id": "u1", "role": "user", "content": "правь"},
        _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
        _output("c1", "fs_apply_file_edits"),
    ])
    _write_journal(tmp_path, tid, [_record("c1")])

    async with _screen(tmp_path, tid) as scr:
        rows = scr.query(ToolCallRow)
        assert len(rows) == 1
        assert not scr.query(DiffBlock)


@pytest.mark.asyncio
async def test_collapse_threshold_comes_from_config(tmp_path):
    """Порог свёртки берётся из diff_tui.trace_inline_max_lines."""
    tid = "threshold"
    _write_thread(tmp_path, tid, [
        {"id": "u1", "role": "user", "content": "правь"},
        _call("c1", "fs_write_file", {"path": "src/app.py"}),
        _output("c1", "fs_write_file"),
    ])
    _write_journal(tmp_path, tid, [_record("c1", hunks=_long_hunks(30), add=30, del_=0)])

    async with _screen(tmp_path, tid,
                       config={"diff_tui": {"trace_inline_max_lines": 3}}) as scr:
        block = scr.query_one(DiffBlock)
        assert _body_lines(block) == 3
        assert block.query_one(".diff-more", Static).visual.plain == "… ещё 27 строк"


@pytest.mark.asyncio
async def test_collapse_threshold_defaults_without_config(tmp_path):
    """Без ключа в конфиге — дефолт 30 ЭКРАННЫХ рядов.

    Строк берём заведомо больше дефолта: иначе тест не отличил бы работающий
    лимит от отсутствующего."""
    tid = "threshold_default"
    _write_thread(tmp_path, tid, [
        {"id": "u1", "role": "user", "content": "правь"},
        _call("c1", "fs_write_file", {"path": "src/app.py"}),
        _output("c1", "fs_write_file"),
    ])
    _write_journal(tmp_path, tid, [_record("c1", hunks=_long_hunks(40), add=40, del_=0)])

    async with _screen(tmp_path, tid) as scr:
        assert _body_lines(scr.query_one(DiffBlock)) == 30


# --- фокус: клик по диффу не трогает позицию Tab -----------------------------
#
# Проверять это можно ТОЛЬКО в настоящей ленте: фокус перехватывает не блок, а
# его фокусируемый предок (#trace), и в упрощённом хосте без такого предка
# перехватывать нечего — дефект там не виден.


def _focus_fixture(tmp_path, tid: str) -> None:
    """Лента с диффом посередине: строка тула → блок → ещё одна строка тула."""
    _write_thread(tmp_path, tid, [
        {"id": "u1", "role": "user", "content": "правь"},
        _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
        _output("c1", "fs_apply_file_edits"),
        _call("c2", "run_python_code", {"code": "print(1)"}),
        _output("c2", "run_python_code"),
    ])
    # Строк заведомо больше лимита: тестам нужна сноска «ещё N строк» — по ней
    # они и кликают. Лимит эти тесты задают СВОЙ, маленький (`_FOCUS_CONFIG`):
    # с дефолтными 30 рядами блок вырастает так, что кнопка копирования уходит
    # за кадр, и клик промахивается мимо экрана.
    _write_journal(tmp_path, tid, [_record("c1", hunks=_long_hunks(40), add=40, del_=0)])


#: Лимит для тестов фокуса: блок обрезан (сноска есть) и при этом короток.
_FOCUS_CONFIG = {"model": "M", "diff_tui": {"trace_inline_max_lines": 5}}


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [".block-body", ".diff-more", ".block-copy"])
async def test_click_on_block_keeps_current_focus(tmp_path, selector):
    """Клик по телу/хвосту/кнопке фокус не двигает — ни на блок, ни на #trace."""
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
async def test_tab_after_click_on_block_goes_where_it_would_without_click(tmp_path):
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
    """Хвост «… ещё N строк» пересобирается под ширину.

    Бюджет свёрнутого блока считается в ЭКРАННЫХ рядах, значит и факт усечения,
    и число скрытых строк зависят от ширины. Хвост, собранный один раз до
    раскладки, протухал бы; а если усечение появлялось только после неё —
    хвоста не было вовсе, и блок нельзя было развернуть.
    """
    tid = "tail_width"
    _write_thread(tmp_path, tid, [
        {"id": "u1", "role": "user", "content": "правь"},
        _call("c1", "fs_write_file", {"path": "src/app.py"}),
        _output("c1", "fs_write_file"),
    ])
    # Строк меньше лимита, но они длинные: на узком окне перенос съест бюджет
    # рядов, и усечение появится там, где на широком его не было.
    long_hunks = "@@ -1,8 +1,8 @@" + "".join("\n+" + "y" * 200 for _ in range(8))
    _write_journal(tmp_path, tid, [_record("c1", hunks=long_hunks, add=8, del_=0)])

    async with _pilot(tmp_path, tid, {"model": "M",
                                      "diff_tui": {"trace_inline_max_lines": 30}}) as pilot:
        block = pilot.app.screen.query_one(DiffBlock)

        await pilot.resize_terminal(40, 30)
        await pilot.pause()
        await pilot.pause()
        assert list(block.query(".diff-more")), "на узком окне усечение есть, а хвоста нет"

        await pilot.resize_terminal(240, 30)
        await pilot.pause()
        await pilot.pause()
        assert not list(block.query(".diff-more")), "на широком усечения нет, а хвост остался"
