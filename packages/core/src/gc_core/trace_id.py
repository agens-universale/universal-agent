"""Внешний trace_id: нормализация значения и слаг для имени лог-файла.

trace_id приходит извне (оркестратор pdlc) и попадает в HTTP-заголовки, в записи
логов и — через слаг — в имя лог-файла. Поэтому валидация делается один раз, на
входе: ``requests`` кидает ``InvalidHeader`` на CR/LF и на не-строку, а эта ошибка
ретраебельна, то есть кривое значение стоит ~150 секунд снов на каждый вызов
модели вместо быстрой ошибки на старте.

Модуль намеренно без зависимостей от остального кода: его импортирует
``utils/logging_module``, который грузится очень рано.
"""

from typing import Any, Dict, Optional

# Предел длины сырого значения. Заголовок он не ломает, но защищает имя файла
# (basename ограничен 255 байтами) и логи от мусора.
MAX_TRACE_ID_LEN = 200
# Предел длины слага в имени файла: agent_<kind>_<slug>_<run_id>.log должен
# оставаться заметно короче лимита файловой системы.
MAX_TRACE_SLUG_LEN = 64


def normalize_trace_id(value: Any) -> Optional[str]:
    """Привести внешний trace_id к строке либо вернуть None (отсутствие).

    ``None`` и пустая (после strip) строка — отсутствие, а не ошибка: незаданный
    ``${ENV:VAR}`` резолвится в "" с варнингом, и это штатный путь.
    Управляющие символы, превышение длины и символы вне latin-1 — ``ValueError``.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > MAX_TRACE_ID_LEN:
        raise ValueError(f"trace_id длиннее {MAX_TRACE_ID_LEN} символов: {len(text)}")
    for ch in text:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise ValueError("trace_id не должен содержать управляющие символы (включая CR, LF и tab)")
    # HTTP-заголовки кодируются latin-1 (RFC 7230), поэтому кириллица, эмодзи и
    # прочее вне 0-255 роняют КАЖДЫЙ запрос к модели с UnicodeEncodeError. Хуже
    # того, ошибка ретраебельна: 5 попыток со снами 10/20/40/80s на каждый вызов,
    # и только потом падение. Ловим на старте — ровно ради этого валидация и есть.
    try:
        text.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "trace_id должен кодироваться в latin-1 (ограничение HTTP-заголовков): "
            f"недопустимый символ {text[exc.start : exc.end]!r} в позиции {exc.start}"
        ) from exc
    return text


def trace_id_slug(value: str) -> str:
    """Безопасная для имени файла производная от trace_id.

    Регистр сохраняется, ``[A-Za-z0-9_-]`` проходят как есть, всё остальное
    (включая ``/``, ``\\``, точки и не-ASCII) → ``_``. Обрезка до
    ``MAX_TRACE_SLUG_LEN``. Именно поэтому в путь идёт слаг, а не сырое значение.
    """
    chars = [ch if (ch.isascii() and (ch.isalnum() or ch in "-_")) else "_" for ch in value]
    return "".join(chars)[:MAX_TRACE_SLUG_LEN]


def trace_id_from_config(config_dict: Optional[Dict[str, Any]]) -> str:
    """Прочитать trace_id из плоского config_dict; "" означает отсутствие.

    Единственный разрешённый способ чтения: пустая строка обязана трактоваться
    как отсутствие, поэтому проверять ``"trace_id" in config_dict`` нельзя.
    """
    raw = (config_dict or {}).get("trace_id")
    if raw is None:
        return ""
    return str(raw).strip()


def trace_header_name(config_dict: Optional[Dict[str, Any]], default: str) -> str:
    """Имя заголовка для trace_id: ``agent.trace_id_header_name`` или ``default``.

    Дефолты разные по назначению: для бэкенда модели — ``x_trace_id``, для MCP —
    ``trace_id`` (как просит ТЗ). Один конфиг-ключ переопределяет оба.
    """
    raw = (config_dict or {}).get("trace_id_header_name")
    name = str(raw).strip() if raw is not None else ""
    return name or default


def model_trace_header_enabled(config_dict: Optional[Dict[str, Any]]) -> bool:
    """Разрешена ли отправка trace_id в заголовке к бэкенду модели.

    ``agent.trace_header_enabled``, по умолчанию False: рассылка трейса наружу —
    строго opt-in, как и для MCP-серверов (``mcp.servers.<name>.
    trace_header_enabled``). Флаг гейтит ТОЛЬКО исходящий заголовок; имя
    лог-файла, записи логов и ``meta.runtime.trace_ids`` от него не зависят.
    """
    return bool((config_dict or {}).get("trace_header_enabled", False))
