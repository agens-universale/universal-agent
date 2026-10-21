import asyncio
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from gc_core.config_modules.backend import DEFAULT_REQUEST_TIMEOUT_SEC
from gc_core.runtime_notices import maybe_push_llm_failure_notice
from gc_core.utils.backend_errors import (
    embedded_choice_error,
    is_image_rejection_error,
    is_image_request_error,
    is_retriable_backend_error,
)
from gc_core.utils.connect_backend import arequest_backend_chat_completions

_CHAT_COMPLETIONS_HTTP_RETRY_DELAY_SEC = 3.0
_CHAT_COMPLETIONS_HTTP_RETRY_FACTOR = 0.5
# До 5 попыток с понижением max_tokens после HTTP-ошибки (один HTTP-запрос
# на шаг, без transport-ретраев — сеть уже отработала на первой попытке).
_CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS = 5


def _raise_on_embedded_choice_error(
    response: Dict[str, Any], *, salvage_reasoning: bool = False
) -> None:
    """Поднять ошибку, зашитую провайдером внутрь HTTP-200
    (`embedded_choice_error`). Иначе такая ошибка «успешно» проходит
    транспорт и фаталит позже в конвертации, уже за retry-границей;
    вызывается до выхода из неё, поэтому получает тот же ретрай-цикл, что и
    HTTP-ошибки. Top-level форма иначе падает KeyError'ом в get_output_list
    (dct['choices']); в стриме она уже обрабатывается
    (chat_completions_stream.py)."""
    error = embedded_choice_error(
        response, salvage_reasoning=salvage_reasoning
    )
    if error is not None:
        raise error


def _reduced_chat_completions_max_tokens(
    *, configured: int, minimum: int
) -> int:
    reduced = int(configured * _CHAT_COMPLETIONS_HTTP_RETRY_FACTOR)
    return max(int(minimum), reduced)


def _payload_has_images(payload: Dict[str, Any]) -> bool:
    """В сообщениях запроса есть части ``image_url`` (так их собирает проекция)."""
    for message in payload.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in content
        ):
            return True
    return False


def _raise_if_image_failure(
    exc: BaseException, payload: Dict[str, Any]
) -> None:
    """Картиночную ошибку запроса — наверх как есть: понижение max_tokens её не
    лечит, а «Ошибка LLM» была бы ложной тревогой — потолок картинок и повтор с
    меньшим их числом делает контроллер. На повторе адаптивного цикла — так же:
    наверх уходит сам отказ, первая ошибка ничего о причине не скажет.

    Отказ от зрения говорит о картинках сам; предел (413, число картинок) — только
    если в запросе были картинки: 413 служебного вызова без картинок идёт прежним
    путём, с «Ошибкой LLM»."""
    if is_image_rejection_error(exc) or (
        is_image_request_error(exc) and _payload_has_images(payload)
    ):
        raise exc


def _adaptive_retry_allowed(exc: BaseException, enabled: bool) -> bool:
    """Повторять ли с пониженным max_tokens: вызов это разрешает, и сбой из
    тех, что такой повтор чинит."""
    return enabled and is_retriable_backend_error(exc)


async def arequest_chat_completions_with_policy(  # noqa: PLR0913  # S107: won't fix, см. CONTRIBUTING.md
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
    salvage_reasoning: bool = False,
    logger: logging.Logger | None = None,
    on_delta=None,
    request_label: Optional[str] = None,
    runtime_notices_enabled: bool = False,
    notice_url: Optional[str] = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SEC,
    config_proxy: Optional[str] = None,
    adaptive_retry: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Execute chat/completions request with the minimal adaptive max_tokens policy.

    Transport remains in `utils.connect_backend`; this module owns the request policy:
    first call uses transport retries (network); on HTTP 4xx/5xx only, up to
    `_CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS` lower-max_tokens retries without
    transport retries. Connection errors/timeouts are not retried here.

    ``adaptive_retry=False`` — без шагов с пониженным max_tokens: вызов со
    своим бюджетом повторов (судья действий) повторяет и считает сбои сам.
    """

    last_payload: Dict[str, Any] = {}

    async def _send_once(
        max_tokens: int, *, transport_use_retry: bool
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        payload = payload_factory(max_tokens)
        last_payload.clear()
        last_payload.update(payload)
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
            _raise_on_embedded_choice_error(
                response, salvage_reasoning=salvage_reasoning
            )
        return response, payload

    try:
        return await _send_once(
            initial_max_tokens, transport_use_retry=use_retry
        )
    except Exception as first_exc:
        _raise_if_image_failure(first_exc, last_payload)
        if not _adaptive_retry_allowed(first_exc, adaptive_retry):
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
                "chat/completions backend error; adaptive retry %s/%s (max_tokens %s -> %s)",
                step,
                _CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS,
                current_max_tokens,
                reduced_max_tokens,
                exc_info=True,
            )
            # Пауза кооперативная: time.sleep заморозил бы весь loop.
            await asyncio.sleep(_CHAT_COMPLETIONS_HTTP_RETRY_DELAY_SEC)
            try:
                return await _send_once(
                    reduced_max_tokens, transport_use_retry=False
                )
            except Exception as retry_exc:
                _raise_if_image_failure(retry_exc, last_payload)
                current_max_tokens = reduced_max_tokens
                if not is_retriable_backend_error(retry_exc):
                    break

        run_logger.error(
            "chat/completions adaptive max_tokens retries failed; raising original backend error",
            exc_info=True,
        )
        maybe_push_llm_failure_notice(
            first_exc,
            request_label=request_label,
            url=notice_url,
            enabled=runtime_notices_enabled,
        )
        raise first_exc
