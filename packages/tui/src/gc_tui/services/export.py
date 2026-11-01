"""Экспорт треда — json (pretty-обёртка) / txt (аннотированный лог)."""

from __future__ import annotations

import json
import re
from typing import Optional

from gc_core.attachments.model import attachments_of, chip_of, wh_text
from gc_core.auto_routing.levels import AUTO
from gc_core.message_policy.user_message_policy import (
    is_technical_user_message_text,
    resolve_technical_user_content_type,
    user_content_to_text,
)
from gc_core.persistence.model_choice_record import parse_model_choice
from gc_core.persistence.reasoning_record import parse_reasoning

from gc_tui.services.answer_flow import AnswerFlow
from gc_tui.services.trace_mapper import collect_render_context, map_message
from gc_tui.services.trace_render import finish_close_of

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_TECH_INNER_RE = re.compile(
    r"<technical\s+type=\"[a-z_]+\">(.*?)</technical>",
    re.IGNORECASE | re.DOTALL,
)


# --- helpers --------------------------------------------------------------


def _env_ts(rec: dict) -> str:
    v = rec.get("ts")
    return v if isinstance(v, str) else ""


def _build_header(
    segments: list[dict], *, api_mode: str, thread_id: str, system: str
) -> dict:
    # Экспорт — сообщения треда (строки с `payload`): записи итога хода и
    # смены модели в счёт не идут.
    all_recs = [
        r
        for seg in segments
        for r in seg["messages"]
        if isinstance(r.get("payload"), dict)
    ]
    n_sum = sum(1 for seg in segments if seg.get("compacted_by"))
    return {
        "thread_id": thread_id,
        "system": system,
        "backend_api_mode": api_mode,
        "messages_count": len(all_recs),
        "ts_first": _env_ts(all_recs[0]) if all_recs else "",
        "ts_last": _env_ts(all_recs[-1]) if all_recs else "",
        "summarizations": n_sum,
    }


# --- json -----------------------------------------------------------------


def _render_json(segments: list[dict], header: dict) -> str:
    out_segments = []
    for seg in segments:
        msgs = [
            r["payload"]
            for r in seg["messages"]
            if isinstance(r.get("payload"), dict)
        ]
        out_segments.append(
            {
                "index": seg["index"],
                "messages": msgs,
                "compacted_by": seg.get("compacted_by"),
            }
        )
    return json.dumps(
        {"thread": header, "segments": out_segments},
        ensure_ascii=False,
        indent=2,
    )


# --- txt ------------------------------------------------------------------


def _indent(text: str) -> list[str]:
    return ["    " + ln for ln in str(text).split("\n")]


def _as_text(v) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def _assistant_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in (
                "output_text",
                "text",
            ):
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return ""


def _split_think(text: str) -> tuple[str, str]:
    m = _THINK_RE.search(text)
    if not m:
        return "", text.strip()
    return m.group(1).strip(), (text[: m.start()] + text[m.end() :]).strip()


def _strip_technical(content: str) -> str:
    m = _TECH_INNER_RE.search(content)
    return m.group(1).strip() if m else content.strip()


def _format_json_str(value) -> str:
    """double-encoded JSON-строку (или dict/list) → pretty; иначе как есть."""
    if isinstance(value, str):
        try:
            return json.dumps(json.loads(value), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return _as_text(value)


def _build_call_name_map(records: list[dict]) -> dict:
    out: dict[str, str] = {}
    for r in records:
        p = r.get("payload")
        if isinstance(p, dict) and p.get("type") == "function_call":
            cid, name = p.get("call_id"), p.get("name")
            if isinstance(cid, str) and isinstance(name, str):
                out[cid] = name
    return out


def _header_line(h: dict) -> str:
    parts = [f"Thread {h['thread_id']}"]
    if h["ts_first"] or h["ts_last"]:
        parts.append(f"{h['ts_first']} → {h['ts_last']}")
    parts.append(f"{h['messages_count']} messages")
    if h["summarizations"]:
        parts.append(f"summarized ×{h['summarizations']}")
    if h["backend_api_mode"]:
        parts.append(h["backend_api_mode"])
    return "===== " + " · ".join(parts) + " ====="


def _image_lines(payload: dict) -> list[str]:
    """Картинки записи: подпись, формат и размер, путь файла. Экспорт несёт текст
    и пути, байтов картинок в нём нет (дизайн картинок §12)."""
    return [
        f"    └ {chip_of(att)} · {wh_text(att)} · {att.path}"
        for att in attachments_of(payload)
    ]


def _render_record(n: int, payload: dict, name_by_call: dict) -> list[str]:
    role, typ = payload.get("role"), payload.get("type")

    if typ == "function_call":
        name = payload.get("name") or "<tool>"
        cid = payload.get("call_id") or ""
        head = f"[{n}] CALL {name}" + (f" (call_id={cid})" if cid else "")
        return [head, *_indent(_format_json_str(payload.get("arguments")))]

    if typ == "function_call_output":
        cid = payload.get("call_id") or ""
        name = name_by_call.get(cid, "")
        head = f"[{n}] OUTPUT {name}".rstrip() + (
            f" (call_id={cid})" if cid else ""
        )
        return [head, *_indent(_format_json_str(payload.get("output")))]

    if typ in ("reasoning", "think"):
        text = payload.get("summary") or payload.get("content") or ""
        return [f"[{n}] THINK", *_indent(_as_text(text))]

    if role == "user":
        images = _image_lines(payload)
        if "display_content" in payload:
            disp = (payload.get("display_content") or "").strip()
            if not disp:
                return []  # mid-run доставка — не выводим как USER
            return [f"[{n}] USER", *_indent(disp), *images]
        content = user_content_to_text(payload.get("content"))
        if is_technical_user_message_text(content):
            ttype = resolve_technical_user_content_type(content)
            return [
                f"[{n}] USER [technical:{ttype}]",
                *_indent(_strip_technical(content)),
                *images,
            ]
        return [f"[{n}] USER", *_indent(content), *images]

    if role == "assistant":
        think, answer = _split_think(_assistant_text(payload.get("content")))
        body: list[str] = []
        if think:
            body.append("think:")
            body += ["  " + ln for ln in think.split("\n")]
            body.append("--")
        body += answer.split("\n")
        return [f"[{n}] ASSISTANT", *["    " + ln for ln in body]]

    return [
        f"[{n}] {role or typ or 'MESSAGE'}",
        *_indent(json.dumps(payload, ensure_ascii=False, indent=2)),
    ]


def _model_choice_lines(rec: dict) -> list[str]:
    """Смена модели — одна строка: имя модели, у `auto` — сам выбор."""
    record = parse_model_choice(rec)
    if record is None:
        return []
    name = record.alias if record.choice != AUTO else None
    return [f"────────── модель: {name or record.choice} ──────────", ""]


def _thought_lines(n: int, rec: dict) -> list[str]:
    """Записанная мысль модели — блок `THOUGHT` с отступом, как `THINK`."""
    record = parse_reasoning(rec)
    if record is None:
        return []
    return [f"[{n}] THOUGHT", *_indent(record.text), ""]


def _render_txt(segments: list[dict], header: dict) -> str:
    out: list[str] = [_header_line(header), ""]
    all_recs = [r for seg in segments for r in seg["messages"]]
    name_by_call = _build_call_name_map(all_recs)
    n = 0
    for seg in segments:
        for r in seg["messages"]:
            payload = r.get("payload")
            if not isinstance(payload, dict):
                lines = _thought_lines(n + 1, r)
                if lines:
                    n += 1
                out += lines or _model_choice_lines(r)
                continue
            n += 1
            out += _render_record(n, payload, name_by_call)
            out.append("")
        comp = seg.get("compacted_by")
        if comp:
            idx = seg["index"] + 1
            out.append(
                f"────────── КОНТЕКСТ СЖАТ #{idx} · {comp['messages_in_segment']} сообщений → сводка ──────────"
            )
            out += comp["synthesis"].split("\n")
            out.append(f"────────── /конец сводки #{idx} ──────────")
            out.append("")
    if n == 0 and not any(s.get("compacted_by") for s in segments):
        out.append("(пусто)")
    return "\n".join(out).rstrip() + "\n"


# --- ответы агента --------------------------------------------------------


def agent_answers(segments: list[dict]) -> list[str]:
    """Ответы агента треда по порядку (по сегментам сжатия): проза, отчёт
    финала и план ровно так, как их показывает лента (`AnswerFlow`):
    повторы внутри хода и отклонённые попытки turn_finish не считаются.
    Реплики, вывод тулов и промежуточные ответы не входят. Как в ленте:
    контекст рендера — по всем сегментам, сохранённая при сжатии копия
    записи (тот же `id` в следующем файле) не считается второй раз."""
    payloads = [
        r["payload"]
        for seg in segments
        for r in seg["messages"]
        if isinstance(r.get("payload"), dict)
    ]
    context = collect_render_context(payloads)
    flow = AnswerFlow()
    answers: list[str] = []
    attempt: Optional[int] = None  # индекс отчёта попытки в answers
    for no, payload in enumerate(_first_by_id(payloads)):
        ev = map_message(payload, no=no, ts="", context=context)
        if ev is None:
            continue
        if ev.kind == "user":
            flow.new_turn()
            attempt = None
        elif ev.kind == "agent":
            attempt = _take_agent(flow, ev, answers, attempt)
    return answers


def _first_by_id(payloads: list[dict]) -> list[dict]:
    """Записи без повторов `id`: первое вхождение (правило ленты)."""
    seen: set = set()
    out = []
    for payload in payloads:
        mid = payload.get("id")
        if mid is not None:
            if mid in seen:
                continue
            seen.add(mid)
        out.append(payload)
    return out


def _take_agent(flow, ev, answers: list[str], attempt: Optional[int]):
    """Учесть agent-событие; вернуть индекс отчёта текущей попытки."""
    if ev.tool_name == "submit_solution":
        close = flow.finish(finish_close_of(ev), tracked=bool(ev.call_id))
        text = (close.body or close.note).strip()
        if flow.report_of_intermediate:
            text = ev.summary.strip()
        if ev.call_id:
            _forget(answers, attempt)
            attempt = None
        if text:
            answers.append(text)
            if ev.call_id:
                attempt = len(answers) - 1
        return attempt
    if not (ev.summary or "").strip():
        return attempt
    intermediate = ev.tool_name == "intermediate_reply"
    verdict = flow.agent((ev.summary or "").strip(), intermediate=intermediate)
    if verdict.drop_attempt:
        _forget(answers, attempt)
        attempt = None
    if verdict.final_of_intermediate or (verdict.show and not intermediate):
        answers.append(ev.summary)
    return attempt


def _forget(answers: list[str], index: Optional[int]) -> None:
    if index is not None:
        del answers[index]


# --- public API -----------------------------------------------------------


def export_thread(
    segments: list[dict],
    *,
    fmt: str,
    api_mode: str,
    thread_id: str,
    system: str,
) -> str:
    header = _build_header(
        segments, api_mode=api_mode, thread_id=thread_id, system=system
    )
    if fmt == "json":
        return _render_json(segments, header)
    return _render_txt(segments, header)
