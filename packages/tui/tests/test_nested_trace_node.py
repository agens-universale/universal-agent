"""Узел вложенного трейса субагентов (спека шага 2 окна ленты, §5.9).

Все тесты узла — здесь: при снятии узла остальные тесты окна и
отрисовщика кусков не меняются."""

from __future__ import annotations

import pytest
from feed_dump import (
    HISTORY_THREAD,
    load_history_fixture,
    without_ephemeral,
    write_history,
)
from gc_core.thread_store import append_subagent_record
from gc_core.threads.facade import run_sync
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_testkit.tui_support import pinned_snapshot_clock
from gc_tui.services.nested_trace import NestedSource
from gc_tui.services.stores.file_store import FileTaskStore
from test_feed_parity_baseline import (
    _SIZE,
    _app,
    _settled_dump,
    dump_after_live_feed,
    dump_after_open,
)
from test_history_parity import _chunk_options


def _record(seq: int, parent: str, call_id: str) -> dict:
    return {
        "seq": seq,
        "parent_call_id": parent,
        "child_run_id": f"run_{parent}",
        "kind": "call",
        "payload": {"call_id": call_id, "name": "fs_read_file"},
    }


_RECORDS = [_record(1, "p1", "n1"), _record(2, "p2", "n2")]


def _node(records, window, reads=None) -> NestedSource:
    def read():
        if reads is not None:
            reads.append(1)
        return list(records)

    return NestedSource(
        read, window_parent_ids=lambda: set(window), root=None, injected=()
    )


def test_actions_only_for_given_parents():
    actions = _node(_RECORDS, {"p1", "p2"}).actions_for({"p1"})
    assert [(a.kind, a.parent_call_id, a.tool_call_id) for a in actions] == [
        ("mount_nested_tool", "p1", "n1")
    ]


def test_parents_outside_retention_skipped():
    reads: list = []
    node = _node(_RECORDS, {"p2"}, reads)
    assert node.actions_for({"p1"}) == []
    # Кусок без родителей в окне трассу не читает.
    assert reads == []
    actions = node.actions_for({"p1", "p2"})
    assert [a.parent_call_id for a in actions] == ["p2"]


def test_live_written_sidecar_visible_on_reload(tmp_path):
    threads = str(tmp_path)
    store = FileTaskStore(threads_dir=threads, system="cli")
    node = NestedSource(
        lambda: store.read_subagent_trace(HISTORY_THREAD),
        window_parent_ids=lambda: {"p1"},
        root=None,
        injected=(),
    )
    assert node.actions_for({"p1"}) == []
    rec = _record(1, "p1", "n1")
    run_sync(
        append_subagent_record(
            FileThreadStore(threads),
            ThreadKey(system="cli", thread=HISTORY_THREAD),
            parent_call_id=rec["parent_call_id"],
            child_run_id=rec["child_run_id"],
            kind=rec["kind"],
            payload=rec["payload"],
        )
    )
    actions = node.actions_for({"p1"})
    assert [a.tool_call_id for a in actions] == ["n1"]


async def _dump_rendered_with_node(name: str, tmp_path) -> list:
    """Тред фикстуры записан целиком; все ходы — отрисовщиком кусков с
    узлом, трасса субагентов читается из хранилища по требованию."""
    from gc_tui.services.history_model import build_history
    from gc_tui.services.history_render import FeedHost, render_turns
    from gc_tui.services.trace_retention import (
        subagent_parent_ids_from_events,
    )

    threads = tmp_path / "node"
    async with _app(threads).run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        write_history(str(threads), load_history_fixture(name))
        store = FileTaskStore(threads_dir=str(threads), system="cli")
        segments, _cursor = store.read_history(HISTORY_THREAD)
        inputs = scr._history_inputs()
        model = build_history(segments, inputs=inputs)
        node = NestedSource(
            lambda: store.read_subagent_trace(HISTORY_THREAD),
            window_parent_ids=lambda: subagent_parent_ids_from_events(
                model.events()
            ),
            root=inputs.root,
            injected=inputs.injected_fields,
        )
        render_turns(
            FeedHost(scr.query_one("#trace")),
            model,
            range(model.turn_count()),
            before=None,
            options=_chunk_options(scr),
            nested=node,
        )
        return await _settled_dump(pilot)


@pytest.mark.asyncio
async def test_subagents_fixture_nested_matches_open(tmp_path, monkeypatch):
    """Вложенное из узла = вложенное, которое рисует открытие сессии
    (подрисовка из трассы субагентов); остальные строки — тоже."""
    with pinned_snapshot_clock(monkeypatch):
        opened = await dump_after_open("subagents", tmp_path)
        rendered = await _dump_rendered_with_node("subagents", tmp_path)
    assert any("-in-agent" in row[2] for row in opened)
    assert rendered == opened


@pytest.mark.asyncio
@pytest.mark.parametrize("splits", [(), (3,), (1, 4, 7)])
async def test_subagents_fixture_nested_matches_live(
    splits, tmp_path, monkeypatch
):
    """Вложенное из узла = вложенное, которое живой путь рисует по
    событиям прогресса раннера."""
    with pinned_snapshot_clock(monkeypatch):
        live = await dump_after_live_feed("subagents", tmp_path, splits=splits)
        rendered = await _dump_rendered_with_node("subagents", tmp_path)
    assert rendered == without_ephemeral(live)
