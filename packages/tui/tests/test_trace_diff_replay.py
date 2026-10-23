"""Реплей треда с лимитом развёрнутых блоков и дифф в detail-карточке."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("textual")

from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.thread_store import edits_journal_path
from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
from gc_tui.widgets.diff_block import DiffBlock
from gc_tui.widgets.tool_blocks_view import BlockAction
from gc_tui.widgets.tool_call_row import ToolCallRow
from textual import events
from textual.containers import VerticalScroll
from textual.widgets import Static

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


def _output(call_id: str, name: str, extra: dict | None = None) -> dict:
    body = {"tool": name, "ok": True}
    body.update(extra or {})
    return {
        "id": f"fco_{call_id}",
        "type": "function_call_output",
        "call_id": call_id,
        "name": name,
        "output": json.dumps(body),
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
    call_id: str | None,
    *,
    turn_id: str = "t1",
    path: str = "src/app.py",
    hunks: str = SHORT_HUNKS,
    add: int = 1,
    del_: int = 1,
    op: str = "edit",
) -> EditRecord:
    return EditRecord(
        path=path,
        turn_id=turn_id,
        ts=1.0,
        call_id=call_id,
        op=op,
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


def _blocks(scr) -> list[DiffBlock]:
    return [
        w
        for w in scr.query_one("#trace", VerticalScroll).children
        if isinstance(w, DiffBlock)
    ]


def _path_of(block: DiffBlock) -> str:
    return block._record.path


def _has_body(block: DiffBlock) -> bool:
    return bool(block.query(".block-body"))


def _body_lines(block: DiffBlock) -> int:
    return block.query_one(".block-body", Static).visual.plain.count("\n") + 1


def _phrase(scr, block: str) -> BlockAction:
    """Фраза раскрытия внизу блока ``block`` верхнего уровня (P4 §4.0)."""
    return next(
        w for w in scr.query(BlockAction) if w.key == f"{block}:bottom"
    )


def _turns_thread(
    tmp_path, tid: str, turns: int, *, hunks: str = SHORT_HUNKS
) -> None:
    """Тред с одной правкой на ход: turns вызовов, turns записей журнала.
    Между правками — ответ агента: каждая правка — свой блок (P4 §2.2)."""
    payloads: list[dict] = [{"id": "u1", "role": "user", "content": "правь"}]
    records: list[EditRecord] = []
    for i in range(1, turns + 1):
        payloads.append(
            _call(f"c{i}", "fs_apply_file_edits", {"path": f"f{i}.py"})
        )
        payloads.append(_output(f"c{i}", "fs_apply_file_edits"))
        payloads.append(
            {"id": f"a{i}", "role": "assistant", "content": f"готово {i}"}
        )
        records.append(
            _record(f"c{i}", turn_id=f"t{i}", path=f"f{i}.py", hunks=hunks)
        )
    _write_thread(tmp_path, tid, payloads)
    _write_journal(tmp_path, tid, records)


@pytest.mark.asyncio
async def test_replay_expands_only_last_turns(tmp_path):
    """Правки в 5 ходах при лимите 3 → тело только у блоков трёх последних."""
    tid = "five_turns"
    _turns_thread(tmp_path, tid, 5)

    async with _pilot(
        tmp_path, tid, config={"agent_edits": {"replay_expanded_turns": 3}}
    ) as pilot:
        blocks = _blocks(pilot.app.screen)
        assert len(blocks) == 5
        assert [_has_body(b) for b in blocks] == [
            False,
            False,
            True,
            True,
            True,
        ]
        # Компактный блок — без тела; своего хвоста под блоком нет, все
        # его строки считает фраза блока (P4 §6.3).
        assert not blocks[0].query(".diff-more")
        assert _phrase(pilot.app.screen, "block:0").phrase == "+ ещё 3 строки"


@pytest.mark.asyncio
async def test_replay_limit_one_expands_only_last_turn(tmp_path):
    """Лимит 1 → тело только у блока последнего хода."""
    tid = "limit_one"
    _turns_thread(tmp_path, tid, 3)

    async with _pilot(
        tmp_path, tid, config={"agent_edits": {"replay_expanded_turns": 1}}
    ) as pilot:
        blocks = _blocks(pilot.app.screen)
        assert [_has_body(b) for b in blocks] == [False, False, True]


@pytest.mark.asyncio
async def test_compact_block_expands_on_click(tmp_path):
    """Клик по фразе компактного блока разворачивает тело целиком."""
    tid = "expand_click"
    _turns_thread(tmp_path, tid, 2, hunks=_long_hunks(30))

    async with _pilot(
        tmp_path, tid, config={"agent_edits": {"replay_expanded_turns": 1}}
    ) as pilot:
        block = _blocks(pilot.app.screen)[0]
        assert not _has_body(block)
        phrase = _phrase(pilot.app.screen, "block:0")
        phrase.on_click(
            events.Click(
                widget=phrase,
                x=0,
                y=0,
                delta_x=0,
                delta_y=0,
                button=1,
                shift=False,
                meta=False,
                ctrl=False,
                chain=1,
            )
        )
        await pilot.pause(0.1)
        assert _body_lines(block) == 30


@pytest.mark.asyncio
async def test_replay_window_counts_only_turns_with_blocks(tmp_path):
    """Ходы из одних шелл-записей (call_id=None) блоков не дают — окно не тратят.

    Журнал: t1-t3 правлены fs-тулами, t4/t5 — командами оболочки. При лимите 3
    в ленте три блока, и все три должны быть развёрнуты: окно, посчитанное по
    всем turn_id подряд, отдало бы тело только последнему."""
    tid = "shell_turns"
    payloads: list[dict] = [{"id": "u1", "role": "user", "content": "правь"}]
    records: list[EditRecord] = []
    for i in (1, 2, 3):
        payloads.append(
            _call(f"c{i}", "fs_apply_file_edits", {"path": f"f{i}.py"})
        )
        payloads.append(_output(f"c{i}", "fs_apply_file_edits"))
        records.append(_record(f"c{i}", turn_id=f"t{i}", path=f"f{i}.py"))
    for i in (4, 5):
        payloads.append(
            _call(f"s{i}", "execute_command", {"command": "sed -i s/a/b/"})
        )
        payloads.append(_output(f"s{i}", "execute_command"))
        records.append(
            _record(None, turn_id=f"t{i}", path=f"f{i}.py", op="shell")
        )
    _write_thread(tmp_path, tid, payloads)
    _write_journal(tmp_path, tid, records)

    async with _pilot(
        tmp_path, tid, config={"agent_edits": {"replay_expanded_turns": 3}}
    ) as pilot:
        blocks = _blocks(pilot.app.screen)
        assert len(blocks) == 3
        assert [_has_body(b) for b in blocks] == [True, True, True]


@pytest.mark.asyncio
async def test_replay_edit_without_journal_record_has_no_block(tmp_path):
    """Правка без записи в журнале при реплее → диффа нет; две правки
    подряд — блок `Edited 2 файла` (P4 §6.2)."""
    tid = "no_record"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_write_file", {"path": "src/app.py"}),
            _output("c1", "fs_write_file"),
            _call("c2", "fs_write_file", {"path": "src/other.py"}),
            _output("c2", "fs_write_file"),
        ],
    )
    _write_journal(
        tmp_path, tid, [_record("c2", turn_id="t1", path="src/other.py")]
    )

    async with _pilot(tmp_path, tid) as pilot:
        rows = list(pilot.app.screen.query(ToolCallRow))
        assert [r.mode for r in rows] == ["header", "member", "member"]
        assert [_path_of(b) for b in _blocks(pilot.app.screen)] == [
            "src/other.py"
        ]


async def _open_detail(pilot) -> None:
    row = pilot.app.screen.query_one(ToolCallRow)
    row.on_click(
        events.Click(
            widget=row,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=True,
            chain=1,
        )
    )
    await pilot.pause(0.2)


def _screen_text(screen) -> str:
    return "\n".join(str(w.render()) for w in screen.query(Static))


@pytest.mark.asyncio
async def test_detail_card_renders_diff_for_edit_tool(tmp_path):
    """Карточка правки: дифф движком (номера строк, маркеры), а не сырые хэнки."""
    tid = "detail_edit"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
            _output(
                "c1",
                "fs_apply_file_edits",
                {"diff": SHORT_HUNKS, "applied": True},
            ),
        ],
    )
    _write_journal(tmp_path, tid, [_record("c1")])

    async with _pilot(tmp_path, tid) as pilot:
        await _open_detail(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ToolCallDetailScreen)
        diff = screen.query_one(".detail-diff", Static).visual.plain
        assert "2 -    return 1" in diff and "2 +    return 2" in diff
        text = _screen_text(screen)
        assert "@@" not in text  # сырое поле с хэнками убрано
        assert '"applied": true' in text  # прочий вывод на месте


def _filled_widths(screen) -> set[int]:
    """Длины ЗАЛИТЫХ строк диффа карточки (движок красит только `+`/`-`)."""
    plain = screen.query_one(".detail-diff", Static).visual.plain
    return {
        len(line)
        for line in plain.splitlines()
        if line.lstrip().split(" ", 1)[-1].startswith(("+", "-"))
    }


@pytest.mark.asyncio
async def test_detail_diff_fills_to_card_width(tmp_path):
    """Заливка диффа в карточке тянется до края тела, а не до длинной строки.

    Без `fill_width` фон обрывался по самой длинной строке хэнка — правый край
    карточки выглядел рваным, в отличие от блока ленты и вьюера правок.
    """
    tid = "detail_fill"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
            _output("c1", "fs_apply_file_edits", {"applied": True}),
        ],
    )
    _write_journal(tmp_path, tid, [_record("c1")])

    async with _pilot(tmp_path, tid) as pilot:
        await _open_detail(pilot)
        screen = pilot.app.screen
        body = screen.query_one("#trace", VerticalScroll)
        # Колонка скроллбара резервируется: `size.width` её не вычитает.
        assert _filled_widths(screen) == {body.size.width - 1}


@pytest.mark.asyncio
async def test_detail_diff_rebuilds_on_width_change(tmp_path):
    """Смена ширины окна — заливка переезжает за новым краем карточки."""
    tid = "detail_fill_resize"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_apply_file_edits", {"path": "src/app.py"}),
            _output("c1", "fs_apply_file_edits", {"applied": True}),
        ],
    )
    _write_journal(tmp_path, tid, [_record("c1")])

    async with _pilot(tmp_path, tid) as pilot:
        await _open_detail(pilot)
        screen = pilot.app.screen
        body = screen.query_one("#trace", VerticalScroll)
        before = _filled_widths(screen)

        await pilot.resize_terminal(70, 40)
        await pilot.pause()
        after = _filled_widths(screen)
        assert after != before
        assert after == {body.size.width - 1}


@pytest.mark.asyncio
async def test_detail_card_full_diff_without_collapse(tmp_path):
    """Дифф в карточке — целиком, без свёртки по trace_inline_max_lines."""
    tid = "detail_full"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            _call("c1", "fs_write_file", {"path": "src/app.py"}),
            _output("c1", "fs_write_file", {"diff": _long_hunks(30)}),
        ],
    )
    _write_journal(
        tmp_path, tid, [_record("c1", hunks=_long_hunks(30), add=30, del_=0)]
    )

    async with _pilot(
        tmp_path, tid, config={"agent_edits": {"trace_inline_max_lines": 3}}
    ) as pilot:
        await _open_detail(pilot)
        diff = pilot.app.screen.query_one(".detail-diff", Static).visual.plain
        assert diff.count("\n") + 1 == 30


@pytest.mark.asyncio
async def test_detail_card_for_non_file_tool_unchanged(tmp_path):
    """Не-файловый тул: карточка как была — сырой payload, без блока диффа."""
    tid = "detail_plain"
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "покажи"},
            _call("c1", "execute_command", {"command": "ls"}),
            _output("c1", "execute_command", {"stdout": "a.py\nb.py"}),
        ],
    )
    _write_journal(tmp_path, tid, [_record("other")])

    async with _pilot(tmp_path, tid) as pilot:
        await _open_detail(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ToolCallDetailScreen)
        assert not screen.query(".detail-diff")
        assert "a.py" in _screen_text(screen)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -5, True])
async def test_broken_inline_threshold_keeps_body(tmp_path, value):
    """Битый trace_inline_max_lines не даёт пустого тела: минимум одна строка."""
    tid = f"broken_{value}"
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

    async with _pilot(
        tmp_path,
        tid,
        config={"agent_edits": {"trace_inline_max_lines": value}},
    ) as pilot:
        block = pilot.app.screen.query_one(DiffBlock)
        assert _has_body(block)
        body = block.query_one(".block-body", Static).visual.plain
        assert "line 0" in body  # тело не пустое: минимум одна строка диффа
        assert _body_lines(block) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [-5])
async def test_broken_replay_limit_falls_back_to_default(tmp_path, value):
    """Отрицательное в replay_expanded_turns — мусор, действует дефолт.

    Ровно `0` — не мусор, а «без ограничения» (см. соседний тест). Правило то
    же, что у остальных ключей секции (`agent_edits.ctx.section_limit`).
    `true` сюда не доходит: схема (`int`) приводит его к 1 ещё при загрузке, а
    правило «bool — мусор» самого `section_limit` проверяет
    `agent_edits/test_ctx.py`."""
    tid = f"broken_replay_{abs(hash(value)) % 1000}"
    _turns_thread(tmp_path, tid, 2)

    async with _pilot(
        tmp_path, tid, config={"agent_edits": {"replay_expanded_turns": value}}
    ) as pilot:
        blocks = _blocks(pilot.app.screen)
        assert [_has_body(b) for b in blocks] == [True, True]


@pytest.mark.asyncio
async def test_replay_limit_zero_expands_every_turn(tmp_path):
    """`replay_expanded_turns: 0` — окно не режем: развёрнуты ВСЕ ходы."""
    tid = "replay_zero"
    _turns_thread(tmp_path, tid, 5)

    async with _pilot(
        tmp_path, tid, config={"agent_edits": {"replay_expanded_turns": 0}}
    ) as pilot:
        blocks = _blocks(pilot.app.screen)
        assert len(blocks) == 5
        assert [_has_body(b) for b in blocks] == [True] * 5
