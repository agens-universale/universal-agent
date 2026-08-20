import asyncio
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from gc_core.config_modules.backend import DEFAULT_REQUEST_TIMEOUT_SEC
from gc_core.runtime_notices import maybe_push_llm_failure_notice
from gc_core.utils.connect_backend import arequest_backend_chat_completions
from gc_core.utils.backend_errors import EmbeddedChoiceError, is_retriable_backend_error
from gc_core.utils.portal import get_portal

_CHAT_COMPLETIONS_HTTP_RETRY_DELAY_SEC = 3.0
_CHAT_COMPLETIONS_HTTP_RETRY_FACTOR = 0.5
# До 5 попыток с понижением max_tokens после HTTP-ошибки (один HTTP-запрос
# на шаг, без transport-ретраев — сеть уже отработала на первой попытке).
_CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS = 5


def _raise_embedded_from_error_dict(err: Dict[str, Any]) -> None:
    code = err.get("code")
    try:
        status = int(code) if code is not None else None
    except (TypeError, ValueError):
        status = None
    raise EmbeddedChoiceError(
        "provider returned embedded choice error: "
        f"code={code!r} message={err.get('message')!r}",
        status_code=status,
        metadata=err.get("metadata"),
        raw_code=code,
    )


def _raise_on_embedded_choice_error(response: Dict[str, Any]) -> None:
    """Поднять EmbeddedChoiceError, если провайдер зашил ошибку внутрь HTTP-200:
    choices[0].error = {code, message, metadata} (стиль OpenRouter, напр. 504
    "Upstream idle timeout"), top-level {"error": {...}} без choices, или
    finish_reason="error" с пустым выбором (без content/tool_calls). Иначе такая
    ошибка «успешно» проходит транспорт и фаталит позже в конвертации, уже за
    retry-границей; вызывается до выхода из неё, поэтому получает тот же
    ретрай-цикл, что и HTTP-ошибки.
    finish_reason="error" С контентом не трогаем: его принимает permissive-путь
    normalize_finish_reason (с warning).

    Top-level форма ({"error": {...}} без choices) иначе падает KeyError'ом в
    get_output_list (dct['choices']) за retry-границей; в стриме она уже
    обрабатывается (chat_completions_stream.py)."""
    if not isinstance(response, dict):
        return
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        ch0 = choices[0]
        err = ch0.get("error")
        if isinstance(err, dict):
            _raise_embedded_from_error_dict(err)
        fr = ch0.get("finish_reason")
        if isinstance(fr, str) and fr.lower() == "error":
            msg = ch0.get("message") or {}
            if not (msg.get("content") or msg.get("tool_calls")):
                raise EmbeddedChoiceError(
                    "provider returned finish_reason='error' with empty choice"
                )
        return
    # Нет валидного choices — проверяем top-level error (вторая типовая форма).
    top_err = response.get("error")
    if isinstance(top_err, dict):
        _raise_embedded_from_error_dict(top_err)


def _reduced_chat_completions_max_tokens(*, configured: int, minimum: int) -> int:
    reduced = int(configured * _CHAT_COMPLETIONS_HTTP_RETRY_FACTOR)
    return max(int(minimum), reduced)


async def arequest_chat_completions_with_policy(
    *,
    payload_factory: Callable[[int], Dict[str, Any]],
    initial_max_tokens: int,
    min_response_tokens: int,
    headers: Dict[str, str],
    url: str,
    verify: bool,
    cert: Optional[Tuple[str, str]] = None,
    use_retry: bool,
    retry_embedded_choice_errors: bool = True,
    logger: logging.Logger | None = None,
    on_delta=None,
    request_label: Optional[str] = None,
    runtime_notices_enabled: bool = False,
    notice_url: Optional[str] = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SEC,
    config_proxy: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Execute chat/completions request with the minimal adaptive max_tokens policy.

    Transport remains in `utils.connect_backend`; this module owns the request policy:
    first call uses transport retries (network); on HTTP 4xx/5xx only, up to
    `_CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS` lower-max_tokens retries without
    transport retries. Connection errors/timeouts are not retried here.
    """

    async def _send_once(
        max_tokens: int, *, transport_use_retry: bool
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        payload = payload_factory(max_tokens)
        response = await arequest_backend_chat_completions(
            payload,
            headers,
            url=url,
            verify=verify,
            cert=cert,
            use_retry=transport_use_retry,
            logger=logger,
            on_delta=on_delta,
            timeout=timeout,
            config_proxy=config_proxy,
        )
        if retry_embedded_choice_errors:
            _raise_on_embedded_choice_error(response)
        return response, payload

    try:
        return await _send_once(initial_max_tokens, transport_use_retry=use_retry)
    except Exception as first_exc:
        if not is_retriable_backend_error(first_exc):
            maybe_push_llm_failure_notice(
                first_exc,
                request_label=request_label,
                url=notice_url,
                enabled=runtime_notices_enabled,
            )
            raise

        run_logger = logger or logging.getLogger(__name__)
        current_max_tokens = initial_max_tokens
        for step in range(1, _CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS + 1):
            reduced_max_tokens = _reduced_chat_completions_max_tokens(
                configured=current_max_tokens,
                minimum=min_response_tokens,
            )

            run_logger.warning(
                "chat/completions backend error; adaptive retry %s/%s "
                "(max_tokens %s -> %s)",
                step,
                _CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS,
                current_max_tokens,
                reduced_max_tokens,
                exc_info=True,
            )
            # Пауза кооперативная: time.sleep заморозил бы весь loop.
            await asyncio.sleep(_CHAT_COMPLETIONS_HTTP_RETRY_DELAY_SEC)
            try:
                return await _send_once(reduced_max_tokens, transport_use_retry=False)
            except Exception as retry_exc:
                current_max_tokens = reduced_max_tokens
                if not is_retriable_backend_error(retry_exc):
                    break

        run_logger.error(
            "chat/completions adaptive max_tokens retries failed; "
            "raising original backend error",
            exc_info=True,
        )
        maybe_push_llm_failure_notice(
            first_exc,
            request_label=request_label,
            url=notice_url,
            enabled=runtime_notices_enabled,
        )
        raise first_exc


def request_chat_completions_with_policy(
    **kwargs: Any,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Синхронный двойник. Реализация одна — асинхронная; здесь только портал."""
    return get_portal().run(arequest_chat_completions_with_policy(**kwargs))
