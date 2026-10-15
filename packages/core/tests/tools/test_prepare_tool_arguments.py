"""Пользовательские служебные поля снимаются до валидации схемы тула."""
import json

from gc_testkit import run_coro
from gc_testkit.settings import make_settings


def _entry_with_injected(fields=("reason", "audit_tag")):
    from gc_core.tools.registry import get_builtin_tools_data
    entry = dict(get_builtin_tools_data(make_settings())["fs_read_file"])
    entry["injected_fields"] = list(fields)
    return entry


def test_prepare_returns_stripped_args_and_service_values():
    from gc_core.tools.arguments_validation import prepare_tool_arguments
    raw = json.dumps({"audit_tag": "8", "reason": "why", "path": "a.py"})
    args, service = prepare_tool_arguments(raw, _entry_with_injected())
    assert args == {"path": "a.py"}
    assert service == {"audit_tag": "8", "reason": "why"}


def test_prepare_handles_dict_input_and_bad_json():
    from gc_core.tools.arguments_validation import prepare_tool_arguments
    args, service = prepare_tool_arguments({"path": "a.py"}, _entry_with_injected())
    assert args == {"path": "a.py"} and service == {}
    assert prepare_tool_arguments("{not json", _entry_with_injected()) == (None, {})
    assert prepare_tool_arguments("[1,2]", _entry_with_injected()) == (None, {})


def test_garbage_in_service_field_does_not_fail_validation():
    """Мусор в `audit_tag` (тип нарушен) больше не роняет вызов: поле снято до валидации."""
    from gc_core.tools import aapply_tool
    from gc_core.tools.registry import get_builtin_tools_data
    from gc_core.tools.inject_tool_fields import inject_property_into_spec
    from gc_core.execution.state import SharedStateData
    from gc_core.states import State

    tools_data = get_builtin_tools_data(make_settings())
    entry = tools_data["session_todo"]
    inject_property_into_spec(entry["spec"], field_name="audit_tag",
                              property_schema={"type": "integer", "minimum": 1, "maximum": 10})
    entry["injected_fields"] = ["audit_tag"]
    # Поле аудита имеет неверный тип, а собственные аргументы session_todo валидны.
    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=100,
        system_prompt_id="v1",
        use_server_subagents=False,
        shared=SharedStateData(),
    )
    raw = json.dumps({
        "todos": [{"content": "step one", "status": "pending"}],
        "audit_tag": "high",
    })
    result = json.loads(run_coro(aapply_tool("session_todo", raw, tools_data_=tools_data, state=state)))
    assert result.get("ok") is True
    assert result.get("error") is None
    assert state.session_todo == [{"content": "step one", "status": "pending"}]


def test_reason_for_think_still_remaps_to_thoughts_through_apply_tool():
    """Образец: tools/session/test_think.py::test_prepare_args_before_strip_remaps_reason_to_thoughts,
    но через полный конвейер aapply_tool → prepare_tool_arguments."""
    from gc_core.tools import aapply_tool
    from gc_core.tools.registry import get_builtin_tools_data

    tools_data = get_builtin_tools_data(make_settings())
    entry = tools_data["session_think"]
    entry["injected_fields"] = ["reason"]
    raw = json.dumps({"reason": "long reasoning about the next step"})
    result = json.loads(run_coro(aapply_tool("session_think", raw, tools_data_=tools_data)))
    assert result["ok"] is True
    assert result.get("error") is None


def test_tool_arguments_valid_uses_same_order_as_apply_tool():
    from gc_core.tools import tool_arguments_valid
    tools_data = {"x": _entry_with_injected()}
    raw = json.dumps({"audit_tag": "high", "path": "a.py"})
    assert tool_arguments_valid("x", raw, tools_data) is True


def test_loop_detector_fingerprint_ignores_service_fields():
    from gc_core.loop_detector.detector import _parse_stripped_args
    tools_data = {"x": _entry_with_injected()}
    a = _parse_stripped_args("x", json.dumps({"audit_tag": 3, "path": "a.py"}), tools_data, make_settings().settings)
    b = _parse_stripped_args("x", json.dumps({"audit_tag": 9, "path": "a.py"}), tools_data, make_settings().settings)
    assert a == b == {"path": "a.py"}


def test_received_args_in_failure_feedback_is_original_string_with_service_fields():
    """received_args в фидбэке модели — исходная строка вызова,
    а не уже очищенный от служебных полей dict, даже когда валидация проваливается
    после всех коэрсеров (решение 6 канона автороутинга, §5.3)."""
    from gc_core.tools import aapply_tool
    from gc_core.tools.arguments_validation import WrongFunctionArgumentsException

    tools_data = {"fs_read_file": _entry_with_injected()}
    raw = json.dumps({
        "reason": "why I call this",
        "path": "a.py",
        "offset": "not-an-int-and-not-coercible-xyz",
    })
    try:
        run_coro(aapply_tool("fs_read_file", raw, tools_data_=tools_data))
    except WrongFunctionArgumentsException as exc:
        assert exc.received_args == raw
        assert "reason" in exc.received_args
    else:
        raise AssertionError("expected WrongFunctionArgumentsException")


def _zero_arg_tools(extra_fields):
    """Тул без собственных параметров, с пользовательским служебным полем или без."""
    from gc_core.tools.inject_tool_fields import inject_property_into_spec

    class ZeroArg:
        def __init__(self, **kwargs):
            pass

        async def acall(self, **kwargs):
            return "ok:" + json.dumps(kwargs)

    entry = {
        "tool_fabric": ZeroArg,
        "description": "snap",
        "injected_fields": [],
        "spec": {
            "type": "function", "name": "mcp_pw_browser_snapshot",
            "description": "snap",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    for field_name, schema in (extra_fields or {}).items():
        entry["injected_fields"].extend(inject_property_into_spec(
            entry["spec"], field_name=field_name, property_schema=schema,
        ))
    return {"mcp_pw_browser_snapshot": entry}


def test_empty_arguments_of_a_tool_without_own_parameters_are_an_empty_object():
    """Пользовательское служебное поле не превращает пустую схему в параметризованную."""
    from gc_core.tools import aapply_tool, tool_arguments_valid

    for extra in (None, {"audit_tag": {"type": "integer"}}):
        tools_data = _zero_arg_tools(extra)
        assert run_coro(aapply_tool("mcp_pw_browser_snapshot", "", tools_data)) == "ok:{}"
        assert tool_arguments_valid("mcp_pw_browser_snapshot", "", tools_data) is True


def test_empty_arguments_of_an_entry_without_a_schema_are_still_rejected():
    """Запись без схемы (`spec`) — не тул без параметров: снятый с реестра
    встроенный субагент снова отбивает пустую строку, а не подставляет `{}`."""
    from gc_core.tools.arguments_validation import prepare_tool_arguments
    assert prepare_tool_arguments("", {}) == (None, {})


def test_empty_arguments_of_a_tool_with_parameters_are_still_rejected():
    import pytest
    from gc_core.tools import aapply_tool
    from gc_core.tools.arguments_validation import WrongFunctionArgumentsException
    from gc_core.tools.registry import get_builtin_tools_data
    tools_data = get_builtin_tools_data(make_settings())
    tools_data["fs_read_file"]["injected_fields"] = ["audit_tag"]
    with pytest.raises(WrongFunctionArgumentsException):
        run_coro(aapply_tool("fs_read_file", "", tools_data))


def test_arguments_without_service_fields_strips_only_what_the_entry_injected():
    from gc_core.tools.arguments_validation import arguments_without_service_fields
    entry = _entry_with_injected(("audit_tag",))
    raw = json.dumps({"audit_tag": 9, "path": "a.py"})
    assert json.loads(arguments_without_service_fields(raw, entry)) == {"path": "a.py"}
    assert arguments_without_service_fields('{"path":  "a.py"}', entry) == '{"path":  "a.py"}'
    assert arguments_without_service_fields("{not json", entry) == "{not json"


def test_prepare_branches_with_a_fabric_hook_and_an_empty_schema():
    """Характеризация разбора: хук фабрики зовётся только для JSON-объекта,
    пустые аргументы тула без своих параметров — `{}` в обход хука."""
    from gc_core.tools.arguments_validation import prepare_tool_arguments

    calls = []

    class Fabric:
        @staticmethod
        def prepare_args_before_strip(args):
            calls.append(dict(args))
            return args.get("replace", {**args, "added": 1})

    entry = {"tool_fabric": Fabric, "injected_fields": ["audit_tag", 7],
             "spec": {"parameters": {"properties": {"audit_tag": {}}}}}
    raw = {"audit_tag": 3, "path": "a"}
    assert prepare_tool_arguments(raw, entry) == ({"path": "a", "added": 1}, {"audit_tag": 3})
    assert raw == {"audit_tag": 3, "path": "a"}
    assert prepare_tool_arguments('{"replace": [1]}', entry) == (None, {})
    assert prepare_tool_arguments("null", entry) == (None, {})
    assert len(calls) == 2
    assert prepare_tool_arguments(None, entry) == ({}, {})
    assert prepare_tool_arguments("  ", entry) == ({}, {})
    assert len(calls) == 2
    entry["spec"]["parameters"]["properties"]["path"] = {}
    assert prepare_tool_arguments("  ", entry) == (None, {})
    assert prepare_tool_arguments({"path": "a"}, {"tool_fabric": object()}) == ({"path": "a"}, {})
