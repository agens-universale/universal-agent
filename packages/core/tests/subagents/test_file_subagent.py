"""Адаптер FileDefinedSubagent и проверка обязательных инструментов."""

import asyncio

import pytest
from gc_core.tools.subagents.custom.file_definitions import SubagentDefinition
from gc_core.tools.subagents.custom.file_subagent import (
    REQUIRED_TOOL_NAMES,
    BoundFileSubagent,
    FileDefinedSubagent,
)

REQUIRED = (
    "turn_finish",
    "session_read_stored_tool_output",
    "session_memory",
    "session_todo",
)


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
    остаётся валидным, а фактическую выдачу обеспечивает фильтр в abuild_tools_data
    (см. test_subagent_tools_filter).
    """

    def test_absent_allowlist_reports_nothing(self):
        assert FileDefinedSubagent.required_tools_banned(_definition()) == []

    def test_allowlist_without_required_is_not_a_problem(self):
        # Главное изменение: ядро не нужно упоминать в tools.
        assert (
            FileDefinedSubagent.required_tools_banned(
                _definition(tools=("fs_read_file",))
            )
            == []
        )

    def test_empty_allowlist_is_not_a_problem(self):
        assert (
            FileDefinedSubagent.required_tools_banned(_definition(tools=()))
            == []
        )

    @pytest.mark.parametrize("banned", REQUIRED)
    def test_stop_tools_ban_is_reported(self, banned):
        # Запрет не исполняется, но оператор должен узнать, что он проигнорирован.
        assert FileDefinedSubagent.required_tools_banned(
            _definition(stop_tools=(banned,))
        ) == [banned]

    def test_all_bans_reported_at_once(self):
        assert FileDefinedSubagent.required_tools_banned(
            _definition(stop_tools=REQUIRED)
        ) == sorted(REQUIRED)

    def test_unrelated_stop_tools_are_ignored(self):
        assert (
            FileDefinedSubagent.required_tools_banned(
                _definition(stop_tools=("exec_python",))
            )
            == []
        )

    def test_required_set_is_exact(self):
        assert set(REQUIRED) == REQUIRED_TOOL_NAMES


class TestSpecification:
    def test_shape(self):
        spec = FileDefinedSubagent.tool_specification(_definition())
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
                definition=_definition(**kw),
                task="сделай Х",
                reason="потому что",
            )
        )

    def test_role_carries_body_and_handoff_contract(self):
        role, assignment = self._instance().get_initial_data()
        assert role.startswith("Ты — писатель.")
        # Контракт сдачи приклеивается принудительно — отменить его из файла нельзя.
        assert "HANDOFF CONTRACT" in role
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
                runtime_notices_enabled=True,
            )
        )
        assert inst.task == "t"


class TestBoundFileSubagent:
    """BoundFileSubagent замыкает definition на контракт раннера/реестра."""

    def test_bound_is_frozen_dataclass(self):
        bound = FileDefinedSubagent.bound(_definition())
        assert isinstance(bound, BoundFileSubagent)

    def test_agent_name_comes_from_definition(self):
        bound = FileDefinedSubagent.bound(_definition())
        assert bound.agent_name == "subagent_doc_writer"

    def test_spec_matches_tool_specification(self):
        defn = _definition()
        bound = FileDefinedSubagent.bound(defn)
        assert (
            bound.get_agent_specification()
            == FileDefinedSubagent.tool_specification(defn)
        )

    def test_create_instance_proxies_task_and_reason(self):
        bound = FileDefinedSubagent.bound(_definition())
        inst = asyncio.run(bound.create_instance(task="t", reason="r"))
        assert inst.task == "t"
        assert inst.reason == "r"
        assert inst.definition.name == "subagent_doc_writer"

    def test_create_instance_tolerates_extra_runner_kwargs(self):
        bound = FileDefinedSubagent.bound(_definition())
        inst = asyncio.run(
            bound.create_instance(
                task="t",
                reason="r",
                model="какая-то-модель",
                runtime_notices_enabled=True,
            )
        )
        assert inst.task == "t"

    def test_create_instance_ignores_definition_kwarg(self):
        # definition только из bound; случайный definition в kwargs не переопределяет.
        bound = FileDefinedSubagent.bound(_definition())
        other = _definition(role="Другая роль")
        inst = asyncio.run(
            bound.create_instance(task="t", reason="r", definition=other)
        )
        assert inst.definition.role == "Ты — писатель."

    def test_class_get_agent_specification_raises_with_bound_hint(self):
        with pytest.raises(NotImplementedError) as exc_info:
            FileDefinedSubagent.get_agent_specification()
        msg = str(exc_info.value)
        assert "bound" in msg
