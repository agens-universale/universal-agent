"""Runtime reminder: the same marked tool was called with the same args.

The tool always still runs. This module only records fingerprints and, after a
batch, may emit one technical notice. Skip/cache/hard-stop are out of scope.
"""

from __future__ import annotations

import json
from typing import Any

from loop_detector.constants import LOOP_WARN_ATTR, LOOP_WARN_KEY, WINDOW_SIZE

_BRIEF_VALUE_LIMIT = 100
# Слот в окне, если args разобрать нельзя: consecutive соседних маркированных
# всё равно рвётся, notice по этому вызову не строится.
_UNPARSED_ARGS = {"_unparsed": True}


def is_enabled(config_dict: dict[str, Any] | None) -> bool:
    raw = (config_dict or {}).get("loop_detector")
    if not isinstance(raw, dict):
        return True
    return bool(raw.get("enabled", True))


def fingerprint(tool_name: str, args: dict[str, Any]) -> str:
    return json.dumps(
        {"args": args, "tool": tool_name},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _truncate_text(text: str, limit: int = _BRIEF_VALUE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _brief_value(value: Any) -> Any:
    if isinstance(value, str):
        return _truncate_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    dumped = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return _truncate_text(dumped)


def brief_args(args: dict[str, Any]) -> str:
    brief = {key: _brief_value(val) for key, val in args.items()}
    return json.dumps(brief, ensure_ascii=False, sort_keys=True, default=str)


def resolve_thresholds(
    tools_data: dict[str, Any] | None,
    tool_name: str,
) -> tuple[int, int] | None:
    entry = (tools_data or {}).get(tool_name) or {}
    raw = entry.get(LOOP_WARN_KEY)
    if raw is None:
        fabric = entry.get("tool_fabric")
        raw = getattr(fabric, LOOP_WARN_ATTR, None)
    if not isinstance(raw, dict):
        return None
    try:
        consecutive = int(raw["consecutive"])
        in_window = int(raw["in_window"])
    except (KeyError, TypeError, ValueError):
        return None
    if consecutive < 1 or in_window < 1:
        return None
    return consecutive, in_window


def _ensure_lists(state: Any) -> None:
    window = getattr(state, "loop_detector_window", None)
    if not isinstance(window, list):
        state.loop_detector_window = []
    pending = getattr(state, "loop_detector_pending", None)
    if not isinstance(pending, list):
        state.loop_detector_pending = []


def clear(state: Any) -> None:
    state.loop_detector_window = []
    state.loop_detector_pending = []


def _injected_field_names(
    tools_data: dict[str, Any] | None,
    config_dict: dict[str, Any] | None,
) -> list[str]:
    """Имена полей, которые навешивает inject_tool_fields (reason и любые другие)."""
    names: list[str] = []
    seen: set[str] = set()

    def _add(field: object) -> None:
        if not isinstance(field, str):
            return
        text = field.strip()
        if text and text not in seen:
            seen.add(text)
            names.append(text)

    raw = (config_dict or {}).get("inject_tool_fields")
    if isinstance(raw, dict):
        for key in raw:
            _add(key)
    for entry in (tools_data or {}).values():
        if not isinstance(entry, dict):
            continue
        for field in entry.get("injected_fields") or []:
            _add(field)
    return names


def _parse_stripped_args(
    tool_name: str,
    raw_arguments: str | dict[str, Any],
    tools_data: dict[str, Any] | None,
    config_dict: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    entry = (tools_data or {}).get(tool_name) or {}
    if isinstance(raw_arguments, dict):
        parsed: Any = dict(raw_arguments)
    else:
        try:
            parsed = json.loads(raw_arguments or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(parsed, dict):
        return None
    fabric = entry.get("tool_fabric")
    prepare = getattr(fabric, "prepare_args_before_strip", None)
    if callable(prepare):
        parsed = prepare(parsed)
        if not isinstance(parsed, dict):
            return None
    injected = _injected_field_names(tools_data, config_dict)
    if not injected:
        return parsed
    stripped = dict(parsed)
    for field in injected:
        stripped.pop(field, None)
    return stripped


def _count_consecutive(window: list[str], fp: str) -> int:
    count = 0
    for item in reversed(window):
        if item != fp:
            break
        count += 1
    return count


def _notice_for(tool_name: str, args: dict[str, Any]) -> str:
    return (
        f"Loop detector: you called {tool_name} with the same arguments again "
        f"({brief_args(args)}).\n"
        "The tool still ran; this probably does not produce new information. Use the previous\n"
        "result, change arguments, or try a different tactic."
    )


def observe(
    state: Any,
    ctx: Any,
    tool_name: str,
    raw_arguments: str | dict[str, Any],
) -> None:
    config_dict = getattr(ctx, "config_dict", None) or {}
    if not is_enabled(config_dict):
        return
    tools_data = getattr(ctx, "tools_data", None)
    args = _parse_stripped_args(tool_name, raw_arguments, tools_data, config_dict)
    fp = fingerprint(tool_name, args if args is not None else _UNPARSED_ARGS)
    _ensure_lists(state)
    state.loop_detector_window.append(fp)
    if len(state.loop_detector_window) > WINDOW_SIZE:
        state.loop_detector_window = state.loop_detector_window[-WINDOW_SIZE:]
    # Кольцо — все вызовы; пороги и notice — только маркированные с разобранными args.
    thresholds = resolve_thresholds(tools_data, tool_name)
    if thresholds is None or args is None:
        return
    consecutive_limit, in_window_limit = thresholds
    count_consecutive = _count_consecutive(state.loop_detector_window, fp)
    count_in_window = state.loop_detector_window.count(fp)
    if count_consecutive >= consecutive_limit or count_in_window >= in_window_limit:
        already = any(
            fingerprint(str(hit.get("tool") or ""), hit.get("args") or {}) == fp
            for hit in state.loop_detector_pending
        )
        if not already:
            state.loop_detector_pending.append({"tool": tool_name, "args": args})


def flush(state: Any) -> str | None:
    pending = list(getattr(state, "loop_detector_pending", None) or [])
    state.loop_detector_pending = []
    if not pending:
        return None
    if len(pending) == 1:
        hit = pending[0]
        return _notice_for(str(hit.get("tool") or ""), hit.get("args") or {})
    lines = ["Loop detector: you repeated these tool calls:"]
    for hit in pending:
        tool_name = str(hit.get("tool") or "")
        args = hit.get("args") or {}
        lines.append(f"- {tool_name} ({brief_args(args)})")
    lines.append(
        "The tools still ran; this probably does not produce new information. Use the previous "
        "result, change arguments, or try a different tactic."
    )
    return "\n".join(lines)
