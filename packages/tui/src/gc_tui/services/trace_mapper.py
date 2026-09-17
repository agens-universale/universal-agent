"""raw message dict → TraceEvent (§3 спеки)."""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from gc_tui.models import TraceEvent
from gc_tui.services.tool_identity import tool_id


_TECHNICAL_TAG_RE = re.compile(r"<technical[^>]*>(.*?)</technical>", re.DOTALL)
# Greedy: strips first <think> → LAST </think> (one span). Robust к тому, что
# модель цитирует/обсуждает think-теги внутри рассуждения (рассогласованные/
# вложенные теги). Tradeoff: реальный ответ МЕЖДУ двумя think-блоками будет
# срезан — патологический редкий случай vs частая утечка рассуждения наружу.
# Вторая ветка (<think>.*) — если </think> нет вовсе (модель не закрыла тег),
# режем до конца строки (зеркалит stream_sink._THINK_RE), иначе сырое рассуждение
# течёт в трейс. Порядок важен: закрытый спан матчим первым.
_THINK_BLOCK_RE = re.compile(r"<think>.*</think>|<think>.*", re.DOTALL | re.IGNORECASE)
_SUMMARY_TAG_RE = re.compile(r"<summary[^>]*>(.*?)</summary>", re.DOTALL)
_SUMMARIZED_PREFIX_RE = re.compile(
    r"^Summarized context from previous dialogue\.\s*", re.IGNORECASE
)
# Новый формат summary (master): <summary> содержит вложенные секционные теги
# (<task_and_context>…) — разворачиваем их в текст, иначе сырой XML утекает в плашку.
_SUMMARY_SECTION_RE = re.compile(r"<([a-z][a-z0-9_]*)>(.*?)</\1>", re.DOTALL)


def _clean_summary(inner_raw: str) -> str:
    """Очищенный текст сводки: <summary>-содержимое, без <think> и служебного префикса.

    Новый формат master кладёт в <summary> вложенные секционные теги
    (<task_and_context>…) — разворачиваем их в читаемые "Section: body" строки,
    иначе сырой XML утекает в плашку. Старый плоский формат проходит насквозь.
    """
    text = inner_raw or ""
    text = _SUMMARIZED_PREFIX_RE.sub("", text).strip()
    m = _SUMMARY_TAG_RE.search(text)
    if m:
        text = m.group(1)
    text = _THINK_BLOCK_RE.sub("", text)  # вырезаем reasoning модели
    sections = _SUMMARY_SECTION_RE.findall(text)
    if sections:
        text = "\n".join(
            f"{tag.replace('_', ' ').capitalize()}: {body.strip()}"
            for tag, body in sections
            if body.strip()
        )
    return text.strip()


def _str_or_none(v: Any) -> Optional[str]:
    return v if isinstance(v, str) else None


def _extract_assistant_text(content: Any) -> str:
    """Extract user-visible text from assistant content.

    Handles:
    - str → strip <think>...</think>, return remainder
    - list[{"type": "output_text", "text": "..."}] → concat text fields, strip think, return
    - None / other → empty string
    """
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        # chat_completions backend structured content
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")
            if item_type in ("output_text", "text"):
                t = item.get("text", "")
                if isinstance(t, str):
                    parts.append(t)
        raw = "".join(parts)
    else:
        return ""

    cleaned = _THINK_BLOCK_RE.sub("", raw).strip()
    return cleaned


def _output_marked_skipped(output: Any) -> bool:
    """True, если output тула — synthetic skip лишнего ``turn_finish``
    (processing_responses.partition_work_then_finish): верхнеуровневый
    ``"skipped": true`` в JSON-выводе. Единственный источник такого маркера в
    tool-output'ах — skip-путь; логи finish-gate и вложенная метка суммаризатора
    сюда не попадают (не function_call_output / не верхний уровень)."""
    if not isinstance(output, str):
        return False
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and data.get("skipped") is True


def collect_skipped_call_ids(messages) -> set:
    """call_id'ы штатно пропущенных финализаторов — по их synthetic skip-output'ам.
    Вызывающие делают pre-pass по батчу записей и передают набор в map_message,
    чтобы подавить и парный `function_call` (иначе turn_finish рисует фантомную
    прозу-«ответ», а второй turn_ask_human — фантомный баннер)."""
    ids: set = set()
    for m in messages:
        if not isinstance(m, dict) or m.get("type") != "function_call_output":
            continue
        if _output_marked_skipped(m.get("output")):
            cid = m.get("call_id")
            if isinstance(cid, str):
                ids.add(cid)
    return ids


def map_message(
    msg: dict, *, no: int, ts: str, skipped_call_ids: Optional[set] = None
) -> Optional[TraceEvent]:
    """Превращает payload-сообщение thread_store в TraceEvent.

    ``skipped_call_ids`` — набор call_id штатно пропущенных финализаторов (см.
    collect_skipped_call_ids). Их `function_call` не рендерим (парный skip-output
    подавляется отдельно по маркеру `skipped: true`)."""
    if not isinstance(msg, dict):
        return None
    role = msg.get("role")
    typ = msg.get("type")
    mid = _str_or_none(msg.get("id"))

    # USER
    if role == "user":
        # Скилл-сообщения несут display_content (чистый промпт) и wire content
        # (с инструкцией скилла). display_content обрабатываем первым, до
        # технического разбора: скилл-сообщения не technical.
        if "display_content" in msg:
            disp = (msg.get("display_content") or "").strip()
            if not disp:
                return None                      # mid-run доставка → реплику не рисуем
            return TraceEvent(
                no=no, event_ts_utc_iso=ts, kind="user",
                title="", summary=disp, full_payload=msg, message_id=mid,
            )
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            stripped = content.strip()
            # Synthetic technical messages (<technical type="...">...</technical>).
            # error_message — показываем (бэкенд-ошибки нужны пользователю).
            # Остальные (bounce, summary, context) — hidden=True: kind=system
            # учитывается polling-логикой (thinking_indicator), но в трейс не рендерится.
            if stripped.startswith("<technical") and "</technical>" in stripped:
                m = _TECHNICAL_TAG_RE.search(stripped)
                inner_text = (m.group(1).strip() if m else stripped)[:120]
                tech_type = ""
                if m:
                    tag_m = re.search(r'type="([^"]+)"', stripped)
                    tech_type = tag_m.group(1) if tag_m else ""
                if tech_type == "error_message":
                    # Ошибки бэкенда — заметная красная строка (см. _render_event).
                    cid = msg.get("call_id")
                    return TraceEvent(
                        no=no, event_ts_utc_iso=ts, kind="error_msg",
                        title="!", summary=inner_text,
                        full_payload=msg, message_id=mid,
                        call_id=cid if isinstance(cid, str) else None,
                    )
                if tech_type == "dialog_summary":
                    # Суммаризация — видимый маркер + блок очищенной сводки.
                    # Берём ПОЛНЫЙ inner (не обрезанный [:120]).
                    full_inner = m.group(1) if m else stripped
                    return TraceEvent(
                        no=no, event_ts_utc_iso=ts, kind="summary",
                        title="summary", summary="контекст сжат",
                        summary_content=_clean_summary(full_inner),
                        full_payload=msg, message_id=mid, hidden=False,
                    )
                # bounce / current_context — служебное, скрываем.
                return TraceEvent(
                    no=no, event_ts_utc_iso=ts, kind="system",
                    title="policy", summary=f"[policy] {inner_text}",
                    full_payload=msg, message_id=mid,
                    hidden=True,
                )
            return TraceEvent(
                no=no, event_ts_utc_iso=ts, kind="user",
                title="", summary=stripped,
                full_payload=msg, message_id=mid,
            )
        return None

    # AGENT (text)
    if role == "assistant":
        text = _extract_assistant_text(msg.get("content"))
        if text:
            return TraceEvent(
                no=no, event_ts_utc_iso=ts, kind="agent",
                title="", summary=text,
                full_payload=msg, message_id=mid,
            )
        return None

    # THINK (reasoning) — suppressed: reasoning is shown via the ThinkingIndicator
    # tail (docs/tui/specs/2026-06-16-reasoning-indicator-design.md), not as a
    # trace row. Export keeps reasoning (services/export.py).
    if typ in ("reasoning", "think"):
        return None

    # Special tool-event handling (must precede generic function_call mapping).
    name = msg.get("name") if isinstance(msg.get("name"), str) else None
    name = tool_id(name)
    call_id = msg.get("call_id") if isinstance(msg.get("call_id"), str) else None

    # SKIPPED turn_finish (processing_responses.partition_work_then_finish): лишний
    # finish после первого или все finish после ask_human. Ни synthetic
    # skip-output (`skipped: true`), ни парный `function_call` не рендерим —
    # служебный статус, не ошибка. В data_*.jsonl и raw-payload всё остаётся.
    if typ == "function_call_output" and _output_marked_skipped(msg.get("output")):
        return None
    if typ == "function_call" and skipped_call_ids and call_id in skipped_call_ids:
        return None

    # ASK_HUMAN — kind=ask_human, summary=question (TraceScreen mounts banner).
    if typ == "function_call" and name == "ask_human":
        args = coerce_args(msg.get("arguments"))
        question = args.get("question", "")
        if isinstance(question, str) and question.strip():
            return TraceEvent(
                no=no, event_ts_utc_iso=ts, kind="ask_human",
                title="ask_human", summary=question.strip(),
                full_payload=msg, message_id=mid,
                call_id=call_id, tool_name=name,
            )
        return None

    # SUBMIT_SOLUTION call — task_report как kind=agent;
    # comment кладём sidecar-ключом в копию payload, чтобы не мутировать оригинал.
    if typ == "function_call" and name == "submit_solution":
        args = coerce_args(msg.get("arguments"))
        report = args.get("task_report") or ""
        comment = args.get("comment") or ""
        if isinstance(report, str) and report.strip():
            payload: dict = dict(msg)
            if isinstance(comment, str) and comment.strip():
                payload["_submit_comment"] = comment.strip()
            return TraceEvent(
                no=no, event_ts_utc_iso=ts, kind="agent",
                title="", summary=report.strip(),
                full_payload=payload, message_id=mid,
                call_id=call_id, tool_name=name,
            )
        return None

    # INTERMEDIATE_REPLY call — answer как kind=agent (проза с маркером ·);
    # output глушим (машинный статус пользователю не нужен).
    if typ == "function_call" and name == "intermediate_reply":
        args = coerce_args(msg.get("arguments"))
        answer = args.get("answer") or ""
        if isinstance(answer, str) and answer.strip():
            return TraceEvent(
                no=no, event_ts_utc_iso=ts, kind="agent",
                title="", summary=answer.strip(),
                full_payload=msg, message_id=mid,
                call_id=call_id, tool_name="intermediate_reply",
            )
        return None

    # SESSION_TODO call — чек-лист inline (kind=todo); успешный output глушим ниже.
    # Битые аргументы (items is None) — НЕ чек-лист: проваливаемся в generic
    # call-строку, статус сбоя виден механикой (не врём «чек-лист очищен»).
    if typ == "function_call" and name == "session_todo":
        from gc_core.tools.subagents.progress import todo_items_from_arguments
        _todo_items = todo_items_from_arguments(msg.get("arguments"))
        if _todo_items is not None:
            return TraceEvent(
                no=no, event_ts_utc_iso=ts, kind="todo",
                title="session_todo", summary=_todo_checklist_summary(_todo_items),
                full_payload=msg, message_id=mid,
                call_id=call_id, tool_name=name,
            )

    # INTERMEDIATE_REPLY output — глушим (машинный статус не нужен). Как и у
    # submit_solution: в chat_completions у output НЕТ name, но output-JSON
    # содержит "tool" → проверяем оба пути (иначе паразитная строка `  |- …`).
    if typ == "function_call_output":
        if name == "intermediate_reply":
            return None
        _ir_out = msg.get("output")
        if isinstance(_ir_out, str):
            try:
                _ir_data = json.loads(_ir_out)
            except json.JSONDecodeError:
                _ir_data = None
            if isinstance(_ir_data, dict) and tool_id(_ir_data.get("tool")) == "intermediate_reply":
                return None

    # SESSION_TODO output — успешный глушим (машинный статус не нужен; чек-лист
    # уже в call). Ошибку (ok: false) НЕ глушим: битый вызов рендерится generic
    # call-строкой, и его провал должен быть виден.
    if typ == "function_call_output":
        _st_out = msg.get("output")
        _st_data = None
        if isinstance(_st_out, str):
            try:
                _st_data = json.loads(_st_out)
            except json.JSONDecodeError:
                _st_data = None
        _st_is_todo = name == "session_todo" or (
            isinstance(_st_data, dict) and tool_id(_st_data.get("tool")) == "session_todo"
        )
        if _st_is_todo and not (
            isinstance(_st_data, dict) and _st_data.get("ok") is False
        ):
            return None

    # SUBMIT_SOLUTION / turn_finish output — глушим в двух случаях:
    # 1. backend прислал `name == "submit_solution"` в output-payload (responses-mode).
    # 2. chat_completions: поля `name` нет, но output-JSON содержит
    #    "tool": "submit_solution" (живой apply_tool; раньше — cycle-stop stub).
    # Раньше «TraceScreen отфильтрует по call_id» — это убирало виджет, но
    # сам TraceEvent с kind="result" доходил до _render_records и сбрасывал
    # sentinel _last_event_kind = "_finalized" (spec 2026-05-28-thinking-indicator-final-answer)
    # → индикатор re-mount'ился на следующем тике.
    if typ == "function_call_output":
        if name == "submit_solution":
            return None
        _out = msg.get("output")
        if isinstance(_out, str):
            try:
                _data = json.loads(_out)
            except json.JSONDecodeError:
                _data = None
            if isinstance(_data, dict) and tool_id(_data.get("tool")) == "submit_solution":
                return None

    # ASK_HUMAN output — Фикс A. Идентификация в двух режимах (как в
    # submit_solution-filter выше):
    # 1. responses-mode: `name == "ask_human"` на верхнем уровне payload-а.
    # 2. chat_completions-mode: `name` отсутствует, но output-JSON содержит
    #    "tool": "ask_human".
    # Непустой response → kind='user' (рендерится как обычная реплика юзера);
    # пустой/cancel/broken/ok=False → kind='system' с меткой
    # (служит сигналом banner-cleanup-у в _render_event и в replay тоже).
    if typ == "function_call_output":
        is_ask_human = name == "ask_human"
        if not is_ask_human:
            _out = msg.get("output")
            if isinstance(_out, str):
                try:
                    _data = json.loads(_out)
                except json.JSONDecodeError:
                    _data = None
                if isinstance(_data, dict) and tool_id(_data.get("tool")) == "ask_human":
                    is_ask_human = True
        if is_ask_human:
            response_text = _extract_ask_human_response(msg.get("output"))
            if response_text:
                return TraceEvent(
                    no=no, event_ts_utc_iso=ts, kind="user",
                    title="", summary=response_text,
                    full_payload=msg, message_id=mid,
                    call_id=call_id, tool_name="ask_human",
                )
            return TraceEvent(
                no=no, event_ts_utc_iso=ts, kind="system",
                title="ask_human", summary="Агент спрашивал (без ответа)",
                full_payload=msg, message_id=mid,
                call_id=call_id, tool_name="ask_human",
            )

    # function_call / function_call_output — Task 2.6
    if typ in ("function_call", "function_call_output"):
        return _map_function_event(msg, no=no, ts=ts, mid=mid)

    return None


def coerce_args(arguments: Any) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _extract_ask_human_response(output: Any) -> Optional[str]:
    """Достать `response` из JSON-output ask_human-тула.
    Возвращает None для broken JSON, отсутствующего поля, пустой строки
    или sentinel-значения '(empty response)'."""
    if not isinstance(output, str):
        return None
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # Структурный сигнал «не было ответа от юзера» — ok=False. Tul выставляет
    # это для трёх случаев: empty input (response="(empty response)"),
    # KeyboardInterrupt ("(user interrupted)") и Exception
    # ("(error reading input: ...)"). Любой из них — нет user-band-реплики.
    if data.get("ok") is False:
        return None
    response = data.get("response")
    if not isinstance(response, str):
        return None
    stripped = response.strip()
    if not stripped or stripped == "(empty response)":
        return None
    return stripped


def _scalar_preview(v: Any) -> str:
    if isinstance(v, str):
        if len(v) > 60:
            return f"<{len(v)} chars>"
        return repr(v)
    if isinstance(v, (int, float, bool)) or v is None:
        return repr(v)
    if isinstance(v, list):
        return f"<list {len(v)}>"
    if isinstance(v, dict):
        return "<object>"
    return "<?>"


def _text_preview(v: Any, limit: int) -> str:
    """Обрезать длинную строку до ~limit символов с «…», без `<N chars>`."""
    if not isinstance(v, str):
        return _scalar_preview(v)
    text = " ".join(v.split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return repr(text)


_TODO_GLYPHS = {"completed": "✓", "in_progress": "→", "pending": "○"}


def _todo_checklist_summary(todos: list) -> str:
    """Первая строка `чек-лист K/M`, далее пункты ✓/→/○. Пустой список — «очищен»."""
    lines: list[str] = []
    done = 0
    for item in todos:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        glyph = _TODO_GLYPHS.get(item.get("status"))
        if not content or glyph is None:
            continue
        if item.get("status") == "completed":
            done += 1
        lines.append(f"  {glyph} {content}")
    if not lines:
        return "чек-лист очищен"
    return "\n".join([f"чек-лист {done}/{len(lines)}", *lines])


def _arg_summary(name: str, args: dict) -> str:
    if name.startswith("subagent_"):
        # Спека: reason ~80, task ~60 — текст, не <N chars>.
        pairs: list[str] = []
        if "reason" in args:
            pairs.append(f"reason={_text_preview(args.get('reason'), 80)}")
        if "task" in args:
            pairs.append(f"task={_text_preview(args.get('task'), 60)}")
        return ", ".join(pairs)
    if name in ("read_file", "list_files"):
        p = args.get("path")
        return f"path={p!r}" if isinstance(p, str) else ""
    if name in ("write_to_file", "append_to_file"):
        p = args.get("path"); c = args.get("content", "")
        clen = len(c) if isinstance(c, str) else 0
        return f"path={p!r}, content=<{clen} chars>" if isinstance(p, str) else ""
    if name == "search_and_replace":
        p = args.get("path"); o = args.get("old", ""); n = args.get("new", "")
        return (f"path={p!r}, old=<{len(o) if isinstance(o, str) else 0} chars>, "
                f"new=<{len(n) if isinstance(n, str) else 0} chars>")
    if name == "search_files":
        q = args.get("query") or args.get("pattern")
        return f"query={q!r}" if isinstance(q, str) else ""
    if name == "execute_command":
        c = args.get("command")
        return f"command={c!r}" if isinstance(c, str) else ""
    if name == "run_python_code":
        code = args.get("code", "")
        return f"code=<{len(code) if isinstance(code, str) else 0} chars>"
    if name == "fetch_url":
        u = args.get("url")
        return f"url={u!r}" if isinstance(u, str) else ""
    if name == "thread_notes":
        a = args.get("action")
        return f"action={a!r}"

    # default: name + 1-3 short scalars
    pairs = []
    for k, v in list(args.items())[:3]:
        pairs.append(f"{k}={_scalar_preview(v)}")
    return ", ".join(pairs)


def _looks_like_error(output: Any) -> bool:
    if not isinstance(output, str):
        return False
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("ok") is False:
        return True
    return bool(data.get("error"))


def _summarize_output(output: Any, *, error: bool) -> str:
    if not isinstance(output, str):
        return ""
    # Единый unwrap: голый dict, double-encoded, list-обёртка, stored_tool_output
    # envelope (с partial-parser для truncated). Та же логика что у preview.
    from gc_tui.services.tool_preview import unwrap_tool_output_dict
    data = unwrap_tool_output_dict(output)
    if data is None:
        # output вообще не JSON — просто короткий plain fallback.
        return output.replace("\n", " ")[:80]
    if not isinstance(data, dict):
        return str(data)[:80]

    # User-stop / step-limit: короткие статусы вместо длинного error.message.
    status = data.get("status")
    if status == "cancelled_by_user":
        return "отменено"
    if status == "not_started_due_to_user_stop":
        return "не запущено (стоп)"
    if status == "step_limit":
        return "лимит шагов"
    if status == "token_limit":
        return "лимит токенов"
    if status == "tool_call_limit":
        return "лимит вызовов"

    if error:
        err = data.get("error") or data.get("message") or ""
        # error может быть dict {type, message, ...} — извлекаем message/type.
        if isinstance(err, dict):
            err = err.get("message") or err.get("type") or json.dumps(err, ensure_ascii=False)
        elif isinstance(err, str) and data.get("message"):
            # Плоский код ошибки (напр. internal_tool_error) сам по себе ничего
            # не сообщает, а человекочитаемый текст лежит рядом в "message".
            err = data["message"]
        return str(err)[:160]

    bits: list[str] = []
    # Порядок важен — сначала более конкретные ключи.
    if "total_lines" in data:
        bits.append(f"{data['total_lines']} строк")
    elif "lines" in data:
        bits.append(f"{data['lines']} строк")
    if "chars" in data:
        bits.append(f"{data['chars']} chars")
    if "matches" in data:
        # matches может быть либо count (int), либо list (для search_files —
        # массив объектов с file/line). Без len() мы бы вкладывали str(list).
        mv = data["matches"]
        mcount = len(mv) if isinstance(mv, (list, tuple)) else mv
        bits.append(f"{mcount} matches")
    if "entries" in data:
        entries = data["entries"]
        entries_count = data.get(
            "count",
            len(entries) if isinstance(entries, list) else "?",
        )
        bits.append(f"{entries_count} entries")
    elif "count" in data:
        bits.append(f"count={data['count']}")
    if "bytes" in data:
        bits.append(f"{data['bytes']} bytes")
    if "exit" in data:
        bits.append(f"exit={data['exit']}")
    if "source" in data:
        bits.append(f"source={data['source']}")
    if bits:
        return " · ".join(bits)
    # Fallback — пустой summary; ToolCallRow отрендерит без `-> ...`.
    return ""


def _map_function_event(
    msg: dict, *, no: int, ts: str, mid: Optional[str]
) -> Optional[TraceEvent]:
    typ = msg.get("type")
    name = msg.get("name") if isinstance(msg.get("name"), str) else None
    name = tool_id(name)
    call_id = msg.get("call_id") if isinstance(msg.get("call_id"), str) else None

    if typ == "function_call":
        args = coerce_args(msg.get("arguments"))
        summary = f"{name or '<tool>'}({_arg_summary(name or '', args)})"
        return TraceEvent(
            no=no, event_ts_utc_iso=ts, kind="call",
            title=name or "", summary=summary,
            full_payload=msg, message_id=mid,
            call_id=call_id, tool_name=name,
        )

    if typ == "function_call_output":
        output = msg.get("output")
        is_err = _looks_like_error(output)
        kind = "error" if is_err else "result"
        summary = _summarize_output(output, error=is_err)
        return TraceEvent(
            no=no, event_ts_utc_iso=ts, kind=kind,
            title="", summary=summary,
            full_payload=msg, message_id=mid,
            call_id=call_id, tool_name=name,
        )

    return None
