"""Снятый прогон отдаётся отдельным статусом, а не как завершённый.

Флаг ``cancelled`` на записи прогона выставлялся, но наружу не отдавался:
``get_run_status`` его не читал, и клиент видел ``completed`` с пустым
результатом. Отличить остановку uvicorn от отказа агента было нельзя.
"""

import inspect

from gc_server import server


def test_cancelled_run_has_its_own_status():
    src = inspect.getsource(server)

    assert '"status": "cancelled"' in src, "статус снятого прогона снова не отдаётся наружу"


def test_cancelled_is_checked_before_error():
    """Порядок важен: у снятого прогона ``error`` пуст, но статус не completed."""
    src = inspect.getsource(server)

    assert src.index("run.cancelled") < src.index("run.error is not None"), (
        "проверка отмены должна идти ДО проверки ошибки"
    )
