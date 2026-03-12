import logging
import os
import time
from typing import Tuple, Dict, Any, Optional


class FlushFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def create_run_logger(
    config_dict: Dict[str, Any],
    run_id: Optional[str] = None,
) -> Tuple[logging.Logger, str]:
    """
    Создать логгер и путь к файлу логов для конкретного запуска.

    - Если config_dict['save_logs'] == True — логи пишутся в отдельный файл.
      Имя файла включает run_id (если задан) или текущий timestamp.
    - Если save_logs == False — возвращается логгер с NullHandler и
      строка с сообщением о том, что логи отключены.
    """
    logger_name = f"agent_run_{run_id}" if run_id else "agent_run"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()

    if config_dict.get("save_logs", False):
        base_dir = config_dict.get("logs_dump_dir") or os.getcwd()
        suffix = run_id or str(int(time.time()))
        logging_path = os.path.join(base_dir, f"agent_{suffix}.log")

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
        logger.addHandler(logging.NullHandler())

    return logger, logging_path


__all__ = ["create_run_logger", "FlushFileHandler"]