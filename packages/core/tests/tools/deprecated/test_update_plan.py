from gc_testkit.helpers.tool_ctx import CallableTool
import pytest

from gc_core.tools.deprecated.update_plan import UpdatePlan


def test_update_plan_success():
    out = CallableTool(UpdatePlan())(current_plan="1. step")
    assert out == "A new plan has been generated. Use it for your next steps."


def test_update_plan_validation():
    with pytest.raises(ValueError):
        CallableTool(UpdatePlan())(current_plan=123)  # type: ignore[arg-type]
