import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

from runtime_notices import maybe_push_llm_failure_notice
from utils.connect_backend import request_backend_chat_completions
from utils.backend_errors import is_retriable_backend_error

_CHAT_COMPLETIONS_HTTP_RETRY_DELAY_SEC = 3.0
_CHAT_COMPLETIONS_HTTP_RETRY_FACTOR = 0.5
# До 5 попыток с понижением max_tokens после HTTP-ошибки (один HTTP-запрос
# на шаг, без transport-ретраев — сеть уже отработала на первой попытке).
_CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS = 5


def _reduced_chat_completions_max_tokens(*, configured: int, minimum: int) -> int:
    reduced = int(configured * _CHAT_COMPLETIONS_HTTP_RETRY_FACTOR)
    return max(int(minimum), reduced)


def request_chat_completions_with_policy(
    *,
    payload_factory: Callable[[int], Dict[str, Any]],
    initial_max_tokens: int,
    min_response_tokens: int,
    headers: Dict[str, str],
    url: str,
    verify: bool,
    cert: Optional[Tuple[str, str]] = None,
    use_retry: bool,
    logger: logging.Logger | None = None,
    on_delta=None,
    request_label: Optional[str] = None,
    runtime_notices_enabled: bool = False,
    notice_url: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Execute chat/completions request with the minimal adaptive max_tokens policy.

    Transport remains in `utils.connect_backend`; this module owns the request policy:
    first call uses transport retries (network); on HTTP 4xx/5xx only, up to
    `_CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS` lower-max_tokens retries without
    transport retries. Connection errors/timeouts are not retried here.
    """

    def _send_once(
        max_tokens: int, *, transport_use_retry: bool
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        payload = payload_factory(max_tokens)
        response = request_backend_chat_completions(
            payload,
            headers,
            url=url,
            verify=verify,
            cert=cert,
            use_retry=transport_use_retry,
            logger=logger,
            on_delta=on_delta,
        )
        return response, payload

    try:
        return _send_once(initial_max_tokens, transport_use_retry=use_retry)
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
            if reduced_max_tokens >= current_max_tokens:
                break

            run_logger.warning(
                "chat/completions HTTP error; adaptive retry %s/%s with lower "
                "max_tokens from %s to %s",
                step,
                _CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS,
                current_max_tokens,
                reduced_max_tokens,
                exc_info=True,
            )
            time.sleep(_CHAT_COMPLETIONS_HTTP_RETRY_DELAY_SEC)
            try:
                return _send_once(reduced_max_tokens, transport_use_retry=False)
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
