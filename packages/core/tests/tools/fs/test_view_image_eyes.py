"""fs_view_image, кандидат 2 — «глаза»: описание, пометка слепоты, сбой (спека §8, §9.2, §10)."""

import io
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from gc_core.attachments.store import ImageStore
from gc_core.attachments.texts import (
    TOOL_NO_VISION,
    TOOL_VISION_FAILED,
    VIEW_DEFAULT_QUESTION,
)
from gc_core.intent_log.operations import append_user_ask
from gc_core.model_router import ResolvedModel
from gc_core.tools.fs import view_image as view_image_module
from gc_core.tools.fs.view_image import ViewImage
from gc_core.utils.stream_assembler import BackendStreamError
from gc_core.vision.audience import Audience
from gc_core.vision.notes import NOTE_EYES_BLIND
from gc_core.vision.rejects import (
    process_image_rejects,
    reset_image_rejects_for_tests,
)
from gc_testkit.helpers.tool_ctx import local_ctx

EYES_URL = "http://eyes.local/v1/chat/completions"
EYES = Audience(
    native=None,
    eyes=ResolvedModel(
        alias="qwen",
        model="qwen-vl",
        url=EYES_URL,
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test",
    ),
    tool_visible=True,
)


@pytest.fixture(autouse=True)
def _fresh_rejects():
    reset_image_rejects_for_tests()
    yield
    reset_image_rejects_for_tests()


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(buf, format="PNG")
    return buf.getvalue()


def _shot(tmp_path):
    src = tmp_path / "shot.png"
    src.write_bytes(_png())
    return src


def _tool(tmp_path, *, notes=None):
    ctx = local_ctx({})
    ctx.image_store = ImageStore(str(tmp_path / "images"))
    ctx.logger = None
    ctx.system_id, ctx.thread_id = "cli", "t1"
    if notes is not None:
        ctx.on_trace_note = notes.append
    state = SimpleNamespace(
        vision_audience=EYES,
        pending_tool_attachments=[],
        current_user_intent=append_user_ask(
            [], "почему кнопка съехала?", user_turn_seq=0
        ),
    )
    return ViewImage(ctx=ctx, state=state, config_dict={}), state


def _patch_eyes(monkeypatch, outcome):
    seen = {}

    async def fake(ctx, state, att, *, question, task_context):
        seen.update(att=att, question=question, task_context=task_context)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(view_image_module, "adescribe_image", fake)
    return seen


def _http_error(status):
    request = httpx.Request("POST", EYES_URL)
    return httpx.HTTPStatusError(
        "http error",
        request=request,
        response=httpx.Response(status, request=request),
    )


async def test_blind_model_gets_the_eyes_description(tmp_path, monkeypatch):
    seen = _patch_eyes(monkeypatch, "Кнопка «Войти» съехала вправо на 40 px.")
    tool, state = _tool(tmp_path)
    src = _shot(tmp_path)

    out = json.loads(await tool.acall(path=str(src)))

    assert out == {
        "ok": True,
        "description": "Кнопка «Войти» съехала вправо на 40 px.",
        "path": str(src),
    }
    assert seen["question"] == VIEW_DEFAULT_QUESTION
    assert seen["task_context"] == "почему кнопка съехала?"
    assert seen["att"].path.startswith(str(tmp_path / "images"))
    assert state.pending_tool_attachments == []


async def test_seeing_model_with_eyes_assigned_looks_itself(
    tmp_path, monkeypatch
):
    """Зрячая модель запроса и назначенные «глаза» разом: картинка прикрепляется
    модели, «глаза» не зовутся — описывать пересказом то, что модель видит сама,
    незачем."""
    seen = _patch_eyes(monkeypatch, "не должно понадобиться")
    tool, state = _tool(tmp_path)
    state.vision_audience = Audience(
        native=ResolvedModel(
            alias="vis",
            model="vis-model",
            url=None,
            authorization=None,
            backend_overrides={},
            tariff=None,
            source="test",
        ),
        eyes=EYES.eyes,
        tool_visible=True,
    )

    out = json.loads(await tool.acall(path=str(_shot(tmp_path))))

    assert out["attached"] is True
    assert seen == {}
    assert len(state.pending_tool_attachments) == 1


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Какого цвета кнопка?", "Какого цвета кнопка?"),
        ("", VIEW_DEFAULT_QUESTION),
    ],
    ids=["asked", "empty"],
)
async def test_question_reaches_the_eyes(
    tmp_path, monkeypatch, question, expected
):
    seen = _patch_eyes(monkeypatch, "синяя")
    tool, _ = _tool(tmp_path)

    await tool.acall(path=str(_shot(tmp_path)), question=question)

    assert seen["question"] == expected


async def test_eyes_rejecting_images_are_marked_once_and_nobody_is_left(
    tmp_path, monkeypatch
):
    _patch_eyes(
        monkeypatch,
        BackendStreamError(
            "At most 0 image(s) may be provided", status_code=400
        ),
    )
    notes = []
    tool, _ = _tool(tmp_path, notes=notes)
    src = _shot(tmp_path)

    first = json.loads(await tool.acall(path=str(src)))
    second = json.loads(await tool.acall(path=str(src)))

    assert first["error"] == {
        "type": "no_vision_model",
        "message": TOOL_NO_VISION,
    }
    assert second["error"] == {
        "type": "no_vision_model",
        "message": TOOL_NO_VISION,
    }
    assert notes == [NOTE_EYES_BLIND.format(alias="qwen")]
    assert process_image_rejects().contains(EYES_URL, "qwen-vl") is True


@pytest.mark.parametrize(
    "failure, reason",
    [
        (httpx.ReadTimeout("timed out"), "ReadTimeout"),
        (_http_error(503), "HTTP 503"),
        ("", "empty response"),
    ],
    ids=["timeout", "5xx", "empty"],
)
async def test_eyes_failure_is_reported_without_marking(
    tmp_path, monkeypatch, failure, reason
):
    _patch_eyes(monkeypatch, failure)
    notes = []
    tool, _ = _tool(tmp_path, notes=notes)

    out = json.loads(await tool.acall(path=str(_shot(tmp_path))))

    assert out["error"] == {
        "type": "vision_failed",
        "message": TOOL_VISION_FAILED.format(reason=reason),
        "reason": reason,
    }
    assert notes == []
    assert process_image_rejects().contains(EYES_URL, "qwen-vl") is False
