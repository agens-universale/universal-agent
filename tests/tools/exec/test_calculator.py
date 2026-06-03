import json

from tools.exec.calculator import Calculator


def test_calculator_success():
    data = json.loads(Calculator()(expression="1 + 2 * 3"))
    assert data["ok"] is True
    assert data["value"] == 7
    assert data["value_type"] == "int"


def test_calculator_no_builtins():
    data = json.loads(Calculator()(expression="sum([1, 2, 3]) * 7"))
    assert data["ok"] is False
    assert data["error"]["type"] == "eval_failed"
    assert "sum" in data["error"]["message"]


def test_calculator_method_on_literal():
    data = json.loads(Calculator()(expression="[1, 1, 2].count(1)"))
    assert data["ok"] is True
    assert data["value"] == 2
    assert data["value_type"] == "int"


def test_calculator_set_literal_serializes_as_str():
    data = json.loads(Calculator()(expression="{1, 2}"))
    assert data["ok"] is True
    assert data["value_type"] == "str"
    assert data["value"] == "{1, 2}"


def test_calculator_literal_container_ops():
    data = json.loads(Calculator()(expression="([1, 2, 3][0] + [1, 2, 3][1] + [1, 2, 3][2]) * 7"))
    assert data["ok"] is True
    assert data["value"] == 42


def test_calculator_errors():
    tool = Calculator()
    assert json.loads(tool(expression=""))["error"]["field"] == "expression"
    assert json.loads(tool(expression="1/0"))["error"]["type"] == "eval_failed"
    assert json.loads(tool(expression="x" * 3000))["error"]["type"] == "expression_too_long"
