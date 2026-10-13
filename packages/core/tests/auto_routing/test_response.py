"""Чтение ответа модели для автовыбора: важные имена, битые вызовы, ответ без
вызовов — на каноне ответа (`utils.convertations.get_output_list`) и реальном
реестре тулов, фильтром по `function_call`."""
import pytest

from gc_core import run_context as rc
from gc_core.auto_routing.response import (
    discarded_answer, has_invalid_call, has_no_tool_call, important_tool_names,
)
from gc_core.config_modules.root import AppConfig
from gc_testkit import run_coro

RAW = {
    "models": {"fast": {}, "mid": {}, "big": {}},
    "slots": {"light": "fast", "default": "mid", "max": "big"},
    "backend": {"api_mode": "chat_completions"},
    "logging": {"save_logs": False},
    "agent": {"use_local_subagents": True},
}


@pytest.fixture(scope="module")
def tools_data():
    """Реальный реестр тулов такта — тот же, что контроллер отдаёт вердикту."""
    cfg = AppConfig.model_validate(RAW)
    return run_coro(rc.acreate_run_context(cfg, level_provider=lambda: "default")).tools_data


def _fc(name, arguments="{}", call_id="call_1"):
    return {"arguments": arguments, "call_id": call_id, "name": name,
            "type": "function_call", "id": f"fc_{call_id}", "status": "completed"}


def _message(text):
    return {"id": "msg_1", "type": "message", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "annotations": [], "logprobs": [], "text": text}]}


def _never(_name):
    return False


def test_important_names_keep_order_and_drop_duplicates_and_foreign_messages(tools_data):
    response = [{"type": "reasoning", "id": "rs1"}, _fc("turn_finish"),
                _fc("fs_read_file", '{"path": "a.py"}', "call_2"),
                _fc("fs_write_file", call_id="call_3"), _fc("turn_finish", call_id="call_4"),
                _message("пишу файл")]
    assert important_tool_names(response, tools_data, is_subagent=_never) == (
        "turn_finish", "fs_write_file")


def test_important_names_of_an_empty_or_missing_response_are_empty(tools_data):
    assert important_tool_names([], tools_data, is_subagent=_never) == ()
    assert important_tool_names(None, tools_data, is_subagent=_never) == ()


def test_a_name_outside_the_registry_is_never_important(tools_data):
    """Имя из списка, снятое `stop_tools`, и выдуманный `subagent_*` — неизвестные
    тулы: у них свой путь (сигнал `score`/`judge`, повод `invalid_call`)."""
    without_finish = {name: entry for name, entry in tools_data.items() if name != "turn_finish"}
    response = [_fc("turn_finish")]
    assert important_tool_names(response, without_finish, is_subagent=_never) == ()
    assert important_tool_names(response, None, is_subagent=_never) == ()
    assert important_tool_names([_fc("subagent_ghost")], tools_data,
                                is_subagent=lambda name: True) == ()




def test_subagent_tools_are_named_by_the_predicate(tools_data):
    response = [_fc("subagent_worker")]
    assert important_tool_names(response, tools_data, is_subagent=_never) == ()
    assert important_tool_names(response, tools_data,
                                is_subagent=lambda name: name == "subagent_worker") == ("subagent_worker",)


@pytest.mark.parametrize("call", [
    _fc("fs_delete_everything", '{"path": "a.py"}', "call_2"),
    _fc("fs_read_file", '{"file": "a.py"}', "call_2"),
    _fc("fs_read_file", '{"path": "a.py"', "call_2"),
], ids=["unknown_tool", "arguments_off_schema", "broken_json"])
def test_has_invalid_call_sees_one_bad_call_in_the_batch(tools_data, call):
    assert has_invalid_call([_fc("fs_read_file", '{"path": "a.py"}'), call], tools_data) is True


def test_has_invalid_call_is_false_for_valid_calls_and_for_no_calls(tools_data):
    assert has_invalid_call([_fc("fs_read_file", '{"path": "a.py"}')], tools_data) is False
    assert has_invalid_call([], tools_data) is False
    assert has_invalid_call([_message("Готово.")], tools_data) is False


def test_has_no_tool_call_covers_prose_an_empty_answer_and_a_salvaged_think():
    assert has_no_tool_call([_message("Готово: тесты зелёные.")]) is True
    assert has_no_tool_call([]) is True and has_no_tool_call(None) is True
    assert has_no_tool_call([_message("<think>Надо прочитать файл.</think>")]) is True
    assert has_no_tool_call([_message("Читаю файл."), _fc("fs_read_file", '{"path": "a.py"}')]) is False


def test_the_discarded_answer_keeps_every_call_as_sent_and_the_prose():
    """Материал сравнения переигрыша: все вызовы выброшенного ответа — и неважные, и
    битые (аргументы строкой, как прислала модель), плюс его проза."""
    broken = '{"path": "b.py", "content": "x"'
    answer = [_message("Правлю файл."), _fc("fs_read_file", '{"path": "a.py"}', "c1"),
              _fc("fs_write_file", broken, "c2")]
    assert discarded_answer(answer) == (
        [{"name": "fs_read_file", "arguments": '{"path": "a.py"}'},
         {"name": "fs_write_file", "arguments": broken}],
        "Правлю файл.",
    )


def test_the_discarded_answer_of_an_empty_or_missing_response_is_empty():
    assert discarded_answer([]) == ([], "") and discarded_answer(None) == ([], "")


def test_the_discarded_answer_keeps_the_think_of_a_reasoning_only_answer():
    assert discarded_answer([_message("<think>Надо прочитать файл.</think>")]) == (
        [], "<think>Надо прочитать файл.</think>",
    )


def test_response_has_no_difficulty_reader():
    from gc_core.auto_routing import response
    assert not hasattr(response, "read_difficulty")


def test_the_readers_of_the_old_floor_are_gone():
    from gc_core.auto_routing import response
    assert not hasattr(response, "artifact_tool_names")
    assert not hasattr(response, "has_finish_call")
