import asyncio
import functools
import gc
import inspect
import json

# Переполнение контекста → force-компакция + повтор запроса; лимит на случай,
# когда даже после сжатия история всё ещё не влезает (единичное огромное сообщение).
import logging
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from gc_core.config import parse_chat_completions_max_tokens
from gc_core.config_modules.gc import gc_force_collect_enabled
from gc_core.config_modules.images import read_images_config
from gc_core.config_modules.strip_tags_projection import (
    persist_reasoning_content_for_model,
    projection_tags_from_config,
)
from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    FINISH_TOOL_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
    SESSION_COMPACT_TOOL_NAME,
    SESSION_TODO_TOOL_NAME,
    TURN_FINALIZING_TOOLS,
)
from gc_core.execution.constants import (
    _MAX_CONTEXT_OVERFLOW_RETRIES,
    _SESSION_COMPACT_RESUME_USER_TEXT,
    _TRUNCATED_ARGUMENTS_HINT,
)
from gc_core.execution.contracts import ExecutionFailure, ModelRequestStopped
from gc_core.execution.model import arequest_model
from gc_core.execution.output_wrapping import (
    maybe_store_and_wrap_tool_output,
)
from gc_core.execution.ports import port
from gc_core.execution.progress import (
    brief_args_summary,
    emit_progress,
    extract_intermediate_prose,
    parse_tool_result_status,
    todo_items_from_arguments,
)
from gc_core.intent_guard.guard import FinishGateGuard
from gc_core.intent_guard.text_answer_judge import ashould_return_text_answer
from gc_core.intent_guard.turn_finish_emit import (
    apply_finish_gate_to_turn_finish_output_in_state,
    get_function_call_output_text,
)
from gc_core.intent_scope.final_answer import amaybe_record_final_answer_intent
from gc_core.intent_scope.hook import amaybe_update_intent_scope_from_tool
from gc_core.loop_detector.detector import clear as clear_loop_detector

# Детектор зацикливания приехал с develop синхронным. Оставлен как есть: его
# наблюдатели — работа с памятью на микросекундах, нативной async-формы у них нет
# (правило ветки: «есть нативная форма — идём через неё; формы нет, работа
# короткая — инлайн на loop»).
from gc_core.loop_detector.detector import flush as flush_loop_detector
from gc_core.loop_detector.detector import observe as observe_loop_detector
from gc_core.tools.arguments_validation import arguments_without_service_fields
from gc_core.message_policy.attachments_outbound import (
    compute_live_ids,
    count_live_images,
    project_outbound,
    request_carries_images,
)
from gc_core.message_policy.chat_completions import is_bridge_echo
from gc_core.message_policy.no_tool_call_bounce import (
    NO_TOOL_CALL_BOUNCE_MESSAGE,
    extract_last_assistant_text,
    format_no_tool_call_bounce,
)
from gc_core.message_policy.stop_persist import is_persist_discarded
from gc_core.message_policy.tool_attachments import (
    build_tool_attachments_message,
)
from gc_core.message_policy.tool_usage_notice.apply import (
    apply_tool_usage_notice_before_llm,
)
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_ERROR_MESSAGE,
    TECHNICAL_TYPE_TECHNICAL,
    build_invalid_tool_call_feedback_message,
    extract_real_user_message_text,
    wrap_technical_user_content,
)
from gc_core.message_policy.user_stop import (
    not_started_due_to_user_stop_output,
)
from gc_core.model_router import AliasCircuitOpenError
from gc_core.request_backend_policy import (
    arequest_chat_completions_with_policy,
)
from gc_core.run_summary_coordinator import (
    build_preserved_batch,
    filter_messages_not_in_state,
)
from gc_core.runtime_notices import maybe_push_llm_failure_notice
from gc_core.settings import AppConfig, settings_of
from gc_core.states import State
from gc_core.summarize.limit_trace_summary import abuild_limit_envelope
from gc_core.tools import (
    WrongFunctionArgumentsException,
    aapply_tool,
    is_internal_tool_error_output,
    is_unknown_tool_output,
)
from gc_core.tools.subagents.sidecar import apersist_subagent_trace
from gc_core.turn_boundary import begin_turn
from gc_core.usage.account import CATEGORY_MAIN as USAGE_CATEGORY_MAIN
from gc_core.usage.account import UsageAccount
from gc_core.usage.context import usage_scope
from gc_core.usage.normalize import prompt_input_tokens
from gc_core.utils.aio_compat import await_callback
from gc_core.utils.backend_errors import (
    FINISH_KIND_LENGTH,
    classify_finish_kind,
    finish_blocks_final,
    finish_prevents_text_final,
    is_auth_error,
    is_context_overflow_error,
    is_image_request_error,
    is_model_unavailable_error,
    is_previous_response_cache_miss,
)
from gc_core.utils.connect_backend import (
    NO_BACKEND_OVERRIDE,
    acreate_call_headers,
    acreate_main_and_vision_headers,
    aresolve_auth_token,
    backend_override_value,
    create_call_data_completions,
    resolve_network_params,
)
from gc_core.utils.connect_backend import (
    arequest_backend_responses as arequest_backend,
)
from gc_core.utils.connect_backend import (
    create_call_data_responses as create_call_data,
)
from gc_core.utils.convertations import (
    output_chat_completion_to_response,
    sanitize_messages_for_chat_completions,
)
from gc_core.utils.format_messages import format_message_responses
from gc_core.utils.generate_message_id import (
    generate_msg_id_sha256 as generate_message_id_func,
)
from gc_core.utils.llm_chat_client import snapshot_chat_completion
from gc_core.utils.logging_module import human_console_enabled
from gc_core.vision.audience import (
    Audience,
    _declared_blind,
    _resolve_eyes,
    compute_audience,
    route_id,
    tools_for_request,
    view_tool_in_set,
)
from gc_core.vision.notes import note_blind
from gc_core.vision.rejects import (
    ImageRejects,
    ImageRequestLimit,
    learn_image_cap,
    next_image_cap,
    process_image_rejects,
)
from gc_core.vision.route import vision_route_key
from gc_core.vision.service import aensure_route

_fallback_logger = logging.getLogger(__name__)


async def _acall_blocking_hook(fn, *args: Any, **kwargs: Any):
    """Позвать «тяжёлый» колбэк вызывающего, не занимая event loop.

    Ядро передаёт сюда КОРУТИННЫЕ имена хуков, и такой хук ждётся нативно —
    ни потока, ни заморозки loop. Ветка с потоком осталась для чужих
    вызывающих, которые передали обычную функцию: позвать её «нативно
    асинхронно» нельзя в принципе, а звать инлайн значит заморозить loop
    приложения на всё время суммаризации.

    Что ядро не сползёт обратно на синхронные имена, стережёт AST-тест
    ``test_loop_hooks_are_native``.
    """
    if fn is None:
        return None
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(lambda: fn(*args, **kwargs))


async def _maybe_persist(
    callback,
    messages: list,
    state,
    *,
    pairing: bool = False,
) -> None:
    """Вызвать persist callback, если не активирован discard-after-stop guard.

    ``pairing=True`` — technical closing A/B при user-stop пишем всегда
    (иначе orphan function_call в jsonl).

    Колбэк по контракту возвращает ``None`` либо awaitable: ядро передаёт сюда
    асинхронный (история треда может лежать в базе, и ждать её надо нативно),
    а внешние вызывающие и тесты — обычный синхронный.
    """
    if callback is None:
        return
    if is_persist_discarded(state, pairing=pairing):
        return
    token = False
    if pairing:
        token = True
        state.allow_pairing_persist = True
    try:
        await await_callback(callback(messages))
    except ExecutionFailure:
        raise
    except Exception as persist_exc:
        _fallback_logger.warning(
            "Thread messages persist warning: %s",
            persist_exc,
            exc_info=True,
        )
    finally:
        if token:
            state.allow_pairing_persist = False


def _function_call_output_message(call_id: Any, output: str) -> Dict:
    return {
        "id": generate_message_id_func("fco"),
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }


async def _commit_user_stop_pairing(
    state: State,
    outputs: List[Dict],
    persist_messages_callback: Optional[Callable[[List[Dict]], None]],
) -> None:
    """Записать A/B в state.messages + jsonl несмотря на discard_writes_after_stop."""
    if not outputs:
        return
    to_append = filter_messages_not_in_state(
        list(getattr(state, "messages", []) or []),
        list(outputs),
    )
    if to_append:
        state.messages.extend(to_append)
    await _maybe_persist(
        persist_messages_callback,
        to_append or list(outputs),
        state,
        pairing=True,
    )


def _summarize_call_data_for_log(call_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Короткая сводка запроса для корреляции с ответом backend.
    """
    messages = call_data.get("messages")
    tools = call_data.get("tools")
    return {
        "model": call_data.get("model"),
        "max_tokens": call_data.get("max_tokens"),
        "messages_count": len(messages)
        if isinstance(messages, list)
        else None,
        "tools_count": len(tools) if isinstance(tools, list) else None,
    }


def _without_tool_call_pairs(
    messages: List[Dict], tool_name: str
) -> List[Dict]:
    """Убрать пары function_call + function_call_output для ``tool_name`` из preserved batch."""
    out: List[Dict] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if (
            i + 1 < len(messages)
            and isinstance(msg, dict)
            and msg.get("type") == "function_call"
            and str(msg.get("name") or "") == tool_name
        ):
            i += 2
            continue
        out.append(msg)
        i += 1
    return out


def _make_technical_feedback_user_message(
    content: str,
    *,
    technical_type: str = TECHNICAL_TYPE_TECHNICAL,
    call_id: str | None = None,
) -> Dict[str, str]:
    msg = {
        "role": "user",
        "content": wrap_technical_user_content(
            content, technical_type=technical_type
        ),
        "id": generate_message_id_func("m"),
    }
    if call_id:
        msg["call_id"] = call_id
    return msg


def _is_previous_response_cache_miss(exc: Exception) -> bool:
    return is_previous_response_cache_miss(exc)


def _get_backend_api_mode(ctx: Any) -> str:
    raw_mode = str(settings_of(ctx).backend.api_mode).strip().lower()
    if raw_mode in {"responses", "chat_completions"}:
        return raw_mode
    raise ValueError(f"Unsupported backend_api_mode: {raw_mode!r}")


def _mode_requires_backend_call(mode: str) -> bool:
    return mode in {"user_ask", "function_call_output"}


def _raise_honest_main_unavailable(
    ctx: Any, resolved: Any, reason: str, source_exc: BaseException
) -> None:
    """Класс 2 фоллбэков недоступности (спека §8): исчерпаны существующие ретраи
    (transport/adaptive max_tokens/401-refresh) для активной модели main-трейса.
    Никаких тихих подмен — честная ошибка хода с моделью/endpoint/причиной и
    подсказкой спасения; ставит per-alias предохранитель + notice."""
    router = getattr(ctx, "model_router", None)
    if router is not None and resolved.alias is not None:
        router.breaker.trip(resolved.alias, reason)
    maybe_push_llm_failure_notice(
        source_exc,
        request_label="main_agent",
        url=resolved.url,
        enabled=bool(getattr(ctx, "is_tui", False)),
    )
    message = (
        f"Модель '{resolved.model}' (алиас '{resolved.alias}') недоступна по адресу "
        f"{resolved.url}: {reason}. Переключите уровень /level или профиль /profiles."
    )
    logger = getattr(ctx, "logger", None) or _fallback_logger
    logger.error(message)
    raise RuntimeError(message) from source_exc


def _resolve_main_for_turn(state: State, ctx: Any) -> Optional[Any]:
    """Резолв `main` по уровню, ЗАФИКСИРОВАННОМУ на ход (`turn_boundary.begin_turn`).

    Контракт §6.4 — «внутри хода модель не перечитывается» — держится не на
    `state.default_model` (воронка берёт модель из `ResolvedModel`, а не оттуда), а
    на пине уровня: `/level` меняется без пересборки executor, и живой резолв уводил
    бы url/токен/модель на другой слот прямо посреди цикла tool-call'ов.

    Предохранитель проверяется КАЖДЫЙ раз (алиас мог открыться посреди хода) —
    `resolve_level` идёт тем же путём `_resolve_alias`, что и `resolve("main")`.
    Пина нет (ход не начинали через `begin_turn` — легаси/тестовые двойники) или
    Router дочерний (у субагента уровень уже заморожен в самом Router'е) — обычный
    резолв. Без `ctx.model_router` — `None`, воронки читают cfg напрямую как раньше."""
    router = getattr(ctx, "model_router", None)
    if router is None:
        return None
    return router.resolve_main_at_level(getattr(state, "turn_level", None))


def _maybe_trip_and_raise_main_unavailable(
    state: State, ctx: Any, exc: Exception
) -> None:
    """Классифицирует `exc` (спека §8) и, если это подтверждённая недоступность
    активной модели main-трейса, поднимает честную ошибку (см. выше). Иначе —
    no-op, вызывающий код должен `raise` исходное исключение без изменений.

    Резолв — по модели ХОДА (`_resolve_main_for_turn`), не по живому уровню: и
    предохранитель, и текст ошибки обязаны назвать тот алиас, которым запрос
    реально шёл, иначе `/level` посреди хода закрыл бы живую модель вместо мёртвой.

    Пропускает ЛЮБОГО ребёнка-субагента (`router.is_subagent_child`, пинованного
    или нет): его class-1 рестарт/деградацию ведёт `SubagentRunner`, а не главный
    трейс — осознанно, не только для явно пинованных."""
    if is_context_overflow_error(exc):
        return
    if not is_model_unavailable_error(exc):
        return
    router = getattr(ctx, "model_router", None)
    if router is None or getattr(router, "is_subagent_child", False):
        return
    try:
        resolved = _resolve_main_for_turn(state, ctx)
    except AliasCircuitOpenError as open_exc:
        resolved = open_exc.resolved
    if resolved.alias is None:
        return  # легаси — предохранитель не ведётся
    reason = str(exc).strip()[:300] or exc.__class__.__name__
    _raise_honest_main_unavailable(ctx, resolved, reason, exc)


def begin_turn_main_trace(state: State, ctx: Any) -> None:
    """Начало хода ГЛАВНОГО трейса (спека §6.4/§8): фиксирует модель хода
    (`turn_boundary.begin_turn`) и, если активный алиас уже в предохранителе
    (trip прошлого хода / персистированный breaker при attach), поднимает честную
    ошибку класса 2 ДО похода в транспорт — с той же моделью/endpoint/причиной и
    подсказкой /level //profiles, что и `_request_backend_canonical`.

    `begin_turn` резолвит `main` без `check_breaker=False`, поэтому открытый алиас
    прилетает сюда сырым `AliasCircuitOpenError`; переводим его в тот же честный
    класс 2 через `_maybe_trip_and_raise_main_unavailable` (единый текст, без
    дублирования). Субагентские `begin_turn` идут мимо — их class-1 ведёт
    `SubagentRunner`, а обёртку зовут только main-трейс call-site'ы."""
    try:
        begin_turn(state, ctx)
    except AliasCircuitOpenError as exc:
        _maybe_trip_and_raise_main_unavailable(state, ctx, exc)
        raise


def _request_audience(
    state: State,
    ctx: Any,
    *,
    resolved: Optional[Any],
    api_mode: str,
) -> Audience:
    """«Кто смотрит» на картинки этого запроса (спека §9.1). Считается на каждый
    запрос и кладётся в ``state.vision_audience`` — оттуда его читает тул
    ``fs_view_image`` (тулы в контроллер не ходят). Без ``ctx.model_router``
    (``resolved is None``: легаси-двойники тестов) видеть некому. Допуск зависит только от результатов проверки эффективных маршрутов."""
    if resolved is None:
        audience = Audience(native=None, eyes=None, tool_visible=False)
    else:
        audience = compute_audience(
            ctx.model_router,
            request_resolved=resolved,
            api_mode=api_mode,
            tool_in_set=view_tool_in_set(getattr(state, "tools", None) or []),
            settings=ctx,
            routes=getattr(state, "vision_routes", {}),
            turn_key=getattr(state, "vision_probe_turn", None),
        )
    state.vision_audience = audience
    return audience


def _image_bytes_reader(ctx: Any) -> Callable[[Any], Optional[bytes]]:
    """Чем проекция читает байты вложений — хранилищем картинок контекста.
    Контекст, собранный в обход фабрики, хранилища не имеет: байт нет, живая
    картинка уходит запиской «файл недоступен» — как при удалённом файле."""
    store = getattr(ctx, "image_store", None)
    if store is None:
        return lambda _attachment: None
    return store.read_for_send


def _read_once(
    read_bytes: Callable[[Any], Optional[bytes]],
) -> Callable[[Any], Optional[bytes]]:
    """Байты картинок на один запрос: проекция идёт по двум спискам (state и батч),
    картинки батча есть в обоих — читать и сверять файл второй раз незачем."""
    cache: Dict[str, Optional[bytes]] = {}

    def read(attachment: Any) -> Optional[bytes]:
        if attachment.id not in cache:
            cache[attachment.id] = read_bytes(attachment)
        return cache[attachment.id]

    return read


async def _arequest_backend_canonical(
    messages: List[Dict],
    state: State,
    ctx: Any,
    *,
    set_previous_response_id: bool = True,
) -> Tuple[List[Dict], str, Dict]:
    operations = port(ctx, "operations")
    if operations is None:
        return await _arequest_backend_canonical_local(
            messages,
            state,
            ctx,
            set_previous_response_id=set_previous_response_id,
        )
    result = await arequest_model(
        operations,
        state,
        messages,
        consumer="main",
        set_previous_response_id=set_previous_response_id,
    )
    return result.messages, result.response_id or "", result.response_metadata


async def _arequest_backend_canonical_local(
    messages: List[Dict],
    state: State,
    ctx: Any,
    *,
    set_previous_response_id: bool = True,
) -> Tuple[List[Dict], str, Dict]:
    """Обёртка над одиночной попыткой: на 401 (истёкший токен доступа) пере-выпускает
    токен (`auth_connector.get_token(force=True)`) и повторяет запрос РОВНО один раз.
    Повторный 401 после свежего токена — настоящий auth-fail, уходит наверх внятно.
    Прочие ошибки — наверх без изменений. Все call-site получают это единообразно.

    Класс 2 фоллбэков недоступности (спека §8): открытый алиас активной модели —
    мгновенный отказ до похода в транспорт; подтверждённая недоступность после
    исчерпания ретраев — честная ошибка + предохранитель (см. `_raise_honest_main_unavailable`)."""
    auth_connector = getattr(ctx, "auth_connector", None)
    router = getattr(ctx, "model_router", None)
    if router is not None and not getattr(router, "is_subagent_child", False):
        try:
            _resolve_main_for_turn(state, ctx)
        except AliasCircuitOpenError as exc:
            _raise_honest_main_unavailable(ctx, exc.resolved, exc.reason, exc)

    try:
        return await _arequest_backend_canonical_once(
            messages,
            state,
            ctx,
            set_previous_response_id=set_previous_response_id,
        )
    except Exception as exc:
        if auth_connector is not None and is_auth_error(exc):
            run_logger = getattr(ctx, "logger", None) or _fallback_logger
            run_logger.warning(
                "backend auth 401 → пере-выпуск токена и повтор запроса один раз: %s",
                exc,
            )
            # Пере-выпуск — тот же блокирующий поход в сеть, что и обычный
            # `get_token`, только гарантированный: `force=True` мимо кэша.
            await aresolve_auth_token(auth_connector, force=True)
            try:
                return await _arequest_backend_canonical_once(
                    messages,
                    state,
                    ctx,
                    set_previous_response_id=set_previous_response_id,
                )
            except Exception as retry_exc:
                _maybe_trip_and_raise_main_unavailable(state, ctx, retry_exc)
                raise
        _maybe_trip_and_raise_main_unavailable(state, ctx, exc)
        raise


async def _arequest_responses_once(
    messages: List[Dict],
    state: State,
    ctx: Any,
    *,
    resolved: Any,
    call_headers: Dict[str, str],
    settings: AppConfig,
    set_previous_response_id: bool,
    network: tuple,
) -> Tuple[List[Dict], str, Dict]:
    # Адрес — backend.completions_url в обоих режимах API. Прежний плоский
    # `agent_url` в схеме не существует (строгая валидация его не пропустит).
    verify, cert, use_retry, request_timeout_sec, config_proxy = network
    url = settings.backend.completions_url
    # Нативного показа картинок в responses нет (спека §20.5): вложения уходят
    # записками, ключ attachments на провод не попадает, и ни одна картинка не
    # оседает в цепочке previous_response_id.
    audience = _request_audience(
        state,
        ctx,
        resolved=resolved,
        api_mode="responses",
    )
    state.vision_live_ids = frozenset()
    call_data = create_call_data(
        project_outbound(
            messages,
            audience=audience,
            live_ids=frozenset(),
            api_mode="responses",
            read_bytes=_image_bytes_reader(ctx),
        ),
        state,
        tools=tools_for_request(getattr(state, "tools", None) or [], audience),
        set_previous_response_id=set_previous_response_id,
        resolved=resolved,
        settings=settings,
    )
    with usage_scope(
        getattr(state, "usage", None), USAGE_CATEGORY_MAIN, "responses"
    ):
        res, res_headers = await arequest_backend(
            call_data,
            call_headers,
            url=url,
            verify=verify,
            cert=cert,
            use_retry=use_retry,
            logger=getattr(ctx, "logger", None),
            timeout=request_timeout_sec,
            config_proxy=config_proxy,
        )
    return res["output"], str(res["id"]), res_headers


def _chat_token_limits(
    settings: AppConfig, resolved_for_network: Any
) -> Tuple[int, int]:
    """(max_tokens, min_response_tokens) с учётом per-model оверрайдов backend."""
    # Лимиты токенов — тоже поля backend, и у модели реестра они СВОИ: окно
    # маленькой модели меньше глобального потолка, и без оверрайда адаптивный цикл
    # долбил бы её от чужого потолка к чужому полу. Тот же приём, что в служебной
    # воронке (`utils/llm_chat_client._send_chat_completion_text`).
    _max_tokens_override = backend_override_value(
        resolved_for_network, "chat_completions_max_tokens"
    )
    completions_max_tokens = parse_chat_completions_max_tokens(
        settings.backend.chat_completions_max_tokens
        if _max_tokens_override is NO_BACKEND_OVERRIDE
        else _max_tokens_override,
        default=8096,
    )
    _min_tokens_override = backend_override_value(
        resolved_for_network, "chat_completions_min_response_tokens"
    )
    min_response_tokens = parse_chat_completions_max_tokens(
        settings.backend.chat_completions_min_response_tokens
        if _min_tokens_override is NO_BACKEND_OVERRIDE
        else _min_tokens_override,
        default=1024,
    )
    return completions_max_tokens, min_response_tokens


def _tui_stream_enabled(settings: AppConfig) -> bool:
    """Мастер-свитч live-стриминга TUI: явный ``tui.stream`` побеждает, иначе
    legacy ``backend.stream`` (его дефолт True)."""
    if settings.tui.stream is not None:
        return bool(settings.tui.stream)
    return bool(settings.backend.stream)


def _first_choice_finish_reason(res: Dict[str, Any]) -> Any:
    _choices_raw = res.get("choices")
    if isinstance(_choices_raw, list) and len(_choices_raw) > 0:
        _ch0 = _choices_raw[0]
        if isinstance(_ch0, dict):
            return _ch0.get("finish_reason")
    return None


async def _asend_chat_completions(
    state: State,
    audience: Audience,
    live_ids: frozenset[str],
    *,
    sanitized_messages: List[Dict],
    sanitized_state_messages: List[Dict],
    read_bytes: Callable[[Any], Optional[bytes]],
    settings: AppConfig,
    resolved: Any,
    request_kwargs: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Один поход в транспорт под данные «кто смотрит» и живые картинки: проекция
    обоих списков и набор тулов запроса считаются от них (спека §5, §9.2). Что
    ушло модели целиком, кладётся в state — для тула `fs_view_image`."""
    backend_mode = "chat_completions"
    state.vision_live_ids = (
        live_ids if audience.native is not None else frozenset()
    )
    read_bytes = _read_once(read_bytes)
    chat_state = SimpleNamespace(
        messages=project_outbound(
            sanitized_state_messages,
            audience=audience,
            live_ids=live_ids,
            api_mode=backend_mode,
            read_bytes=read_bytes,
        ),
        tools=getattr(state, "tools", []),
        default_model=getattr(state, "default_model", None),
    )
    outbound_batch = project_outbound(
        sanitized_messages,
        audience=audience,
        live_ids=live_ids,
        api_mode=backend_mode,
        read_bytes=read_bytes,
    )
    request_tools = tools_for_request(
        getattr(state, "tools", None) or [], audience
    )

    def _create_payload(max_tokens: int) -> Dict[str, Any]:
        return create_call_data_completions(
            outbound_batch,
            chat_state,
            tools=request_tools,
            max_tokens=max_tokens,
            settings=settings,
            resolved=resolved,
        )

    with usage_scope(
        getattr(state, "usage", None), USAGE_CATEGORY_MAIN, backend_mode
    ):
        return await arequest_chat_completions_with_policy(
            payload_factory=_create_payload, **request_kwargs
        )


def _request_live_ids(
    merged_messages: List[Dict],
    images_config: Any,
    rejects: ImageRejects,
    *,
    resolved: Any,
    route_key=None,
) -> frozenset[str]:
    """Живые картинки запроса; потолок модели — из реестра (или пробный, в повторе
    после картиночной ошибки)."""
    cap = rejects.cap(route_key) if resolved is not None else None
    return compute_live_ids(
        merged_messages, images=images_config, max_images=cap
    )


def _image_request_rejects(state, resolved, live_ids, route_key):
    """Постоянные отказы плюс успешный потолок своего хода и контекста."""
    if resolved is None:
        return None, process_image_rejects()
    scope = (
        route_key,
        getattr(state, "vision_probe_turn", None),
        getattr(state, "user_turn_seq", 0),
        getattr(state, "dialog_steps_offset", 0),
        live_ids,
    )
    rejects = process_image_rejects()
    limit = getattr(state, "vision_request_limit", None)
    if limit is not None and limit.scope == scope:
        rejects = rejects.with_limit(route_key, limit.cap)
    return scope, rejects


async def _asend_with_image_fallback(
    state: State,
    ctx: Any,
    *,
    send: Callable[..., Any],
    resolved: Any,
    merged_messages: List[Dict],
    images_config: Any,
    route_key=None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Спека §10: запрос нёс живые картинки, а бэкенд ответил картиночной ошибкой
    (модель не видит, предел числа картинок, тело больше предела прокси) → повтор в
    том же шаге с меньшим числом картинок (`next_image_cap`: 0 — записки). Явный
    факт — отказ от зрения, названный предел — запоминается на процесс сразу
    (`learn_image_cap`); успешный потолок повторов — в state до нового хода,
    сжатия истории или смены живых картинок, модели или адреса. Каждый повтор
    шлёт строго меньше картинок — цепочка конечна; не
    прошёл даже запрос без картинок — наверх уходит его ошибка. Запрос без картинок
    и прочие ошибки — наверх без изменений (прежние пути)."""
    live_ids_for = functools.partial(
        _request_live_ids,
        merged_messages,
        images_config,
        resolved=resolved,
        route_key=route_key,
    )
    live_ids = live_ids_for(process_image_rejects())
    scope, rejects = _image_request_rejects(
        state, resolved, live_ids, route_key
    )
    audience = _request_audience(
        state,
        ctx,
        resolved=resolved,
        api_mode="chat_completions",
    )
    live_ids = live_ids_for(rejects)
    reject_log = getattr(ctx, "logger", None) or _fallback_logger
    trial_cap = None
    while True:
        try:
            result = await send(audience, live_ids)
            if trial_cap is not None:
                state.vision_request_limit = ImageRequestLimit(
                    scope, trial_cap
                )
            return result
        except Exception as exc:
            if not (
                request_carries_images(audience, live_ids)
                and is_image_request_error(exc)
            ):
                raise
            sent = count_live_images(merged_messages, live_ids)
            newly_blind = learn_image_cap(route_key, exc)
            trial_cap = next_image_cap(exc, sent)
            trial = process_image_rejects().with_limit(
                route_key,
                trial_cap,
            )
            audience = _request_audience(
                state,
                ctx,
                resolved=resolved,
                api_mode="chat_completions",
            )
            live_ids = live_ids_for(trial)
            reject_log.warning(
                "модель %s не приняла картинки (%s в запросе) → повтор с %s: %s",
                resolved.model,
                sent,
                count_live_images(merged_messages, live_ids),
                exc,
            )
            # Строка в ленте — только тому, кто впервые сделал модель слепой, и
            # только когда смотреть больше некому: при связке «слепая основная +
            # глаза» — тишина.
            if newly_blind and audience.nobody:
                note_blind(ctx, resolved, eyes=False, logger=reject_log)


async def _acreate_vision_call_headers(state, ctx, resolved):
    """Refresh the eyes' effective key without probing or fetching auth twice."""
    settings = settings_of(ctx)
    router = getattr(ctx, "model_router", None)
    eyes = _resolve_eyes(router) if router is not None else None
    if (
        eyes is None
        or _declared_blind(eyes, settings)
        or (
            settings.backend.api_mode == "chat_completions"
            and resolved is not None
            and route_id(eyes) == route_id(resolved)
        )
    ):
        return await acreate_call_headers(state, ctx, resolved=resolved)
    headers, eyes_headers = await acreate_main_and_vision_headers(
        state, ctx, resolved, eyes
    )
    prepared = snapshot_chat_completion(
        ctx, state, resolved=eyes, headers=eyes_headers
    )
    if not hasattr(state, "vision_routes"):
        state.vision_routes = {}
    state.vision_routes[route_id(eyes)] = vision_route_key(prepared)
    return headers


async def _aprepare_native_vision(
    state, ctx, resolved, call_headers, messages, settings
):
    """Register the authenticated route and probe only live image candidates."""
    if resolved is None:
        return None
    prepared = snapshot_chat_completion(
        ctx, state, resolved=resolved, headers=call_headers
    )
    key = vision_route_key(prepared)
    if not hasattr(state, "vision_routes"):
        state.vision_routes = {}
    state.vision_routes[route_id(resolved)] = key
    candidates = compute_live_ids(
        [*(getattr(state, "messages", []) or []), *messages],
        images=read_images_config(settings),
    )
    if candidates and not _declared_blind(resolved, settings):
        await aensure_route(ctx, state, prepared)
        from gc_core.vision.service import confirmed_route_headers

        call_headers.update(confirmed_route_headers(prepared))
    return key


async def _arequest_backend_canonical_once(
    messages: List[Dict],
    state: State,
    ctx: Any,
    *,
    set_previous_response_id: bool = True,
) -> Tuple[List[Dict], str, Dict]:
    """
    Унифицированный вызов backend для двух режимов API (одна попытка).

    Возвращает:
    - канонические сообщения (responses-like),
    - backend response id,
    - backend response headers (может быть пустым dict).
    """
    # Main-трейс: воронка получает весь ResolvedModel (url/authorization/
    # backend_overrides), не только id модели (спека §6/§7) — поэтому и резолв идёт
    # по уровню, зафиксированному на ход (`_resolve_main_for_turn`), а не по живому:
    # иначе /level посреди хода уводил бы запрос на другой слот. Без ctx.model_router
    # (легаси/тестовые двойники) — resolved=None, воронки читают настройки напрямую как раньше.
    resolved = _resolve_main_for_turn(state, ctx)
    call_headers = await _acreate_vision_call_headers(state, ctx, resolved)
    settings = settings_of(ctx)
    backend_mode = _get_backend_api_mode(ctx)
    # Per-model url/token/backend-квирки — chat_completions-only (спека §4): в responses
    # из ResolvedModel действует только id модели, сетевые параметры — из settings.backend.
    resolved_for_network = (
        resolved if backend_mode == "chat_completions" else None
    )
    (
        verify,
        cert,
        use_retry,
        request_timeout_sec,
        config_proxy,
        retry_embedded_choice_errors,
    ) = resolve_network_params(settings, resolved_for_network)

    if backend_mode == "responses":
        return await _arequest_responses_once(
            messages,
            state,
            ctx,
            resolved=resolved,
            call_headers=call_headers,
            settings=settings,
            set_previous_response_id=set_previous_response_id,
            network=(
                verify,
                cert,
                use_retry,
                request_timeout_sec,
                config_proxy,
            ),
        )

    route_key = await _aprepare_native_vision(
        state, ctx, resolved, call_headers, messages, settings
    )

    url = (
        resolved.url
        if resolved is not None
        else settings.backend.completions_url
    )
    sanitized_messages = sanitize_messages_for_chat_completions(
        messages,
        invalid_tool_call_factory=build_invalid_tool_call_feedback_message,
    )
    sanitized_state_messages = sanitize_messages_for_chat_completions(
        getattr(state, "messages", []) or [],
        invalid_tool_call_factory=build_invalid_tool_call_feedback_message,
    )
    # Лимиты токенов — тоже поля backend, и у модели реестра они СВОИ: окно
    # маленькой модели меньше глобального потолка, и без оверрайда адаптивный цикл
    # долбил бы её от чужого потолка к чужому полу. Тот же приём, что в служебной
    # воронке (`utils/llm_chat_client._send_chat_completion_text`).
    completions_max_tokens, min_response_tokens = _chat_token_limits(
        settings, resolved_for_network
    )

    _stream_on = _tui_stream_enabled(settings)
    _on_delta = getattr(ctx, "on_stream_delta", None) if _stream_on else None
    prompt_message_count = len(getattr(state, "messages", []) or [])

    send = functools.partial(
        _asend_chat_completions,
        state,
        sanitized_messages=sanitized_messages,
        sanitized_state_messages=sanitized_state_messages,
        read_bytes=_image_bytes_reader(ctx),
        settings=settings,
        resolved=resolved,
        request_kwargs={
            "initial_max_tokens": completions_max_tokens,
            "min_response_tokens": min_response_tokens,
            "headers": call_headers,
            "url": url,
            "verify": verify,
            "cert": cert,
            "use_retry": use_retry,
            "retry_embedded_choice_errors": retry_embedded_choice_errors,
            "salvage_reasoning": True,
            "logger": getattr(ctx, "logger", None),
            "on_delta": _on_delta,
            "request_label": "main_agent",
            "runtime_notices_enabled": bool(getattr(ctx, "is_tui", False)),
            "notice_url": url,
            "timeout": request_timeout_sec,
            "config_proxy": config_proxy,
        },
    )
    res, call_data = await _asend_with_image_fallback(
        state,
        ctx,
        send=send,
        resolved=resolved,
        # Вложения (спека §5): запрос собирается из ВСЕЙ истории плюс батч с
        # дедупликацией по id (`create_call_data_completions`). Живость считается по
        # слитой последовательности (повтор по id считается один раз), проекция
        # ложится на оба списка и сохраняет id — дедупликация видит те же ключи.
        merged_messages=[*sanitized_state_messages, *sanitized_messages],
        images_config=read_images_config(settings),
        route_key=route_key,
    )
    if not isinstance(res, dict):
        raise ValueError("chat/completions backend response must be an object")
    _run_log = getattr(ctx, "logger", None) or _fallback_logger
    _finish = _first_choice_finish_reason(res)
    # Сохраняем finish_reason на state — catch-блок WrongFunctionArgumentsException
    # использует его, чтобы отличить усечённый JSON (length) от обычной невалидности.
    state.last_finish_reason = _finish
    _run_log.info(
        "chat/completions response meta: max_tokens_requested=%s finish_reason=%s usage=%s",
        call_data.get("max_tokens"),
        _finish,
        res.get("usage"),
    )
    # Для режима chat/completions всегда используем только choices[0].
    first_choice_payload = dict(res)
    choices = res.get("choices")
    if isinstance(choices, list) and len(choices) > 0:
        first_choice_payload["choices"] = [choices[0]]
    persist_reasoning = persist_reasoning_content_for_model(resolved, settings)
    canonical = output_chat_completion_to_response(
        first_choice_payload,
        logger_override=getattr(ctx, "logger", _fallback_logger),
        log_context={
            "url": url,
            "request_summary": _summarize_call_data_for_log(call_data),
        },
        persist_reasoning_content=persist_reasoning,
    )
    resp_messages = canonical["output"]
    response_id = str(canonical.get("id") or res.get("id") or "")
    if not response_id:
        raise ValueError("chat/completions response id is empty")
    remember_main_prompt_tokens(
        state,
        res.get("usage"),
        prompt_message_count,
        backend_mode=backend_mode,
    )
    return resp_messages, response_id, {}


def remember_main_prompt_tokens(
    state: Any,
    usage_raw: Any,
    message_count: int,
    *,
    backend_mode: str,
) -> None:
    """Записать input последнего главного prompt этого State.

    Только успешный ``chat_completions``. Нет ``prompt_tokens`` / ``input_tokens`` —
    поля не трогаем. Ветка ``responses`` и любой другой вызов число не меняют.
    """
    if backend_mode != "chat_completions":
        return
    tokens = prompt_input_tokens(usage_raw)
    if tokens is None:
        return
    if (
        isinstance(message_count, bool)
        or not isinstance(message_count, int)
        or message_count < 0
    ):
        return
    state.last_main_prompt_tokens = tokens
    state.last_main_prompt_message_count = message_count


def _is_tool_outputs_with_technical_tail(messages: List[Dict]) -> bool:
    """Батч «ответы тулов + технический хвост» (напоминание loop detector, картинки
    тулов): хотя бы один ``function_call_output``, а кроме них — только
    user-сообщения без реплики человека (``extract_real_user_message_text`` пуст).
    Ни вызовов, ни ответа модели.

    Такой батч продолжает ответы тулов, а не открывает запрос человека. Реплика в
    батче (в т.ч. пара «текст + @-сноска» из очереди TUI) или вызов в нём (хвост
    ``session_compact`` несёт пары call/output) оставляют ``user_ask``.
    """
    has_output = False
    for message in messages:
        if message.get("type") == "function_call_output":
            has_output = True
        elif message.get("role") != "user" or extract_real_user_message_text(
            message
        ):
            return False
    return has_output


def check_mode(messages, logger) -> Union[str, None]:
    if len(messages) == 0:
        raise ValueError("Empty messages list")
    if messages[-1].get("role", None) == "user":
        if _is_tool_outputs_with_technical_tail(messages):
            return "function_call_output"
        return "user_ask"
    for message in messages:
        if message.get("type", None) == "function_call":
            return "function_call"
    for message in messages:
        if message.get("type", None) == "function_call_output":
            return "function_call_output"
    if messages[-1].get("role", None) == "assistant":
        return "text_response"

    logger.debug("Got Undefined mode for messages:")
    logger.debug(str(messages))

    raise ValueError("Undefined mode")


def _batch_has_pending_function_calls(messages: List[Dict]) -> bool:
    """True, если в батче есть function_call — их надо исполнить до step/token/tool лимитов
    и до разбора очереди реплик.

    Иначе лимит между «модель уже вернула tools» и process_tool_calls даёт
    orphan call без output и теряет заявленную работу (sidecar/диск/summary).
    """
    return any(
        message.get("type") == "function_call" for message in messages or []
    )


def group_messages(messages: List[Dict]) -> Dict[str, List[Dict]]:
    tool_calls = []
    text_messages = []
    tool_outputs = []
    for m in messages:
        m_type = m.get("type", "message")
        if m_type == "function_call":
            tool_calls.append(m)
        elif m_type == "message":
            text_messages.append(m)
        elif m_type == "function_call_output":
            tool_outputs.append(m)
        elif m_type == "reasoning":
            # Responses metadata stays in the original persisted batch;
            # it is not a tool or a user-visible assistant message.
            continue
        else:
            raise ValueError(f"message type error with: {m}")

    return {
        "tool_calls": tool_calls,
        "text_messages": text_messages,
        "tool_outputs": tool_outputs,
    }


async def _aask_backend_and_record(messages, state: State, ctx: Any):
    """Сходить в бэкенд каноническим путём и запомнить заголовки ответа.

    Общее тело `aprocess_user_ask` и `aprocess_tool_outputs`: точки вызова у них
    разные (вопрос пользователя и выдача тулов), и тесты подменяют каждую по
    имени, поэтому обе остаются, а повтор тела ушёл сюда.
    """
    (
        resp_messages,
        response_id,
        res_headers,
    ) = await _arequest_backend_canonical(
        messages,
        state,
        ctx,
    )
    state.set_resp_headers_metadata(res_headers or {})
    return resp_messages, response_id


async def aprocess_user_ask(messages, state: State, ctx: Any):
    """Ход по сообщению пользователя."""
    return await _aask_backend_and_record(messages, state, ctx)


def partition_work_then_finish(
    tool_calls: List[Dict],
) -> tuple[List[Dict], List[Dict]]:
    """Work в порядке батча, затем не больше одного ``turn_finish``.

    Work — все вызовы кроме ``turn_finish``. Позиция finish work не режет.
    Первый finish идёт после work, остальные — skip. Успешный
    ``turn_ask_human`` подавляет весь оставшийся work и finish уже по
    фактическому output, не здесь.
    """
    work: List[Dict] = []
    finish_calls: List[Dict] = []
    for call in tool_calls:
        if str(call.get("name") or "") == FINISH_TOOL_NAME:
            finish_calls.append(call)
        else:
            work.append(call)
    if not finish_calls:
        return work, []
    return work + [finish_calls[0]], finish_calls[1:]


def _first_finish_call(tool_calls: List[Dict]) -> Optional[Dict]:
    for call in tool_calls:
        if str(call.get("name") or "") == FINISH_TOOL_NAME:
            return call
    return None


def _output_for_call(messages: List[Dict], call_id: Any) -> Any:
    for msg in messages:
        if (
            msg.get("type") == "function_call_output"
            and msg.get("call_id") == call_id
        ):
            return msg.get("output")
    return None


def _closing_finish_after_batch(
    tool_calls: List[Dict],
    batch_outputs: List[Dict],
    *,
    stop_requested: bool,
) -> Optional[Dict]:
    """Закрывать ход или нет — только после батча, по фактическому output.

    Первый ``turn_finish`` исходного батча. ``ok: false`` (отказ тула / skip
    после ask) или user-stop — ход не сдаём. Иначе — этот вызов идёт в gate.
    """
    if stop_requested:
        return None
    finish = _first_finish_call(tool_calls)
    if finish is None:
        return None
    finish_ok, _ = parse_tool_result_status(
        _output_for_call(batch_outputs, finish.get("call_id"))
    )
    if finish_ok is False:
        return None
    return finish


def _split_batch_new_around_finish(
    batch_new: List[Dict],
    finish_call_id: Any,
) -> tuple[List[Dict], List[Dict], List[Dict]]:
    """Префикс (work), finish-вывод, хвост (notice/compact) — без перестановки.

    Разрез по позиции первого ``function_call_output`` с ``finish_call_id``.
    Technical user (loop-detector, session_compact resume) остаются после
    finish, иначе в трейсе получается tool → user → tool и strict-бэкенд
    отвечает 400. Если finish-вывода нет — весь батч считается префиксом.
    """
    finish_idx = None
    for i, msg in enumerate(batch_new):
        if (
            msg.get("type") == "function_call_output"
            and msg.get("call_id") == finish_call_id
        ):
            finish_idx = i
            break
    if finish_idx is None:
        return list(batch_new), [], []
    return (
        batch_new[:finish_idx],
        batch_new[finish_idx : finish_idx + 1],
        batch_new[finish_idx + 1 :],
    )


# finish_reason == "length": JSON вызова усечён потолком max_tokens — объясняем
# модели причину и советуем компактный вариант / разбиение на вызовы.


def _attach_truncation_hint(
    exc: WrongFunctionArgumentsException, state: Any
) -> None:
    """Дописать объяснение про усечение к фидбэку, не затирая подсказку самого
    тула (wrong_arguments_feedback_hint), которую положило место возбуждения."""
    if (
        classify_finish_kind(getattr(state, "last_finish_reason", None))
        != FINISH_KIND_LENGTH
    ):
        return
    hints = [
        h
        for h in (
            getattr(exc, "extra_hint", None),
            _TRUNCATED_ARGUMENTS_HINT,
        )
        if h
    ]
    exc.extra_hint = "\n\n".join(hints)


def _extra_finish_skip_outputs(skipped: List[Dict]) -> List[Dict]:
    # skip extra-finish готовим заранее, чтобы сфлашить их до ask_human вместе
    # с уже выполненными тулами.
    return [
        {
            "id": generate_message_id_func("fco"),
            "type": "function_call_output",
            "call_id": m["call_id"],
            "output": (
                f'{{"ok": false, "skipped": true, "message": '
                f'"Skipped: extra {FINISH_TOOL_NAME} in this batch; '
                f'one {FINISH_TOOL_NAME} per batch."}}'
            ),
        }
        for m in skipped
    ]


def _after_ask_human_skip_output(call_id: Any) -> Dict[str, Any]:
    return {
        "id": generate_message_id_func("fco"),
        "type": "function_call_output",
        "call_id": call_id,
        "output": (
            f'{{"ok": false, "skipped": true, "message": '
            f'"Skipped: {ASK_HUMAN_TOOL_NAME} already received the '
            f"user reply. This call was generated before that reply; "
            f'reconsider it based on the reply in a new batch."}}'
        ),
    }


def _not_started_outputs(
    remaining: List[Dict], state: State, ctx: Any
) -> List[Dict]:
    """Envelope B для вызовов, не стартовавших из-за user-stop."""
    outputs: List[Dict] = []
    for rem in remaining:
        rem_name = str(rem.get("name") or "")
        rem_out = not_started_due_to_user_stop_output(rem_name)
        rem_out = maybe_store_and_wrap_tool_output(
            rem_out,
            call_id=rem["call_id"],
            tool_name=rem_name,
            state=state,
            settings=ctx,
            logger=getattr(ctx, "logger", None),
        )
        outputs.append(_function_call_output_message(rem["call_id"], rem_out))
    return outputs


async def _aexecute_tool_call(
    m: Dict,
    tool_name: str,
    tool_arguments: str,
    state: State,
    ctx: Any,
    run_agent_func,
) -> Tuple[Any, bool, Any]:
    """Один вызов тула с live-прогрессом и sidecar-трейсом → (сырой output, невалидные ли аргументы, ok)."""
    tool_call_id = m.get("call_id")
    call_id_str = str(tool_call_id) if tool_call_id is not None else None
    # intermediate_reply в UI — просто текст «· …», не строка тула ▸/✓,
    # поэтому ниже шлём отдельный сигнал, без «старт тула».
    is_intermediate = tool_name == INTERMEDIATE_REPLY_TOOL_NAME
    operations = port(ctx, "operations")
    clock = operations.monotonic if operations is not None else time.monotonic
    started_at = clock()

    # Live-прогресс субагента: «тул пошёл». На обычном parent-прогоне — no-op.
    # Sidecar persist параллельно (нужен thread identity; sink не требуется).
    # ask_human: только banner (ask_human/ask_human_done); nested tool-row
    # без sidecar → Ctrl+click молча no-op — не монтируем.
    persist_tool_trace = (
        not is_intermediate and tool_name != ASK_HUMAN_TOOL_NAME
    )
    emit_tool_row = persist_tool_trace
    if emit_tool_row:
        _todo_items = (
            todo_items_from_arguments(tool_arguments)
            if tool_name == SESSION_TODO_TOOL_NAME
            else None
        )
        emit_progress(
            ctx,
            state,
            "tool_start",
            tool_name=tool_name,
            tool_call_id=call_id_str,
            args_summary=brief_args_summary(
                tool_arguments, tool_name=tool_name
            ),
            todo_items=_todo_items,
        )
    if persist_tool_trace:
        await apersist_subagent_trace(ctx, kind="call", payload=dict(m))
    was_invalid = False
    try:
        # call_id для субагента: чтобы вложенные строки в TUI знали,
        # под какой корневой subagent_* они относятся.
        state.tool_calls += 1
        tool_res_raw, was_invalid = await _ainvoke_tool(
            tool_name,
            tool_arguments,
            state,
            ctx,
            run_agent_func,
            tool_call_id,
        )
    except WrongFunctionArgumentsException as exc:
        # Отказ одного вызова — не отказ хода: невалидные аргументы становятся
        # output этого вызова (как unknown_tool), остальные вызовы батча
        # исполняются. Дальше общий путь: tool_end, sidecar, function_call_output.
        if getattr(exc, "call_id", None) is None:
            exc.call_id = tool_call_id
        _attach_truncation_hint(exc, state)
        was_invalid = True
        tool_res_raw = exc.to_tool_output()
    # Live-прогресс: тул закончил — или текст intermediate_reply.
    # В историю треда это не пишется.
    elapsed_ms = int((clock() - started_at) * 1000)
    ok, error_brief = parse_tool_result_status(tool_res_raw)
    if is_intermediate:
        await _aemit_intermediate_reply(
            ctx,
            state,
            tool_name,
            tool_arguments,
            tool_res_raw,
            call_id_str=call_id_str,
            elapsed_ms=elapsed_ms,
            ok=ok,
            error_brief=error_brief,
        )
        return tool_res_raw, was_invalid, ok
    if emit_tool_row:
        emit_progress(
            ctx,
            state,
            "tool_end",
            elapsed_ms=elapsed_ms,
            tool_name=tool_name,
            tool_call_id=call_id_str,
            ok=ok if ok is not None else True,
            error_brief=error_brief,
        )
    if persist_tool_trace:
        out_text = (
            tool_res_raw
            if isinstance(tool_res_raw, str)
            else json.dumps(tool_res_raw, ensure_ascii=False)
        )
        await apersist_subagent_trace(
            ctx,
            kind="error" if ok is False else "result",
            payload={
                "type": "function_call_output",
                "call_id": tool_call_id,
                "output": out_text,
            },
        )
    return tool_res_raw, was_invalid, ok


async def _ainvoke_tool(
    tool_name,
    tool_arguments,
    state,
    ctx,
    run_agent_func,
    tool_call_id,
):
    operations = port(ctx, "operations")
    output = await aapply_tool(
        tool_name=tool_name,
        arguments=tool_arguments,
        ctx=ctx,
        state=state,
        run_agent_func=run_agent_func,
        call_id=tool_call_id,
        **(
            {"tool_operation_id": operations.next_operation_id("tool")}
            if operations is not None
            else {}
        ),
    )
    return output, False


async def _aemit_intermediate_reply(
    ctx,
    state,
    tool_name,
    tool_arguments,
    tool_res_raw,
    *,
    call_id_str,
    elapsed_ms,
    ok,
    error_brief,
) -> None:
    prose = extract_intermediate_prose(tool_arguments, tool_res_raw)
    emit_progress(
        ctx,
        state,
        "intermediate_reply",
        elapsed_ms=elapsed_ms,
        tool_name=tool_name,
        tool_call_id=call_id_str,
        prose=prose,
        ok=ok if ok is not None else True,
        error_brief=error_brief,
    )
    ir_payload: Dict[str, Any] = {"prose": prose or ""}
    if call_id_str:
        ir_payload["call_id"] = call_id_str
    await apersist_subagent_trace(
        ctx, kind="intermediate_reply", payload=ir_payload
    )


def _track_repeat_errors(
    state: State, tool_name: str, tool_res_raw: Any
) -> bool:
    """Счётчики повторов unknown-tool / internal-error на state; → был ли unknown-tool."""
    is_unknown = is_unknown_tool_output(tool_res_raw)
    if is_unknown:
        if tool_name == getattr(state, "last_unknown_tool_name", None):
            state.unknown_tool_repeat_count = (
                int(getattr(state, "unknown_tool_repeat_count", 0) or 0) + 1
            )
        else:
            state.last_unknown_tool_name = tool_name
            state.unknown_tool_repeat_count = 1
    else:
        state.last_unknown_tool_name = None
        state.unknown_tool_repeat_count = 0

    if is_internal_tool_error_output(tool_res_raw):
        if tool_name == getattr(state, "last_internal_error_tool_name", None):
            state.internal_error_repeat_count = (
                int(getattr(state, "internal_error_repeat_count", 0) or 0) + 1
            )
        else:
            state.last_internal_error_tool_name = tool_name
            state.internal_error_repeat_count = 1
    else:
        state.last_internal_error_tool_name = None
        state.internal_error_repeat_count = 0
    return is_unknown


def _apply_session_compact_to_batch(
    state: State,
    batch_snapshot: List[Dict],
    tools_outputs: List[Dict],
) -> List[Dict]:
    # Если внутри batch сработал session_compact — вместо сырых output'ов
    # вернуть сохранённые пары call/output + technical-сообщение «продолжай»,
    # чтобы не оставить висячий function_call и не тащить старое окно дальше.
    state.session_compact_applied = False
    snapshot = (
        getattr(state, "_session_compact_batch_snapshot", None)
        or batch_snapshot
    )
    if hasattr(state, "_session_compact_batch_snapshot"):
        delattr(state, "_session_compact_batch_snapshot")
    preserved = build_preserved_batch(
        current_messages=[*snapshot, *tools_outputs],
        mode="function_call_output",
        state_messages=list(getattr(state, "messages", []) or []),
    )
    preserved = _without_tool_call_pairs(preserved, SESSION_COMPACT_TOOL_NAME)
    resume = _make_technical_feedback_user_message(
        _SESSION_COMPACT_RESUME_USER_TEXT,
        technical_type=TECHNICAL_TYPE_TECHNICAL,
    )
    return [*preserved, resume]


async def _afinalize_tool_batch(
    state: State,
    ctx: Any,
    tools_outputs: List[Dict],
    batch_snapshot: List[Dict],
    *,
    aborted_by_user_stop: bool,
    persist_messages_callback: Optional[Callable[[List[Dict]], None]],
) -> List[Dict]:
    """Хвост батча: user-stop пары, session_compact, notice loop-детектора, gc."""
    if aborted_by_user_stop:
        # run_agent после stop часто выходит на check_stop до обычного persist —
        # закрывающие A/B пишем сразу (исключение discard_writes).
        await _commit_user_stop_pairing(
            state, tools_outputs, persist_messages_callback
        )
        # Прогон остановлен: notice агенту не нужен, окно прошлого батча
        # не должно всплыть при продолжении сессии.
        clear_loop_detector(state)
        # Картинки батча тоже: ход остановлен, смотреть их некому.
        state.pending_tool_attachments = []

    if getattr(state, "session_compact_applied", False):
        tools_outputs = _apply_session_compact_to_batch(
            state, batch_snapshot, tools_outputs
        )

    notice = flush_loop_detector(state)
    if notice:
        tools_outputs.append(
            _make_technical_feedback_user_message(
                notice,
                technical_type=TECHNICAL_TYPE_TECHNICAL,
            )
        )

    # Картинки из тулов и из ответа на turn_ask_human — одним техническим
    # сообщением в самом конце батча (спека §7.1): tool-сообщение картинку не
    # несёт. После принятого turn_finish оно уходит вместе с хвостом
    # (`_split_batch_new_around_finish`).
    pending_attachments = getattr(state, "pending_tool_attachments", None)
    state.pending_tool_attachments = []
    if pending_attachments:
        tools_outputs.append(
            build_tool_attachments_message(
                pending_attachments,
                message_id=generate_message_id_func("m"),
            )
        )

    # C3: один collect на границе «батч тулов → LLM-шаг» — подбираем циклический
    # мусор между шагами. Default on для CLI/TUI, off для server; явный
    # gc.force_collect в конфиге перекрывает default.
    if gc_force_collect_enabled(
        ctx,
        is_tui=bool(getattr(ctx, "is_tui", False)),
        is_cli_mode=bool(getattr(ctx, "is_cli_mode", False)),
    ):
        # Рантайм может заменить сборку (Temporal: общий worker не
        # останавливается ради одного workflow); локально — gc.collect.
        collect = getattr(ctx, "collect_garbage", None)
        (collect or gc.collect)()

    return tools_outputs


async def _abefore_tool_call(
    i: int,
    tool_name: str,
    state: State,
    *,
    prior: List[Dict],
    persist_messages_callback,
) -> None:
    # smart-гейт (спека §3): считаем КАЖДЫЙ исполняемый вызов, кроме завершителя хода.
    if tool_name not in TURN_FINALIZING_TOOLS:
        state.turn_tool_calls += 1
    # Human-wait (ask_human | confirm-in-tool): до блокировки сфлашить уже
    # посчитанные output'ы предыдущих тулов + skip'ы, чтобы TUI не держал их в running.
    if prior and (i > 0 or tool_name == ASK_HUMAN_TOOL_NAME):
        await _maybe_persist(persist_messages_callback, prior, state)


async def _afinish_tool_call(
    m: Dict,
    tool_name: str,
    tool_arguments: str,
    tool_res_raw: Any,
    state: State,
    ctx: Any,
) -> Dict:
    """Сжать output, обновить loop-детектор и intent-scope → function_call_output."""
    # Длинный output ужимаем в stored-envelope; остальное оставляем как есть.
    tool_res = maybe_store_and_wrap_tool_output(
        tool_res_raw,
        call_id=m["call_id"],
        tool_name=tool_name,
        state=state,
        settings=ctx,
        logger=getattr(ctx, "logger", None),
    )
    if tool_name != FINISH_TOOL_NAME:
        observe_loop_detector(state, ctx, tool_name, tool_arguments)
        await _acall_blocking_hook(
            amaybe_update_intent_scope_from_tool,
            tool_name=tool_name,
            tool_output_raw=tool_res_raw,
            tool_arguments=tool_arguments,
            state=state,
            ctx=ctx,
        )
    return _function_call_output_message(m["call_id"], tool_res)


async def aprocess_tool_calls(
    messages: List[Dict],
    state: State,
    ctx: Any,
    run_agent_func,
    persist_messages_callback: Optional[Callable[[List[Dict]], None]] = None,
):
    """Исполнить batch function_call из ответа модели → список function_call_output.

    На каждый call по порядку:
      1) если сейчас пойдёт блокирующий ask_human — сначала записать уже готовые
         output'ы предыдущих тулов (и skip), чтобы в UI они не висели «running»;
      2) для субагента (вложенный прогон): сигнал «тул стартовал» в live-канал TUI;
      3) вызвать тул (для subagent_* ещё передаём call_id, чтобы nested-строки
         в TUI цеплялись к этой корневой строке субагента);
      4) для субагента: сигнал «тул закончил» или текст intermediate_reply;
         эти сигналы только в памяти TUI, в thread jsonl не пишутся;
      5) при необходимости ужать длинный output, обновить intent-scope,
         собрать function_call_output.

    До цикла по batch:
      - work (все кроме turn_finish) в порядке батча;
      - только первый turn_finish в executed; остальные extra-finish — skip
        сразу (сообщение «лишний finish», не «ход уже сдан»);
      - успешный ask в цикле даёт skip-output всему оставшемуся work и первому
        finish: эти вызовы были сгенерированы до ответа человека;
      - skip extra-finish готовим заранее, чтобы сфлашить их до ask_human
        вместе с уже выполненными тулами.

    User-stop: если ``state.stop_requested`` до/между тулами — remaining
    не ``apply_tool``, сразу envelope B; active (уже вернул A) + B сразу
    пишем в state/jsonl (исключение из discard_writes_after_stop).

    Live-прогресс субагента шлётся только если это вложенный ctx (есть
    parent_call_id и progress sink). У родителя sink может быть для чтения
    очереди, но сам parent тулы туда не пишет.
    """
    grouped_messages = group_messages(messages)
    if len(grouped_messages["tool_outputs"]) > 0:
        raise ValueError("Messages has tool_outputs")

    # Снимок batch до исполнения: если сработает session_compact, из него
    # соберём «что оставить» вместо сырого списка output'ов.
    batch_snapshot = list(messages)
    state._session_compact_batch_snapshot = batch_snapshot
    state.session_compact_applied = False
    # Картинки батча (fs_view_image, ответ на turn_ask_human) копятся здесь и
    # уходят одним техническим сообщением в конце. Остаток прерванного батча
    # (исключение посреди исполнения) в новый батч не протекает.
    state.pending_tool_attachments = []

    tools_outputs = []
    # Work, затем не больше одного finish; skipped — лишние turn_finish.
    unknown_in_batch = 0
    invalid_in_batch = 0
    tool_calls = grouped_messages["tool_calls"]
    executed, skipped = partition_work_then_finish(tool_calls)
    # Skip-output'ы синтетические — считаем заранее, чтобы сфлашить ВМЕСТЕ с
    # executed-output'ами до блокирующего turn_ask_human (иначе пропущенный тул
    # тоже висит "running" до ответа юзера).
    skip_outputs = _extra_finish_skip_outputs(skipped)
    aborted_by_user_stop = False
    successful_ask_human = False
    # Human-wait (ask_human | confirm-in-tool): до блокировки сфлашить уже
    # посчитанные output'ы предыдущих тулов + skip'ы (I2), чтобы TUI не держал
    # их в running на время wait. Полный батч персистится ещё раз на выходе
    # process_tool_calls (run_agent → _maybe_persist); уже записанные id
    # пропускаются в append_new_thread_messages по make_message_key.
    for i, m in enumerate(executed):
        # Между тулами / до старта — если stop, remaining не apply_tool → envelope B.
        if getattr(state, "stop_requested", False):
            tools_outputs.extend(
                _not_started_outputs(executed[i:], state, ctx)
            )
            aborted_by_user_stop = True
            break

        tool_name = str(m.get("name") or "")
        if successful_ask_human:
            tools_outputs.append(_after_ask_human_skip_output(m["call_id"]))
            continue
        # smart-гейт: считаем КАЖДЫЙ исполняемый вызов, кроме завершителя хода.
        # До try — упавший вызов (неверные аргументы) тоже считается.
        await _abefore_tool_call(
            i,
            tool_name,
            state,
            prior=list(tools_outputs) + skip_outputs,
            persist_messages_callback=persist_messages_callback,
        )
        tool_arguments = str(m.get("arguments", ""))
        (
            tool_res_raw,
            was_invalid,
            ok,
        ) = await _aexecute_tool_call(
            m, tool_name, tool_arguments, state, ctx, run_agent_func
        )
        invalid_in_batch += int(was_invalid)
        if tool_name == ASK_HUMAN_TOOL_NAME and ok is True:
            successful_ask_human = True
        unknown_in_batch += int(
            _track_repeat_errors(state, tool_name, tool_res_raw)
        )

        # Длинный output ужимаем в stored-envelope; остальное оставляем как есть.
        tools_outputs.append(
            await _afinish_tool_call(
                m, tool_name, tool_arguments, tool_res_raw, state, ctx
            )
        )

        # После apply: если стопнули mid-tool (envelope A от runner) — хвост batch → B.
        if getattr(state, "stop_requested", False):
            tools_outputs.extend(
                _not_started_outputs(executed[i + 1 :], state, ctx)
            )
            aborted_by_user_stop = True
            break

    # Skip-output'ы уже посчитаны выше и сфлашены перед блокировкой — добавляем в
    # возврат (тред/state дедупят повторный персист по make_message_key).
    state.last_batch_unknown_tool_count = unknown_in_batch
    state.last_batch_invalid_arguments_count = invalid_in_batch
    tools_outputs.extend(skip_outputs)

    return await _afinalize_tool_batch(
        state,
        ctx,
        tools_outputs,
        batch_snapshot,
        aborted_by_user_stop=aborted_by_user_stop,
        persist_messages_callback=persist_messages_callback,
    )


async def aprocess_tool_outputs(messages, state: State, ctx: Any):
    """Ход по выдаче тулов."""
    return await _aask_backend_and_record(messages, state, ctx)


def _pickup_pending_as_new_turn(
    get_pending_messages: Optional[Callable[[], List[Dict]]],
    state: State,
    ctx: Any,
) -> Optional[List[Dict]]:
    """Ход закончен (finish-gate accept / text-response accept, спека §6.4): очередь
    непуста → фиксируем модель нового хода (``begin_turn``) и возвращаем сообщения,
    которыми продолжится цикл вместо возврата из ``run_agent``. ``None`` — очередь
    пуста, ход завершается как обычно."""
    if get_pending_messages is None:
        return None
    pending = get_pending_messages()
    if not pending:
        return None
    # Новый пользовательский ход: окно прошлого хода не должно давать
    # ложный повтор на первом же чтении с теми же аргументами.
    clear_loop_detector(state)
    try:
        begin_turn_main_trace(state, ctx)
    except Exception as exc:
        # Очередь УЖЕ слита (`get_pending_messages` — drain, не peek): уронив ход
        # здесь, мы потеряли бы набранное пользователем сообщение бесследно —
        # вернуть его в очередь некуда. Поэтому ход продолжаем на модели прошлого
        # хода: тот же честный отказ придёт из `_request_backend_canonical` (единая
        # точка текста), но сообщение к тому моменту уже в трейсе и в логе.
        logger = getattr(ctx, "logger", None) or _fallback_logger
        logger.warning(
            "фиксация модели нового хода не удалась (%s) — подхваченное сообщение "
            "не теряем, ход продолжается на прежней модели",
            exc,
        )
    return pending


_NO_RETURN = object()


class _Outcome:
    """Итог обработки одного режима: новые messages, либо результат прогона, либо рестарт цикла."""

    __slots__ = ("completed_step", "messages", "mode", "restart", "result")

    def __init__(
        self,
        messages=None,
        *,
        result=_NO_RETURN,
        restart=False,
        completed_step=False,
    ):
        self.messages = messages
        self.result = result
        self.restart = restart
        self.completed_step = completed_step
        self.mode = None


class _AgentLoop:
    """Главный цикл агента: одна итерация = один режим (user_ask / function_call / …).

    Состояние итераций (счётчик ошибок тулов, ретраи переполнения контекста)
    живёт на объекте; ``arun`` — тот же while-цикл, что был в ``arun_agent``.
    """

    def __init__(
        self,
        state: State,
        ctx: Any,
        *,
        get_full_history_messages,
        persist_messages_callback,
        pre_backend_compaction_hook,
        turn_end_compaction_hook,
        force_compact_hook,
        get_pending_messages,
        check_stop,
        session_meta,
        mark_finalizing,
        mark_finish_gate_checking,
    ) -> None:
        self.state = state
        self.ctx = ctx
        self.get_full_history_messages = get_full_history_messages
        self.persist_messages_callback = persist_messages_callback
        self.pre_backend_compaction_hook = pre_backend_compaction_hook
        self.turn_end_compaction_hook = turn_end_compaction_hook
        self.force_compact_hook = force_compact_hook
        self.get_pending_messages = get_pending_messages
        self.check_stop = check_stop
        self.session_meta = session_meta
        self.mark_finalizing = mark_finalizing
        self.mark_finish_gate_checking = mark_finish_gate_checking

        self.tool_error_cnt = 0
        self.context_overflow_retries = 0
        self.run_logger = getattr(ctx, "logger", None) or _fallback_logger
        agent_cfg = settings_of(ctx).agent
        self.proj_tags = projection_tags_from_config(ctx)
        self.console_human = human_console_enabled(ctx)
        self.max_tool_errors = agent_cfg.no_tool_call_max_count
        self.session_token_limit = agent_cfg.session_token_limit
        self.max_tool_calls_limit = agent_cfg.max_tool_calls
        self.max_unknown_tool_repeats = int(
            agent_cfg.max_unknown_tool_repeats or 5
        )
        state.last_unknown_tool_name = None
        state.unknown_tool_repeat_count = 0
        state.last_batch_unknown_tool_count = 0
        state.last_batch_invalid_arguments_count = 0

    # ------------------------------------------------------------------ loop
    async def arun(self, messages) -> Union[str, None]:
        state = self.state
        first_poll = True
        while True:
            if self.check_stop is not None and self.check_stop():
                clear_loop_detector(state)
                return None
            messages = self._poll_pending(messages, first_poll=first_poll)
            first_poll = False
            # Если от backend'а или get_pending_messages пришёл пустой список сообщений,
            # не передаём его дальше в check_mode, чтобы не падать
            # с ValueError('Empty messages list'). Вместо этого генерируем понятное
            # сообщение‑обратную связь для агента.
            if not messages:
                messages = self._bounce_empty_messages()
                continue
            step = await self._astep(messages, _get_backend_api_mode(self.ctx))
            if step.result is not _NO_RETURN:
                return step.result
            messages = step.messages
            if step.restart:
                continue
            self._count_step(step.completed_step, step.mode)

    async def _astep(self, messages, backend_mode: str) -> _Outcome:
        """Одна итерация цикла: подготовка батча и обработка режима."""
        try:
            messages, mode, early = await self._aprepare_batch(
                messages, backend_mode
            )
        except ExecutionFailure:
            raise
        except Exception as e:
            # Ошибки до успешного разбора батча (санитайз, check_mode, …) — bounce.
            return _Outcome(self._bounce_on_error(e), completed_step=True)
        if early is not _NO_RETURN:
            return _Outcome(result=early)
        # Вызовы backend не ловим здесь: после исчерпания transport/adaptive
        # ретраев исключение уходит наверх как есть.
        try:
            outcome = await self._adispatch(mode, messages, backend_mode)
        except WrongFunctionArgumentsException as e:
            # Аргументы отвалились на exec — ход модели всё равно «закрыт».
            outcome = _Outcome(
                self._bounce_wrong_arguments(e), completed_step=True
            )
        except ExecutionFailure:
            raise
        except ModelRequestStopped:
            # stop во время ожидания модели — как stop между шагами.
            clear_loop_detector(self.state)
            return _Outcome(result=None)
        except Exception as backend_exc:
            if not await self._atry_recover_context_overflow(backend_exc):
                raise
            outcome = _Outcome(messages, restart=True)
        outcome.mode = mode
        return outcome

    # ----------------------------------------------------------- iteration
    def _poll_pending(self, messages, *, first_poll: bool):
        """Подхват сообщений из очереди (``get_pending_messages``) перед шагом цикла.

        `messages` здесь ещё не записан в state (запись — после опроса). Свежий
        ответ модели с вызовами очередь не разбирает: вызовы исполняются в этом
        проходе, реплики дождутся следующего опроса (дописанная реплика увела бы
        шаг в user_ask, и вызовы остались бы без исполнения). К ответам тулов (с
        техническим хвостом) реплики ДОПИСЫВАЮТСЯ: подмена оставляла в state
        вызов без ответа. Первый опрос тоже дописывает: стартовый батч — не ответ
        модели, а вызовы в нём (responses без previous_response_id) — история
        треда; сообщение, пришедшее между стартом прогона и первым запросом к
        модели, иначе вытесняло бы стартовое. Прочее (текстовый ответ модели,
        отскок, подхваченный новый ход) реплика подменяет, как раньше: ответ
        модели, дописанный к реплике, в responses ушёл бы в дельте повторно — он
        уже в цепочке сервера."""
        if self.get_pending_messages is None:
            return messages
        if not first_poll and _batch_has_pending_function_calls(messages):
            return messages
        pending = self.get_pending_messages()
        if not pending:
            return messages
        if first_poll or _is_tool_outputs_with_technical_tail(messages):
            return [*messages, *pending]
        return pending

    def _technical_bounce(self, text: str) -> List[Dict]:
        return [
            _make_technical_feedback_user_message(
                text,
                technical_type=TECHNICAL_TYPE_TECHNICAL,
            )
        ]

    def _finish_reason_bounce(self) -> List[Dict]:
        """No-tool-call bounce с учётом finish_reason последнего ответа
        (blocked → просьба переформулировать, length → короче, иначе общий)."""
        return self._technical_bounce(
            format_no_tool_call_bounce(
                finish_reason=getattr(self.state, "last_finish_reason", None)
            )
        )

    def _bounce_empty_messages(self) -> List[Dict]:
        self.run_logger.error("Empty messages list")
        self.tool_error_cnt += 1
        self.state.steps += 1
        return self._finish_reason_bounce()

    def _bounce_on_error(self, e: Exception) -> List[Dict]:
        self.run_logger.error(e, exc_info=True)
        self.tool_error_cnt += 1
        return self._finish_reason_bounce()

    def _bounce_wrong_arguments(
        self, e: WrongFunctionArgumentsException
    ) -> List[Dict]:
        self.run_logger.error(e, exc_info=True)
        self.tool_error_cnt += 1
        # Страховка для путей вне батча (finish-аргументы и т.п.): батч
        # свои невалидные вызовы закрывает сам в process_tool_calls.
        _attach_truncation_hint(e, self.state)
        return [
            _make_technical_feedback_user_message(
                e.to_llm_feedback(),
                technical_type=TECHNICAL_TYPE_ERROR_MESSAGE,
                call_id=getattr(e, "call_id", None),
            )
        ]

    async def _atry_recover_context_overflow(
        self, backend_exc: Exception
    ) -> bool:
        # Переполнение контекстного окна модели → не крашимся: форс-сжатие
        # истории (безусловное, как /compact) и повтор запроса со сжатым
        # контекстом. Лимит попыток — если даже сжатие не помогает, уходим
        # наверх. Прочие ошибки — как раньше, наверх без изменений.
        if not (
            self.force_compact_hook is not None
            and is_context_overflow_error(backend_exc)
            and self.context_overflow_retries < _MAX_CONTEXT_OVERFLOW_RETRIES
        ):
            return False
        self.context_overflow_retries += 1
        self.run_logger.warning(
            "Context overflow → force-compaction + retry (%s/%s): %s",
            self.context_overflow_retries,
            _MAX_CONTEXT_OVERFLOW_RETRIES,
            backend_exc,
        )
        await _acall_blocking_hook(self.force_compact_hook)
        return True

    def _count_step(
        self, count_completed_step: bool, mode_for_step_count
    ) -> None:
        # step += 1 только после завершённого хода модели (tool-batch / text /
        # bounce / pre-mode error). user_ask и function_call_output — half-turn
        # к LLM, step не тратят. skip_initial: resume `-t` не считает первый ask.
        state = self.state
        skip_initial_step = bool(
            getattr(state, "skip_initial_user_ask_step_when_history", False)
        )
        skip_this_step = (
            skip_initial_step
            and state.steps == 0
            and mode_for_step_count == "user_ask"
        )
        if count_completed_step and not skip_this_step:
            state.steps += 1

    # ------------------------------------------------------------- prepare
    def _log_incoming(self, messages) -> None:
        for m in messages:
            m_formatted = format_message_responses(m)
            if self.console_human and len(m_formatted) > 0:
                print(m_formatted)
            self.run_logger.info(
                "\n<message>\n%s\n</message>\n",
                json.dumps(m, ensure_ascii=False, indent=2),
            )
            self.run_logger.info(
                "dialog_message",
                extra={
                    "event": "dialog_message",
                    "payload": m,
                    "trace": m_formatted or None,
                },
            )

    def _sanitize_batch(self, messages) -> List[Dict]:
        # В state и в тред уходит уже санитайзнутый набор (битые function_call ->
        # technical user message). Сырой вывод бэкенда логируется выше; если
        # санитайз изменил батч — логируем то, что реально закоммичено (иначе
        # в agent_log не видно подставленного фидбека, хотя он есть в threads jsonl).
        _pre_sanitize_digest = json.dumps(
            messages, ensure_ascii=False, sort_keys=True, default=str
        )
        messages = sanitize_messages_for_chat_completions(
            messages,
            invalid_tool_call_factory=build_invalid_tool_call_feedback_message,
        )
        _post_sanitize_digest = json.dumps(
            messages, ensure_ascii=False, sort_keys=True, default=str
        )
        if _post_sanitize_digest != _pre_sanitize_digest:
            self.run_logger.info(
                "\n<message_batch_committed>\n"
                "Санитайз для chat/completions изменил батч относительно сырого "
                "ответа выше (например, невалидный JSON вызова тула заменён на "
                "техническое user-сообщение — то же, что уходит в persist треда).\n"
                "</message_batch_committed>\n"
            )
            for m in messages:
                self.run_logger.info(
                    "\n<message_committed>\n%s\n</message_committed>\n",
                    json.dumps(m, ensure_ascii=False, indent=2, default=str),
                )
        return messages

    async def _alimit_envelope(self, messages):
        """Envelope лимита (step / tool_calls / tokens) либо _NO_RETURN."""
        state, ctx = self.state, self.ctx
        # Лимиты — только когда нет pending function_call: сначала дожимаем весь
        # батч tools от модели, иначе call уже в state/jsonl, а exec/sidecar/result
        # теряются. step = завершённый ход модели (после tool-batch / text), не half-turn.
        if _batch_has_pending_function_calls(messages):
            return _NO_RETURN
        if state.steps >= state.max_steps:
            state.terminal_outcome = "step_limit"
            return await _acall_blocking_hook(
                abuild_limit_envelope, state, "step_limit", ctx
            )
        if (
            self.max_tool_calls_limit is not None
            and state.tool_calls >= self.max_tool_calls_limit
        ):
            state.terminal_outcome = "tool_call_limit"
            return await _acall_blocking_hook(
                abuild_limit_envelope,
                state,
                "tool_call_limit",
                ctx,
            )
        if self.session_token_limit is not None:
            _baseline = UsageAccount.from_meta(
                getattr(state, "run_start_usage_meta", None)
            )
            _spent = state.usage.delta_from(_baseline).totals()["total"]
            if _spent >= self.session_token_limit:
                state.terminal_outcome = "token_limit"
                return await _acall_blocking_hook(
                    abuild_limit_envelope,
                    state,
                    "token_limit",
                    ctx,
                )
        return _NO_RETURN

    async def _aprepare_batch(self, messages, backend_mode: str):
        """Лог, санитайз, коммит в state/тред, лимиты, режим, pre-backend хуки.

        → (messages, mode, early): ``early`` — результат прогона при достигнутом
        лимите/бюджете ошибок, иначе ``_NO_RETURN``.
        """
        state = self.state
        self._log_incoming(messages)
        messages = self._sanitize_batch(messages)
        new_state_messages = filter_messages_not_in_state(
            list(getattr(state, "messages", []) or []),
            list(messages),
        )
        if new_state_messages:
            state.messages += new_state_messages
        await _maybe_persist(self.persist_messages_callback, messages, state)

        envelope = await self._alimit_envelope(messages)
        if envelope is not _NO_RETURN:
            return messages, None, envelope
        if self.tool_error_cnt > self.max_tool_errors:
            state.terminal_outcome = "tool_errors"
            return messages, None, "Agent tools call errors."

        mode = check_mode(messages, self.run_logger)
        if backend_mode == "chat_completions" and _mode_requires_backend_call(
            mode
        ):
            await self._apre_backend_compaction(messages, mode)
            messages = await self._aapply_tool_usage_notice(messages)
        return messages, mode, _NO_RETURN

    async def _apre_backend_compaction(self, messages, mode) -> None:
        if self.pre_backend_compaction_hook is None:
            return
        try:
            await _acall_blocking_hook(
                self.pre_backend_compaction_hook, messages, mode
            )
        except ExecutionFailure:
            raise
        except Exception as pre_backend_exc:
            self.run_logger.warning(
                "Pre-backend compaction hook warning: %s",
                pre_backend_exc,
                exc_info=True,
            )

    async def _aapply_tool_usage_notice(self, messages) -> List[Dict]:
        if not isinstance(self.session_meta, dict):
            return messages
        state = self.state
        try:
            _messages_before_notice = len(messages)
            messages = apply_tool_usage_notice_before_llm(
                messages,
                state,
                self.ctx,
                self.session_meta,
                step_messages=messages,
                logger=self.run_logger,
            )
            notice_messages = messages[_messages_before_notice:]
            if notice_messages:
                to_append = filter_messages_not_in_state(
                    list(getattr(state, "messages", []) or []),
                    list(notice_messages),
                )
                if to_append:
                    state.messages += to_append
                await self._apersist_notice_messages(notice_messages)
        except ExecutionFailure:
            raise
        except Exception as mem_exc:
            self.run_logger.warning(
                "Memory messages hook warning: %s",
                mem_exc,
                exc_info=True,
            )
        return messages

    async def _apersist_notice_messages(self, notice_messages) -> None:
        if self.persist_messages_callback is None:
            return
        try:
            await await_callback(
                self.persist_messages_callback(notice_messages)
            )
        except ExecutionFailure:
            raise
        except Exception as persist_exc:
            self.run_logger.warning(
                "Thread notice messages persist warning: %s",
                persist_exc,
                exc_info=True,
            )

    # ------------------------------------------------------------ dispatch
    async def _adispatch(self, mode, messages, backend_mode: str) -> _Outcome:
        if mode == "user_ask":
            return await self._auser_ask(messages, backend_mode)
        if mode == "function_call":
            return await self._afunction_call(messages, backend_mode)
        if mode == "function_call_output":
            return await self._afunction_call_output(messages, backend_mode)
        if mode == "text_response":
            return await self._atext_response(messages)
        raise ValueError(f"unconventional mode: {mode}")

    def _remember_response_id(self, response_id, backend_mode: str) -> None:
        if backend_mode == "responses":
            self.state.previous_response_id = response_id
        self.state.last_backend_response_id = response_id

    async def _auser_ask(self, messages, backend_mode: str) -> _Outcome:
        state, ctx = self.state, self.ctx
        try:
            messages, response_id = await aprocess_user_ask(
                messages, state, ctx
            )
        except ExecutionFailure:
            raise
        except Exception as user_ask_exc:
            can_retry_with_full_history = (
                backend_mode == "responses"
                and state.previous_response_id is not None
                and self.get_full_history_messages is not None
                and _is_previous_response_cache_miss(user_ask_exc)
            )
            if not can_retry_with_full_history:
                raise

            self.run_logger.warning(
                "Cache miss for previous_response_id=%s. Retry with full history.",
                state.previous_response_id,
            )
            full_history_messages = self.get_full_history_messages()
            state.previous_response_id = None
            (
                messages,
                response_id,
                _,
            ) = await _arequest_backend_canonical(
                full_history_messages,
                state,
                ctx,
                set_previous_response_id=False,
            )
        self._remember_response_id(response_id, backend_mode)
        # Half-turn: ответ модели обработаем на следующей итерации.
        return _Outcome(messages)

    async def _afunction_call_output(
        self, messages, backend_mode: str
    ) -> _Outcome:
        messages, response_id = await aprocess_tool_outputs(
            messages, self.state, self.ctx
        )
        self._remember_response_id(response_id, backend_mode)
        # Half-turn: как user_ask — ждём разбора ответа модели.
        return _Outcome(messages)

    async def _afunction_call(self, messages, backend_mode: str) -> _Outcome:
        state = self.state
        pending_tool_calls = group_messages(messages).get("tool_calls", [])
        messages = await aprocess_tool_calls(
            messages,
            state,
            self.ctx,
            run_agent_func=arun_agent,
            persist_messages_callback=self.persist_messages_callback,
        )
        closing_finish = _closing_finish_after_batch(
            pending_tool_calls,
            messages,
            stop_requested=bool(getattr(state, "stop_requested", False)),
        )
        if closing_finish is not None:
            return await self._afinish_turn(
                messages, closing_finish, backend_mode
            )
        return self._after_tool_batch(messages)

    async def _afinish_turn(
        self, messages, closing_finish, backend_mode: str
    ) -> _Outcome:
        state = self.state
        finish_call_id = closing_finish.get("call_id")
        batch_new = filter_messages_not_in_state(
            list(getattr(state, "messages", []) or []),
            messages,
        )
        work_new, finish_new, tail_new = _split_batch_new_around_finish(
            batch_new, finish_call_id
        )
        # Work до гейта; finish в state до гейта (I3: persist после
        # apply). Technical-хвост нужен только при reject, когда
        # агент продолжит ход. После принятого finish его нельзя
        # оставлять в state/jsonl: следующий пользователь иначе
        # увидит устаревший notice как начало нового хода.
        if work_new:
            state.messages += work_new
            await _maybe_persist(
                self.persist_messages_callback, work_new, state
            )
        if finish_new:
            # I3: finish output в state до gate; persist — после apply.
            state.messages += finish_new
        finish_arguments = closing_finish.get("arguments")
        if backend_mode != "chat_completions":
            if finish_new:
                await _maybe_persist(
                    self.persist_messages_callback, finish_new, state
                )
            state.current_user_intent = []
            state.steps += 1
            clear_loop_detector(state)
            return _Outcome(result=finish_arguments)

        decision = await self._aevaluate_finish_gate(
            finish_arguments, finish_call_id
        )
        if decision.outcome == "reject":
            if tail_new:
                state.messages += tail_new
                await _maybe_persist(
                    self.persist_messages_callback, tail_new, state
                )
            # Вердикт уже в output turn_finish (ok: false +
            # message). Не подменяем батч technical-user:
            # следующий шаг цикла сам уйдёт в
            # function_call_output и позовёт модель.
            # Finish-reject — завершённый ход (batch + gate).
            # Work уже в трейсе, не откатываем.
            return _Outcome(messages, completed_step=True)
        # accept / fail_open_error / fail_open_limit / прочее —
        # все завершают ход одинаково.
        state.steps += 1
        state.flush_current_user_intent()
        clear_loop_detector(state)
        return await self._aclose_turn(finish_arguments)

    async def _aevaluate_finish_gate(self, finish_arguments, finish_call_id):
        state, ctx = self.state, self.ctx
        gate = FinishGateGuard(ctx=ctx, state=state, logger=self.run_logger)
        # Индикатор «сверяюсь…» — только когда судья реально пойдёт
        # (gate.enabled). Smart-skip / strict=off / не-chat → без LLM,
        # флаг не поднимаем (иначе ложный flash + лишний runtime-meta).
        # Снимаем строго в finally, даже если evaluate бросит.
        checking = self.mark_finish_gate_checking is not None and gate.enabled
        if checking:
            await await_callback(self.mark_finish_gate_checking(True))
        try:
            decision = await gate.aevaluate_turn_finish(
                finish_handoff_arguments=finish_arguments,
            )
        finally:
            if checking:
                await await_callback(self.mark_finish_gate_checking(False))
        await apply_finish_gate_to_turn_finish_output_in_state(
            state=state,
            finish_call_id=finish_call_id,
            decision=decision,
            logger=self.run_logger,
            persist_messages_callback=self.persist_messages_callback,
        )
        final_submit_output = get_function_call_output_text(
            state.messages, finish_call_id
        )
        await _acall_blocking_hook(
            amaybe_update_intent_scope_from_tool,
            tool_name=FINISH_TOOL_NAME,
            tool_output_raw=final_submit_output or "",
            tool_arguments=finish_arguments or "",
            state=state,
            ctx=ctx,
        )
        return decision

    async def _aclose_turn(self, result) -> _Outcome:
        """Конец хода: компакция turn-end и подхват pending-сообщения новым ходом."""
        if self.turn_end_compaction_hook is not None:
            await _acall_blocking_hook(self.turn_end_compaction_hook)
        # Сообщение, пришедшее во время (блокирующей) компакции, не
        # теряем: продолжаем новым ходом на уже сжатом контексте.
        new_turn_messages = _pickup_pending_as_new_turn(
            self.get_pending_messages, self.state, self.ctx
        )
        if new_turn_messages is not None:
            return _Outcome(new_turn_messages, restart=True)
        return _Outcome(result=result)

    def _after_tool_batch(self, messages) -> _Outcome:
        state = self.state
        n_unknown = int(
            getattr(state, "last_batch_unknown_tool_count", 0) or 0
        )
        n_invalid = int(
            getattr(state, "last_batch_invalid_arguments_count", 0) or 0
        )
        if n_unknown + n_invalid > 0:
            # Бюджет считаем по ходам без нормального вызова, а не
            # по вызовам: один кривой батч стоит одну единицу, иначе
            # он гасит прогон раньше, чем модель увидит фидбэк.
            self.tool_error_cnt += 1
        else:
            self.tool_error_cnt = 0
        if (
            int(getattr(state, "unknown_tool_repeat_count", 0) or 0)
            >= self.max_unknown_tool_repeats
        ):
            unknown_name = (
                getattr(state, "last_unknown_tool_name", None) or "unknown"
            )
            state.terminal_outcome = "tool_errors"
            return _Outcome(
                result=f"Agent stopped: repeated unknown tool '{unknown_name}'."
            )
        if (
            int(getattr(state, "internal_error_repeat_count", 0) or 0)
            >= self.max_unknown_tool_repeats
        ):
            broken_name = (
                getattr(state, "last_internal_error_tool_name", None)
                or "unknown"
            )
            state.terminal_outcome = "tool_errors"
            return _Outcome(
                result=(
                    f"Agent stopped: tool '{broken_name}' keeps failing with the same client-side error."
                )
            )
        # Полный ход: LLM уже вернул tools, batch исполнен.
        # Невалидный finish сюда же: отказной output в трейсе, ход открыт.
        return _Outcome(messages, completed_step=True)

    async def _atext_decision(
        self, assistant_text: str, last_finish: Any
    ) -> Tuple[bool, Optional[str]]:
        """→ (accept, reject_feedback) для прозы без вызова тула."""
        # blocked / length — не финал даже в allow: отказ провайдера
        # или усечение (обрезок тулы/ответа). Иначе allow принимает
        # мусор и крутит до no_tool_call_max_count / сдаёт ход.
        # Payload уже в state/jsonl; reject → bounce, не discard.
        if finish_prevents_text_final(last_finish):
            return False, None
        # Модель повторила наш мост «(tool results received)» — шаблон из истории,
        # не итог: напоминание вызвать тул, как при прозе без вызова.
        if is_bridge_echo(assistant_text):
            return False, None
        decision = await ashould_return_text_answer(
            self.ctx, self.state, assistant_text
        )
        return decision.accept, decision.reject_feedback

    def _text_reject_bounce_msg(
        self,
        reject_feedback: Optional[str],
        last_finish: Any,
        default_msg: str,
    ) -> str:
        """Текст bounce при отклонённой прозе; ``default_msg`` — по finish_reason."""
        if reject_feedback is not None:  # judge-reject c причиной
            return reject_feedback
        if finish_blocks_final(last_finish):
            self.run_logger.warning(
                "Provider blocked the completion (%s); requesting a rephrased attempt",
                last_finish,
            )
            return default_msg
        if classify_finish_kind(last_finish) == FINISH_KIND_LENGTH:
            self.run_logger.warning(
                "Agent response truncated (finish_reason=length); requesting a shorter tool call"
            )
            return default_msg
        # deny / пусто / нет гейта / ошибка
        self.run_logger.warning(
            "Agent returned text response without tool call, requesting tool call"
        )
        return NO_TOOL_CALL_BOUNCE_MESSAGE

    async def _atext_response(self, messages) -> _Outcome:
        state, ctx = self.state, self.ctx
        assistant_text = extract_last_assistant_text(
            messages, tags=self.proj_tags, strip_think=True
        )
        last_finish = getattr(state, "last_finish_reason", None)
        bounce_msg = format_no_tool_call_bounce(finish_reason=last_finish)
        decision_accept, decision_reject_feedback = await self._atext_decision(
            assistant_text, last_finish
        )

        if decision_accept:
            # Финальная проза принята → TUI-индикатор «✓ завершаю…»
            # на время записи/саммари/flush (mark_finalizing пишет
            # runtime.finalizing в meta; снимаем строго в finally).
            state.steps += 1
            if self.mark_finalizing is not None:
                await await_callback(self.mark_finalizing(True))
            try:
                # Корутинным именем, как соседние intent-scope
                # хуки: синхронное имя fail-open внутри проглотил
                # бы, и сводка молча подменилась бы сырым текстом.
                await _acall_blocking_hook(
                    amaybe_record_final_answer_intent,
                    assistant_text=assistant_text,
                    state=state,
                    ctx=ctx,
                )
                state.flush_current_user_intent()
                clear_loop_detector(state)
            finally:
                if self.mark_finalizing is not None:
                    await await_callback(self.mark_finalizing(False))
            return await self._aclose_turn(assistant_text)
        # === reject ===
        self.tool_error_cnt += 1
        bounce_msg = self._text_reject_bounce_msg(
            decision_reject_feedback, last_finish, bounce_msg
        )
        return _Outcome(
            self._technical_bounce(bounce_msg), completed_step=True
        )


# Keep the public helper/monkeypatch boundary while sharing one loop.


async def arun_agent(
    messages,
    state: State,
    ctx: Any,
    *,
    get_full_history_messages: Optional[Callable[[], List[Dict]]] = None,
    persist_messages_callback: Optional[Callable[[List[Dict]], None]] = None,
    pre_backend_compaction_hook: Optional[
        Callable[[List[Dict], str], None]
    ] = None,
    turn_end_compaction_hook: Optional[Callable[[], None]] = None,
    force_compact_hook: Optional[Callable[[], None]] = None,
    get_pending_messages: Optional[Callable[[], List[Dict]]] = None,
    check_stop: Optional[Callable[[], bool]] = None,
    session_meta: Optional[Dict[str, Any]] = None,
    mark_finalizing: Optional[Callable[[bool], None]] = None,
    mark_finish_gate_checking: Optional[Callable[[bool], None]] = None,
) -> Union[str, None]:
    loop = _AgentLoop(
        state,
        ctx,
        get_full_history_messages=get_full_history_messages,
        persist_messages_callback=persist_messages_callback,
        pre_backend_compaction_hook=pre_backend_compaction_hook,
        turn_end_compaction_hook=turn_end_compaction_hook,
        force_compact_hook=force_compact_hook,
        get_pending_messages=get_pending_messages,
        check_stop=check_stop,
        session_meta=session_meta,
        mark_finalizing=mark_finalizing,
        mark_finish_gate_checking=mark_finish_gate_checking,
    )
    return await loop.arun(messages)
