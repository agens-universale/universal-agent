"""raw message dict → TraceEvent (§3 спеки)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Optional

from gc_core.attachments.model import ORIGIN_USER, Attachment, attachments_of
from gc_core.constants.tool_names import PROPOSE_PLAN_TOOL_NAME
from gc_core.message_policy.nudges import output_for_card
from gc_core.vision.errors import (
    ERR_NO_IMAGE_STORE,
    ERR_NO_VISION_MODEL,
    ERR_TOO_MANY_IMAGES,
    ERR_VISION_FAILED,
    REASON_EMPTY_RESPONSE,
    REASON_HTTP,
)

from gc_tui.models import TraceEvent
from gc_tui.services.image_chips import images_mentioned
from gc_tui.services.path_display import truncate_path
from gc_tui.services.tool_identity import tool_id

#: Сводки строки fs_view_image в ленте (визуальный контракт картинок §4): модель
#: получает английский текст ошибки, человек — русский по типу ошибки.
ROW_NO_VISION = "нет модели для просмотра изображений"
ROW_VISION_FAILED = "модель для зрения не ответила ({reason})"
ROW_TOO_MANY_IMAGES = "не прикреплено: за шаг не больше {n} изображений"
ROW_NO_IMAGE_STORE = "не просмотрено: нет хранилища изображений"
# Причина сбоя «глаз» (`error.reason`: код HTTP, имя исключения, пустой ответ) — частые
# по-русски; прочие показываются как есть.
_REASON_TIMEOUT = "таймаут"
_REASON_NO_CONNECTION = "нет связи"
_REASON_EMPTY = "пустой ответ"
_REASON_SERVER_ERROR = "ошибка сервера {status}"
_HTTP_REASON_RE = re.compile(
    re.escape(REASON_HTTP).replace(re.escape("{status}"), r"(\d{3})")
)

_TECHNICAL_TAG_RE = re.compile(r"<technical[^>]*>(.*?)</technical>", re.DOTALL)
# Greedy: strips first <think> → LAST </think> (one span). Robust к тому, что
# модель цитирует/обсуждает think-теги внутри рассуждения (рассогласованные/
# вложенные теги). Tradeoff: реальный ответ МЕЖДУ двумя think-блоками будет
# срезан — патологический редкий случай vs частая утечка рассуждения наружу.
# Вторая ветка (<think>.*) — если </think> нет вовсе (модель не закрыла тег),
# режем до конца строки (зеркалит stream_sink._THINK_RE), иначе сырое рассуждение
# течёт в трейс. Порядок важен: закрытый спан матчим первым.
_THINK_BLOCK_RE = re.compile(
    r"<think>.*</think>|<think>.*", re.DOTALL | re.IGNORECASE
)
_SUMMARY_TAG_RE = re.compile(r"<summary[^>]*>(.*?)</summary>", re.DOTALL)
_SUMMARIZED_PREFIX_RE = re.compile(
    r"^Summarized context from previous dialogue\.\s*", re.IGNORECASE
)
# Новый формат summary (master): <summary> содержит вложенные секционные теги
# (<task_and_context>…) — разворачиваем их в текст, иначе сырой XML утекает в плашку.
_SUMMARY_SECTION_RE = re.compile(r"<([a-z][a-z0-9_]*)>(.*?)</\1>", re.DOTALL)

#: Имя тула плана в том виде, в каком его видит маппер (без пространства имён).
PROPOSE_PLAN_ID = tool_id(PROPOSE_PLAN_TOOL_NAME)


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


def collect_image_index(messages) -> dict[str, Attachment]:
    """Пользовательские картинки батча по подписи (`Изображение #N`).

    Вызывающие делают pre-pass по записям (как collect_skipped_call_ids) и передают
    индекс в map_message: ответ на turn_ask_human несёт в тексте только плашки, а сами
    вложения лежат в следующем техническом сообщении (дизайн картинок §20.3). Номер
    сквозной по треду — подпись уникальна; снимки агента (origin=tool) делят одну
    подпись и в индекс не идут."""
    index: dict[str, Attachment] = {}
    for m in messages:
        if not isinstance(m, dict):
            continue
        for att in attachments_of(m):
            if att.origin == ORIGIN_USER:
                index[att.label] = att
    return index


@dataclass(frozen=True)
class RenderContext:
    """Сведения о батче записей, которые map_message не видит в одном сообщении.

    ``skipped_call_ids`` — call_id штатно пропущенных финализаторов (см.
    collect_skipped_call_ids): их `function_call` не рендерим (парный skip-output
    подавляется отдельно по маркеру `skipped: true`).
    ``image_index`` — пользовательские картинки батча по подписи (см.
    collect_image_index): ответ на turn_ask_human берёт из него свои картинки.
    ``closing_replies`` — call_id закрывающего вызова → текст модели того же
    ответа (turn_close.closing_replies): по нему отчёт становится итогом."""

    skipped_call_ids: frozenset = frozenset()
    image_index: Mapping[str, Attachment] = field(default_factory=dict)
    closing_replies: Mapping[str, str] = field(default_factory=dict)


def collect_render_context(messages: Iterable) -> RenderContext:
    """Pre-pass по payload'ам батча — один на всех вызывающих (лента, превью, сегменты
    сжатого треда)."""
    # Ленивый импорт: пакет тулов тянет всё ядро, лента грузится без него.
    from gc_core.tools.turn.turn_close import closing_replies

    payloads = list(messages)
    return RenderContext(
        skipped_call_ids=frozenset(collect_skipped_call_ids(payloads)),
        image_index=collect_image_index(payloads),
        closing_replies=closing_replies(payloads),
    )


def with_closing_replies(
    context: RenderContext, payloads: list
) -> RenderContext:
    """Контекст, дополненный текстом ответов из ``payloads``: найденный
    текст встаёт только на место пустого."""
    # Ленивый импорт: пакет тулов тянет всё ядро, лента грузится без него.
    from gc_core.tools.turn.turn_close import closing_replies

    known = context.closing_replies
    merged = dict(known)
    for call_id, text in closing_replies(payloads).items():
        if not known.get(call_id):
            merged[call_id] = text
    return replace(context, closing_replies=merged)


def response_tail(payloads: list) -> list:
    """Записи ещё не законченного ответа модели — после последнего
    результата тула или реплики пользователя. Живой опрос несёт их в
    следующий тик: текст ответа и его вызов могут прийти разными пачками."""
    # Ленивый импорт: пакет тулов тянет всё ядро, лента грузится без него.
    from gc_core.tools.turn.turn_close import starts_new_response

    for i in range(len(payloads) - 1, -1, -1):
        if starts_new_response(payloads[i]):
            return payloads[i + 1 :]
    return list(payloads)


# Лимит текста технической врезки (Ж1 батча 4) — см. комментарий у среза ниже.
# Величина — со смоука 5 (З3): при 120 весь бюджет уходил вступлению отказа
# схемы («Wrong arguments for `X`: Arguments for tool 'X' do not match its
# schema. Details:»), и причина отказа на экран не попадала вовсе. Строка ленты
# переносится по ширине, поэтому бюджет здесь — страховка от патологии, а не
# ширина строки: тот же вывод, что и у пути с текстом ошибки инструмента.
_TECH_TEXT_LIMIT = 240


def map_message(
    msg: dict,
    *,
    no: int,
    ts: str,
    context: Optional[RenderContext] = None,
) -> Optional[TraceEvent]:
    """Превращает payload-сообщение thread_store в TraceEvent.

    ``context`` — сведения о батче (collect_render_context); без него — пустые."""
    context = context or RenderContext()
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
                return None  # mid-run доставка → реплику не рисуем
            return TraceEvent(
                no=no,
                event_ts_utc_iso=ts,
                kind="user",
                title="",
                summary=disp,
                full_payload=msg,
                message_id=mid,
                attachments=tuple(attachments_of(msg)),
            )
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            stripped = content.strip()
            # Synthetic technical messages (<technical type="...">...</technical>).
            # error_message — показываем (бэкенд-ошибки нужны пользователю).
            # Остальные (bounce, summary, context) — hidden=True: kind=system
            # учитывается polling-логикой (thinking_indicator), но в трейс не рендерится.
            if (
                stripped.startswith("<technical")
                and "</technical>" in stripped
            ):
                m = _TECHNICAL_TAG_RE.search(stripped)
                # Текст врезки режется по границе слова (Ж1 батча 4): жёсткий
                # срез рвал сообщение посреди слова («path='<roo») — тот же
                # дефект, что батч 2 закрыл в сводке результата инструмента,
                # только эта ветка мимо той правки прошла. Переводы строк
                # схлопываем в пробелы — как в plain-ветке `_summarize_output`:
                # врезка занимает одну строку ленты, а «\n» границей слова
                # не считается, и обрез откатывался к концу вступления, выбрасывая
                # причину отказа (смоук 5, З3).
                inner_text = _truncate_word_boundary(
                    (m.group(1).strip() if m else stripped).replace("\n", " "),
                    _TECH_TEXT_LIMIT,
                )
                tech_type = ""
                if m:
                    tag_m = re.search(r'type="([^"]+)"', stripped)
                    tech_type = tag_m.group(1) if tag_m else ""
                if tech_type == "error_message":
                    # Ошибки бэкенда — заметная красная строка (см. _render_event).
                    cid = msg.get("call_id")
                    return TraceEvent(
                        no=no,
                        event_ts_utc_iso=ts,
                        kind="error_msg",
                        title="!",
                        summary=inner_text,
                        full_payload=msg,
                        message_id=mid,
                        call_id=cid if isinstance(cid, str) else None,
                    )
                if tech_type == "dialog_summary":
                    # Суммаризация — видимый маркер + блок очищенной сводки.
                    # Берём ПОЛНЫЙ inner (не обрезанный по _TECH_TEXT_LIMIT).
                    full_inner = m.group(1) if m else stripped
                    return TraceEvent(
                        no=no,
                        event_ts_utc_iso=ts,
                        kind="summary",
                        title="summary",
                        summary="контекст сжат",
                        summary_content=_clean_summary(full_inner),
                        full_payload=msg,
                        message_id=mid,
                        hidden=False,
                    )
                # bounce / current_context — служебное, скрываем.
                return TraceEvent(
                    no=no,
                    event_ts_utc_iso=ts,
                    kind="system",
                    title="policy",
                    summary=f"[policy] {inner_text}",
                    full_payload=msg,
                    message_id=mid,
                    hidden=True,
                )
            return TraceEvent(
                no=no,
                event_ts_utc_iso=ts,
                kind="user",
                title="",
                summary=stripped,
                full_payload=msg,
                message_id=mid,
                attachments=tuple(attachments_of(msg)),
            )
        return None

    # AGENT (text)  # noqa: ERA001  # описание формата, не выключенный код
    if role == "assistant":
        text = _extract_assistant_text(msg.get("content"))
        if text:
            return TraceEvent(
                no=no,
                event_ts_utc_iso=ts,
                kind="agent",
                title="",
                summary=text,
                full_payload=msg,
                message_id=mid,
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
    call_id = (
        msg.get("call_id") if isinstance(msg.get("call_id"), str) else None
    )

    # SKIPPED turn_finish (processing_responses.partition_work_then_finish): лишний
    # finish после первого или все finish после ask_human. Ни synthetic
    # skip-output (`skipped: true`), ни парный `function_call` не рендерим —
    # служебный статус, не ошибка. В data_*.jsonl и raw-payload всё остаётся.
    if typ == "function_call_output" and _output_marked_skipped(
        msg.get("output")
    ):
        return None
    if typ == "function_call" and call_id in context.skipped_call_ids:
        return None

    # ASK_HUMAN — kind=ask_human, summary=question (TraceScreen mounts banner).
    if typ == "function_call" and name == "ask_human":
        args = coerce_args(msg.get("arguments"))
        question = args.get("question", "")
        if isinstance(question, str) and question.strip():
            return TraceEvent(
                no=no,
                event_ts_utc_iso=ts,
                kind="ask_human",
                title="ask_human",
                summary=question.strip(),
                full_payload=msg,
                message_id=mid,
                call_id=call_id,
                tool_name=name,
            )
        return None

    # SUBMIT_SOLUTION call — task_report как kind=agent;
    # comment и текст модели того же ответа кладём sidecar-ключами в копию
    # payload, чтобы не мутировать оригинал.
    if typ == "function_call" and name == "submit_solution":
        args = coerce_args(msg.get("arguments"))
        report = args.get("task_report") or ""
        comment = args.get("comment") or ""
        if isinstance(report, str) and report.strip():
            payload: dict = dict(msg)
            if isinstance(comment, str) and comment.strip():
                payload["_submit_comment"] = comment.strip()
            reply = context.closing_replies.get(call_id or "", "")
            if reply:
                payload["_submit_reply"] = reply
            return TraceEvent(
                no=no,
                event_ts_utc_iso=ts,
                kind="agent",
                title="",
                summary=report.strip(),
                full_payload=payload,
                message_id=mid,
                call_id=call_id,
                tool_name=name,
            )
        return None

    # INTERMEDIATE_REPLY call — answer как kind=agent (проза с маркером ·);
    # output глушим (машинный статус пользователю не нужен).
    if typ == "function_call" and name == "intermediate_reply":
        args = coerce_args(msg.get("arguments"))
        answer = args.get("answer") or ""
        if isinstance(answer, str) and answer.strip():
            return TraceEvent(
                no=no,
                event_ts_utc_iso=ts,
                kind="agent",
                title="",
                summary=answer.strip(),
                full_payload=msg,
                message_id=mid,
                call_id=call_id,
                tool_name="intermediate_reply",
            )
        return None

    # TURN_PROPOSE_PLAN call — план как проза агента (markdown): смысл
    # одобрения несёт баннер, а не строка вызова.
    if typ == "function_call" and name == PROPOSE_PLAN_ID:
        plan_text = coerce_args(msg.get("arguments")).get("plan") or ""
        if isinstance(plan_text, str) and plan_text.strip():
            return TraceEvent(
                no=no,
                event_ts_utc_iso=ts,
                kind="agent",
                title="",
                summary=plan_text.strip(),
                full_payload=msg,
                message_id=mid,
                call_id=call_id,
                tool_name=name,
            )
        return None

    # TURN_PROPOSE_PLAN output — успешный глушим (машинный статус не нужен);
    # отказ (вне plan, слишком длинный) остаётся виден обычной строкой.
    if typ == "function_call_output":
        _pp_out = msg.get("output")
        if isinstance(_pp_out, str):
            try:
                _pp_data = json.loads(_pp_out)
            except json.JSONDecodeError:
                _pp_data = None
            if (
                isinstance(_pp_data, dict)
                and tool_id(_pp_data.get("tool")) == PROPOSE_PLAN_ID
                and _pp_data.get("ok") is True
            ):
                return None

    # SESSION_TODO call — чек-лист inline (kind=todo); успешный output глушим ниже.
    # Битые аргументы (items is None) — НЕ чек-лист: проваливаемся в generic
    # call-строку, статус сбоя виден механикой (не врём «чек-лист очищен»).
    if typ == "function_call" and name == "session_todo":
        from gc_core.execution.progress import todo_items_from_arguments

        _todo_items = todo_items_from_arguments(msg.get("arguments"))
        if _todo_items is not None:
            return TraceEvent(
                no=no,
                event_ts_utc_iso=ts,
                kind="todo",
                title="session_todo",
                summary=_todo_checklist_summary(_todo_items),
                full_payload=msg,
                message_id=mid,
                call_id=call_id,
                tool_name=name,
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
            if (
                isinstance(_ir_data, dict)
                and tool_id(_ir_data.get("tool")) == "intermediate_reply"
            ):
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
            isinstance(_st_data, dict)
            and tool_id(_st_data.get("tool")) == "session_todo"
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
            if (
                isinstance(_data, dict)
                and tool_id(_data.get("tool")) == "submit_solution"
            ):
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
                if (
                    isinstance(_data, dict)
                    and tool_id(_data.get("tool")) == "ask_human"
                ):
                    is_ask_human = True
        if is_ask_human:
            response_text = _extract_ask_human_response(msg.get("output"))
            if response_text:
                # Картинки ответа лежат в следующем техническом сообщении, а не на
                # output (иначе проекция отправила бы их дважды) — сцепка по плашкам
                # в тексте ответа (дизайн картинок §20.3).
                return TraceEvent(
                    no=no,
                    event_ts_utc_iso=ts,
                    kind="user",
                    title="",
                    summary=response_text,
                    full_payload=msg,
                    message_id=mid,
                    call_id=call_id,
                    tool_name="ask_human",
                    attachments=images_mentioned(
                        response_text, context.image_index
                    ),
                )
            return TraceEvent(
                no=no,
                event_ts_utc_iso=ts,
                kind="system",
                title="ask_human",
                summary="Агент спрашивал (без ответа)",
                full_payload=msg,
                message_id=mid,
                call_id=call_id,
                tool_name="ask_human",
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


# Аргумент с таким именем — путь (Е6 батча 3): показывается всегда; свёртка
# `<N chars>` — только для содержимого.
_PATH_KEYS = frozenset(
    {"path", "file", "file_path", "dir", "directory", "cwd", "src", "dst"}
)

# Путь показывается ЦЕЛИКОМ (Ж2 батча 4): строка ленты переносится по ширине,
# как это уже происходит с длинным текстом ошибки, и фиксированный бюджет
# прятал каталог и начало имени там, где на экране хватало места. Потолок —
# страховка от патологии (глубокое дерево, генерированные имена), не бюджет.
# Величина — со смоука 5: обрезка сегментная и имя файла держит при любом
# потолке, платят за него только промежуточные каталоги. 100 оставляет корень,
# первый каталог и имя, укладывая строку вызова в две строки ленты; 160 отдавал
# третью строку за один каталог сверху.
_PATH_LIMIT = 100


def _path_preview(value: str) -> str:
    """Путь под потолком — целиком, за потолком — со схлопнутой серединой.

    Режем именно `truncate_path`, а НЕ прозаическим `_truncate_word_boundary`:
    пробел в пути — не граница слова, а часть имени каталога (`C:\\Program
    Files`, `C:\\Users\\Имя Фамилия`, `OneDrive - Company`). Прозаическое
    правило на таком пути отрезало бы всё после первого же пробела, и от
    длинного пути осталось бы `'C:\\Program…'` — без каталога и без имени файла.
    `truncate_path` схлопывает середину и всегда сохраняет имя файла — ровно то,
    ради чего путь и показывают в строке вызова.
    """
    return repr(truncate_path(value, _PATH_LIMIT))


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
        return f"path={_path_preview(p)}" if isinstance(p, str) else ""
    if name in ("write_to_file", "append_to_file"):
        p = args.get("path")
        c = args.get("content", "")
        clen = len(c) if isinstance(c, str) else 0
        return (
            f"path={_path_preview(p)}, content=<{clen} chars>"
            if isinstance(p, str)
            else ""
        )
    if name == "search_and_replace":
        # Имена — как в схеме тула (`search`/`replace`), а не `old`/`new`:
        # схема запрещает лишние ключи, поэтому по старым именам подпись
        # ВСЕГДА показывала `<0 chars>` — при любом вызове.
        p = args.get("path")
        o = args.get("search", "")
        n = args.get("replace", "")
        path_shown = (
            _path_preview(p) if isinstance(p, str) else _scalar_preview(p)
        )
        return (
            f"path={path_shown}, "
            f"search=<{len(o) if isinstance(o, str) else 0} chars>, "
            f"replace=<{len(n) if isinstance(n, str) else 0} chars>"
        )
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

    # default: name + 1-3 short scalars.
    pairs = []
    for k, v in list(args.items())[:3]:
        shown = (
            _path_preview(v)
            if (k in _PATH_KEYS and isinstance(v, str))
            else _scalar_preview(v)
        )
        pairs.append(f"{k}={shown}")
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


def _vision_error_row(err: dict) -> str:
    """Русская сводка ошибки fs_view_image по её типу; "" — ошибка не про зрение."""
    kind = err.get("type")
    if kind == ERR_NO_VISION_MODEL:
        return ROW_NO_VISION
    if kind == ERR_TOO_MANY_IMAGES:
        return ROW_TOO_MANY_IMAGES.format(n=err.get("limit"))
    if kind == ERR_NO_IMAGE_STORE:
        return ROW_NO_IMAGE_STORE
    if kind != ERR_VISION_FAILED:
        return ""
    return ROW_VISION_FAILED.format(
        reason=_vision_reason_text(str(err.get("reason") or ""))
    )


def _vision_reason_text(reason: str) -> str:
    """Причина сбоя «глаз» для человека: таймаут, 5xx, пустой ответ и обрыв связи —
    по-русски (имена исключений httpx/openai: `ReadTimeout`, `APIConnectionError`…),
    остальное — как есть."""
    if "Timeout" in reason:
        return _REASON_TIMEOUT
    if "Connect" in reason:
        return _REASON_NO_CONNECTION
    if reason == REASON_EMPTY_RESPONSE:
        return _REASON_EMPTY
    http = _HTTP_REASON_RE.fullmatch(reason)
    if http and http.group(1).startswith("5"):
        return _REASON_SERVER_ERROR.format(status=http.group(1))
    return reason


def _truncate_word_boundary(text: str, limit: int) -> str:
    """Обрезать ПРОЗУ до `limit` символов, не разрывая слово посередине.

    Режем по последнему пробелу в пределах лимита и добавляем «…» — иначе
    длинный вывод/ошибка обрывались бы буквально на полуслове (живой баг:
    `path='<roo` вместо честного `path='<root>…`). Если пробела в пределах
    лимита нет вовсе (одно длинное «слово») — режем по limit как есть, но «…»
    всё равно добавляем: обрыв должен быть явным, а не выглядеть завершённым
    текстом.

    Правило «резать по пробелу» верно ТОЛЬКО для прозы — сводки результата,
    текста ошибки, технической врезки. Структурированную строку, где пробел не
    граница слова, оно калечит: у пути после раннего пробела (`C:\\Program
    Files\\…`) отрезается всё, включая имя файла. Путь режет `_path_preview`
    через `path_display.truncate_path`; третьему вызывающему со структурой, а не
    прозой, сюда тоже нельзя."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind(" ")
    if boundary > 0:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


def _summarize_output(output: Any, *, error: bool) -> str:
    if not isinstance(output, str):
        return ""
    output = output_for_card(output)
    # Единый unwrap: голый dict, double-encoded, list-обёртка, stored_tool_output
    # envelope (с partial-parser для truncated). Та же логика что у preview.
    from gc_tui.services.tool_preview import unwrap_tool_output_dict

    data = unwrap_tool_output_dict(output)
    if data is None:
        # output вообще не JSON — просто короткий plain fallback.
        return _truncate_word_boundary(output.replace("\n", " "), 80)
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
        # error может быть dict {type, reason, message, ...}. `reason` — причина
        # для человека: у отказов защит и режима рядом с ней лежит инструкция
        # модели (`guidance` либо хвост `message`), ей в ленте не место. Обычная
        # ошибка тула причины не несёт — её описание и есть message.
        if isinstance(err, dict):
            vision_row = _vision_error_row(err)
            if vision_row:
                return vision_row
            err = (
                err.get("reason")
                or err.get("message")
                or err.get("type")
                or json.dumps(err, ensure_ascii=False)
            )
        elif isinstance(err, str) and data.get("message"):
            # Плоский код ошибки (напр. internal_tool_error) сам по себе ничего
            # не сообщает, а человекочитаемый текст лежит рядом в "message".
            err = data["message"]
        return _truncate_word_boundary(str(err), 160)

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
    wire_name = msg.get("name") if isinstance(msg.get("name"), str) else None
    name = tool_id(wire_name)
    call_id = (
        msg.get("call_id") if isinstance(msg.get("call_id"), str) else None
    )

    if typ == "function_call":
        args = coerce_args(msg.get("arguments"))
        summary = f"{name or '<tool>'}({_arg_summary(name or '', args)})"
        return TraceEvent(
            no=no,
            event_ts_utc_iso=ts,
            kind="call",
            title=name or "",
            summary=summary,
            full_payload=msg,
            message_id=mid,
            call_id=call_id,
            tool_name=name,
            wire_name=wire_name,
        )

    if typ == "function_call_output":
        output = msg.get("output")
        is_err = _looks_like_error(output)
        kind = "error" if is_err else "result"
        summary = _summarize_output(output, error=is_err)
        return TraceEvent(
            no=no,
            event_ts_utc_iso=ts,
            kind=kind,
            title="",
            summary=summary,
            full_payload=msg,
            message_id=mid,
            call_id=call_id,
            tool_name=name,
        )

    return None
