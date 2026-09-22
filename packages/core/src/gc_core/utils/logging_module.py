import datetime
import json
import logging
import os
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from gc_core.trace_id import trace_id_from_config, trace_id_slug


class FlushFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


_RESERVED_LOGRECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


class RunContextFilter(logging.Filter):
    """Проставляет run_id / agent_kind / trace_id на каждую запись per-run логгера.

    ``trace_id`` опционален и с дефолтом: класс публичный (в ``__all__``) и
    конструируется позиционно. Пустое значение не ставится вообще — «отсутствует»
    должно означать отсутствие ключа, а не пустую строку.
    """

    def __init__(self, run_id: str, agent_kind: str, trace_id: str = "") -> None:
        super().__init__()
        self._run_id = run_id
        self._agent_kind = agent_kind
        self._trace_id = trace_id or ""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = self._run_id
        if not hasattr(record, "agent_kind"):
            record.agent_kind = self._agent_kind
        if self._trace_id and not hasattr(record, "trace_id"):
            record.trace_id = self._trace_id
        return True


class JsonlFormatter(logging.Formatter):
    """Рендерит LogRecord (+ поля из ``extra=``) одним JSON-объектом на строку.

    ``redactor`` (опц.) применяется к payload перед сериализацией.
    """

    def __init__(self, redactor: Any = None) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        dt = datetime.datetime.fromtimestamp(record.created, datetime.UTC)
        ts = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z"
        payload: Dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "run_id": getattr(record, "run_id", None),
            "agent_kind": getattr(record, "agent_kind", None),
            "event": getattr(record, "event", None),
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_KEYS or key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if self._redactor is not None:
            payload = self._redactor.redact(payload)
        return json.dumps(payload, ensure_ascii=False, default=str)


class RedactingTextFormatter(logging.Formatter):
    """Текст-форматтер с опциональной маскировкой финальной строки.

    ``redactor is None`` → поведение идентично stdlib ``logging.Formatter``.
    """

    def __init__(self, fmt: Optional[str] = None, redactor: Any = None) -> None:
        super().__init__(fmt)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if self._redactor is not None:
            text = self._redactor.redact(text)
        return text


def generate_run_id() -> str:
    """Generate unique run identifier for log isolation."""
    return f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def resolve_log_level(name: Optional[str]) -> int:
    """Строковый log_level → числовой уровень logging (дефолт INFO)."""
    return _LOG_LEVELS.get(str(name or "info").strip().lower(), logging.INFO)


def human_console_enabled(config_dict: Dict[str, Any]) -> bool:
    """Печатать ли человекочитаемый рендер (>>>>>>>>>> …) в консоль.

    Только при ``console_trace`` И текстовом формате логов: в jsonl-режиме на
    stdout уже идёт структурный event-поток, и дублировать его человекочитаемым
    рендером не нужно (иначе jsonl-потребитель получает мусор пополам с JSON).
    """
    if not config_dict.get("console_trace", True):
        return False
    return str(config_dict.get("logging_format", "text")).strip().lower() != "jsonl"


def _normalize_agent_kind(agent_kind: Optional[str]) -> str:
    value = (agent_kind or "main").strip().lower()
    if not value:
        return "main"
    if value.startswith("subagent_"):
        value = value[len("subagent_"):]
    normalized = "".join(ch if ch.isalnum() else "_" for ch in value).strip("_")
    return normalized or "main"


def build_run_log_paths(
    config_dict: Dict[str, Any],
    run_id: Optional[str] = None,
    agent_kind: Optional[str] = None,
    log_ext: str = "log",
) -> Tuple[str, str]:
    """
    Пути к логам одного запуска: общий лог агента (расширение log_ext) и
    отдельный trace-лог суммаризации (всегда .jsonl).
    """
    base_dir = config_dict.get("logs_dump_dir")
    if not isinstance(base_dir, str) or not base_dir.strip():
        raise ValueError(
            "logs_dump_dir пуст; "
            "config_modules.root.resolve_paths() должен был его заполнить"
        )
    suffix = run_id or generate_run_id()
    kind = _normalize_agent_kind(agent_kind)
    trace = trace_id_from_config(config_dict)
    if trace:
        # Слаг, а не сырое значение: os.makedirs ниже работает по уже собранному
        # пути, поэтому '/' в trace создал бы подкаталог, а '..' вышел бы из
        # logs_dump_dir. run_id остаётся: trace не уникален на ран (ретрай
        # оркестратора переиспользует его, сабагент наследует дословно), а файл
        # открывается в mode="a" — без run_id раны слиплись бы в один файл.
        suffix = f"{trace_id_slug(trace)}_{suffix}"
    logging_path = os.path.join(base_dir, f"agent_{kind}_{suffix}.{log_ext}")
    summary_trace_path = os.path.join(base_dir, f"summary_trace_{kind}_{suffix}.jsonl")
    return logging_path, summary_trace_path


_DISABLED_LOG_PATH = (
    "Null. Логи отключены. Включите логи в файле config.yml если они необходимы"
)
_TEXT_FMT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
# Отдельная строка формата, а не условная подстановка: '%(trace_id)s' падает на
# любой записи без атрибута, и строка теряется через handleError. Поэтому формат
# с trace выбирается только вместе с фильтром, который атрибут гарантирует.
_TEXT_FMT_TRACE = (
    "%(asctime)s - %(name)s - %(levelname)s - [trace=%(trace_id)s] %(message)s"
)


def _build_text_logger(logger, config_dict, run_id, kind, redactor):
    # Историческое поведение main: DEBUG-уровень, .log файл, stderr ERROR-хендлер.
    logger.setLevel(logging.DEBUG)
    trace = trace_id_from_config(config_dict)
    if trace:
        logger.addFilter(RunContextFilter(run_id, kind, trace))
        text_fmt = _TEXT_FMT_TRACE
    else:
        text_fmt = _TEXT_FMT
    if config_dict.get("save_logs", False):
        logging_path, summary_trace_path = build_run_log_paths(
            config_dict, run_id=run_id, agent_kind=kind, log_ext="log"
        )
        os.makedirs(os.path.dirname(logging_path), exist_ok=True)
        handler = FlushFileHandler(logging_path, encoding="utf-8", mode="a")
        handler.setFormatter(RedactingTextFormatter(text_fmt, redactor=redactor))
        logger.addHandler(handler)
    else:
        logging_path = _DISABLED_LOG_PATH
        summary_trace_path = ""
    err_handler = logging.StreamHandler()
    err_handler.setLevel(logging.ERROR)
    err_handler.setFormatter(RedactingTextFormatter(text_fmt, redactor=redactor))
    logger.addHandler(err_handler)
    return logger, logging_path, summary_trace_path


def _build_jsonl_logger(logger, config_dict, run_id, kind, redactor):
    logger.setLevel(resolve_log_level(config_dict.get("log_level")))
    logger.addFilter(RunContextFilter(run_id, kind, trace_id_from_config(config_dict)))
    formatter = JsonlFormatter(redactor=redactor)
    logging_path = _DISABLED_LOG_PATH
    summary_trace_path = ""
    if config_dict.get("save_logs", False):
        logging_path, summary_trace_path = build_run_log_paths(
            config_dict, run_id=run_id, agent_kind=kind, log_ext="jsonl"
        )
        os.makedirs(os.path.dirname(logging_path), exist_ok=True)
        file_handler = FlushFileHandler(logging_path, encoding="utf-8", mode="a")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    if config_dict.get("console_trace", True):
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        logger.addHandler(stdout_handler)
    return logger, logging_path, summary_trace_path


def create_run_logger(
    config_dict: Dict[str, Any],
    run_id: Optional[str] = None,
    agent_kind: Optional[str] = None,
    *,
    log_handlers: Optional[List[logging.Handler]] = None,
) -> Tuple[logging.Logger, str, str]:
    """
    Создать per-run логгер. Формат берётся из config_dict['logging_format']:
    'text' (дефолт) = историческое поведение; 'jsonl' = структурный event-поток.
    Редактирование секретов — только при redact_secrets.enabled.

    ВСТРАИВАЮЩЕМУ: своя доставка логов — через ``log_handlers``.

    Абстракции для логирования у ядра нет и не нужно: возвращается обычный
    ``logging.Logger``, а лучшего контракта, чем стандартная библиотека, не
    придумать. Нужен был только способ подключиться, и мешали ему две вещи:

    * ``logger.propagate = False`` — записи не всплывали в корневой логгер, и
      приложение со своей настройкой ``logging`` не видело НИЧЕГО. Теперь
      управляется ключом ``logging_propagate`` (дефолт ``false`` — прежнее
      поведение: ран пишет в свой файл и не засоряет чужие хендлеры);
    * ``logger.handlers.clear()`` — стирало всё, подключённое заранее. Поэтому
      хендлеры принимаются ЗДЕСЬ и ставятся после очистки.

    Файловые хендлеры ядра при этом остаются: свой обработчик ДОБАВЛЯЕТСЯ, а не
    заменяет их. Отключить файл можно как и раньше — ``save_logs: false``.

    ВНИМАНИЕ на ``ctx.logging_path`` и ``ctx.summary_trace_path``: это пути к
    ФАЙЛАМ, и при ``save_logs: false`` они пусты. Потребители обязаны это
    переживать (см. ``agent_executor`` и ``summarize/summary_logging``).
    """
    effective_run_id = run_id or generate_run_id()
    kind = _normalize_agent_kind(agent_kind)
    logger_name = f"agent_run_{kind}_{effective_run_id}"
    logger = logging.getLogger(logger_name)
    logger.propagate = bool(config_dict.get("logging_propagate", False))
    logger.handlers.clear()
    for existing in list(logger.filters):
        logger.removeFilter(existing)

    # Ленивый импорт: security тяжёлый, а logging грузится очень рано (run_context).
    from gc_core.security.log_redaction import build_redactor
    redactor = build_redactor(config_dict.get("redact_secrets"))

    fmt = str(config_dict.get("logging_format", "text")).strip().lower()
    if fmt == "jsonl":
        built = _build_jsonl_logger(logger, config_dict, effective_run_id, kind, redactor)
    else:
        built = _build_text_logger(logger, config_dict, effective_run_id, kind, redactor)

    # Хендлеры вызывающего — ПОСЛЕ сборки: handlers.clear() выше стёр бы их.
    # Их мы запоминаем как ЧУЖИЕ: закрывать чужое хозяйство не наше дело.
    foreign = set()
    for handler in log_handlers or ():
        logger.addHandler(handler)
        foreign.add(id(handler))
    _FOREIGN_HANDLERS[logger.name] = foreign
    return built


#: Хендлеры, переданные вызывающим. Их ``release_run_logger`` не трогает.
_FOREIGN_HANDLERS: dict[str, set] = {}


def release_run_logger(logger: Any) -> None:
    """Закрыть хендлеры прогона и снять логгер из реестра ``logging``.

    Имя логгера уникально на ран (``agent_run_<kind>_<run_id>``), а
    ``logging`` держит созданные объекты в ``loggerDict`` до конца процесса и
    сам их не отдаёт. Один процесс — один-два логгера, и это было незаметно.
    Но ветка объявляет сценарий «много ``AsyncAgent`` в одном процессе», где
    каждый агент и каждый субагент заводит свой: рост становится линейным, и
    вместе с логгерами живут ОТКРЫТЫЕ ``FlushFileHandler`` — то есть
    дескрипторы.

    Чужие хендлеры (``log_handlers`` вызывающего) не закрываются: их жизнь
    принадлежит тому, кто их передал.
    """
    if logger is None:
        return
    name = getattr(logger, "name", None)
    if not name or not str(name).startswith("agent_run_"):
        return                       # не наш логгер — не наше дело
    foreign = _FOREIGN_HANDLERS.pop(name, set())
    for handler in list(getattr(logger, "handlers", ())):
        logger.removeHandler(handler)
        if id(handler) in foreign:
            continue
        try:
            handler.close()
        except Exception:
            pass
    logging.Logger.manager.loggerDict.pop(name, None)


__all__ = [
    "FlushFileHandler",
    "JsonlFormatter",
    "RedactingTextFormatter",
    "RunContextFilter",
    "build_run_log_paths",
    "create_run_logger",
    "generate_run_id",
    "human_console_enabled",
    "release_run_logger",
    "resolve_log_level",
]
