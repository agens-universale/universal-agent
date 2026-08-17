import json

from gc_core.tools.exec.calculator import Calculator


from tests.helpers.tool_ctx import with_local_ctx


def test_calculator_success():
    data = json.loads(with_local_ctx(Calculator)(expression="1 + 2 * 3"))
    assert data["ok"] is True
    assert data["value"] == 7
    assert data["value_type"] == "int"


def test_calculator_parentheses():
    data = json.loads(with_local_ctx(Calculator)(expression="(1+2+3)*7"))
    assert data["ok"] is True
    assert data["value"] == 42
    assert data["value_type"] == "int"


def test_calculator_pow():
    data = json.loads(with_local_ctx(Calculator)(expression="2**10"))
    assert data["ok"] is True
    assert data["value"] == 1024
    assert data["value_type"] == "int"


def test_calculator_float_division():
    data = json.loads(with_local_ctx(Calculator)(expression="-3.5/2"))
    assert data["ok"] is True
    assert data["value"] == -1.75
    assert data["value_type"] == "float"


def test_calculator_deny_name_sum():
    data = json.loads(with_local_ctx(Calculator)(expression="sum([1, 2, 3]) * 7"))
    assert data["ok"] is False
    # parsed ok (Call -> Name -> ...), rejected by walker as disallowed
    assert data["error"]["type"] == "disallowed_syntax"


def test_calculator_deny_attribute_escape():
    data = json.loads(with_local_ctx(Calculator)(expression="().__class__"))
    assert data["ok"] is False
    assert data["error"]["type"] == "disallowed_syntax"


def test_calculator_deny_import_escape():
    data = json.loads(with_local_ctx(Calculator)(expression="__import__('os')"))
    assert data["ok"] is False
    assert data["error"]["type"] == "disallowed_syntax"


def test_calculator_deny_subscript():
    data = json.loads(with_local_ctx(Calculator)(expression="[1,2][0]"))
    assert data["ok"] is False
    assert data["error"]["type"] == "disallowed_syntax"


def test_calculator_deny_method_call():
    data = json.loads(with_local_ctx(Calculator)(expression="[1].count(1)"))
    assert data["ok"] is False
    assert data["error"]["type"] == "disallowed_syntax"


def test_calculator_deny_set_literal():
    data = json.loads(with_local_ctx(Calculator)(expression="{1, 2}"))
    assert data["ok"] is False
    assert data["error"]["type"] == "disallowed_syntax"


def test_calculator_deny_string_constant():
    data = json.loads(with_local_ctx(Calculator)(expression="'a' + 'b'"))
    assert data["ok"] is False
    assert data["error"]["type"] == "disallowed_syntax"


def test_calculator_deny_bool():
    data = json.loads(with_local_ctx(Calculator)(expression="True and False"))
    assert data["ok"] is False
    assert data["error"]["type"] == "disallowed_syntax"


def test_calculator_deny_compare():
    data = json.loads(with_local_ctx(Calculator)(expression="1 < 2"))
    assert data["ok"] is False
    assert data["error"]["type"] == "disallowed_syntax"


def test_calculator_deny_syntax_error():
    data = json.loads(with_local_ctx(Calculator)(expression="1 +"))
    assert data["ok"] is False
    assert data["error"]["type"] == "disallowed_syntax"


def test_calculator_deny_name():
    data = json.loads(with_local_ctx(Calculator)(expression="x"))
    assert data["ok"] is False
    assert data["error"]["type"] == "disallowed_syntax"


def test_calculator_non_str_rejected():
    data = json.loads(with_local_ctx(Calculator)(expression=123))
    assert data["ok"] is False
    assert data["error"]["type"] == "invalid_argument"
    assert data["error"]["field"] == "expression"


def test_calculator_validate_before_eval():
    # C6: disallowed right operand must be rejected *before* the left side
    # (e.g. ZeroDivisionError) is evaluated.
    data = json.loads(with_local_ctx(Calculator)(expression="1/0 + ().__class__"))
    assert data["ok"] is False
    assert data["error"]["type"] == "disallowed_syntax"


def test_calculator_floor_division():
    data = json.loads(with_local_ctx(Calculator)(expression="7 // 2"))
    assert data["ok"] is True
    assert data["value"] == 3


def test_calculator_modulo():
    data = json.loads(with_local_ctx(Calculator)(expression="7 % 3"))
    assert data["ok"] is True
    assert data["value"] == 1


def test_calculator_errors():
    tool = with_local_ctx(Calculator)
    assert json.loads(tool(expression=""))["error"]["field"] == "expression"
    assert json.loads(tool(expression="1/0"))["error"]["type"] == "eval_failed"
    assert json.loads(tool(expression="x" * 3000))["error"]["type"] == "expression_too_long"
