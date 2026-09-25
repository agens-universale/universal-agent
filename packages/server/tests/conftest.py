"""Swarm — только Linux (docs/swarm.md): на Windows его тесты не собираются в прогон.

Один механизм на все `test_swarm_*`, а не маркер в каждом файле: точечные
маркеры устаревают — новый тест снова зовёт `os.killpg`, `signal.SIGKILL` или
берёт `/usr/bin/true` обёрткой, и Windows-прогон краснеет не по делу.

Порог — именно Windows, а не «не Linux»: на macOS логика swarm (раннер,
хранилище, чистка) исполняется и проверяется; то, что там требует Linux, свои
модули пропускают сами (`test_swarm_endpoints`).
"""
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items):
    if sys.platform != "win32":
        return
    skip = pytest.mark.skip(reason="swarm только для Linux: на Windows нет killpg, SIGKILL и bwrap")
    for item in items:
        # Хук видит все элементы сессии, а не только этого каталога. Путь
        # разрешаем с обеих сторон: на subst-диске или через junction иначе
        # сравнение молча ложно, и пропуск не срабатывает.
        if item.path.name.startswith("test_swarm_") and item.path.resolve().parent == _HERE:
            item.add_marker(skip)
