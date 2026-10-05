"""Видимость тула просмотра на запрос (спека §9.2): ``fs_view_image`` в наборе
тулов запроса, только когда есть кому смотреть."""

from gc_core.constants.tool_names import FS_VIEW_IMAGE_NAME
from gc_core.model_router import ResolvedModel
from gc_core.vision.audience import (
    Audience,
    tools_for_request,
    view_tool_in_set,
)

_MODEL = ResolvedModel(
    alias="vis",
    model="vendor/vis",
    url="http://llm.test/v1/chat/completions",
    authorization=None,
    backend_overrides={},
    tariff=None,
    source="test",
)
SEES = Audience(native=_MODEL, eyes=None, tool_visible=True)
NOBODY = Audience(native=None, eyes=None, tool_visible=False)
VIEW_TOOL = {
    "type": "function",
    "name": FS_VIEW_IMAGE_NAME,
    "description": "view",
    "parameters": {"type": "object", "properties": {}},
}
READ_TOOL = {
    "type": "function",
    "name": "fs_read_file",
    "description": "read",
    "parameters": {"type": "object", "properties": {}},
}


def test_view_image_tool_name():
    assert FS_VIEW_IMAGE_NAME == "fs_view_image"


def test_view_tool_is_hidden_when_nobody_can_see():
    assert tools_for_request([READ_TOOL, VIEW_TOOL], NOBODY) == [READ_TOOL]


def test_view_tool_stays_when_someone_can_see():
    assert tools_for_request([READ_TOOL, VIEW_TOOL], SEES) == [
        READ_TOOL,
        VIEW_TOOL,
    ]


def test_view_tool_in_set():
    assert view_tool_in_set([READ_TOOL, VIEW_TOOL]) is True
    assert view_tool_in_set([READ_TOOL]) is False
