from types import SimpleNamespace

import pytest
from gc_core import processing_responses as pr
from gc_core.execution.contracts import MainModelUnavailable


def test_the_honest_main_error_is_a_typed_runtime_error():
    resolved = SimpleNamespace(alias="mid", model="mid-model", url="http://mid/v1/chat/completions")
    with pytest.raises(MainModelUnavailable) as caught:
        pr._raise_honest_main_unavailable(SimpleNamespace(is_tui=False), resolved, "503", RuntimeError("503"))
    assert isinstance(caught.value, RuntimeError)
    assert "mid-model" in str(caught.value)
