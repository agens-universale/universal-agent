"""Скрытый fs_view_image исполняется и честно отказывает (спека §9.2).

Фильтр набора тулов прячет тул от модели, когда смотреть некому, но диспетчер
ищет тул в полном ctx.tools_data: выдуманный вызов исполняется и отвечает
no_vision_model — не unknown_tool, счётчик max_unknown_tool_repeats не растёт.
"""

import json
from types import SimpleNamespace

import pytest

from gc_core.attachments.texts import TOOL_NO_VISION
from gc_core.constants.tool_names import FS_VIEW_IMAGE_NAME
from gc_core.tools import aapply_tool, is_unknown_tool_output
from gc_core.tools.fs.view_image import ViewImage
from gc_core.vision.audience import Audience
from gc_testkit.helpers.tool_ctx import local_ctx


def _tools_data():
    spec = ViewImage.tool_description()
    return {
        spec["name"]: {
            "tool_fabric": ViewImage,
            "spec": spec,
            "description": "",
        }
    }


@pytest.mark.parametrize(
    "audience",
    [None, Audience(native=None, eyes=None, tool_visible=False)],
    ids=["no-audience", "nobody"],
)
async def test_hidden_tool_call_answers_no_vision_model_not_unknown_tool(
    tmp_path, audience
):
    state = SimpleNamespace(
        vision_audience=audience, pending_tool_attachments=[]
    )

    out = await aapply_tool(
        FS_VIEW_IMAGE_NAME,
        {"path": str(tmp_path / "shot.png")},
        _tools_data(),
        ctx=local_ctx(),
        state=state,
    )

    assert is_unknown_tool_output(out) is False
    assert json.loads(out)["error"] == {
        "type": "no_vision_model",
        "message": TOOL_NO_VISION,
    }
    assert state.pending_tool_attachments == []
