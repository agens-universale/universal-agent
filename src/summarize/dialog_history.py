"""Подготовка многосообщенийной истории для промпта суммаризации: ходы, батчи, хвост, лог запросов."""

from typing import Any, Dict, List

from summarize.summary_serialization import message_text_for_summary, strip_original_user_messages_block
from user_message_policy import is_technical_user_message_text


def collect_user_messages_from_history(messages: List[Dict[str, Any]]) -> List[str]:
    """Собирает из истории сообщений уникальные тексты пользовательских реплик для лога запросов.

    Назначение: сформировать «журнал» явных пользовательских требований, который
    далее уходит в промпт суммаризации (`user_requests_log`) отдельно от полной истории.

    Поведение: обходятся только сообщения с `role == "user"`; текст берётся через
    `message_text_for_summary`, отбрасываются пустые и технические сообщения
    (`is_technical_user_message_text`); дубликаты по точному совпадению текста не
    добавляются повторно (порядок — первое вхождение).

    Args:
        messages: Список сообщений диалога в формате словарей (как в хранилище треда).

    Returns:
        Список уникальных непустых пользовательских текстов в порядке первого появления.
    """
    out: List[str] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = message_text_for_summary(msg).strip()
        if not text:
            continue
        if is_technical_user_message_text(text):
            continue
        if text not in out:
            out.append(text)
    return out


def normalize_user_messages_log(messages: List[str] | None) -> List[str]:
    """Нормализует внешний лог пользовательских сообщений в список строк без мусора.

    Назначение: безопасно принять `user_messages_log` из состояния (может быть
    не списком или содержать «технические» реплики) и привести к тому же виду,
    что и результат `collect_user_messages_from_history`.

    Поведение: не-список даёт пустой результат; элементы приводятся к строке и
    обрезаются по краям; пустые и технические строки отбрасываются; дубликаты
    по полному тексту не повторяются.

    Args:
        messages: Сырой лог или ``None``.

    Returns:
        Очищенный список уникальных пользовательских текстов.
    """
    out: List[str] = []
    if not isinstance(messages, list):
        return out
    for item in messages:
        text = str(item).strip()
        if not text:
            continue
        if is_technical_user_message_text(text):
            continue
        if text not in out:
            out.append(text)
    return out


def truncate_text(text: str, max_chars: int) -> str:
    """Укорачивает строку до `max_chars` с явным маркером обрезки.

    Назначение: предсказуемо ограничить размер текста в промптах и логах без
    молчаливой потери контекста.

    Args:
        text: Исходный текст.
        max_chars: Максимальная длина сохраняемого префикса; при ≤ 0 текст не режется.

    Returns:
        Исходная строка или префикс длины `max_chars` с суффиксом ``... [TEXT CUTTED]``.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + " ... [TEXT CUTTED]"


def message_text_limited(message: Dict[str, Any], max_message_chars: int) -> str:
    """Возвращает текст одного сообщения для суммаризации с опциональной обрезкой.

    Назначение: единообразно извлечь «видимый» текст сообщения (включая мультимодальные
    поля через `message_text_for_summary`), убрав служебный блок original-user-messages,
    и при необходимости усечь до лимита символов.

    Args:
        message: Словарь сообщения из истории треда.
        max_message_chars: Если положительный — результат проходит через
            `truncate_text`; иначе возвращается полный текст после strip-блока.

    Returns:
        Строка текста сообщения, готовая для подсчёта длины батчей и для промпта.
    """
    text = strip_original_user_messages_block(message_text_for_summary(message))
    if max_message_chars > 0:
        return truncate_text(text, max_message_chars)
    return text


def build_history_text(
    messages: List[Dict[str, Any]],
    max_chars: int = 200_000,
    max_message_chars: int = 0,
) -> str:
    """Склеивает историю в нумерованный многострочный текст для подстановки в промпт LLM.

    Назначение: получить компактное, линейное представление батча сообщений с
    контролем размера, чтобы не превысить лимиты контекста/логирования.

    Поведение: для каждого сообщения извлекается текст (с опциональной обрезкой
    на сообщение через `message_text_limited`), пустые пропускаются; строки вида
    ``"{n}. {text}"`` накапливаются, пока суммарная длина (плюс переносы) не
    превысит `max_chars` — остальное не включается.

    Args:
        messages: Сообщения одного батча в порядке хронологии.
        max_chars: Верхняя граница длины итоговой строки (по грубой оценке).
        max_message_chars: Если > 0, каждое сообщение обрезается до этой длины
            перед агрегированием.

    Returns:
        Текст диалога для вставки в XML-блоки промпта или логи.
    """
    parts: List[str] = []
    total = 0
    for idx, msg in enumerate(messages, start=1):
        text = message_text_limited(msg, max_message_chars).strip()
        if not text:
            continue
        row = f"{idx}. {text}"
        total += len(row) + 1
        if total > max_chars:
            break
        parts.append(row)
    return "\n".join(parts)


def trim_history_to_max_dialogue_chars(
    messages: List[Dict[str, Any]],
    *,
    max_dialogue_chars: int,
    max_message_chars: int,
) -> List[Dict[str, Any]]:
    """Обрезает историю с конца назад так, чтобы суммарный объём текста уложился в лимит.

    Назначение: перед суммаризацией отбросить самые старые сообщения, если полный
    диалог слишком длинный; сохраняется хвост диалога (наиболее актуальная часть).

    Поведение: при `max_dialogue_chars <= 0` история не режется. Иначе с конца
    накапливается длина `message_text_limited` для каждого сообщения; как только
    сумма превышает лимит, берётся суффикс начиная со следующего индекса; если
    даже одного сообщения «не влезает» в бюджет, возвращается список из последнего
    сообщения.

    Args:
        messages: Полная история в хронологическом порядке.
        max_dialogue_chars: Максимальная суммарная длина текста выбранного суффикса.
        max_message_chars: Лимит на одно сообщение при оценке длины (как в батчинге).

    Returns:
        Возможно укороченный список сообщений (суффикс исходного списка).
    """
    if not messages:
        return []
    if max_dialogue_chars <= 0:
        return messages
    total = 0
    start_idx = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        total += len(message_text_limited(messages[i], max_message_chars))
        if total > max_dialogue_chars:
            start_idx = i + 1
            break
        start_idx = i
    if start_idx >= len(messages):
        return messages[-1:]
    return messages[start_idx:]


def trim_original_user_messages_to_max_chars(messages: List[str], max_chars: int) -> List[str]:
    """Оставляет в логе пользовательских запросов хвост, укладывающийся в лимит символов.

    Назначение: ограничить размер блока `<user_requests_log>` в промпте, сохранив
    наиболее поздние формулировки пользователя.

    Поведение: при `max_chars <= 0` список не меняется. Иначе сообщения перебираются
    с конца к началу; в результат попадают подряд последние элементы, пока суммарная
    длина не превысит `max_chars` (первое сообщение, не поместившееся целиком при
    уже непустом результате, отбрасывается вместе с более старыми).

    Args:
        messages: Список пользовательских текстов (уже нормализованный).
        max_chars: Ограничение на суммарную длину выбранного хвоста.

    Returns:
        Подсписок `messages` — последние элементы в исходном порядке.
    """
    if not messages:
        return []
    if max_chars <= 0:
        return messages
    out: List[str] = []
    used = 0
    for msg in reversed(messages):
        msg_len = len(msg)
        if out and used + msg_len > max_chars:
            break
        out.insert(0, msg)
        used += msg_len
    return out


def group_into_turns(messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Разбивает плоский список сообщений на «ходы» (turns) для батчинга и форматирования.

    Назначение: представить диалог как последовательность логических шагов: пары
    вызов инструмента + вывод, пользователь с первым ответом ассистента, либо
    отдельные сообщения, не вошедшие в склейку.

    Поведение: предварительно по `call_id` сопоставляются `function_call`/`tool_call`
    с соответствующим `*_output`. Затем линейным проходом формируются группы:
    вызов+вывод; одиночный вызов без вывода; одиночный output; для `role=user`
    в ход включаются последующие сообщения приёмника до того же сообщения
    ассистента (`role=assistant`, `type=message`), либо прерывание при новом
    пользователе или tool-сообщении.

    Args:
        messages: История сообщений в порядке возникновения.

    Returns:
        Список ходов; каждый ход — список одного или нескольких исходных сообщений.
    """
    def _is_tool_call(msg: Dict[str, Any]) -> bool:
        """True, если сообщение — вызов инструмента (function/tool call)."""
        return msg.get("type") in {"function_call", "tool_call"}

    def _is_tool_call_output(msg: Dict[str, Any]) -> bool:
        """True, если сообщение — результат выполнения вызова инструмента."""
        return msg.get("type") in {"function_call_output", "tool_call_output"}

    if not messages:
        return []
    turns: List[List[Dict[str, Any]]] = []
    consumed: set[int] = set()
    fc_to_fco: Dict[int, int] = {}

    for i, msg in enumerate(messages):
        if not _is_tool_call(msg):
            continue
        call_id = msg.get("call_id")
        for j, msg2 in enumerate(messages):
            if j in consumed:
                continue
            if _is_tool_call_output(msg2) and msg2.get("call_id") == call_id:
                fc_to_fco[i] = j
                consumed.add(j)
                break

    i = 0
    consumed.clear()
    while i < len(messages):
        if i in consumed:
            i += 1
            continue
        msg = messages[i]
        msg_type = msg.get("type", "message")
        role = msg.get("role")

        if _is_tool_call(msg):
            if i in fc_to_fco:
                j = fc_to_fco[i]
                turns.append([msg, messages[j]])
                consumed.add(j)
            else:
                turns.append([msg])
            i += 1
            continue

        if _is_tool_call_output(msg):
            turns.append([msg])
            i += 1
            continue

        if role == "user":
            turn: List[Dict[str, Any]] = [msg]
            j = i + 1
            while j < len(messages):
                nxt = messages[j]
                if j in consumed:
                    j += 1
                    continue
                nxt_type = nxt.get("type", "message")
                nxt_role = nxt.get("role")
                if nxt_role == "assistant" and nxt_type == "message":
                    turn.append(nxt)
                    consumed.add(j)
                    j += 1
                    break
                if nxt_role == "user" or nxt_type in {
                    "function_call",
                    "tool_call",
                    "function_call_output",
                    "tool_call_output",
                }:
                    break
                j += 1
            turns.append(turn)
            i = j
            continue

        turns.append([msg])
        i += 1

    return turns


def split_turns_into_char_batches(
    turns: List[List[Dict[str, Any]]],
    *,
    batch_chars_limit: int,
    max_message_chars: int = 0,
) -> List[List[List[Dict[str, Any]]]]:
    """Делит последовательность ходов на батчи по приблизительному лимиту символов.

    Назначение: инкрементальная суммаризация — несколько вызовов LLM с нарастающим
    `accumulated_summary`, чтобы не отправлять сверхдлинный текст одним запросом.

    Поведение: при неположительном `batch_chars_limit` или пустых `turns` возвращается
    либо один батч со всеми ходами, либо пустой список. Иначе ходы идут по порядку;
    длина хода — сумма длин `message_text_limited` по сообщениям хода. Новый батч
    начинается, если добавление хода превысит лимит; также батч «закрывается» при
    достижении или превышении лимита накопленной длины (включение хода может
    отодвинуть границу на следующий батч).

    Args:
        turns: Список ходов из `group_into_turns`.
        batch_chars_limit: Максимум символов на один батч (оценка по тексту сообщений).
        max_message_chars: Лимит длины одного сообщения при подсчёте.

    Returns:
        Список батчей; каждый батч — список ходов (каждый ход — список сообщений).
    """
    if batch_chars_limit <= 0 or not turns:
        return [turns] if turns else []
    batches: List[List[List[Dict[str, Any]]]] = []
    current: List[List[Dict[str, Any]]] = []
    current_chars = 0
    for turn in turns:
        turn_chars = sum(len(message_text_limited(msg, max_message_chars)) for msg in turn)
        if current and current_chars + turn_chars > batch_chars_limit:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(turn)
        current_chars += turn_chars
        if current_chars >= batch_chars_limit:
            batches.append(current)
            current = []
            current_chars = 0
    if current:
        batches.append(current)
    return batches


def format_last_dialog_steps(
    messages: List[Dict[str, Any]],
    max_steps: int = 5,
    message_max_chars: int = 5000,
    steps_offset: int = 0,
) -> str:
    """Форматирует последние N ходов диалога в XML-подобные блоки для сообщения-сводки.

    Назначение: дать модели и пользователю компактный «хвост» недавних реплик
    рядом с текстовой сводкой (`last_dialog_steps` внутри `summary_message_body.build_dialog_summary_content`).

    Поведение: история группируется в ходы; берётся срез последних `max_steps` ходов;
    каждый ход нумеруется с учётом `steps_offset` и позиции среза в общей ленте
    (`start_idx`); внутри хода тексты сообщений (после `message_text_limited`)
    склеиваются через перевод строки; пустые ходы пропускаются.

    Args:
        messages: Сообщения (обычно уже усечённая к суммаризации история).
        max_steps: Сколько последних ходов включить.
        message_max_chars: Лимит символов на сообщение при рендере.
        steps_offset: Добавка к номеру первого показанного хода (сдвиг нумерации).

    Returns:
        Многострочная строка с блоками ``<turn k>...</turn k>`` или пустая, если
        нечего показать.
    """
    turns = group_into_turns(messages)
    tail = turns[-max_steps:] if len(turns) > max_steps else turns
    start_idx = steps_offset + (len(turns) - len(tail)) + 1
    parts: List[str] = []
    for idx, turn in enumerate(tail, start=start_idx):
        rendered: List[str] = []
        for msg in turn:
            text = message_text_limited(msg, message_max_chars).strip()
            if text:
                rendered.append(text)
        text = "\n".join(rendered).strip()
        if not text:
            continue
        parts.append(f"<turn {idx}>\n{text}\n</turn {idx}>")
    return "\n\n".join(parts)
