import json

from tools.simple_tools.reflect import Reflect


def test_reflect_success_and_trim():
    data = json.loads(Reflect()(content="  hello  "))
    assert data["ok"] is True
    assert data["content"] == "hello"
    assert data["length"] == 5


def test_reflect_truncation_and_invalid():
    data = json.loads(Reflect()(content="x" * 9000))
    assert data["ok"] is True
    assert data["truncated"] is True
    assert data["length"] == 8000
    bad = json.loads(Reflect()(content=123))  # type: ignore[arg-type]
    assert bad["ok"] is False and bad["error"]["field"] == "content"
