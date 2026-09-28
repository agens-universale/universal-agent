"""Логгеры прогонов не должны копиться вместе с открытыми дескрипторами.

Имя логгера уникально на ран (``agent_run_<kind>_<run_id>``), а ``logging``
держит созданные объекты в ``loggerDict`` до конца процесса и сам их не
отдаёт. Один процесс — один-два логгера, и это было незаметно. Но ветка
объявляет сценарий «много ``AsyncAgent`` в одном процессе», где свой логгер
заводит каждый агент и каждый субагент: рост линейный, а вместе с логгерами
живут ОТКРЫТЫЕ ``FlushFileHandler``, то есть файловые дескрипторы.
"""

import logging

from gc_core.utils.logging_module import create_run_logger, release_run_logger


def _cfg(tmp_path):
    return {
        "save_logs": True,
        "console_trace": False,
        "logs_dump_dir": str(tmp_path),
    }


def test_release_removes_the_logger_from_the_registry(tmp_path):
    logger, _path, _trace = create_run_logger(_cfg(tmp_path), run_id="r1")
    name = logger.name
    assert name in logging.Logger.manager.loggerDict

    release_run_logger(logger)

    assert name not in logging.Logger.manager.loggerDict, (
        "логгер прогона остался в реестре logging до конца процесса"
    )


def test_release_closes_the_file_handlers(tmp_path):
    logger, _path, _trace = create_run_logger(_cfg(tmp_path), run_id="r2")
    files = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
    assert files, "проверка пустая: файловых хендлеров не было"

    release_run_logger(logger)

    assert all(h.stream is None or h.stream.closed for h in files), (
        "файловый хендлер остался открытым — дескриптор течёт"
    )


def test_foreign_handlers_are_left_alone(tmp_path):
    """Хендлеры вызывающего не наши: их жизнью распоряжается он.

    Наблюдать надо САМ вызов ``close()``, а не состояние потока:
    ``StreamHandler.close()`` поток не закрывает (в отличие от
    ``FileHandler``), и проверка «поток ещё открыт» была бы зелёной даже
    тогда, когда чужой хендлер закрыли.
    """
    closed: list = []

    class _Mine(logging.Handler):
        def emit(self, record):
            pass

        def close(self):
            closed.append(True)
            super().close()

    mine = _Mine()
    logger, _p, _t = create_run_logger(
        _cfg(tmp_path), run_id="r3", log_handlers=[mine]
    )
    assert mine in logger.handlers, (
        "проверка пустая: чужой хендлер не прицепился"
    )

    release_run_logger(logger)

    assert not closed, (
        "закрыли чужой хендлер — его жизнь принадлежит вызывающему"
    )


def test_release_ignores_a_foreign_logger():
    """Чужой логгер по имени не наш — трогать его нельзя."""
    other = logging.getLogger("someone.elses.logger")

    release_run_logger(other)

    assert "someone.elses.logger" in logging.Logger.manager.loggerDict


def test_many_runs_do_not_accumulate(tmp_path):
    """Свойство целиком: сто прогонов не оставляют ста логгеров."""
    before = len(logging.Logger.manager.loggerDict)

    for i in range(100):
        logger, _p, _t = create_run_logger(_cfg(tmp_path), run_id=f"many-{i}")
        release_run_logger(logger)

    grew = len(logging.Logger.manager.loggerDict) - before
    assert grew <= 2, f"после 100 прогонов реестр вырос на {grew} логгеров"
