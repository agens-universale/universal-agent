import pytest

from tools.simple_tools.deprecated_tools.update_plan import UpdatePlan


def test_update_plan_success():
    out = UpdatePlan()(current_plan="1. step")
    assert out == "A new plan has been generated. Use it for your next steps."


def test_update_plan_validation():
    with pytest.raises(ValueError):
        UpdatePlan()(current_plan=123)  # type: ignore[arg-type]
