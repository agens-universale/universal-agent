import json

from tools.simple_tools.reflect import Reflect


def test_reflect_success_message():
    data = json.loads(Reflect()(content="  hello  "))
    assert data["ok"] is True
    assert (
        data["message"]
        == "Reflection added to the dialogue context. For persistent notes, use thread_notes or similar tools."
    )
    assert data["error"] is None


def test_reflect_invalid_argument():
    data = json.loads(Reflect()(content="x" * 9000))
    assert data["ok"] is True
    assert data["error"] is None
    bad = json.loads(Reflect()(content=123))  # type: ignore[arg-type]
    assert bad["message"] is None
    assert bad["ok"] is False and bad["error"]["field"] == "content"
