import logging
import time
from typing import Any, Callable, Dict, Tuple

import requests

from utils.connect_backend import request_backend_chat_completions

_CHAT_COMPLETIONS_HTTP_RETRY_DELAY_SEC = 3.0
_CHAT_COMPLETIONS_HTTP_RETRY_FACTOR = 0.5
# До 5 попыток с понижением max_tokens после HTTP-ошибки (см. retry_with_backoff:
# до 5 transport-ретраев на каждую попытку при use_retry=True).
_CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS = 5
_CHAT_COMPLETIONS_HTTP_RETRY_SKIP_STATUSES = {401, 403, 404}


def _is_http_backend_error(exc: Exception) -> bool:
    if not isinstance(exc, requests.exceptions.HTTPError):
        return False
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code in _CHAT_COMPLETIONS_HTTP_RETRY_SKIP_STATUSES:
        return False
    if status_code is None:
        return True
    try:
        return int(status_code) >= 400
    except (TypeError, ValueError):
        return False


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
    use_retry: bool,
    logger: logging.Logger | None = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Execute chat/completions request with the minimal adaptive max_tokens policy.

    Transport remains in `utils.connect_backend`; this module owns the request policy:
    up to `_CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS` lower-max_tokens one-shot
    retries for HTTP backend failures, no payload changes for connection errors/timeouts.
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
            use_retry=transport_use_retry,
            logger=logger,
        )
        return response, payload

    try:
        return _send_once(initial_max_tokens, transport_use_retry=use_retry)
    except Exception as first_exc:
        if not _is_http_backend_error(first_exc):
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
                return _send_once(reduced_max_tokens, transport_use_retry=use_retry)
            except Exception as retry_exc:
                current_max_tokens = reduced_max_tokens
                if not _is_http_backend_error(retry_exc):
                    break

        run_logger.error(
            "chat/completions adaptive max_tokens retries failed; "
            "raising original backend error",
            exc_info=True,
        )
        raise first_exc
