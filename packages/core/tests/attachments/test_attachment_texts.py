"""Тексты для модели: шаблоны заполняются ровно теми полями, что обещает контракт."""

import pytest

from gc_core.attachments.texts import (
    EYES_SYSTEM_PROMPT,
    STUB_FILE_MISSING,
    STUB_MODEL_BLIND,
    STUB_NOBODY,
    STUB_OUT_OF_CONTEXT,
    SUMMARY_IMAGES_CLOSE,
    SUMMARY_IMAGES_OPEN,
    TECH_LINE_ASK,
    TECH_LINE_TOOL,
    TOOL_ATTACHED,
    TOOL_NO_VISION,
    TOOL_VISION_FAILED,
    VIEW_DEFAULT_QUESTION,
)

LABEL = "Изображение #3"
WH = "png 1456×819"
PATH = "/t/images/cli_t1_0123456789abcdef.png"


@pytest.mark.parametrize(
    "template, fields, expected",
    [
        (
            STUB_OUT_OF_CONTEXT,
            {"label": LABEL, "wh": WH, "path": PATH},
            f"[{LABEL} · {WH} · not in context · file {PATH} · view it with fs_view_image]",
        ),
        (
            STUB_MODEL_BLIND,
            {"label": LABEL, "wh": WH, "path": PATH},
            f"[{LABEL} · {WH} · you cannot see this image yourself · call fs_view_image path={PATH} — another model will describe it for you]",
        ),
        (
            STUB_NOBODY,
            {"label": LABEL, "wh": WH},
            f"[{LABEL} · {WH} · not viewed: no available model can work with images — ask the user to describe it in words]",
        ),
        (
            STUB_FILE_MISSING,
            {"label": LABEL, "path": PATH},
            f"[{LABEL} · file unavailable: {PATH}]",
        ),
        (
            TECH_LINE_TOOL,
            {"source": "shots/login.png", "wh": WH},
            f"Image from fs_view_image: shots/login.png · {WH}",
        ),
        (
            TECH_LINE_ASK,
            {"label": LABEL, "wh": WH},
            f"{LABEL} from the user's answer to turn_ask_human · {WH}",
        ),
        (
            TOOL_VISION_FAILED,
            {"reason": "timeout"},
            "vision model did not respond (timeout); you may retry the call",
        ),
    ],
)
def test_template_renders_the_contract_text(template, fields, expected):
    assert template.format(**fields) == expected


def test_fixed_texts():
    assert (
        TOOL_ATTACHED == "image attached; you will see it in the next message"
    )
    assert (
        TOOL_NO_VISION
        == "image not viewed: no available model can work with images"
    )
    assert VIEW_DEFAULT_QUESTION == (
        "Describe the image in detail: quote all visible text verbatim, list UI elements, "
        "point out layout defects."
    )
    assert EYES_SYSTEM_PROMPT == (
        "You are the vision of a text-only agent. Describe precisely; quote text from the "
        "image verbatim; point out layout defects and inconsistencies; if the question is "
        "narrow, answer it first, then briefly describe the rest."
    )
    assert (SUMMARY_IMAGES_OPEN, SUMMARY_IMAGES_CLOSE) == (
        "<images>",
        "</images>",
    )
