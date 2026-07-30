"""Адаптер FileDefinedSubagent и проверка обязательных инструментов."""
import asyncio

import pytest

from tools.subagents.custom.file_definitions import SubagentDefinition
from tools.subagents.custom.file_subagent import (
    REQUIRED_TOOL_NAMES,
    FileDefinedSubagent,
    build_tool_specification,
    required_tools_banned_in_definition,
)

REQUIRED = ("turn_finish", "session_read_stored_tool_output")


def _definition(tools=None, stop_tools=(), role="Ты — писатель."):
    return SubagentDefinition(
        name="subagent_doc_writer",
        description="Пишет документацию.",
        role=role,
        tools=tools,
        stop_tools=tuple(stop_tools),
        source_path="/a/doc.md",
    )


class TestRequiredTools:
    """Служебное ядро выдаётся всегда: перечислять не нужно, stop_tools не отбирает.

    Гарантия, а не запрет — поэтому определение с любым набором tools/stop_tools
    остаётся валидным, а фактическую выдачу обеспечивает фильтр в build_tools_data
    (см. test_subagent_tools_filter).
    """

    def test_absent_allowlist_reports_nothing(self):
        assert required_tools_banned_in_definition(_definition()) == []

    def test_allowlist_without_required_is_not_a_problem(self):
        # Главное изменение: ядро не нужно упоминать в tools.
        assert required_tools_banned_in_definition(_definition(tools=("fs_read_file",))) == []

    def test_empty_allowlist_is_not_a_problem(self):
        assert required_tools_banned_in_definition(_definition(tools=())) == []

    @pytest.mark.parametrize("banned", REQUIRED)
    def test_stop_tools_ban_is_reported(self, banned):
        # Запрет не исполняется, но оператор должен узнать, что он проигнорирован.
        assert required_tools_banned_in_definition(
            _definition(stop_tools=(banned,))
        ) == [banned]

    def test_all_bans_reported_at_once(self):
        assert required_tools_banned_in_definition(
            _definition(stop_tools=REQUIRED)
        ) == sorted(REQUIRED)

    def test_unrelated_stop_tools_are_ignored(self):
        assert required_tools_banned_in_definition(
            _definition(stop_tools=("exec_python",))
        ) == []

    def test_required_set_is_exactly_two(self):
        assert REQUIRED_TOOL_NAMES == set(REQUIRED)


class TestSpecification:
    def test_shape(self):
        spec = build_tool_specification(_definition())
        assert spec["name"] == "subagent_doc_writer"
        assert spec["description"] == "Пишет документацию."
        assert spec["strict"] is True
        params = spec["parameters"]
        assert sorted(params["properties"].keys()) == ["reason", "task"]
        assert params["required"] == ["reason", "task"]
        assert params["additionalProperties"] is False


class TestInstance:
    def _instance(self, **kw):
        return asyncio.run(
            FileDefinedSubagent.create_instance(
                definition=_definition(**kw), task="сделай Х", reason="потому что"
            )
        )

    def test_agent_name_comes_from_definition(self):
        assert self._instance().get_agent_name() == "subagent_doc_writer"

    def test_role_carries_body_and_handoff_contract(self):
        role, assignment = self._instance().get_initial_data()
        assert role.startswith("Ты — писатель.")
        # Контракт сдачи приклеивается принудительно — отменить его из файла нельзя.
        assert "UNIFIED HANDOFF CONTRACT" in role
        assert "WORKFLOW CONTEXT" in role
        assert "сделай Х" in assignment
        assert "потому что" in assignment

    def test_extra_runner_kwargs_are_tolerated(self):
        # Раннер зовёт create_instance одинаково для всех сабагентов.
        inst = asyncio.run(
            FileDefinedSubagent.create_instance(
                definition=_definition(),
                task="t",
                reason="r",
                model="какая-то-модель",
                config_dict={"a": 1},
                runtime_notices_enabled=True,
            )
        )
        assert inst.task == "t"
