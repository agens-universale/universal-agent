"""Инлайн-дифф под строкой ВЛОЖЕННОГО (субагентского) тула.

Правки субагента пишутся в журнал ТРЕДА (ход родителя, свой call_id), поэтому
блок собирается тем же хелпером, что и у обычной строки; отличается только
монтирование — в иерархию под своей nested-строкой.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("textual")

from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.thread_store import edits_journal_path
from gc_testkit.thread_writes import subagent_record
from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.services.diff_render import copyable_hunks
from gc_tui.widgets.diff_block import DiffBlock
from gc_tui.widgets.tool_call_row import ToolCallRow
from textual.containers import VerticalScroll
from textual.widgets import Static

SHORT_HUNKS = (
    "--- \n+++ \n@@ -1,3 +1,3 @@\n def f():\n-    return 1\n+    return 2\n"
)

PARENT_CALL = "call_sub_1"


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


def _delegating_thread(tmp_path, tid: str, *, before: list[dict] = ()) -> None:
    """Тред с subagent_*-вызовом; `before` — обычные тулы до делегации."""
    _write_thread(
        tmp_path,
        tid,
        [
            {"id": "u1", "role": "user", "content": "правь"},
            *before,
            _call(
                PARENT_CALL, "subagent_worker", {"task": "x", "reason": "y"}
            ),
            _output(PARENT_CALL, "subagent_worker"),
        ],
    )


def _sidecar_call(tmp_path, tid: str, call_id: str, path: str) -> None:
    subagent_record(
        str(tmp_path),
        "cli",
        tid,
        parent_call_id=PARENT_CALL,
        child_run_id="run_c1",
        kind="call",
        payload={
            "type": "function_call",
            "name": "fs_write_file",
            "arguments": json.dumps({"path": path}),
            "call_id": call_id,
        },
    )


def _sidecar_result(tmp_path, tid: str, call_id: str) -> None:
    subagent_record(
        str(tmp_path),
        "cli",
        tid,
        parent_call_id=PARENT_CALL,
        child_run_id="run_c1",
        kind="result",
        payload={
            "type": "function_call_output",
            "call_id": call_id,
            "output": "ok",
        },
    )


def _write_sidecar_edit(tmp_path, tid: str, call_id: str, path: str) -> None:
    """call + result вложенного файлового тула в sidecar субагента."""
    _sidecar_call(tmp_path, tid, call_id, path)
    _sidecar_result(tmp_path, tid, call_id)


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
async def _screen(tmp_path, tid: str, config: dict | None = None):
    cfg = {"agent.model": "M"}
    if config:
        cfg.update(config)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    async with app.run_test(size=(100, 40)) as pilot:
        pilot.app.screen.attach_active_task(tid, is_resume=True)
        await pilot.pause(0.3)
        yield pilot.app.screen


def _trace_children(scr) -> list:
    return list(scr.query_one("#trace", VerticalScroll).children)


def _rows_and_blocks(scr) -> list:
    return [
        w
        for w in _trace_children(scr)
        if isinstance(w, (ToolCallRow, DiffBlock))
    ]


def _path(block: DiffBlock) -> str:
    """Файл блока — из записи журнала: в шапке его больше нет (он в строке тула)."""
    return block._record.path


@pytest.mark.asyncio
async def test_nested_edit_gets_block_under_its_own_row(tmp_path):
    """Правка субагента с записью в журнале → блок под ЕЁ строкой, не под родителем."""
    tid = "nested_edit"
    _delegating_thread(tmp_path, tid)
    _write_sidecar_edit(tmp_path, tid, "call_child_9", "a.py")
    _write_journal(tmp_path, tid, [_record("call_child_9", path="a.py")])

    async with _screen(tmp_path, tid) as scr:
        seq = _rows_and_blocks(scr)
        # parent subagent_* → nested строка → её блок
        assert [type(w).__name__ for w in seq] == [
            "ToolCallRow",
            "ToolCallRow",
            "DiffBlock",
        ]
        assert seq[0].call_id == PARENT_CALL and not seq[0]._level
        assert seq[1].call_id == "call_child_9" and seq[1]._level
        assert _path(seq[2]) == "a.py"
        assert not seq[2].query(".block-label")  # ветки нет
        assert "a.py +1 −1" in seq[1]._render_content().plain
        assert seq[1].query_one(".block-copy", Static).display
        assert seq[1]._copy_text == copyable_hunks(SHORT_HUNKS, path="a.py")


@pytest.mark.asyncio
async def test_no_block_for_nested_tool_without_journal_record(tmp_path):
    """Вложенный тул без записи в журнале → блока нет."""
    tid = "nested_norecord"
    _delegating_thread(tmp_path, tid)
    _write_sidecar_edit(tmp_path, tid, "call_child_9", "a.py")
    _write_journal(tmp_path, tid, [_record("other")])

    async with _screen(tmp_path, tid) as scr:
        assert len(scr.query(ToolCallRow)) == 2
        assert not scr.query(DiffBlock)


@pytest.mark.asyncio
async def test_plain_and_two_nested_edits_each_get_own_block(tmp_path):
    """Обычная правка + субагент с двумя правками → три блока по своим строкам."""
    tid = "mixed"
    _delegating_thread(
        tmp_path,
        tid,
        before=[
            _call("c1", "fs_apply_file_edits", {"path": "plain.py"}),
            _output("c1", "fs_apply_file_edits"),
        ],
    )
    _write_sidecar_edit(tmp_path, tid, "child_1", "a.py")
    _write_sidecar_edit(tmp_path, tid, "child_2", "b.py")
    _write_journal(
        tmp_path,
        tid,
        [
            _record("c1", path="plain.py"),
            _record("child_1", path="a.py"),
            _record("child_2", path="b.py"),
        ],
    )

    async with _screen(tmp_path, tid) as scr:
        seq = _rows_and_blocks(scr)
        assert [type(w).__name__ for w in seq] == [
            "ToolCallRow",
            "DiffBlock",  # обычная правка
            "ToolCallRow",  # subagent_*
            "ToolCallRow",
            "DiffBlock",  # первая вложенная
            "ToolCallRow",
            "DiffBlock",  # вторая вложенная
        ]
        assert _path(seq[1]) == "plain.py"
        assert _path(seq[4]) == "a.py"
        assert _path(seq[6]) == "b.py"


@pytest.mark.asyncio
async def test_nested_batch_blocks_land_under_own_rows(tmp_path):
    """Batch внутри субагента: call,call,result,result → блок под СВОЕЙ строкой.

    Результат первой правки приходит, когда за её строкой уже стоит соседний
    вызов: блок обязан встать между ними, а хвостовой якорь цепочки — не
    сдвинуться, иначе следующая nested-строка встала бы в середину."""
    tid = "nested_batch"
    _delegating_thread(tmp_path, tid)
    _sidecar_call(tmp_path, tid, "child_1", "a.py")
    _sidecar_call(tmp_path, tid, "child_2", "b.py")
    _sidecar_result(tmp_path, tid, "child_1")
    _sidecar_result(tmp_path, tid, "child_2")
    _write_sidecar_edit(tmp_path, tid, "child_3", "c.py")
    _write_journal(
        tmp_path,
        tid,
        [
            _record("child_1", path="a.py"),
            _record("child_2", path="b.py"),
            _record("child_3", path="c.py"),
        ],
    )

    async with _screen(tmp_path, tid) as scr:
        seq = _rows_and_blocks(scr)[1:]  # без строки самого subagent_*
        assert [
            w.call_id if isinstance(w, ToolCallRow) else _path(w) for w in seq
        ] == ["child_1", "a.py", "child_2", "b.py", "child_3", "c.py"]


@pytest.mark.asyncio
async def test_nested_block_keeps_nested_indent_and_tie(tmp_path):
    """У вложенного блока тело — сразу под СВОЕЙ nested-строкой, на +2 от её
    глифа; прижат к ней без отступов, как и обычный."""
    tid = "nested_indent"
    _delegating_thread(tmp_path, tid)
    _write_sidecar_edit(tmp_path, tid, "call_child_9", "a.py")
    _write_journal(tmp_path, tid, [_record("call_child_9", path="a.py")])

    async with _screen(tmp_path, tid) as scr:
        block = scr.query_one(DiffBlock)
        assert block.has_class("diff-nested")
        assert block.styles.margin.top == 0
        assert block.styles.margin.bottom == 0
        assert block.styles.border_left[0] == ""  # линии-поводка нет

        lines = [
            "".join(seg.text for seg in strip).rstrip()
            for strip in scr.app.screen._compositor.render_strips()
        ]
        indent = lambda s: len(s) - len(s.lstrip(" "))
        row = next(i for i, s in enumerate(lines) if "Write  a.py" in s)
        body = next(i for i, s in enumerate(lines) if "def f():" in s)
        assert body == row + 1
        assert indent(lines[body]) - indent(lines[row]) == 2


@pytest.mark.asyncio
async def test_nested_collapse_threshold_comes_from_config(tmp_path):
    """Порог свёртки у вложенного блока — тот же ключ конфига."""
    tid = "nested_threshold"
    _delegating_thread(tmp_path, tid)
    _write_sidecar_edit(tmp_path, tid, "call_child_9", "a.py")
    long_hunks = (
        "\n".join(["@@ -1,30 +1,30 @@"] + [f"+line {i}" for i in range(30)])
        + "\n"
    )
    _write_journal(
        tmp_path,
        tid,
        [
            _record(
                "call_child_9", path="a.py", hunks=long_hunks, add=30, del_=0
            )
        ],
    )

    async with _screen(
        tmp_path, tid, config={"agent_edits": {"trace_inline_max_lines": 3}}
    ) as scr:
        block = scr.query_one(DiffBlock)
        body = block.query_one(".block-body", Static).visual.plain
        assert body.count("\n") + 1 == 3
        assert (
            block.query_one(".diff-more", Static).visual.plain
            == "… ещё 27 строк"
        )


@pytest.mark.asyncio
async def test_nested_lookup_failure_does_not_break_trace(
    tmp_path, monkeypatch
):
    """Сбой построения блока вложенной правки: лента и nested-строка на месте."""
    from gc_tui.services.edits_lookup import EditsLookup

    def boom(self, call_id):
        raise RuntimeError("журнал битый")

    monkeypatch.setattr(EditsLookup, "by_call_id", boom)

    tid = "nested_broken"
    _delegating_thread(tmp_path, tid)
    _write_sidecar_edit(tmp_path, tid, "call_child_9", "a.py")
    _write_journal(tmp_path, tid, [_record("call_child_9", path="a.py")])

    async with _screen(tmp_path, tid) as scr:
        rows = [w for w in _trace_children(scr) if isinstance(w, ToolCallRow)]
        assert [w.call_id for w in rows] == [PARENT_CALL, "call_child_9"]
        assert not scr.query(DiffBlock)
