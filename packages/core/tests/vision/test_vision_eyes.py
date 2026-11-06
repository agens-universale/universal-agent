"""«Глаза»: запрос к модели для зрения (спека §8, §10).

Одна картинка частью image_url, контекст задачи и вопрос; temperature 0.1,
до 1500 токенов, потребитель и категория расхода — vision. Картиночный отказ
«глаз» — не «модель недоступна»: ни предохранителя, ни отката на активную модель.
"""

import base64
import io
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from gc_testkit import as_coro
from gc_testkit.settings import fake_ctx
from vision_fakes import warm_routes

from gc_core.attachments.model import ORIGIN_TOOL, TOOL_LABEL
from gc_core.attachments.normalize import normalize_image
from gc_core.attachments.store import ImageStore
from gc_core.attachments.texts import EYES_SYSTEM_PROMPT
from gc_core.config_modules.root import AppConfig
from gc_core.intent_log.operations import append_user_ask
from gc_core.model_router import ModelRouter
from gc_core.utils import llm_chat_client
from gc_core.vision.eyes import adescribe_image, task_context_of


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(buf, format="PNG")
    return buf.getvalue()


def _router() -> ModelRouter:
    raw = {
        "models": {"qwen": {}, "deepseek": {}},
        "slots": {"default": "deepseek"},
        "assign": {"vision": "qwen"},
    }
    return ModelRouter(
        AppConfig.model_validate(raw),
        choice_provider=lambda: "default",
    )


def _ctx(tmp_path, router):
    ctx = fake_ctx(
        {
            "backend.completions_url": "http://backend.local/v1/chat/completions",
            "backend.request_timeout_sec": 600,
            "backend.api_mode": "chat_completions",
        },
        model_router=router,
        logger=None,
        auth_connector=None,
        is_tui=False,
        image_store=ImageStore(str(tmp_path / "images")),
    )

    warm_routes(ctx)
    return ctx


def _state(*asks):
    intent = []
    for ask in asks:
        intent = append_user_ask(intent, ask, user_turn_seq=0)
    return SimpleNamespace(
        state_id="state-1",
        vision_probe_turn="test-root",
        x_target=None,
        default_model="legacy-model",
        usage=None,
        messages=[],
        tools=[],
        call_metadata={},
        previous_response_id=None,
        current_user_intent=intent,
    )


def _staged(ctx):
    return ctx.image_store.stage(
        normalize_image(_png()),
        origin=ORIGIN_TOOL,
        label=TOOL_LABEL,
        source="/proj/shot.png",
    )


def _openrouter_no_image_input() -> httpx.HTTPStatusError:
    """Ответ OpenRouter слепой модели на картинку (спека §10)."""
    request = httpx.Request(
        "POST", "https://openrouter.ai/api/v1/chat/completions"
    )
    response = httpx.Response(
        404,
        request=request,
        text='{"error":{"message":"No endpoints found that support image input","code":404}}',
    )
    return httpx.HTTPStatusError(
        "Client error '404 Not Found'", request=request, response=response
    )


async def test_eyes_send_one_image_with_the_task_and_the_question(
    tmp_path, monkeypatch
):
    captured = []

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        payload = payload_factory(initial_max_tokens)
        captured.append((payload, kwargs))
        return {
            "choices": [{"message": {"content": "  кнопка сдвинута вправо  "}}]
        }, payload

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )
    ctx = _ctx(tmp_path, _router())
    att = _staged(ctx)

    text = await adescribe_image(
        ctx,
        _state("почему кнопка съехала?"),
        att,
        question="Где кнопка «Войти»?",
        task_context="почему кнопка съехала?",
    )

    assert text == "кнопка сдвинута вправо"
    [(payload, kwargs)] = captured
    assert payload["model"] == "qwen"
    assert payload["temperature"] == 0.1
    assert payload["max_completion_tokens"] == 1500
    assert kwargs["request_label"] == "vision"
    system, user = payload["messages"]
    assert system == {"role": "system", "content": EYES_SYSTEM_PROMPT}
    assert user["role"] == "user"
    task_part, question_part, image_part = user["content"]
    assert task_part == {
        "type": "text",
        "text": "<task_context>\nпочему кнопка съехала?\n</task_context>",
    }
    assert question_part == {
        "type": "text",
        "text": "<question>\nГде кнопка «Войти»?\n</question>",
    }
    encoded = base64.b64encode(Path(att.path).read_bytes()).decode("ascii")
    assert image_part == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{encoded}"},
    }


async def test_without_task_context_only_the_question_and_the_image_go(
    tmp_path, monkeypatch
):
    captured = []

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        payload = payload_factory(initial_max_tokens)
        captured.append(payload)
        return {"choices": [{"message": {"content": "ok"}}]}, payload

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )
    ctx = _ctx(tmp_path, _router())

    await adescribe_image(
        ctx, _state(), _staged(ctx), question="?", task_context=""
    )

    [payload] = captured
    assert [part["type"] for part in payload["messages"][1]["content"]] == [
        "text",
        "image_url",
    ]


async def test_eyes_rejecting_images_trip_nothing_and_do_not_fall_back(
    tmp_path, monkeypatch
):
    """Картиночный 404 OpenRouter — не «модель недоступна» (слой 3, спека §10)."""
    models = []

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        models.append(payload_factory(initial_max_tokens)["model"])
        raise _openrouter_no_image_input()

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )
    router = _router()
    ctx = _ctx(tmp_path, router)

    with pytest.raises(httpx.HTTPStatusError):
        await adescribe_image(
            ctx, _state(), _staged(ctx), question="?", task_context=""
        )

    assert models == ["qwen"]
    assert router.breaker.snapshot() == {}


async def test_missing_file_is_an_error_not_an_empty_request(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(lambda **kw: calls.append(kw)),
    )
    ctx = _ctx(tmp_path, _router())
    att = _staged(ctx)
    Path(att.path).unlink()

    with pytest.raises(FileNotFoundError):
        await adescribe_image(
            ctx, _state(), att, question="?", task_context=""
        )

    assert calls == []


def test_task_context_is_the_open_turn_request_capped():
    assert task_context_of(
        _state("первая просьба", "уточнение посреди хода")
    ) == ("первая просьба\nуточнение посреди хода")
    assert task_context_of(_state("x" * 2000)) == "x" * 1500
    assert task_context_of(SimpleNamespace()) == ""
