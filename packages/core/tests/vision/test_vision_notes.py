"""Пометка «изображение не просмотрено» и её канал (спека изображений §10).

Хук `RunContext.on_trace_note` — по прецеденту `on_stream_delta`: аргумент
фабрики контекста, наследуется дочерним контекстом субагента (иначе отказ внутри
субагента не дошёл бы до ленты). Хука нет (CLI, сервер, SDK) — предупреждение в
журнал прогона.
"""

from types import SimpleNamespace

import pytest
from gc_testkit.settings import make_settings

from gc_core import run_context as rc
from gc_core.model_router import ResolvedModel
from gc_core.tools.subagents.runner import SubagentRunner
from gc_core.vision.notes import (
    NOTE_EYES_BLIND,
    NOTE_MAIN_BLIND,
    emit_trace_note,
    note_alias,
    note_blind,
)
from gc_testkit import as_coro


class _Log:
    def __init__(self):
        self.warnings: list = []

    def warning(self, msg, *args, **kwargs):
        self.warnings.append(msg % args if args else msg)


def _resolved(alias, model) -> ResolvedModel:
    return ResolvedModel(
        alias=alias,
        model=model,
        url="http://m/v1",
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test",
    )


@pytest.fixture
def no_tools(monkeypatch):
    monkeypatch.setattr(
        rc,
        "abuild_tools_data",
        as_coro(lambda cfg, on_progress=None, **_kwargs: ({}, [], [], {})),
    )


async def test_context_factory_has_no_hook_by_default(no_tools):
    ctx = await rc.acreate_run_context(
        make_settings({"model": "M", "save_logs": False})
    )
    assert ctx.on_trace_note is None


async def test_context_factory_stores_the_hook(no_tools):
    sink: list = []
    ctx = await rc.acreate_run_context(
        make_settings({"model": "M", "save_logs": False}),
        on_trace_note=sink.append,
    )
    assert ctx.on_trace_note == sink.append


async def test_subagent_context_inherits_the_hook(tmp_path):
    """Отказ «глаз» внутри субагента обязан дойти до ленты родителя."""
    config = {
        "logs_dump_dir": str(tmp_path),
        "save_logs": True,
        "logging_format": "jsonl",
        "console_trace": False,
        "backend_api_mode": "chat_completions",
        "completions_url": "http://localhost:1/v1/chat/completions",
        "model": "test-model",
        "launch_root": str(tmp_path),
        "use_local_subagents": True,
    }
    sink: list = []
    parent_ctx = await rc.acreate_run_context(
        make_settings(config), on_trace_note=sink.append
    )
    # `_build_child_ctx` читает только self._ctx и self._config — state-заглушка.
    child_ctx = await SubagentRunner(
        parent_ctx, SimpleNamespace(), make_settings(config)
    )._build_child_ctx("subagent_worker")

    assert child_ctx.on_trace_note == sink.append


def test_emit_goes_to_the_hook_when_it_is_set():
    sink: list = []
    ctx = SimpleNamespace(on_trace_note=sink.append, logger=_Log())

    emit_trace_note(ctx, "glm не работает с изображениями")

    assert sink == ["glm не работает с изображениями"]
    assert ctx.logger.warnings == []


def test_emit_without_a_hook_warns_in_the_run_log():
    """CLI, сервер, SDK: хука нет — пометка уходит предупреждением в журнал
    прогона (в CLI — и в консольную трассу)."""
    ctx = SimpleNamespace(on_trace_note=None, logger=_Log())

    emit_trace_note(ctx, "glm не работает с изображениями")

    assert ctx.logger.warnings == ["glm не работает с изображениями"]


def test_emit_prefers_the_logger_it_was_given():
    given = _Log()
    ctx = SimpleNamespace(logger=_Log())  # контекст без поля хука вовсе

    emit_trace_note(ctx, "текст", logger=given)

    assert given.warnings == ["текст"]
    assert ctx.logger.warnings == []


def test_note_texts_name_the_model_and_the_way_out():
    assert NOTE_MAIN_BLIND.format(alias="glm") == (
        "glm не работает с изображениями — изображение не просмотрено. "
        "Модель для зрения можно выбрать в /settings → Модели."
    )
    assert NOTE_EYES_BLIND.format(alias="qwen") == (
        "Модель для зрения qwen не работает с изображениями — изображение не "
        "просмотрено. Выберите другую в /settings → Модели."
    )


def test_note_names_the_registry_alias_or_the_legacy_model_id():
    assert note_alias(_resolved("glm", "glm-5.1")) == "glm"
    assert note_alias(_resolved(None, "legacy-model")) == "legacy-model"


@pytest.mark.parametrize(
    "eyes, template", [(False, NOTE_MAIN_BLIND), (True, NOTE_EYES_BLIND)]
)
def test_note_blind_names_the_blind_model_in_its_role(eyes, template):
    notes: list = []
    ctx = SimpleNamespace(on_trace_note=notes.append)

    note_blind(ctx, _resolved("glm", "glm-5.1"), eyes=eyes)

    assert notes == [template.format(alias="glm")]


def test_note_blind_without_a_hook_warns_in_the_given_logger():
    given = _Log()

    note_blind(
        SimpleNamespace(),
        _resolved(None, "legacy-model"),
        eyes=False,
        logger=given,
    )

    assert given.warnings == [NOTE_MAIN_BLIND.format(alias="legacy-model")]
