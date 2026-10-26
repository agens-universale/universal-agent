from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from gc_core.message_projection import project_text_for_model
from gc_core.utils.json_text import parse_json_from_text

_logger = logging.getLogger(__name__)

_FINISH_GATE_ACTIONS = frozenset(
    {"accept", "fix_report", "continue_work", "fix_report_and_continue"}
)

# Последнее вхождение в сыром ответе (модель могла поправить себя).
_TRACE_OK_RE = re.compile(
    r'["\']?trace_ok["\']?\s*:\s*(true|false)', re.IGNORECASE
)
_REPORT_OK_RE = re.compile(
    r'["\']?report_ok["\']?\s*:\s*(true|false)', re.IGNORECASE
)
_IS_QUESTION_RE = re.compile(
    r'["\']?is_question["\']?\s*:\s*(true|false)', re.IGNORECASE
)
_ACTION_RE = re.compile(
    r"""["']?action["']?\s*:\s*["']?"""
    r"""(accept|fix_report_and_continue|fix_report|continue_work)["']?""",
    re.IGNORECASE,
)
_SALVAGE_RAW_MAX_CHARS = 4000


@dataclass
class ValidationResult:
    ok: bool
    action: str
    trace_ok: bool
    report_ok: bool
    trace_gaps: list[str]
    report_mismatches: list[str]
    missing_requirements: list[str]
    conflicts: list[str]
    coverage_score: float
    feedback_for_agent: str
    is_question: bool = False


def fail_open_validation_result() -> ValidationResult:
    """Fail-open: accept finish handoff when the validator LLM path fails (§7.2)."""
    return ValidationResult(
        ok=True,
        action="accept",
        trace_ok=True,
        report_ok=True,
        trace_gaps=[],
        report_mismatches=[],
        missing_requirements=[],
        conflicts=[],
        coverage_score=1.0,
        feedback_for_agent="",
        is_question=False,
    )


def _last_bool_match(pattern: re.Pattern[str], text: str) -> bool | None:
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    return matches[-1].group(1).lower() == "true"


def _last_action_match(text: str) -> str | None:
    matches = list(_ACTION_RE.finditer(text))
    if not matches:
        return None
    token = matches[-1].group(1).strip().lower()
    if token not in _FINISH_GATE_ACTIONS:
        return None
    return token


def salvage_validator_axes(raw: str) -> ValidationResult | None:
    """Если полный JSON не собрался, но в тексте есть оба ``trace_ok`` / ``report_ok``.

    ``action`` вытаскиваем только если токен есть в тексте; иначе берём из матрицы
    осей. Найденный ``action`` нормализуется по осям (как в строгом JSON-парсе).
    Сырой ответ — в ``feedback_for_agent`` (усечённо) для логов; gaps пустые.
    ``None`` — оси не найдены → вызывающий fail-open.
    """
    cleaned = project_text_for_model(raw or "", strip_think=True)
    if not cleaned.strip():
        return None
    trace_ok = _last_bool_match(_TRACE_OK_RE, cleaned)
    report_ok = _last_bool_match(_REPORT_OK_RE, cleaned)
    if trace_ok is None or report_ok is None:
        return None
    is_question = _last_bool_match(_IS_QUESTION_RE, cleaned)
    if is_question is None:
        is_question = False
    raw_action = _last_action_match(cleaned)
    if raw_action is not None:
        action = _normalized_action(raw_action, trace_ok, report_ok)
    else:
        action = expected_action_for_axes(
            trace_ok=trace_ok, report_ok=report_ok
        )
    raw_clip = cleaned.strip()
    if len(raw_clip) > _SALVAGE_RAW_MAX_CHARS:
        raw_clip = raw_clip[:_SALVAGE_RAW_MAX_CHARS] + "…"
    _logger.warning(
        "finish_gate salvaged axes from unparseable reply "
        "trace_ok=%s report_ok=%s action=%s action_from_text=%s raw_chars=%s",
        trace_ok,
        report_ok,
        action,
        raw_action,
        len(cleaned),
    )
    return ValidationResult(
        ok=trace_ok and report_ok,
        action=action,
        trace_ok=trace_ok,
        report_ok=report_ok,
        trace_gaps=[],
        report_mismatches=[],
        missing_requirements=[],
        conflicts=[],
        coverage_score=1.0 if (trace_ok and report_ok) else 0.0,
        feedback_for_agent=raw_clip,
        is_question=is_question,
    )


def expected_action_for_axes(*, trace_ok: bool, report_ok: bool) -> str:
    if trace_ok:
        return "accept" if report_ok else "fix_report"
    return "continue_work" if report_ok else "fix_report_and_continue"


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if x is not None]


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _parse_coverage(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _warn_if_ok_disagrees(
    parsed: dict, ok: bool, trace_ok: bool, report_ok: bool
) -> None:
    if "ok" not in parsed:
        return
    json_ok = _parse_bool(parsed.get("ok"))
    if json_ok is not None and json_ok != ok:
        _logger.warning(
            "finish_gate validator JSON ok=%s disagrees with trace_ok=%s report_ok=%s; using runtime ok=%s",
            json_ok,
            trace_ok,
            report_ok,
            ok,
        )


def _normalized_action(
    action_token: str, trace_ok: bool, report_ok: bool
) -> str:
    """Действие по осям; расхождение с ответом валидатора — в лог, побеждают оси."""
    expected = expected_action_for_axes(trace_ok=trace_ok, report_ok=report_ok)
    if action_token != expected:
        _logger.warning(
            "finish_gate validator action=%r normalized to %r (trace_ok=%s report_ok=%s)",
            action_token,
            expected,
            trace_ok,
            report_ok,
        )
    return expected


def parse_validator_response(raw: str) -> ValidationResult | None:
    """
    Парсинг двухосного JSON валидатора (§7.1–7.2).

    ``None`` — битый JSON, нет обязательных ключей или невалидный ``action`` (fail-open).
    ``<think>`` всегда вырезается: парсер не зависит от ``strip_think`` конфига.
    """
    cleaned = project_text_for_model(raw or "", strip_think=True)
    parsed = parse_json_from_text(cleaned, default=None)
    if not isinstance(parsed, dict):
        return None

    trace_ok = _parse_bool(parsed.get("trace_ok"))
    report_ok = _parse_bool(parsed.get("report_ok"))
    is_question = _parse_bool(parsed.get("is_question"))
    if trace_ok is None or report_ok is None or is_question is None:
        return None

    raw_action = parsed.get("action")
    if not isinstance(raw_action, str) or not raw_action.strip():
        return None
    action_token = raw_action.strip()
    if action_token not in _FINISH_GATE_ACTIONS:
        return None

    ok = trace_ok and report_ok
    _warn_if_ok_disagrees(parsed, ok, trace_ok, report_ok)
    action = _normalized_action(action_token, trace_ok, report_ok)

    trace_gaps = _str_list(parsed.get("trace_gaps"))
    if not trace_gaps:
        trace_gaps = _str_list(parsed.get("missing_requirements"))
    report_mismatches = _str_list(parsed.get("report_mismatches"))
    if not report_mismatches:
        report_mismatches = _str_list(parsed.get("conflicts"))

    missing_requirements = _str_list(
        parsed.get("missing_requirements")
    ) or list(trace_gaps)
    conflicts = _str_list(parsed.get("conflicts")) or list(report_mismatches)

    return ValidationResult(
        ok=ok,
        action=action,
        trace_ok=trace_ok,
        report_ok=report_ok,
        trace_gaps=trace_gaps,
        report_mismatches=report_mismatches,
        missing_requirements=missing_requirements,
        conflicts=conflicts,
        coverage_score=_parse_coverage(parsed.get("coverage_score", 0.0)),
        feedback_for_agent=str(parsed.get("feedback_for_agent") or "").strip(),
        is_question=is_question,
    )
