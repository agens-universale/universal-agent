import asyncio
import gc
import inspect
import json

# Переполнение контекста → force-компакция + повтор запроса; лимит на случай,
# когда даже после сжатия история всё ещё не влезает (единичное огромное сообщение).
import logging
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from gc_core.config import (
    parse_chat_completions_max_tokens,
    resolve_tui_stream_enabled,
)
from gc_core.config_modules.gc import gc_force_collect_enabled
from gc_core.config_modules.strip_tags_projection import (
    persist_reasoning_content_from_config,
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
from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.loop import AgentLoop
from gc_core.execution.model import arequest_model
from gc_core.execution.policy import ExecutionPolicy
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
from gc_core.message_policy.no_tool_call_bounce import (
    NO_TOOL_CALL_BOUNCE_MESSAGE,
    extract_last_assistant_text,
    format_no_tool_call_bounce,
)
from gc_core.message_policy.stop_persist import is_persist_discarded
from gc_core.message_policy.tool_usage_notice.apply import (
    aapply_tool_usage_notice_before_llm as apply_tool_usage_notice_before_llm,
)
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_ERROR_MESSAGE,
    TECHNICAL_TYPE_TECHNICAL,
    build_invalid_tool_call_feedback_message,
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
from gc_core.states import State
from gc_core.summarize.limit_trace_summary import abuild_limit_envelope
from gc_core.tools import (
    WrongFunctionArgumentsException,
    aapply_tool,
    is_internal_tool_error_output,
    is_unknown_tool_output,
)
from gc_core.tools.session.stored_outputs.wrapping import (
    maybe_store_and_wrap_tool_output,
)
from gc_core.tools.subagents.progress import (
    brief_args_summary,
    emit_progress,
    extract_intermediate_prose,
    parse_tool_result_status,
    todo_items_from_arguments,
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
    is_model_unavailable_error,
)
from gc_core.utils.connect_backend import (
    NO_BACKEND_OVERRIDE,
    acreate_call_headers,
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
from gc_core.utils.logging_module import human_console_enabled

_fallback_logger = logging.getLogger(__name__)


async def _acall_blocking_hook(fn, *args: Any, **kwargs: Any):
    return await ExecutionPolicy(loop_bindings())._acall_blocking_hook(
        fn, *args, **kwargs
    )


async def _maybe_persist(
    callback,
    messages: list,
    state,
    *,
    pairing: bool = False,
) -> None:
    return await ExecutionPolicy(loop_bindings())._maybe_persist(
        callback, messages, state, pairing=pairing
    )


def _function_call_output_message(call_id: Any, output: str) -> Dict:
    return ExecutionPolicy(loop_bindings())._function_call_output_message(
        call_id, output
    )


async def _commit_user_stop_pairing(
    state: State,
    outputs: List[Dict],
    persist_messages_callback: Optional[Callable[[List[Dict]], None]],
) -> None:
    return await ExecutionPolicy(loop_bindings())._commit_user_stop_pairing(
        state, outputs, persist_messages_callback
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
    return ExecutionPolicy(loop_bindings())._without_tool_call_pairs(
        messages, tool_name
    )


def _make_technical_feedback_user_message(
    content: str,
    *,
    technical_type: str = TECHNICAL_TYPE_TECHNICAL,
    call_id: str | None = None,
) -> Dict[str, str]:
    return ExecutionPolicy(
        loop_bindings()
    )._make_technical_feedback_user_message(
        content, technical_type=technical_type, call_id=call_id
    )


def _is_previous_response_cache_miss(exc: Exception) -> bool:
    return ExecutionPolicy(loop_bindings())._is_previous_response_cache_miss(
        exc
    )


def _get_backend_api_mode(ctx: Any) -> str:
    return ExecutionPolicy(loop_bindings())._get_backend_api_mode(ctx)


def _mode_requires_backend_call(mode: str) -> bool:
    return ExecutionPolicy(loop_bindings())._mode_requires_backend_call(mode)


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
    level = getattr(state, "turn_level", None)
    if not level or router.is_subagent_child:
        return router.resolve("main")
    return router.resolve_level(level)


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


async def _arequest_backend_canonical(
    messages: List[Dict],
    state: State,
    ctx: Any,
    *,
    set_previous_response_id: bool = True,
) -> Tuple[List[Dict], str, Dict]:
    operations = getattr(ctx, "operations", None)
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
    cfg: Dict[str, Any],
    set_previous_response_id: bool,
    network: tuple,
) -> Tuple[List[Dict], str, Dict]:
    # Preserve explicit legacy overrides. Typed AppConfig exposes its endpoint
    # as backend.completions_url for both API modes.
    verify, cert, use_retry, request_timeout_sec, config_proxy = network
    url = cfg.get("agent_url") or cfg["completions_url"]
    call_data = create_call_data(
        messages,
        state,
        set_previous_response_id=set_previous_response_id,
        resolved=resolved,
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
    cfg: Dict[str, Any], resolved_for_network: Any
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
        cfg.get("chat_completions_max_tokens", 8096)
        if _max_tokens_override is NO_BACKEND_OVERRIDE
        else _max_tokens_override,
        default=8096,
    )
    _min_tokens_override = backend_override_value(
        resolved_for_network, "chat_completions_min_response_tokens"
    )
    min_response_tokens = parse_chat_completions_max_tokens(
        cfg.get("chat_completions_min_response_tokens", 1024)
        if _min_tokens_override is NO_BACKEND_OVERRIDE
        else _min_tokens_override,
        default=1024,
    )
    return completions_max_tokens, min_response_tokens


def _first_choice_finish_reason(res: Dict[str, Any]) -> Any:
    _choices_raw = res.get("choices")
    if isinstance(_choices_raw, list) and len(_choices_raw) > 0:
        _ch0 = _choices_raw[0]
        if isinstance(_ch0, dict):
            return _ch0.get("finish_reason")
    return None


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
    # (легаси/тестовые двойники) — resolved=None, воронки читают cfg напрямую как раньше.
    resolved = _resolve_main_for_turn(state, ctx)
    call_headers = await acreate_call_headers(state, ctx, resolved=resolved)
    cfg = getattr(ctx, "config_dict", {}) or {}
    backend_mode = _get_backend_api_mode(ctx)
    # Per-model url/token/backend-квирки — chat_completions-only (спека §4): в responses
    # из ResolvedModel действует только id модели, сетевые параметры — легаси cfg.
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
    ) = resolve_network_params(cfg, resolved_for_network)

    if backend_mode == "responses":
        return await _arequest_responses_once(
            messages,
            state,
            ctx,
            resolved=resolved,
            call_headers=call_headers,
            cfg=cfg,
            set_previous_response_id=set_previous_response_id,
            network=(
                verify,
                cert,
                use_retry,
                request_timeout_sec,
                config_proxy,
            ),
        )

    url = resolved.url if resolved is not None else cfg["completions_url"]
    sanitized_messages = sanitize_messages_for_chat_completions(
        messages,
        invalid_tool_call_factory=build_invalid_tool_call_feedback_message,
    )
    sanitized_state_messages = sanitize_messages_for_chat_completions(
        getattr(state, "messages", []) or [],
        invalid_tool_call_factory=build_invalid_tool_call_feedback_message,
    )
    chat_state = SimpleNamespace(
        messages=sanitized_state_messages,
        tools=getattr(state, "tools", []),
        default_model=getattr(state, "default_model", None),
    )
    # Лимиты токенов — тоже поля backend, и у модели реестра они СВОИ: окно
    # маленькой модели меньше глобального потолка, и без оверрайда адаптивный цикл
    # долбил бы её от чужого потолка к чужому полу. Тот же приём, что в служебной
    # воронке (`utils/llm_chat_client._send_chat_completion_text`).
    completions_max_tokens, min_response_tokens = _chat_token_limits(
        cfg, resolved_for_network
    )

    def _create_payload(max_tokens: int) -> Dict[str, Any]:
        return create_call_data_completions(
            sanitized_messages,
            chat_state,
            max_tokens=max_tokens,
            config_dict=cfg,
            resolved=resolved,
        )

    _stream_on = resolve_tui_stream_enabled(cfg)
    _on_delta = getattr(ctx, "on_stream_delta", None) if _stream_on else None
    prompt_message_count = len(getattr(state, "messages", []) or [])

    with usage_scope(
        getattr(state, "usage", None), USAGE_CATEGORY_MAIN, backend_mode
    ):
        res, call_data = await arequest_chat_completions_with_policy(
            payload_factory=_create_payload,
            initial_max_tokens=completions_max_tokens,
            min_response_tokens=min_response_tokens,
            headers=call_headers,
            url=url,
            verify=verify,
            cert=cert,
            use_retry=use_retry,
            retry_embedded_choice_errors=retry_embedded_choice_errors,
            salvage_reasoning=True,
            logger=getattr(ctx, "logger", None),
            on_delta=_on_delta,
            request_label="main_agent",
            runtime_notices_enabled=bool(getattr(ctx, "is_tui", False)),
            notice_url=url,
            timeout=request_timeout_sec,
            config_proxy=config_proxy,
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
    persist_reasoning = persist_reasoning_content_from_config(cfg)
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


def check_mode(messages, logger) -> Union[str, None]:
    return ExecutionPolicy(loop_bindings()).check_mode(messages, logger)


def _batch_has_pending_function_calls(messages: List[Dict]) -> bool:
    return ExecutionPolicy(loop_bindings())._batch_has_pending_function_calls(
        messages
    )


def group_messages(messages: List[Dict]) -> Dict[str, List[Dict]]:
    return ExecutionPolicy(loop_bindings()).group_messages(messages)


async def _aask_backend_and_record(messages, state: State, ctx: Any):
    return await ExecutionPolicy(loop_bindings())._aask_backend_and_record(
        messages, state, ctx
    )


async def aprocess_user_ask(messages, state: State, ctx: Any):
    return await ExecutionPolicy(loop_bindings()).aprocess_user_ask(
        messages, state, ctx
    )


def partition_work_then_finish(
    tool_calls: List[Dict],
) -> tuple[List[Dict], List[Dict]]:
    return ExecutionPolicy(loop_bindings()).partition_work_then_finish(
        tool_calls
    )


def _first_finish_call(tool_calls: List[Dict]) -> Optional[Dict]:
    return ExecutionPolicy(loop_bindings())._first_finish_call(tool_calls)


def _output_for_call(messages: List[Dict], call_id: Any) -> Any:
    return ExecutionPolicy(loop_bindings())._output_for_call(messages, call_id)


def _closing_finish_after_batch(
    tool_calls: List[Dict],
    batch_outputs: List[Dict],
    *,
    stop_requested: bool,
) -> Optional[Dict]:
    return ExecutionPolicy(loop_bindings())._closing_finish_after_batch(
        tool_calls, batch_outputs, stop_requested=stop_requested
    )


def _split_batch_new_around_finish(
    batch_new: List[Dict],
    finish_call_id: Any,
) -> tuple[List[Dict], List[Dict], List[Dict]]:
    return ExecutionPolicy(loop_bindings())._split_batch_new_around_finish(
        batch_new, finish_call_id
    )


# finish_reason == "length": JSON вызова усечён потолком max_tokens — объясняем
# модели причину и советуем компактный вариант / разбиение на вызовы.


def _attach_truncation_hint(
    exc: WrongFunctionArgumentsException, state: Any
) -> None:
    return ExecutionPolicy(loop_bindings())._attach_truncation_hint(exc, state)


def _extra_finish_skip_outputs(skipped: List[Dict]) -> List[Dict]:
    # skip extra-finish готовим заранее, чтобы сфлашить их до ask_human вместе
    # с уже выполненными тулами.
    return ExecutionPolicy(loop_bindings())._extra_finish_skip_outputs(skipped)


def _after_ask_human_skip_output(call_id: Any) -> Dict[str, Any]:
    return ExecutionPolicy(loop_bindings())._after_ask_human_skip_output(
        call_id
    )


def _not_started_outputs(
    remaining: List[Dict], state: State, ctx: Any
) -> List[Dict]:
    return ExecutionPolicy(loop_bindings())._not_started_outputs(
        remaining, state, ctx
    )


async def _aexecute_tool_call(
    m: Dict,
    tool_name: str,
    tool_arguments: str,
    state: State,
    ctx: Any,
    run_agent_func,
) -> Tuple[Any, bool, Any]:
    return await ExecutionPolicy(loop_bindings())._aexecute_tool_call(
        m, tool_name, tool_arguments, state, ctx, run_agent_func
    )


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
    return await ExecutionPolicy(loop_bindings())._aemit_intermediate_reply(
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


def _track_repeat_errors(
    state: State, tool_name: str, tool_res_raw: Any
) -> bool:
    return ExecutionPolicy(loop_bindings())._track_repeat_errors(
        state, tool_name, tool_res_raw
    )


def _apply_session_compact_to_batch(
    state: State, batch_snapshot: List[Dict], tools_outputs: List[Dict]
) -> List[Dict]:
    # Если внутри batch сработал session_compact — вместо сырых output'ов
    # вернуть сохранённые пары call/output + technical-сообщение «продолжай»,
    # чтобы не оставить висячий function_call и не тащить старое окно дальше.
    return ExecutionPolicy(loop_bindings())._apply_session_compact_to_batch(
        state, batch_snapshot, tools_outputs
    )


async def _afinalize_tool_batch(
    state: State,
    ctx: Any,
    tools_outputs: List[Dict],
    batch_snapshot: List[Dict],
    *,
    aborted_by_user_stop: bool,
    persist_messages_callback: Optional[Callable[[List[Dict]], None]],
) -> List[Dict]:
    return await ExecutionPolicy(loop_bindings())._afinalize_tool_batch(
        state,
        ctx,
        tools_outputs,
        batch_snapshot,
        aborted_by_user_stop=aborted_by_user_stop,
        persist_messages_callback=persist_messages_callback,
    )


async def _abefore_tool_call(
    i: int,
    tool_name: str,
    state: State,
    *,
    prior: List[Dict],
    persist_messages_callback,
) -> None:
    # smart-гейт (спека §3): считаем КАЖДЫЙ исполняемый вызов, кроме завершителя хода.
    return await ExecutionPolicy(loop_bindings())._abefore_tool_call(
        i,
        tool_name,
        state,
        prior=prior,
        persist_messages_callback=persist_messages_callback,
    )


async def _afinish_tool_call(
    m: Dict,
    tool_name: str,
    tool_arguments: str,
    tool_res_raw: Any,
    state: State,
    ctx: Any,
) -> Dict:
    return await ExecutionPolicy(loop_bindings())._afinish_tool_call(
        m, tool_name, tool_arguments, tool_res_raw, state, ctx
    )


async def aprocess_tool_calls(
    messages: List[Dict],
    state: State,
    ctx: Any,
    run_agent_func,
    persist_messages_callback: Optional[Callable[[List[Dict]], None]] = None,
):
    return await ExecutionPolicy(loop_bindings()).aprocess_tool_calls(
        messages, state, ctx, run_agent_func, persist_messages_callback
    )


async def aprocess_tool_outputs(messages, state: State, ctx: Any):
    return await ExecutionPolicy(loop_bindings()).aprocess_tool_outputs(
        messages, state, ctx
    )


def _pickup_pending_as_new_turn(
    get_pending_messages: Optional[Callable[[], List[Dict]]],
    state: State,
    ctx: Any,
) -> Optional[List[Dict]]:
    return ExecutionPolicy(loop_bindings())._pickup_pending_as_new_turn(
        get_pending_messages, state, ctx
    )


def loop_bindings():
    """Per-run bindings preserve embedding callers' patchable helper boundary."""
    return SimpleNamespace(
        ASK_HUMAN_TOOL_NAME=ASK_HUMAN_TOOL_NAME,
        Any=Any,
        Dict=Dict,
        ExecutionFailure=ExecutionFailure,
        FINISH_KIND_LENGTH=FINISH_KIND_LENGTH,
        FINISH_TOOL_NAME=FINISH_TOOL_NAME,
        FinishGateGuard=FinishGateGuard,
        INTERMEDIATE_REPLY_TOOL_NAME=INTERMEDIATE_REPLY_TOOL_NAME,
        List=List,
        NO_TOOL_CALL_BOUNCE_MESSAGE=NO_TOOL_CALL_BOUNCE_MESSAGE,
        SESSION_COMPACT_TOOL_NAME=SESSION_COMPACT_TOOL_NAME,
        SESSION_TODO_TOOL_NAME=SESSION_TODO_TOOL_NAME,
        TECHNICAL_TYPE_ERROR_MESSAGE=TECHNICAL_TYPE_ERROR_MESSAGE,
        TECHNICAL_TYPE_TECHNICAL=TECHNICAL_TYPE_TECHNICAL,
        TURN_FINALIZING_TOOLS=TURN_FINALIZING_TOOLS,
        UsageAccount=UsageAccount,
        WrongFunctionArgumentsException=WrongFunctionArgumentsException,
        _MAX_CONTEXT_OVERFLOW_RETRIES=_MAX_CONTEXT_OVERFLOW_RETRIES,
        _SESSION_COMPACT_RESUME_USER_TEXT=_SESSION_COMPACT_RESUME_USER_TEXT,
        _TRUNCATED_ARGUMENTS_HINT=_TRUNCATED_ARGUMENTS_HINT,
        _aask_backend_and_record=_aask_backend_and_record,
        _abefore_tool_call=_abefore_tool_call,
        _acall_blocking_hook=_acall_blocking_hook,
        _aemit_intermediate_reply=_aemit_intermediate_reply,
        _aexecute_tool_call=_aexecute_tool_call,
        _afinalize_tool_batch=_afinalize_tool_batch,
        _afinish_tool_call=_afinish_tool_call,
        _after_ask_human_skip_output=_after_ask_human_skip_output,
        _apply_session_compact_to_batch=_apply_session_compact_to_batch,
        _arequest_backend_canonical=_arequest_backend_canonical,
        _attach_truncation_hint=_attach_truncation_hint,
        _batch_has_pending_function_calls=_batch_has_pending_function_calls,
        _closing_finish_after_batch=_closing_finish_after_batch,
        _commit_user_stop_pairing=_commit_user_stop_pairing,
        _extra_finish_skip_outputs=_extra_finish_skip_outputs,
        _fallback_logger=_fallback_logger,
        _first_finish_call=_first_finish_call,
        _function_call_output_message=_function_call_output_message,
        _get_backend_api_mode=_get_backend_api_mode,
        _is_previous_response_cache_miss=_is_previous_response_cache_miss,
        _make_technical_feedback_user_message=_make_technical_feedback_user_message,
        _maybe_persist=_maybe_persist,
        _mode_requires_backend_call=_mode_requires_backend_call,
        _not_started_outputs=_not_started_outputs,
        _output_for_call=_output_for_call,
        _pickup_pending_as_new_turn=_pickup_pending_as_new_turn,
        _split_batch_new_around_finish=_split_batch_new_around_finish,
        _track_repeat_errors=_track_repeat_errors,
        _without_tool_call_pairs=_without_tool_call_pairs,
        aapply_tool=aapply_tool,
        abuild_limit_envelope=abuild_limit_envelope,
        amaybe_record_final_answer_intent=amaybe_record_final_answer_intent,
        amaybe_update_intent_scope_from_tool=amaybe_update_intent_scope_from_tool,
        apersist_subagent_trace=apersist_subagent_trace,
        apply_finish_gate_to_turn_finish_output_in_state=apply_finish_gate_to_turn_finish_output_in_state,
        apply_tool_usage_notice_before_llm=apply_tool_usage_notice_before_llm,
        aprocess_tool_calls=aprocess_tool_calls,
        aprocess_tool_outputs=aprocess_tool_outputs,
        aprocess_user_ask=aprocess_user_ask,
        arun_agent=arun_agent,
        ashould_return_text_answer=ashould_return_text_answer,
        asyncio=asyncio,
        await_callback=await_callback,
        begin_turn_main_trace=begin_turn_main_trace,
        brief_args_summary=brief_args_summary,
        build_invalid_tool_call_feedback_message=build_invalid_tool_call_feedback_message,
        build_preserved_batch=build_preserved_batch,
        check_mode=check_mode,
        classify_finish_kind=classify_finish_kind,
        clear_loop_detector=clear_loop_detector,
        emit_progress=emit_progress,
        extract_intermediate_prose=extract_intermediate_prose,
        extract_last_assistant_text=extract_last_assistant_text,
        filter_messages_not_in_state=filter_messages_not_in_state,
        finish_blocks_final=finish_blocks_final,
        finish_prevents_text_final=finish_prevents_text_final,
        flush_loop_detector=flush_loop_detector,
        format_message_responses=format_message_responses,
        format_no_tool_call_bounce=format_no_tool_call_bounce,
        gc=gc,
        gc_force_collect_enabled=gc_force_collect_enabled,
        generate_message_id_func=generate_message_id_func,
        get_function_call_output_text=get_function_call_output_text,
        group_messages=group_messages,
        human_console_enabled=human_console_enabled,
        inspect=inspect,
        is_context_overflow_error=is_context_overflow_error,
        is_internal_tool_error_output=is_internal_tool_error_output,
        is_persist_discarded=is_persist_discarded,
        is_unknown_tool_output=is_unknown_tool_output,
        json=json,
        maybe_store_and_wrap_tool_output=maybe_store_and_wrap_tool_output,
        not_started_due_to_user_stop_output=not_started_due_to_user_stop_output,
        observe_loop_detector=observe_loop_detector,
        parse_tool_result_status=parse_tool_result_status,
        partition_work_then_finish=partition_work_then_finish,
        projection_tags_from_config=projection_tags_from_config,
        sanitize_messages_for_chat_completions=sanitize_messages_for_chat_completions,
        time=time,
        todo_items_from_arguments=todo_items_from_arguments,
        wrap_technical_user_content=wrap_technical_user_content,
    )


# Keep the public helper/monkeypatch boundary while sharing one loop.
class _AgentLoop(AgentLoop):
    def __init__(self, state, ctx, **kwargs):
        super().__init__(state, ctx, bindings=loop_bindings(), **kwargs)


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
