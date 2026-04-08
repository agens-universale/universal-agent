import logging
import os
import time
import uuid
from typing import Tuple, Dict, Any, Optional


class FlushFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def generate_run_id() -> str:
    """Generate unique run identifier for log isolation."""
    return f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


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
) -> Tuple[str, str]:
    """
    Сконструировать пути к логам одного запуска:
    - общий лог агента;
    - отдельный trace-лог суммаризации.
    """
    base_dir = config_dict.get("logs_dump_dir") or os.getcwd()
    suffix = run_id or generate_run_id()
    kind = _normalize_agent_kind(agent_kind)
    logging_path = os.path.join(base_dir, f"agent_{kind}_{suffix}.log")
    summary_trace_path = os.path.join(base_dir, f"summary_trace_{kind}_{suffix}.jsonl")
    return logging_path, summary_trace_path


def create_run_logger(
    config_dict: Dict[str, Any],
    run_id: Optional[str] = None,
    agent_kind: Optional[str] = None,
) -> Tuple[logging.Logger, str, str]:
    """
    Создать логгер и путь к файлу логов для конкретного запуска.

    - Если config_dict['save_logs'] == True — логи пишутся в отдельный файл.
      Имя файла включает run_id (если задан) или текущий timestamp.
    - Если save_logs == False — возвращается логгер с NullHandler и
      строки-плейсхолдеры о том, что логи отключены.
    """
    effective_run_id = run_id or generate_run_id()
    kind = _normalize_agent_kind(agent_kind)
    logger_name = f"agent_run_{kind}_{effective_run_id}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()

    # Файловый хендлер — только если явно включены логи.
    if config_dict.get("save_logs", False):
        logging_path, summary_trace_path = build_run_log_paths(
            config_dict, run_id=effective_run_id, agent_kind=kind
        )

        handler = FlushFileHandler(logging_path, encoding="utf-8", mode="a")
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    else:
        logging_path = (
            "Null. Логи отключены. Включите логи в файле config.yml если они необходимы"
        )
        summary_trace_path = ""

    # Всегда дублируем ошибки в stderr, чтобы исключения были видны даже при
    # отключённом файловом логировании.
    err_handler = logging.StreamHandler()
    err_handler.setLevel(logging.ERROR)
    err_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    err_handler.setFormatter(err_formatter)
    logger.addHandler(err_handler)

    return logger, logging_path, summary_trace_path


__all__ = [
    "create_run_logger",
    "FlushFileHandler",
    "build_run_log_paths",
    "generate_run_id",
]