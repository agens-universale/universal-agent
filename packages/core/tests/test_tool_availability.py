"""Гейт доступности тулов для канала технических подсказок.

Канал (`tool_usage_notice`, project_memory-хинты, todo-напоминание) раньше
спрашивал только конфиг (`stop_tools`) и потому звал модель к тулам, которых в
ране нет: набор сужается ещё файлом кастомного сабагента, harbor-режимом и
неподнявшимся MCP. Здесь пинится и сам резолвер, и три точки его применения.
"""

from types import SimpleNamespace

from gc_core.message_policy.todo_reminder import apply_todo_reminder
from gc_core.message_policy.tool_availability import available_tool_names, tool_is_available
from gc_core.message_policy.tool_usage_notice.apply import apply_tool_usage_notice_before_llm
from gc_core.states import SharedStateData, State


def _state(tools_spec=None) -> State:
    return State(
        verbose=False,
        model="m",
        tools_spec=tools_spec or [],
        tools_descr=[],
        skills_list=[],
        max_steps=100,
        system_prompt_id="v1",
        use_server_subagents=False,
        shared=SharedStateData(working_memory={"state": "note"}),
    )


def _notice_cfg(**extra):
    cfg = {
        "tool_usage_notice": [
            {
                "tool_name": "session_memory",
                "step_messages_count": 10,
                "notice_message": "You have not used session_memory for some steps.",
            }
        ],
        "stop_tools": [],
    }
    cfg.update(extra)
    return cfg


class TestResolver:
    def test_tools_data_keys_win(self):
        ctx = SimpleNamespace(tools_data={"fs_read_file": {}, "turn_finish": {}})
        assert available_tool_names(ctx) == {"fs_read_file", "turn_finish"}

    def test_falls_back_to_tools_spec(self):
        ctx = SimpleNamespace(tools_spec=[{"name": "fs_read_file"}, {"name": "x"}])
        assert available_tool_names(ctx) == {"fs_read_file", "x"}

    def test_reads_state_tools(self):
        # State.tools — тот же список схем; нужен раннеру, где ctx ребёнка ещё нет.
        assert available_tool_names(_state([{"name": "session_memory"}])) == {"session_memory"}

    def test_first_knowing_source_wins(self):
        ctx = SimpleNamespace(tools_data={"a": {}})
        assert available_tool_names(ctx, _state([{"name": "b"}])) == {"a"}

    def test_unknown_sources_give_none(self):
        # Контракт: «не смогли узнать» ≠ «тулов нет».
        assert available_tool_names(None, SimpleNamespace(config_dict={})) is None
        assert available_tool_names(SimpleNamespace(tools_data={}, tools_spec=[])) is None

    def test_none_never_suppresses(self):
        assert tool_is_available(None, "что_угодно") is True

    def test_known_set_suppresses_absent(self):
        assert tool_is_available({"fs_read_file"}, "session_memory") is False
        assert tool_is_available({"session_memory"}, "session_memory") is True


class TestToolUsageNoticeGate:
    def _apply(self, ctx):
        state = _state()
        state.tool_usage_notice_cnt = {"session_memory": 10}
        return apply_tool_usage_notice_before_llm(
            [{"role": "user", "content": "hi", "id": "u1"}],
            state,
            ctx,
            {},
            step_messages=[{"role": "assistant", "content": "ok", "id": "a1"}],
        )

    def test_notice_suppressed_when_tool_absent(self):
        ctx = SimpleNamespace(
            config_dict=_notice_cfg(),
            tools_data={"fs_read_file": {}, "turn_finish": {}},
        )
        assert len(self._apply(ctx)) == 1

    def test_notice_kept_when_tool_present(self):
        ctx = SimpleNamespace(
            config_dict=_notice_cfg(),
            tools_data={"fs_read_file": {}, "session_memory": {}},
        )
        out = self._apply(ctx)
        assert len(out) == 2
        assert "not used session_memory" in out[1]["content"]

    def test_unknown_tool_set_keeps_old_behaviour(self):
        # ctx без тулов (синтетический/тестовый) — гейт не применяется.
        assert len(self._apply(SimpleNamespace(config_dict=_notice_cfg()))) == 2


class TestProjectMemoryGate:
    def test_hint_suppressed_when_tool_absent(self, monkeypatch):
        from gc_core import project_memory

        monkeypatch.setattr(project_memory, "get_hash", lambda _ctx: "hash-1")
        cfg = {
            "enable_project_memory_notice": True,
            "project_memory_params": {"check_interval_messages": 1},
            "stop_tools": [],
        }
        absent = SimpleNamespace(config_dict=cfg, tools_data={"fs_read_file": {}})
        present = SimpleNamespace(config_dict=cfg, tools_data={"fs_read_file": {}, "project_memory": {}})
        base = [{"role": "user", "content": "hi", "id": "u1"}]

        out_absent = apply_tool_usage_notice_before_llm(base, _state(), absent, {}, step_messages=[])
        out_present = apply_tool_usage_notice_before_llm(base, _state(), present, {}, step_messages=[])
        assert len(out_absent) == 1
        assert len(out_present) == 2
        assert "project memory" in out_present[1]["content"].lower()


class TestTodoReminderGate:
    def _apply(self, available):
        state = _state()
        state.todo_reminder_cnt = 9
        return apply_todo_reminder(
            [],
            state,
            {"todo_params": {"reminder_steps": 10}, "stop_tools": []},
            step_messages=[],
            available_tools=available,
        )

    def test_suppressed_when_tool_absent(self):
        assert self._apply({"fs_read_file"}) == []

    def test_sent_when_tool_present(self):
        assert len(self._apply({"session_todo"})) == 1

    def test_unknown_set_keeps_old_behaviour(self):
        assert len(self._apply(None)) == 1
