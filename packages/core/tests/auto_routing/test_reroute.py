"""Вердикт переигрыша: три повода, общий список важных тулов, порядок кодов — на
каноне ответа (`utils.convertations.get_output_list`) и реальном реестре тулов."""

import pytest

from gc_core import run_context as rc
from gc_core.auto_routing.policy import (
    REROUTE_IMPORTANT,
    REROUTE_INVALID_CALL,
    REROUTE_NO_TOOL_CALL,
    RoutingParams,
)
from gc_core.auto_routing.reroute import FloorVerdict, floor_verdict
from gc_core.config_modules.root import AppConfig
from gc_core.tools.subagents.registry import is_subagent_tool
from gc_testkit import run_coro

RAW = {
    "models": {"fast": {}, "mid": {}, "big": {}},
    "slots": {"light": "fast", "default": "mid", "max": "big"},
    "backend": {"api_mode": "chat_completions"},
    "logging": {"save_logs": False},
}

FILE_WRITE_NAMES = (
    "fs_write_file",
    "fs_append_file",
    "fs_apply_file_edits",
    "fs_search_and_replace",
    "fs_save_base64_file",
    "download_file",
)
EXEC_NAMES = ("exec_command", "exec_python", "skills_run_script")


def _registry(**agent):
    """Реальный реестр тулов и плоский конфиг, из которого он собран. Бэкенд —
    `chat_completions` (`session_compact` регистрируется только там), субагенты
    включены: их тулы обязаны быть в реестре, иначе они неизвестны."""
    raw = {**RAW, "agent": {"use_local_subagents": True, **agent}}
    cfg = AppConfig.model_validate(raw)
    ctx = run_coro(
        rc.acreate_run_context(cfg, level_provider=lambda: "default")
    )
    return ctx.tools_data, cfg


@pytest.fixture(scope="module")
def registry():
    return _registry()


def _fc(name, arguments="{}", call_id="call_1"):
    return {
        "arguments": arguments,
        "call_id": call_id,
        "name": name,
        "type": "function_call",
        "id": f"fc_{call_id}",
        "status": "completed",
    }


def _message(text):
    return {
        "id": "msg_1",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "annotations": [],
                "logprobs": [],
                "text": text,
            }
        ],
    }


def _verdict(registry, response, *, is_subagent=None):
    """Вердикт так, как его зовёт контроллер: реестр такта и предикат субагента
    поверх того же конфига."""
    tools_data, cfg = registry
    return floor_verdict(
        response,
        tools_data=tools_data,
        is_subagent=is_subagent or (lambda name: is_subagent_tool(name, cfg)),
    )


# --- важный тул -------------------------------------------


@pytest.mark.parametrize("tool_name", FILE_WRITE_NAMES)
def test_every_file_write_tool_is_important(registry, tool_name):
    verdict = _verdict(
        registry, [_fc(tool_name, '{"path": "a.py", "content": "x"}')]
    )
    assert verdict.important_tools == (tool_name,) and verdict.tools == (
        tool_name,
    )
    assert (
        verdict.triggers[0] == REROUTE_IMPORTANT
        and verdict.reason == REROUTE_IMPORTANT
    )
    assert verdict.fires is True


@pytest.mark.parametrize(
    "tool_name", ["session_memory", "project_memory", "session_compact"]
)
def test_memory_and_compaction_are_important(registry, tool_name):
    verdict = _verdict(registry, [_fc(tool_name)])
    assert (
        verdict.tools == (tool_name,)
        and verdict.triggers[0] == REROUTE_IMPORTANT
    )


def test_a_plan_update_is_not_important_under_any_policy(registry):
    """Замер 2026-09-24: слабая почти всегда лишь отмечает пункт плана, а его
    переигрыш давал половину всех переигрышей и до 44 % шагов сильной.
    План создаётся на старте хода (первые шаги — на сильной); финал и проверка
    финала по-прежнему на сильной."""
    assert "session_todo" in registry[0]
    assert _verdict(registry, [_fc("session_todo", '{"todos": []}')]) is None


@pytest.mark.parametrize("tool_name", ["turn_finish", "turn_ask_human"])
def test_turn_closing_tools_are_important(registry, tool_name):
    verdict = _verdict(registry, [_fc(tool_name)])
    assert (
        verdict.tools == (tool_name,)
        and verdict.triggers[0] == REROUTE_IMPORTANT
    )


@pytest.mark.parametrize(
    "tool_name",
    ["subagent_worker", "subagent_planner", "subagent_code_reviewer"],
)
def test_builtin_subagent_tools_are_important(registry, tool_name):
    verdict = _verdict(
        registry,
        [_fc(tool_name, '{"reason": "step 2", "task": "write the report"}')],
    )
    assert (
        verdict.tools == (tool_name,)
        and verdict.triggers[0] == REROUTE_IMPORTANT
    )


def test_a_custom_subagent_is_important_through_the_predicate(registry):
    """Кастомный субагент в общий список не входит и входить не может: его имя
    знает только реестр субагентов — вердикту его отдаёт предикат контроллера."""
    tools_data, cfg = registry
    entry = {
        "spec": {
            "type": "function",
            "name": "subagent_db_migrator",
            "description": "migrations",
            "parameters": {
                "type": "object",
                "properties": {"task": {"type": "string"}},
                "required": ["task"],
            },
        },
        "description": "migrations",
        "tool_fabric": None,
    }
    custom = ({**tools_data, "subagent_db_migrator": entry}, cfg)
    call = _fc("subagent_db_migrator", '{"task": "migrate the schema"}')
    assert _verdict(custom, [call]) is None  # настоящий предикат его не знает
    verdict = _verdict(
        custom, [call], is_subagent=lambda name: name == "subagent_db_migrator"
    )
    assert (verdict.tools, verdict.triggers) == (
        ("subagent_db_migrator",),
        (REROUTE_IMPORTANT,),
    )


def test_a_listed_name_taken_away_by_stop_tools_is_not_important():
    """На бенче `stop_tools` снимает `turn_ask_human` и `project_memory`: имени нет
    в реестре такта — это неизвестный тул, а не важный."""
    stripped = _registry(stop_tools=["turn_ask_human", "project_memory"])
    assert (
        "turn_ask_human" not in stripped[0]
        and "project_memory" not in stripped[0]
    )
    assert (
        _verdict(
            stripped,
            [_fc("turn_ask_human"), _fc("project_memory", call_id="call_2")],
        ).important_tools
        == ()
    )


def test_a_subagent_name_is_not_important_while_subagents_are_off():
    without_subagents = _registry(use_local_subagents=False)
    assert "subagent_worker" not in without_subagents[0]
    assert (
        _verdict(without_subagents, [_fc("subagent_worker")]).important_tools
        == ()
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        "turn_intermediate_reply",
        "fs_read_file",
        "fs_find_in_files",
        "fs_list_files",
        "session_think",
        "exec_fetch_url",
    ],
)
def test_observation_and_the_intermediate_reply_are_never_important(
    registry, tool_name
):
    assert tool_name in registry[0]
    verdict = _verdict(registry, [_fc(tool_name)])
    assert verdict is None or verdict.important_tools == ()
    verdict = _verdict(registry, [_fc(tool_name)])
    assert verdict is None or verdict.important_tools == ()


def test_important_names_keep_order_and_drop_duplicates(registry):
    response = [
        _fc("turn_finish"),
        _fc("fs_read_file", call_id="call_2"),
        _fc("fs_write_file", call_id="call_3"),
        _fc("turn_finish", call_id="call_4"),
    ]
    assert _verdict(registry, response).tools == (
        "turn_finish",
        "fs_write_file",
    )


# --- битый вызов: только two_level ---------------------------------------------

INVALID_CALLS = {
    "unknown_tool": _fc("fs_delete_everything", '{"path": "a.py"}'),
    "arguments_off_schema": _fc("fs_read_file", '{"file": "a.py"}'),
    "broken_json": _fc("fs_read_file", '{"path": "a.py"'),
}


@pytest.mark.parametrize("kind", sorted(INVALID_CALLS))
def test_an_invalid_call_fires_under_two_level(registry, kind):
    verdict = _verdict(registry, [INVALID_CALLS[kind]])
    assert (
        verdict.important_tools,
        verdict.invalid_call,
        verdict.no_tool_call,
    ) == ((), True, False)
    assert (
        verdict.triggers == (REROUTE_INVALID_CALL,)
        and verdict.reason == REROUTE_INVALID_CALL
    )
    assert verdict.tools == ()


def test_a_valid_read_does_not_fire_under_two_level(registry):
    assert (
        _verdict(registry, [_fc("fs_read_file", '{"path": "a.py"}')]) is None
    )


# --- ответ без вызовов: только two_level ---------------------------------------

NO_CALL_ANSWERS = {
    "prose": [_message("Готово: тесты зелёные.")],
    "empty": [],
    "reasoning_only": [
        _message("<think>Надо прочитать файл и понять причину.</think>")
    ],
}


@pytest.mark.parametrize("kind", sorted(NO_CALL_ANSWERS))
def test_an_answer_without_calls_fires_under_two_level(registry, kind):
    verdict = _verdict(registry, NO_CALL_ANSWERS[kind])
    assert (
        verdict.important_tools,
        verdict.invalid_call,
        verdict.no_tool_call,
    ) == ((), False, True)
    assert (
        verdict.triggers == (REROUTE_NO_TOOL_CALL,)
        and verdict.reason == REROUTE_NO_TOOL_CALL
    )


def test_prose_next_to_a_valid_call_is_not_an_answer_without_calls(registry):
    response = [
        _message("Сейчас прочитаю файл."),
        _fc("fs_read_file", '{"path": "a.py"}'),
    ]
    assert _verdict(registry, response) is None


# --- порядок кодов -------------------------------------------------------------


def test_codes_come_in_the_fixed_order_and_the_first_one_is_the_reason(
    registry,
):
    response = [
        _fc("fs_read_file", '{"file": "a.py"}'),
        _fc("fs_write_file", '{"path": "a.py", "content": "x"}', "call_2"),
    ]
    verdict = _verdict(registry, response)
    assert verdict.triggers == (REROUTE_IMPORTANT, REROUTE_INVALID_CALL)
    assert verdict.reason == REROUTE_IMPORTANT and verdict.tools == (
        "fs_write_file",
    )
    every = FloorVerdict(("fs_write_file",), True, True)
    assert every.triggers == (
        REROUTE_IMPORTANT,
        REROUTE_INVALID_CALL,
        REROUTE_NO_TOOL_CALL,
    )


def test_a_final_with_a_lone_task_report_gives_two_codes_under_two_level(
    registry,
):
    """Реальная схема `turn_finish` требует `solution_type`, `task`, `task_report` и
    `handoff`: финал слабой модели с одним `task_report` — сразу два повода, и
    причина уровня — первый из них."""
    lone = _verdict(
        registry, [_fc("turn_finish", '{"task_report": "Готово."}')]
    )
    assert lone.triggers == (REROUTE_IMPORTANT, REROUTE_INVALID_CALL)
    assert lone.reason == REROUTE_IMPORTANT and lone.tools == ("turn_finish",)
    complete = '{"solution_type": "answer", "task": "почини тесты", "task_report": "Готово.", "handoff": "нет"}'
    full = _verdict(registry, [_fc("turn_finish", complete)])
    assert full.triggers == (REROUTE_IMPORTANT,) and full.tools == (
        "turn_finish",
    )


def test_a_verdict_without_a_trigger_does_not_fire():
    assert FloorVerdict((), False, False).fires is False
    assert FloorVerdict((), False, False).triggers == ()
