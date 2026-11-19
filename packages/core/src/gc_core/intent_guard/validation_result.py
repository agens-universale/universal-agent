from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from gc_core.message_projection import project_text_for_model

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
_THINK_CLOSE_RE = re.compile(
    r"^[ \t]*</think[ \t]*>[ \t]*$", re.IGNORECASE | re.MULTILINE
)
_FENCE_LINE_RE = re.compile(
    r"^[ \t]*(?P<marker>`{3,}|~{3,})(?P<lang>[\w-]*)[ \t]*$"
)
_FENCE_CLOSE_RE = re.compile(r"^[ \t]*(?P<marker>`{3,}|~{3,})[ \t]*$")
_VERDICT_KEY_RE = re.compile(
    r"[\"'](?:trace_ok|report_ok|is_question|trace_gaps|report_mismatches)[\"']\s*:"
    r"|[\"']action[\"']\s*:\s*[\"']"
    r"(?:accept|fix_report_and_continue|fix_report|continue_work)[\"']",
    re.IGNORECASE,
)
_JSON_FENCE_LABELS = frozenset({"json", "jsonc", "json5", "js"})


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


@dataclass(frozen=True)
class _MarkdownFence:
    start: int
    end: int
    body: str
    lang: str
    closed: bool


def _markdown_fences(text: str) -> list[_MarkdownFence]:
    fences: list[_MarkdownFence] = []
    opening: tuple[int, int, str, str] | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if opening is None:
            match = _FENCE_LINE_RE.fullmatch(stripped)
            if match is not None:
                opening = (
                    offset,
                    offset + len(line),
                    match["marker"],
                    match["lang"],
                )
        else:
            close = _FENCE_CLOSE_RE.fullmatch(stripped)
            start, body_start, marker, lang = opening
            if (
                close is not None
                and close["marker"][0] == marker[0]
                and len(close["marker"]) >= len(marker)
            ):
                fences.append(
                    _MarkdownFence(
                        start,
                        offset + len(line),
                        text[body_start:offset],
                        lang,
                        True,
                    )
                )
                opening = None
        offset += len(line)
    if opening is not None:
        start, body_start, _, lang = opening
        fences.append(
            _MarkdownFence(start, len(text), text[body_start:], lang, False)
        )
    return fences


def _outside_fences(text: str) -> str:
    fences = _markdown_fences(text)
    if not fences:
        return text
    parts: list[str] = []
    position = 0
    for fence in fences:
        parts.extend(
            (text[position : fence.start], " " * (fence.end - fence.start))
        )
        position = fence.end
    parts.append(text[position:])
    return "".join(parts)


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


def _balanced_object_end(text: str, start: int) -> int | None:
    """Find the end of an object, ignoring braces inside JSON strings."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _object_ranges(text: str) -> list[tuple[int, int]]:
    """Return balanced top-level brace ranges, including malformed JSON."""
    ranges: list[tuple[int, int]] = []
    position = 0
    while (start := text.find("{", position)) != -1:
        end = _balanced_object_end(text, start)
        if end is None:
            break
        ranges.append((start, end))
        position = end
    return ranges


def _reasoning_boundary(raw: str) -> int:
    """Find a standalone closing tag, excluding quoted JSON literals."""
    fences = _markdown_fences(raw)
    boundaries = [
        match.end()
        for match in _THINK_CLOSE_RE.finditer(raw)
        if not any(
            fence.start <= match.start() < fence.end
            and (
                fence.closed
                or _parse_unfenced_verdict(raw[match.end() :]) is None
            )
            for fence in fences
        )
    ]
    return boundaries[-1] if boundaries else 0


def _parse_json_verdict(text: str) -> ValidationResult | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return _parse_verdict_object(parsed)


def _has_verdict_object_start(text: str) -> bool:
    position = 0
    while (start := text.find("{", position)) != -1:
        end = _balanced_object_end(text, start) or len(text)
        if _VERDICT_KEY_RE.search(text[start:end]):
            return True
        position = end
    return False


def _fenced_verdict_bodies(text: str) -> tuple[bool, list[str], bool]:
    fences = _markdown_fences(text)
    bodies = [
        fence.body
        for fence in fences
        if _parse_json_verdict(fence.body) is not None
        or _has_verdict_object_start(fence.body)
        or (fence.closed and fence.lang.lower() in _JSON_FENCE_LABELS)
    ]
    unclosed = any(
        not fence.closed
        and _parse_json_verdict(fence.body) is None
        and (
            fence.lang.lower() in _JSON_FENCE_LABELS
            or _has_verdict_object_start(fence.body)
        )
        for fence in fences
    )
    return bool(bodies or unclosed), bodies, unclosed


def _parse_fenced_verdict(text: str) -> tuple[bool, ValidationResult | None]:
    has_fence, bodies, unclosed = _fenced_verdict_bodies(text)
    if (
        unclosed
        or not bodies
        or any(body.strip() != bodies[0].strip() for body in bodies[1:])
    ):
        return has_fence, None
    return True, _parse_json_verdict(bodies[0])


def _strict_json_object_ranges(text: str) -> list[tuple[int, int]]:
    decoder = json.JSONDecoder()
    ranges: list[tuple[int, int]] = []
    position = 0
    while (start := text.find("{", position)) != -1:
        try:
            parsed, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            position = start + 1
            continue
        if isinstance(parsed, dict):
            ranges.append((start, end))
        position = end
    return ranges


def _mask_inline_code(text: str) -> str:
    """Hide Markdown code outside strict JSON objects, preserving offsets."""
    json_ranges = _strict_json_object_ranges(text)
    visible = list(text)
    opening: int | None = None
    position = 0
    range_index = 0
    while position < len(text):
        if (
            range_index < len(json_ranges)
            and position == json_ranges[range_index][0]
        ):
            position = json_ranges[range_index][1]
            range_index += 1
            continue
        char = text[position]
        if char == "\n":
            opening = None
        elif char == "`" and (position == 0 or text[position - 1] != "\\"):
            if opening is None:
                opening = position
            else:
                visible[opening : position + 1] = " " * (
                    position + 1 - opening
                )
                opening = None
        position += 1
    return "".join(visible)


def _unfenced_candidate_bodies(text: str) -> tuple[list[str], bool]:
    visible = _mask_inline_code(text)
    ranges = _object_ranges(visible)
    candidates = [
        text[start:end]
        for start, end in ranges
        if _VERDICT_KEY_RE.search(visible[start:end])
    ]
    incomplete = "{" in visible[ranges[-1][1] if ranges else 0 :]
    return candidates, incomplete


def _parse_unfenced_verdict(text: str) -> ValidationResult | None:
    candidates, incomplete = _unfenced_candidate_bodies(text)
    if incomplete or len(candidates) != 1:
        return None
    return _parse_json_verdict(candidates[0])


def parse_validator_response(raw: str) -> ValidationResult | None:
    """
    Парсинг двухосного JSON валидатора (§7.1–7.2).

    ``None`` — битый JSON, нет обязательных ключей или невалидный ``action`` (fail-open).
    ``<think>`` всегда вырезается: парсер не зависит от ``strip_think`` конфига.
    """
    source = raw or ""
    source = source[_reasoning_boundary(source) :]
    cleaned = project_text_for_model(source, strip_think=True)
    has_fence, fenced_verdict = _parse_fenced_verdict(cleaned)
    if has_fence:
        if fenced_verdict is not None and fenced_verdict.ok:
            outside = _outside_fences(cleaned)
            others, incomplete = _unfenced_candidate_bodies(outside)
            if incomplete or any(
                (other := _parse_json_verdict(body)) is None or not other.ok
                for body in others
            ):
                return None
        return fenced_verdict
    return _parse_unfenced_verdict(_outside_fences(cleaned))


def _candidate_verdict_bodies(raw: str) -> list[str]:
    source = (raw or "")[_reasoning_boundary(raw or "") :]
    cleaned = project_text_for_model(source, strip_think=True)
    _, bodies, _ = _fenced_verdict_bodies(cleaned)
    others, _ = _unfenced_candidate_bodies(_outside_fences(cleaned))
    return bodies + others


def conflicting_validator_verdicts(
    raw: str, previous_raw: str | None = None
) -> ValidationResult | None:
    """Keep unresolved candidate conflicts from being accepted by salvage."""
    bodies = _candidate_verdict_bodies(raw)
    if previous_raw is not None:
        bodies.extend(_candidate_verdict_bodies(previous_raw))
    parsed = [(body, _parse_json_verdict(body)) for body in bodies]
    verdicts = [verdict for _, verdict in parsed if verdict is not None]
    if len(verdicts) < 2 or all(verdict.ok for verdict in verdicts):
        malformed = [body for body, verdict in parsed if verdict is None]
        return _malformed_conflict_result(verdicts, malformed)
    trace_ok = all(verdict.trace_ok for verdict in verdicts)
    report_ok = all(verdict.report_ok for verdict in verdicts)
    trace_gaps = list(
        dict.fromkeys(
            gap for verdict in verdicts for gap in verdict.trace_gaps
        )
    )
    report_mismatches = list(
        dict.fromkeys(
            mismatch
            for verdict in verdicts
            for mismatch in verdict.report_mismatches
        )
    )
    return ValidationResult(
        ok=trace_ok and report_ok,
        action=expected_action_for_axes(
            trace_ok=trace_ok, report_ok=report_ok
        ),
        trace_ok=trace_ok,
        report_ok=report_ok,
        trace_gaps=trace_gaps,
        report_mismatches=report_mismatches,
        missing_requirements=list(trace_gaps),
        conflicts=list(report_mismatches),
        coverage_score=min(verdict.coverage_score for verdict in verdicts),
        feedback_for_agent="",
        is_question=all(verdict.is_question for verdict in verdicts),
    )


def _malformed_conflict_result(
    verdicts: list[ValidationResult], malformed: list[str]
) -> ValidationResult | None:
    if not malformed:
        return None
    rejected = next((verdict for verdict in verdicts if not verdict.ok), None)
    if rejected is not None:
        return rejected
    if not verdicts:
        return None
    trace_rejected = any(
        _last_bool_match(_TRACE_OK_RE, body) is False for body in malformed
    )
    report_rejected = any(
        _last_bool_match(_REPORT_OK_RE, body) is False for body in malformed
    )
    if not (trace_rejected or report_rejected):
        return None
    trace_ok = not trace_rejected
    report_ok = not report_rejected
    return ValidationResult(
        ok=False,
        action=expected_action_for_axes(
            trace_ok=trace_ok, report_ok=report_ok
        ),
        trace_ok=trace_ok,
        report_ok=report_ok,
        trace_gaps=[],
        report_mismatches=[],
        missing_requirements=[],
        conflicts=[],
        coverage_score=0.0,
        feedback_for_agent="Conflicting validator output remained unparseable after retry.",
    )


def _parse_verdict_object(parsed: Any) -> ValidationResult | None:
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
