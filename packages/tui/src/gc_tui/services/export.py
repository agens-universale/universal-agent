"""Экспорт треда — json (pretty-обёртка) / txt (аннотированный лог)."""

from __future__ import annotations

import json
import re

from gc_core.attachments.model import attachments_of, chip_of, wh_text
from gc_core.message_policy.user_message_policy import (
    is_technical_user_message_text,
    resolve_technical_user_content_type,
    user_content_to_text,
)

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
    all_recs = [r for seg in segments for r in seg["messages"]]
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


def _render_txt(segments: list[dict], header: dict) -> str:
    out: list[str] = [_header_line(header), ""]
    all_recs = [r for seg in segments for r in seg["messages"]]
    name_by_call = _build_call_name_map(all_recs)
    n = 0
    for seg in segments:
        for r in seg["messages"]:
            payload = r.get("payload")
            if not isinstance(payload, dict):
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
