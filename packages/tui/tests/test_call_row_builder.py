"""Подпись вызова: один билдер `build_call_row` на четыре пути ленты.

Живой `_render_event`, превью `render_segments_readonly`, вложенный живой
`_mount_nested_tool` и вложенный из sidecar `merge_nested_readonly` — все
рисуют `Label  объект` из ядра (`tool_display` + `tool_args_summary`);
сырого wire-имени в строке нет (спека P3 §2.1)."""

from __future__ import annotations

import json

import pytest

_JIRA = {
    "mcp_jira_create_issue": {
        "server": "jira",
        "tool": "create_issue",
        "title": "Создать задачу",
    }
}


def _content(row) -> str:
    return row._render_content().plain


def test_plain_tool_label_and_target():
    from gc_tui.services.trace_render import build_call_row

    row = build_call_row(
        call_id="c1",
        wire_name="fs_read_file",
        args_summary="src/x.py",
        level=0,
    )
    assert _content(row).startswith("Read  src/x.py")
    assert row.wire_name == "fs_read_file"


def test_legacy_name_gets_label():
    from gc_tui.services.trace_render import build_call_row

    row = build_call_row(
        call_id="c1", wire_name="read_file", args_summary="a.py", level=0
    )
    assert _content(row).startswith("Read  a.py")


def test_mcp_label_from_snapshot():
    from gc_tui.services.trace_render import build_call_row

    row = build_call_row(
        call_id="c1",
        wire_name="mcp_jira_create_issue",
        args_summary="PROJ-1",
        level=0,
        mcp_tools=_JIRA,
    )
    assert _content(row).startswith("jira · Создать задачу  PROJ-1")


def test_mcp_label_without_snapshot_by_server_prefix():
    from gc_tui.services.trace_render import build_call_row

    row = build_call_row(
        call_id="c1",
        wire_name="mcp_jira_create_issue",
        args_summary="PROJ-1",
        level=0,
        mcp_servers=("jira",),
    )
    assert _content(row).startswith("jira · create_issue  PROJ-1")


def test_unknown_tool_called_with_raw_name_in_target():
    from gc_tui.services.trace_render import build_call_row

    row = build_call_row(
        call_id="c1", wire_name="my_tool", args_summary="x=1", level=0
    )
    assert _content(row).startswith("Called  my_tool x=1")


def test_mapper_puts_core_args_summary_into_call_event():
    from gc_tui.services.trace_mapper import map_message

    ev = map_message(
        {
            "type": "function_call",
            "name": "fs_read_file",
            "call_id": "c1",
            "arguments": json.dumps({"path": "src/x.py"}),
        },
        no=1,
        ts="",
    )
    assert ev.kind == "call"
    assert ev.args_summary == "src/x.py"
    assert ev.summary == "src/x.py"


def test_running_and_interrupted_tails():
    from gc_tui.services.trace_render import build_call_row

    row = build_call_row(
        call_id="c1", wire_name="exec_command", args_summary="ls", level=0
    )
    assert _content(row).startswith("Run  ls · ")
    row.mark_interrupted()
    assert _content(row) == "Run  ls · прервано"


def test_subagent_live_status_last_is_label():
    from gc_tui.services.subagent_progress import format_parent_live_status

    assert (
        format_parent_live_status(
            step=4, max_steps=100, last_tool="fs_read_file"
        )
        == "step 4/100 · last=Read"
    )


def test_subagent_live_status_hides_hidden_tool():
    from gc_tui.services.subagent_progress import format_parent_live_status

    assert (
        format_parent_live_status(
            step=4, max_steps=100, last_tool="session_todo"
        )
        == "step 4/100"
    )


def test_subagent_live_status_unknown_tool_by_wire_name():
    from gc_tui.services.subagent_progress import format_parent_live_status

    assert (
        format_parent_live_status(step=2, max_steps=0, last_tool="my_tool")
        == "step 2 · last=my_tool"
    )


def test_subagent_live_status_mcp_is_server_and_tool():
    from gc_tui.services.subagent_progress import format_parent_live_status

    assert (
        format_parent_live_status(
            step=1,
            max_steps=5,
            last_tool="mcp_jira_create_issue",
            mcp_tools=_JIRA,
        )
        == "step 1/5 · last=jira · Создать задачу"
    )
    assert format_parent_live_status(
        step=1,
        max_steps=5,
        last_tool="mcp_jira_create_issue",
        mcp_servers=("jira",),
    ).endswith("last=jira · create_issue")


def test_open_request_by_call_id():
    from gc_tui.messages import OpenToolCallDetailRequest
    from gc_tui.services.trace_render import build_call_row

    row = build_call_row(
        call_id="c42", wire_name="fs_read_file", args_summary="a", level=0
    )
    req = row._open_request()
    assert isinstance(req, OpenToolCallDetailRequest)
    assert req.call_id == "c42"


def _live_row(scr, call_id: str):
    """Строка вызова из ленты, а не из `_active_calls`: тик опроса без
    живого прогона снимает вызовы оттуда (помечает «прервано»), и чтение
    реестра после `pilot.pause()` зависело от того, успел ли тик."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    scr._poll_active()  # тик опроса — детерминированно, не по таймеру
    (row,) = [r for r in scr.query(ToolCallRow) if r.call_id == call_id]
    return row


@pytest.mark.asyncio
async def test_live_top_level_and_nested_have_no_wire_name():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.trace_mapper import map_message

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        ev = map_message(
            {
                "type": "function_call",
                "name": "subagent_worker",
                "call_id": "p1",
                "arguments": json.dumps({"task": "проверка"}),
            },
            no=1,
            ts="",
        )
        scr._render_event(ev)
        await pilot.pause()
        scr._mount_nested_tool(
            parent_call_id="p1",
            tool_name="fs_read_file",
            tool_call_id="n1",
            args_summary="a.py",
        )
        await pilot.pause()
        parent = _live_row(scr, "p1")
        nested = _live_row(scr, "n1")
        assert _content(parent).startswith("Agent  проверка")
        assert "subagent_worker" not in _content(parent)
        assert _content(nested).startswith("Read  a.py")
        assert "fs_read_file" not in _content(nested)


def _app_without_warm_build():
    """TuiApp без сборки настоящего исполнителя: фоновый прогрев на старте
    ставит boot-снимок MCP в случайный момент теста и подменяет снимок,
    заданный тестом (подписи шли уже из него — флап)."""
    from gc_tui.app import TuiApp

    app = TuiApp()
    app._executor_factory = object
    return app


@pytest.mark.asyncio
async def test_live_mcp_label_from_screen_snapshot():
    pytest.importorskip("textual")
    from gc_tui.services.trace_mapper import map_message

    app = _app_without_warm_build()
    async with app.run_test() as pilot:
        app._mcp_snapshot = {
            "jira": {
                "tools": [
                    {
                        "name": "mcp_jira_create_issue",
                        "tool": "create_issue",
                        "title": "Создать задачу",
                    }
                ]
            }
        }
        scr = pilot.app.screen
        ev = map_message(
            {
                "type": "function_call",
                "name": "mcp_jira_create_issue",
                "call_id": "m1",
                "arguments": json.dumps({"key": "PROJ-1"}),
            },
            no=1,
            ts="",
        )
        scr._render_event(ev)
        await pilot.pause()
        row = _live_row(scr, "m1")
        assert _content(row).startswith("jira · Создать задачу")
        assert "mcp_jira_create_issue" not in _content(row)


@pytest.mark.asyncio
async def test_preview_and_sidecar_nested_have_no_wire_name():
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import (
        merge_nested_readonly,
        render_segments_readonly,
    )
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from textual.app import App
    from textual.containers import VerticalScroll

    segments = [
        {
            "index": 0,
            "messages": [
                {
                    "ts": "",
                    "payload": {
                        "type": "function_call",
                        "id": "fc1",
                        "call_id": "p1",
                        "name": "subagent_worker",
                        "arguments": json.dumps({"task": "проверка"}),
                    },
                },
                {
                    "ts": "",
                    "payload": {
                        "type": "function_call",
                        "id": "fc2",
                        "call_id": "m1",
                        "name": "mcp_jira_create_issue",
                        "arguments": json.dumps({"key": "PROJ-1"}),
                    },
                },
            ],
            "compacted_by": None,
        }
    ]
    records = [
        {
            "parent_call_id": "p1",
            "seq": 1,
            "kind": "call",
            "payload": {
                "type": "function_call",
                "name": "fs_read_file",
                "arguments": json.dumps({"path": "a.py"}),
                "call_id": "n1",
            },
        }
    ]

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            trace = self.query_one("#t", VerticalScroll)
            calls = render_segments_readonly(trace, segments, mcp_tools=_JIRA)
            merge_nested_readonly(trace, records, calls, mcp_tools=_JIRA)

    async with _A().run_test() as pilot:
        rows = list(pilot.app.screen.query(ToolCallRow))
        texts = [_content(r) for r in rows]
        assert texts[0].startswith("Agent  проверка")
        assert texts[1].startswith("Read  a.py")
        assert texts[2].startswith("jira · Создать задачу  PROJ-1")
        for wire in ("subagent_worker", "fs_read_file", "mcp_jira"):
            assert all(wire not in t for t in texts)


def test_detail_screen_header_keeps_wire_name_and_args():
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
    from gc_tui.services.trace_mapper import map_message

    ev = map_message(
        {
            "type": "function_call",
            "name": "exec_command",
            "call_id": "c1",
            "arguments": json.dumps({"command": "echo f(x)"}),
        },
        no=3,
        ts="",
    )
    scr = ToolCallDetailScreen(call_event=ev, result_event=None)
    assert scr._header_text() == "Viewer: exec_command(echo f(x)) · #3"


def _jira_meta(title: str) -> dict:
    return {
        "runtime": {
            "mcp_tools": {
                "jira": {
                    "tools": [
                        {
                            "name": "mcp_jira_create_issue",
                            "tool": "create_issue",
                            "title": title,
                        }
                    ]
                }
            }
        }
    }


@pytest.mark.asyncio
async def test_meta_mcp_snapshot_read_once_until_meta_changes(
    tmp_path, monkeypatch
):
    """Без boot-снимка подписи MCP берутся из meta треда: файл читается
    один раз на серию строк и заново — только после изменения meta."""
    import os

    pytest.importorskip("textual")
    from gc_tui.services.stores.file_store import FileTaskStore

    meta = tmp_path / "meta_cli_t1.json"
    meta.write_text(json.dumps(_jira_meta("Создать задачу")), "utf-8")
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    reads = []
    real_get = store.get_task_meta

    def _counting_get(task_id, system=None):
        reads.append(task_id)
        return real_get(task_id, system=system)

    monkeypatch.setattr(store, "get_task_meta", _counting_get)
    app = _app_without_warm_build()
    async with app.run_test() as pilot:
        app._mcp_snapshot = None
        scr = pilot.app.screen
        scr._store = store
        scr._active_task_id = "t1"
        for _ in range(5):
            tools, _servers = scr._mcp_tool_info()
        assert len(reads) == 1
        assert tools["mcp_jira_create_issue"]["title"] == "Создать задачу"

        meta.write_text(json.dumps(_jira_meta("Новая задача")), "utf-8")
        st = meta.stat()
        os.utime(meta, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        tools, _servers = scr._mcp_tool_info()
        assert len(reads) == 2
        assert tools["mcp_jira_create_issue"]["title"] == "Новая задача"


# ---- пути от корня проекта (спека P3 §10, решение владельца) ----

_ROOT = "/work/project"


def _read_call(call_id: str, path: str) -> dict:
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "name": "fs_read_file",
        "call_id": call_id,
        "arguments": json.dumps({"path": path}),
    }


def test_mapper_shows_path_from_the_project_root():
    from gc_tui.services.trace_mapper import (
        collect_render_context,
        map_message,
    )

    call = _read_call("c1", f"{_ROOT}/README.md")
    context = collect_render_context([call], root=_ROOT)
    ev = map_message(call, no=1, ts="", context=context)
    assert ev.args_summary == "README.md"


def test_nested_sidecar_actions_show_path_from_the_project_root():
    from gc_tui.services.subagent_sidecar_mapper import (
        plan_sidecar_nested_actions,
    )

    record = {
        "parent_call_id": "p1",
        "seq": 1,
        "kind": "call",
        "payload": _read_call("n1", f"{_ROOT}/README.md"),
    }
    actions = plan_sidecar_nested_actions([record], root=_ROOT)
    assert actions[0].args_summary == "README.md"


def test_nested_detail_shows_path_from_the_project_root():
    from gc_tui.services.subagent_sidecar_mapper import (
        trace_events_from_nested_detail,
    )

    detail = {"call": _read_call("n1", f"{_ROOT}/README.md")}
    call_ev, _ = trace_events_from_nested_detail("n1", detail, root=_ROOT)
    assert call_ev.args_summary == "README.md"


@pytest.mark.asyncio
async def test_live_call_shows_path_from_the_session_cwd(
    tmp_path, monkeypatch
):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    monkeypatch.chdir(tmp_path)
    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        call = _read_call("c1", str(tmp_path / "README.md"))
        scr._render_records([{"ts": "", "payload": call}])
        await pilot.pause()
        assert _content(_live_row(scr, "c1")).startswith("Read  README.md")


@pytest.mark.asyncio
async def test_preview_shows_path_from_the_project_root(tmp_path):
    pytest.importorskip("textual")
    from gc_testkit.tui_support import tui_settings
    from gc_tui.app import TuiApp
    from gc_tui.screens.preview import PreviewScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow

    segments = [
        {
            "index": 0,
            "messages": [
                {"ts": "", "payload": _read_call("c1", f"{_ROOT}/README.md")}
            ],
            "compacted_by": None,
        }
    ]
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        screen = PreviewScreen(
            segments=segments,
            project_path=_ROOT,
        )
        await pilot.app.push_screen(screen)
        await pilot.pause()
        texts = [_content(r) for r in screen.query(ToolCallRow)]
        assert texts[0].startswith("Read  README.md")


def test_run_row_hides_cd_into_the_project_root():
    from gc_tui.services.trace_mapper import (
        collect_render_context,
        map_message,
    )
    from gc_tui.services.trace_render import build_call_row

    command = f"cd {_ROOT} && ls .superpowers/sdd/2026-10-21-p3a-trace-look"
    call = {
        "type": "function_call",
        "id": "fc_c1",
        "name": "exec_command",
        "call_id": "c1",
        "arguments": json.dumps({"command": command}),
    }
    context = collect_render_context([call], root=_ROOT)
    ev = map_message(call, no=1, ts="", context=context)
    row = build_call_row(
        call_id="c1",
        wire_name=ev.wire_name,
        args_summary=ev.args_summary,
        level=0,
    )
    assert _content(row).startswith(
        "Run  ls .superpowers/sdd/2026-10-21-p3a-trace-look"
    )
    assert ev.full_payload["arguments"] == json.dumps({"command": command})


def _target_styles(row, fragment: str) -> list[str]:
    text = row._render_content()
    return [
        str(s.style)
        for s in text.spans
        if text.plain[s.start : s.end] == fragment
    ]


def test_skill_name_target_uses_skill_colour():
    from gc_tui.palette import SKILL
    from gc_tui.services.trace_render import build_call_row

    row = build_call_row(
        call_id="c1",
        wire_name="skills_load",
        args_summary="brainstorming",
        level=0,
    )
    assert _target_styles(row, "brainstorming") == [SKILL]


def test_skills_list_pack_target_keeps_usual_colour():
    from gc_tui.palette import DIM
    from gc_tui.services.trace_render import build_call_row

    row = build_call_row(
        call_id="c1",
        wire_name="skills_list",
        args_summary="core",
        level=0,
    )
    assert _target_styles(row, "  core") == [DIM]
