import json

from tools.simple_tools.calculator import Calculator


def test_calculator_success():
    data = json.loads(Calculator()(expression="1 + 2 * 3"))
    assert data["ok"] is True
    assert data["value"] == 7
    assert data["value_type"] == "int"


def test_calculator_errors():
    tool = Calculator()
    assert json.loads(tool(expression=""))["error"]["field"] == "expression"
    assert json.loads(tool(expression="1/0"))["error"]["type"] == "eval_failed"
    assert json.loads(tool(expression="x" * 3000))["error"]["type"] == "expression_too_long"
