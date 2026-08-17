from __future__ import annotations

import json
import logging
from typing import Any

_logger = logging.getLogger(__name__)


def _emit(logger: Any, level: int, event: str, **fields: Any) -> None:
    log = logger if logger is not None else _logger
    if not log.isEnabledFor(level):
        return
    payload = json.dumps(fields, ensure_ascii=False, default=str)
    log.log(level, "intent_scope event=%s %s", event, payload)


def log_intent_scope_skipped(
    logger: Any,
    *,
    tool_name: str,
    reason: str,
) -> None:
    _emit(logger, logging.DEBUG, "skipped", tool_name=tool_name, reason=reason)


def log_intent_scope_summary_start(
    logger: Any,
    *,
    tool_name: str,
    output_chars: int,
) -> None:
    _emit(
        logger,
        logging.INFO,
        "summary_start",
        tool_name=tool_name,
        output_chars=output_chars,
    )


def log_intent_scope_summary_done(
    logger: Any,
    *,
    tool_name: str,
    summary_chars: int,
    used_fallback_raw: bool = False,
) -> None:
    _emit(
        logger,
        logging.INFO,
        "summary_done",
        tool_name=tool_name,
        summary_chars=summary_chars,
        used_fallback_raw=used_fallback_raw,
    )


def log_intent_scope_delta_appended(
    logger: Any,
    *,
    tool_name: str,
    delta_chars: int,
) -> None:
    _emit(
        logger,
        logging.INFO,
        "delta_appended",
        tool_name=tool_name,
        delta_chars=delta_chars,
    )
