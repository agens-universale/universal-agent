import json
import re
from typing import Any, Dict, List

from user_message_policy import (
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    is_dialog_summary_user_message_text,
    user_content_to_text,
    wrap_technical_user_content,
)
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func

_ORIGINAL_USER_MESSAGES_BLOCK_RE = re.compile(
    r"<original_user_messages>.*?</original_user_messages>\s*",
    re.IGNORECASE | re.DOTALL,
)


def strip_original_user_messages_block(summary_text: str) -> str:
    """
    Удаляет из текста фрагмент ``<original_user_messages>...</original_user_messages>``.

    Зачем: при **входе в LLM-суммаризацию** этот блок дублирует то, что уже передаётся отдельным полем
    ``<user_requests_log>`` в user-промпте (см. ``thread_summarizer._build_history_text`` через
    ``_message_text_limited``). В дамп треда после compaction текст саммари хранится как выдал пайплайн,
    без потери фрагментов на этапе ``thread_store.archive_and_compact_summary_payload``.

    Что делает: одно regex-замещение по всему тексту, затем ``strip()`` по краям.

    Аргументы:
        summary_text: исходная строка саммари. Не-``str`` трактуется как отсутствие данных.

    Возвращает:
        Очищенную строку либо ``""``, если вход не строка или после очистки текст пуст.
    """
    if not isinstance(summary_text, str):
        return ""
    cleaned = _ORIGINAL_USER_MESSAGES_BLOCK_RE.sub("", summary_text)
    return cleaned.strip()


def history_has_dialog_summary_message(messages: List[Dict[str, Any]]) -> bool:
    """
    Проверяет, есть ли в истории уже вставленное техническое user-сообщение с типом dialog summary.

    Зачем: оркестратор (например, ``agent_executor``) различает «сырой» тред и тред после compaction —
    без этого легко повторить суммаризацию или неверно обработать загруженный из persistence трейс.

    Что делает: проходит по списку, отбирает только dict с ``role == "user"``, извлекает текст через
    ``user_content_to_text`` и вызывает ``is_dialog_summary_user_message_text``.

    Аргументы:
        messages: последовательность сообщений (как в OpenAI-совместимом API). Не-словари пропускаются.

    Возвращает:
        ``True``, если найдено непустое user-сообщение, распознаваемое как dialog summary; иначе ``False``.
    """
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).strip().lower() != "user":
            continue
        content = user_content_to_text(msg.get("content")).strip()
        if not content:
            continue
        if is_dialog_summary_user_message_text(content):
            return True
    return False


def _message_content_to_text(content: Any) -> str:
    """
    Приводит поле ``content`` (или payload tool-вызова) к одной строке для встраивания в текст саммари.

    Зачем: ``message_text_for_summary`` должен работать и с чистой строкой, и с multimodal-списком,
    и с произвольным JSON — без дублирования правил в нескольких ветках.

    Что делает: строка возвращается как есть; список — конкатенация текстовых частей и JSON остального;
    dict — ``json.dumps``; ``None`` — ``""``; прочие типы — ``str(...)``.

    Аргументы:
        content: произвольное значение из тела сообщения или аргументов/вывода function_call.

    Возвращает:
        Скалярную строку без побочных эффектов.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
            parts.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    if content is None:
        return ""
    return str(content)


def message_text_for_summary(message: Dict[str, Any]) -> str:
    """
    Строит каноничное текстовое представление одного сообщения для LLM-суммаризации и метрик размера.

    Зачем: ``thread_summarizer`` и ``calc_messages_chars_total`` должны смотреть на один и тот же
    сериализованный вид, иначе пороги по символам и текст для модели разъедутся.

    Что делает: для ``type=function_call`` / ``function_call_output`` формирует помеченные блоки;
    для обычных сообщений — ``[<role>]`` плюс текст из ``_message_content_to_text``.

    Аргументы:
        message: одно сообщение-трекда. Не-словарь даёт ``""``.

    Возвращает:
        Одна строка — текстовый снимок сообщения для промпта суммаризатора и ``len()`` в метриках.
    """
    if not isinstance(message, dict):
        return ""
    msg_type = str(message.get("type") or "message")
    if msg_type == "function_call":
        name = str(message.get("name") or "")
        arguments = message.get("arguments")
        return f"[function_call] {name}\n{_message_content_to_text(arguments)}"
    if msg_type == "function_call_output":
        output = message.get("output")
        return f"[function_call_output]\n{_message_content_to_text(output)}"
    role = str(message.get("role") or "")
    content = _message_content_to_text(message.get("content"))
    return f"[{role}] {content}".strip()


def calc_messages_chars_total(messages: List[Dict[str, Any]]) -> int:
    """
    Считает суммарную длину в символах сериализованного трека (как его увидит саммари).

    Зачем: в ``compaction`` сравнивают размер контекста с порогом до/после схлопывания; одна точка
    подсчёта через ``message_text_for_summary`` совпадает с тем, что уходит в LLM.

    Что делает: для каждого элемента списка берёт ``len(message_text_for_summary(message))`` и суммирует.

    Аргументы:
        messages: список сообщений того же вида, что обходит pipeline суммаризации.

    Возвращает:
        Неотрицательное целое — суммарное число символов по всем сообщениям.
    """
    total = 0
    for message in messages:
        total += len(message_text_for_summary(message))
    return total


def apply_summary_state_patch(
    *,
    state: Any,
    summary_message_content: str,
    memory_additions: str,
    summarized_messages_count: int,
) -> None:
    """
    Атомарно вносит в объект ``state`` результат compaction-суммаризации (без I/O и без решения «когда»).

    Зачем: каноничное состояние исполнения живёт в ``state`` (см. принципы слоёв); эта утилита — одна
    техническая точка, где согласованно обновляются заметки, оффсет шагов, привязка к предыдущему ответу API
    и список сообщений. Вызывается из ``compaction`` после успешной суммаризации.

    Что делает:
        1. ``state.notes_md`` — дополняет текстом ``memory_additions``, только если он непустой;
           иначе поле не меняет.
        2. ``state.dialog_steps_offset`` — увеличивает на ``summarized_messages_count`` (служебно через
           ``max(0, int(...))``).
        3. ``state.previous_response_id`` — сбрасывает в ``None`` (контракт после compaction).
        4. ``state.messages`` — заменяет на компактный трейс: если в прежней истории есть ``system``,
           оставляет первое такое сообщение первым, затем одно техническое user-сообщение dialog summary
           (``wrap_technical_user_content`` + ``TECHNICAL_TYPE_DIALOG_SUMMARY``).

    Аргументы:
        state:
            Изменяемый in-place объект исполнения; ожидаются атрибуты ``notes_md``, ``dialog_steps_offset``,
            ``previous_response_id``, ``messages``.
        summary_message_content:
            Текст саммари предыдущего диалога; попадает внутрь служебного user-сообщения.
        memory_additions:
            Извлечённые durable дополнения к памяти треда (markdown); при непустом значении дописываются в notes.
        summarized_messages_count:
            Число сообщений, схлопнутых в этом проходе; участвует в ``dialog_steps_offset``.

    Возвращает:
        ``None``; побочный эффект — мутация ``state``.
    """
    notes_base = (state.notes_md or "") if isinstance(getattr(state, "notes_md", None), str) else ""
    notes_new = notes_base
    additions_text = str(memory_additions or "").strip()
    if additions_text:
        notes_new = (
            (notes_base + "\n\n" + additions_text).strip()
            if notes_base
            else additions_text
        )
        state.notes_md = notes_new
    prev_offset = int(getattr(state, "dialog_steps_offset", 0) or 0)
    state.dialog_steps_offset = prev_offset + max(0, int(summarized_messages_count or 0))
    state.previous_response_id = None

    summary_message = {
        "role": "user",
        "content": wrap_technical_user_content(
            (
                "Summarized context from previous dialogue.\n"
                f"{str(summary_message_content or '').strip()}"
            ),
            technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
        ),
        "id": generate_message_id_func("m_sum"),
    }
    previous_messages = (
        list(getattr(state, "messages", []))
        if isinstance(getattr(state, "messages", []), list)
        else []
    )
    system_message = None
    for msg in previous_messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).strip().lower() == "system":
            system_message = msg
            break
    state.messages = [summary_message]
    if isinstance(system_message, dict):
        state.messages = [system_message, summary_message]
    return None
