"""Адаптер FileDefinedSubagent и проверка обязательных инструментов."""
import asyncio

import pytest

from tools.subagents.file_definitions import SubagentDefinition, SubagentDefinitionError
from tools.subagents.file_subagent import (
    REQUIRED_TOOL_NAMES,
    FileDefinedSubagent,
    build_tool_specification,
    validate_required_tools,
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
    def test_absent_allowlist_is_fine(self):
        validate_required_tools(_definition())

    def test_allowlist_with_required_is_fine(self):
        validate_required_tools(_definition(tools=("fs_read_file", *REQUIRED)))

    @pytest.mark.parametrize("missing", REQUIRED)
    def test_allowlist_without_required_fails(self, missing):
        present = tuple(t for t in REQUIRED if t != missing)
        with pytest.raises(SubagentDefinitionError) as exc:
            validate_required_tools(_definition(tools=("fs_read_file", *present)))
        # Оператору нужны оба факта: какой файл и какой инструмент.
        assert "/a/doc.md" in str(exc.value)
        assert missing in str(exc.value)

    @pytest.mark.parametrize("banned", REQUIRED)
    def test_required_in_stop_tools_fails(self, banned):
        with pytest.raises(SubagentDefinitionError) as exc:
            validate_required_tools(_definition(stop_tools=(banned,)))
        assert banned in str(exc.value)
        assert "stop_tools" in str(exc.value)

    def test_all_problems_are_reported_at_once(self):
        # Оператор должен починить файл за один заход, а не по тулу за прогон.
        d = _definition(tools=("fs_read_file",), stop_tools=("turn_finish",))
        message = str(pytest.raises(SubagentDefinitionError, validate_required_tools, d).value)
        assert "turn_finish" in message
        assert "session_read_stored_tool_output" in message

    def test_empty_allowlist_fails(self):
        # tools: [] валиден для парсера, но обязан упереться сюда.
        with pytest.raises(SubagentDefinitionError):
            validate_required_tools(_definition(tools=()))

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
