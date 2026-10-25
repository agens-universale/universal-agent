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
from gc_core.security.call_scope import call_scope
from gc_core.tools.subagents.runner import SubagentRunner
from gc_core.vision.probe import ProbeStatus
from gc_core.vision.notes import (
    TraceNote,
    emit_trace_note,
    note_alias,
    note_blind,
    note_probe,
    unsupported_note,
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
        make_settings({"agent.model": "M", "logging.save_logs": False})
    )
    assert ctx.on_trace_note is None


async def test_context_factory_stores_the_hook(no_tools):
    sink: list = []
    ctx = await rc.acreate_run_context(
        make_settings({"agent.model": "M", "logging.save_logs": False}),
        on_trace_note=sink.append,
    )
    assert ctx.on_trace_note == sink.append


async def test_subagent_context_inherits_the_hook(tmp_path):
    """Отказ «глаз» внутри субагента обязан дойти до ленты родителя."""
    config = {
        "paths.logs_dump_dir": str(tmp_path),
        "logging.save_logs": True,
        "logging.format": "jsonl",
        "logging.console_trace": False,
        "backend.api_mode": "chat_completions",
        "backend.completions_url": "http://localhost:1/v1/chat/completions",
        "agent.model": "test-model",
        "launch_root": str(tmp_path),
        "agent.use_local_subagents": True,
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

    assert sink == [TraceNote("glm не работает с изображениями", "reply")]
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


# Решение владельца 2026-10-24: «модель не принимает изображения» — один
# факт одним текстом на обоих путях (проба и реальный отказ); различается
# только окончание.
_VIA_EYES = "deepseek не принимает изображения · агент посмотрит через gemini"
_MAIN_NOBODY = (
    "deepseek не принимает изображения · изображение не просмотрено · "
    "выберите модель с поддержкой изображений (/model) или назначьте "
    "модель для зрения в настройках"
)
_EYES_BLIND = (
    "gemini не принимает изображения · изображение не просмотрено · "
    "назначьте другую модель для зрения в настройках"
)


def test_image_hint_mentions_model_command():
    from gc_core.vision.notes import NOTE_HINT_MAIN

    assert NOTE_HINT_MAIN == (
        "выберите модель с поддержкой изображений (/model) или назначьте "
        "модель для зрения в настройках"
    )


def test_unsupported_note_texts_and_tones():
    assert unsupported_note("deepseek", viewer="gemini") == (
        _VIA_EYES,
        "info",
    )
    assert unsupported_note("deepseek") == (_MAIN_NOBODY, "warn")
    assert unsupported_note("gemini", eyes_role=True) == (_EYES_BLIND, "warn")


def test_note_names_the_registry_alias_or_the_legacy_model_id():
    assert note_alias(_resolved("glm", "glm-5.1")) == "glm"
    assert note_alias(_resolved(None, "legacy-model")) == "legacy-model"


@pytest.mark.parametrize(
    "eyes, alias, text",
    [(False, "deepseek", _MAIN_NOBODY), (True, "gemini", _EYES_BLIND)],
)
def test_note_blind_names_the_blind_model_in_its_role(eyes, alias, text):
    notes: list = []
    ctx = SimpleNamespace(on_trace_note=notes.append)

    note_blind(ctx, _resolved(alias, "m"), eyes=eyes)

    assert notes == [TraceNote(text, "reply")]
    assert notes[0].tone == "warn"


def test_note_blind_without_a_hook_warns_in_the_given_logger():
    given = _Log()

    note_blind(
        SimpleNamespace(),
        _resolved(None, "deepseek"),
        eyes=False,
        logger=given,
    )

    assert given.warnings == [_MAIN_NOBODY]


def _call_ids(notes):
    return [(n.owner, n.call_id) for n in notes]


def test_eyes_note_inside_a_tool_call_belongs_to_that_call():
    notes: list = []
    ctx = SimpleNamespace(on_trace_note=notes.append)

    with call_scope(state=None, call_id="c1", tool_name="fs_view_image"):
        note_blind(ctx, _resolved("gemini", "gemini-vl"), eyes=True)

    assert notes == [TraceNote(_EYES_BLIND, "call", "c1")]


def test_probe_note_inside_a_tool_call_belongs_to_that_call():
    notes: list = []
    ctx = SimpleNamespace(on_trace_note=notes.append)
    state = SimpleNamespace(vision_probe_turn="root")
    result = SimpleNamespace(
        status=ProbeStatus.UNSUPPORTED, reason="image_unsupported"
    )

    with call_scope(state=None, call_id="c1", tool_name="fs_view_image"):
        note_probe(ctx, state, "key", _resolved("qwen", "qwen-vl"), result)

    assert _call_ids(notes) == [("call", "c1")]


def test_a_call_scope_without_call_id_is_the_reply():
    notes: list = []
    ctx = SimpleNamespace(on_trace_note=notes.append)

    with call_scope(state=None, call_id=None, tool_name="fs_view_image"):
        emit_trace_note(ctx, "текст")

    assert _call_ids(notes) == [("reply", None)]


def test_subagent_notes_carry_the_call_they_were_raised_in():
    """Субагент делит хук родителя. Внутри собственного вызова ребёнка
    пометка несёт его call_id; отказ модели ребёнка вне вызовов тулов
    принадлежит вызову запуска субагента у родителя (его call_id)."""
    notes: list = []
    ctx = SimpleNamespace(on_trace_note=notes.append)

    with call_scope(state=None, call_id="launch", tool_name="subagent_x"):
        emit_trace_note(ctx, "отказ модели ребёнка")
        with call_scope(
            state=None, call_id="child", tool_name="fs_view_image"
        ):
            emit_trace_note(ctx, "отказ глаз ребёнка")

    assert _call_ids(notes) == [("call", "launch"), ("call", "child")]


async def test_admitted_request_failure_notes_the_probe_in_the_tool_call(
    monkeypatch,
):
    """Реальная точка `_arequest_admitted_chat_completion`: отказ маршрута
    «глаз» внутри диспетчера принадлежит вызову тула."""
    from gc_core.utils import llm_chat_client as client
    from gc_core.vision import rejects

    async def boom(**kwargs):
        raise RuntimeError("no images")

    monkeypatch.setattr(client, "arequest_chat_completions_with_policy", boom)
    monkeypatch.setattr(rejects, "learn_image_cap", lambda key, exc: True)
    monkeypatch.setattr(
        "gc_core.vision.route.vision_route_key", lambda prepared: "key"
    )
    monkeypatch.setattr(
        "gc_core.vision.registry.process_vision_probes",
        lambda: SimpleNamespace(
            peek=lambda key: SimpleNamespace(
                status=ProbeStatus.UNSUPPORTED, reason="image_unsupported"
            )
        ),
    )
    notes: list = []
    ctx = SimpleNamespace(on_trace_note=notes.append)
    prepared = SimpleNamespace(resolved=_resolved("qwen", "qwen-vl"))

    with call_scope(state=None, call_id="c1", tool_name="fs_view_image"):
        with pytest.raises(RuntimeError):
            await client._arequest_admitted_chat_completion(
                ctx=ctx, state=SimpleNamespace(), prepared=prepared
            )

    assert _call_ids(notes) == [("call", "c1")]


class _ChildState:
    def __init__(self):
        from gc_core.usage.account import UsageAccount

        self.state_id = "child-state"
        self.steps = 1
        self.shared = None
        self.backend_api_mode = "chat_completions"
        self.messages = []
        self.usage = UsageAccount()


class _ProbeTool:
    """Тул ребёнка: пометка изнутри его вызова, через контекст ребёнка."""

    def __init__(self, ctx=None, **kw):
        self._ctx = ctx

    async def acall(self) -> str:
        emit_trace_note(self._ctx, "отказ глаз ребёнка")
        return "{}"


def _isolate_child_run(monkeypatch):
    """Тяжёлые шаги прогона ребёнка — заглушками; контекст ребёнка и
    диспетчер вызовов — настоящие."""
    from gc_core.summarize.config import resolve_summary_limits
    from gc_core.tools.subagents import runner as runner_mod

    class _Inst:
        async def afinalize_role(self, ctx, state):
            return None

        def get_initial_data(self):
            return "role", "assignment"

    async def _create(*a, **k):
        return _Inst()

    # Класс — из модуля, который диспетчер найдёт сейчас, а не из взятого
    # при сборке: соседи по прогону, снимающие пакет тулов, оставляют в
    # sys.modules другую копию, и подмена старого класса била бы мимо.
    runner_cls = runner_mod.SubagentRunner
    monkeypatch.setattr(
        runner_cls,
        "_build_child_state",
        as_coro(lambda self, role: _ChildState()),
    )
    monkeypatch.setattr(
        runner_cls, "_build_initial_messages", lambda *a, **k: []
    )
    monkeypatch.setattr(runner_cls, "_log_lifecycle", lambda *a, **k: None)
    monkeypatch.setattr(
        runner_mod,
        "get_subagent_class",
        lambda name, source: SimpleNamespace(create_instance=_create),
    )
    monkeypatch.setattr(
        runner_mod, "init_subagent_open_turn", lambda *a, **k: None
    )
    monkeypatch.setattr(
        runner_mod, "get_thread_memory_registers_from_shared", lambda s: {}
    )
    monkeypatch.setattr(
        runner_mod, "subagent_thread_memory_bootstrap_text", lambda c, m: ""
    )
    monkeypatch.setattr(
        runner_mod,
        "resolve_summary_limits_at_level",
        lambda cfg, router, level: resolve_summary_limits(None),
    )


async def test_subagent_notes_owner_through_the_real_dispatcher(
    tmp_path, monkeypatch
):
    """Настоящий путь: диспетчер родителя запускает субагента (вызов
    `launch`), ребёнок через свой контекст пишет пометку вне тулов и
    зовёт свой тул (`child`) тем же диспетчером. Пометка вне тулов
    принадлежит запуску, пометка тула — вызову ребёнка."""
    from gc_core.tools import aapply_tool_result
    from gc_core.usage.account import UsageAccount

    _isolate_child_run(monkeypatch)
    config = {
        "paths.logs_dump_dir": str(tmp_path),
        "logging.save_logs": True,
        "logging.format": "jsonl",
        "logging.console_trace": False,
        "backend.api_mode": "chat_completions",
        "backend.completions_url": "http://localhost:1/v1/chat/completions",
        "agent.model": "test-model",
        "launch_root": str(tmp_path),
        "agent.use_local_subagents": True,
    }
    notes: list = []
    ctx = await rc.acreate_run_context(
        make_settings(config), on_trace_note=notes.append
    )
    orch = SimpleNamespace(
        usage=UsageAccount(),
        default_model="test-model",
        stop_requested=False,
        state_id="orch",
        discard_writes_after_stop=False,
    )
    probe = {"probe": {"tool_fabric": _ProbeTool, "spec": {}}}

    async def child_run(messages, child_state, child_ctx, **kwargs):
        emit_trace_note(child_ctx, "отказ модели ребёнка")
        await aapply_tool_result(
            "probe", {}, probe, child_ctx, call_id="child"
        )
        return "done"

    await aapply_tool_result(
        "subagent_worker",
        {"reason": "r", "task": "t"},
        None,
        ctx,
        state=orch,
        run_agent_func=child_run,
        call_id="launch",
    )

    assert _call_ids(notes) == [("call", "launch"), ("call", "child")]


def _eyes_router(assign: dict) -> "ModelRouter":
    from gc_core.config_modules.root import AppConfig
    from gc_core.model_router import ModelRouter

    snapshot = AppConfig.model_validate(
        {
            "models": {
                "deepseek": {
                    "model": "deepseek-v4-flash",
                    "url": "http://ds/v1/chat/completions",
                    "token": "",
                },
                "gemini": {
                    "model": "gemini-3.5-flash",
                    "url": "http://gm/v1/chat/completions",
                    "token": "",
                },
            },
            "slots": {"default": "deepseek"},
            "assign": assign,
        }
    )
    return ModelRouter(snapshot, choice_provider=lambda: "default")


def _probe_note(
    *, assign: dict, tools: list, blind: str = "main", result=None
) -> list:
    from gc_core.vision.probe import ProbeResult

    notes: list = []
    router = _eyes_router(assign)
    ctx = SimpleNamespace(
        on_trace_note=notes.append,
        model_router=router,
        settings=make_settings(),
    )
    state = SimpleNamespace(vision_probe_turn="root", tools=tools)
    if result is None:
        result = ProbeResult(ProbeStatus.UNSUPPORTED, "image_unsupported")

    note_probe(ctx, state, "key", router.resolve(blind), result)
    return notes


_VIEW_TOOL = [{"name": "fs_view_image"}]


def test_probe_note_is_neutral_when_the_agent_views_through_the_eyes():
    """Владелец 2026-10-24: тул зрения есть — не сбой, а серая пометка
    с именами основной модели и модели зрения."""
    notes = _probe_note(assign={"vision": "gemini"}, tools=_VIEW_TOOL)

    assert notes == [TraceNote(_VIA_EYES, "reply", None, "info")]


@pytest.mark.parametrize(
    "assign, tools",
    [({}, _VIEW_TOOL), ({"vision": "gemini"}, [])],
    ids=["no-eyes-assigned", "view-tool-off"],
)
def test_probe_note_stays_a_warning_without_the_vision_tool(assign, tools):
    """Тот же текст, что у пометки после реального отказа (`note_blind`)."""
    notes = _probe_note(assign=assign, tools=tools)

    assert notes == [TraceNote(_MAIN_NOBODY, "reply")]
    assert notes[0].tone == "warn"


def test_probe_note_of_the_vision_model_itself_names_the_eyes():
    """Отказ пробы маршрута «глаз» (не модели запроса) — вариант 3."""
    notes = _probe_note(
        assign={"vision": "gemini"}, tools=_VIEW_TOOL, blind="vision"
    )

    assert notes == [TraceNote(_EYES_BLIND, "reply")]


@pytest.mark.parametrize(
    "status, reason, text",
    [
        (
            ProbeStatus.TRANSIENT,
            "network",
            "временный сетевой сбой проверки; повтор возможен в новом ходе "
            "после паузы",
        ),
        (
            ProbeStatus.INSUFFICIENT_BUDGET,
            "output_limit",
            "ответ на проверку усечён по лимиту 512 токенов",
        ),
        (
            ProbeStatus.UNCONFIRMED,
            "code_mismatch",
            "модель не прочитала проверочную картинку",
        ),
        (
            ProbeStatus.BLOCKED,
            "access_denied",
            "ошибка доступа при проверке зрения",
        ),
        (
            ProbeStatus.BLOCKED,
            "route_configuration",
            "ошибка настроек маршрута при проверке зрения",
        ),
        (
            ProbeStatus.TRANSIENT,
            "probe_cancelled",
            "проверка зрения отменена",
        ),
        (
            ProbeStatus.BLOCKED,
            "probe_payload_rejected",
            "маршрут отклонил даже маленькую проверочную картинку (413)",
        ),
    ],
)
def test_other_probe_reasons_keep_their_texts(status, reason, text):
    from gc_core.vision.probe import ProbeResult

    notes = _probe_note(
        assign={"vision": "gemini"},
        tools=_VIEW_TOOL,
        result=ProbeResult(status, reason),
    )

    assert notes == [
        TraceNote(
            f"deepseek: {text}. Пользовательское изображение не отправлено.",
            "reply",
        )
    ]


def test_transient_eyes_that_cannot_probe_note_matches_the_stub():
    """Одно правило «глазам можно смотреть»: временный сбой пробы «глаз»,
    когда повтор в этом ходе запрещён (`can_probe` false), — тул скрыт
    (заглушка «видеть некому»), и пометка не обещает «агент посмотрит»."""
    import asyncio

    from gc_core.vision.audience import compute_audience, route_id
    from gc_core.vision.probe import ProbeResult
    from gc_core.vision.registry import process_vision_probes
    from gc_core.vision.route import VisionRouteKey

    async def transient():
        return ProbeResult(ProbeStatus.TRANSIENT, "network")

    notes: list = []
    router = _eyes_router({"vision": "gemini"})
    main, eyes = router.resolve("main"), router.resolve("vision")
    main_key = VisionRouteKey(main.url, main.model, "main")
    eyes_key = VisionRouteKey(eyes.url, eyes.model, "eyes")
    probes = process_vision_probes()
    probes.mark_unsupported(main_key, reason="image_unsupported")
    asyncio.run(probes.ensure(eyes_key, turn_key="root", run=transient))
    routes = {route_id(main): main_key, route_id(eyes): eyes_key}
    ctx = SimpleNamespace(
        on_trace_note=notes.append,
        model_router=router,
        settings=make_settings(),
    )
    state = SimpleNamespace(
        vision_probe_turn="root", tools=_VIEW_TOOL, vision_routes=routes
    )

    audience = compute_audience(
        router,
        request_resolved=main,
        api_mode="chat_completions",
        tool_in_set=True,
        settings=ctx,
        routes=routes,
        turn_key="root",
    )
    note_probe(
        ctx,
        state,
        main_key,
        main,
        ProbeResult(ProbeStatus.UNSUPPORTED, "image_unsupported"),
    )

    assert audience.tool_visible is False
    assert notes == [TraceNote(_MAIN_NOBODY, "reply")]
