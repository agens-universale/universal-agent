"""Каталог логов недоступен на запись (``save_logs: true``): отказ с понятным
сообщением — какой каталог и что сделать, — а не голый ``OSError`` errno.

Каталог «под файлом»: ``makedirs`` падает на любой ОС, права не нужны.
"""

import pytest
from gc_core.utils.logging_module import (
    RunLogDirUnavailable,
    create_run_logger,
)
from gc_testkit.settings import make_settings


@pytest.mark.parametrize("fmt", ["text", "jsonl"])
def test_unwritable_log_dir_is_named_with_the_fix(tmp_path, fmt):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    logs = blocker / "logs"
    settings = make_settings(
        {
            "logging.save_logs": True,
            "logging.format": fmt,
            "paths.logs_dump_dir": str(logs),
        }
    )
    with pytest.raises(RunLogDirUnavailable) as caught:
        create_run_logger(settings, run_id=f"r-{fmt}")
    error = caught.value
    assert isinstance(error, OSError)  # прежние `except OSError` ловят
    assert str(logs) in str(error)
    assert "logging.save_logs" in str(error)
    assert "paths.logs_dump_dir" in str(error)
    assert isinstance(error.__cause__, OSError)


def test_logs_off_need_no_directory(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    settings = make_settings(
        {
            "logging.save_logs": False,
            "paths.logs_dump_dir": str(blocker / "logs"),
        }
    )
    logger, path, _summary = create_run_logger(settings, run_id="r-off")
    assert logger is not None and not path.startswith(str(blocker))
