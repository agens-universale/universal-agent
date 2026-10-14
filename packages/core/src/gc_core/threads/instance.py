"""Имя инстанса агента — владелец аренды треда.

По умолчанию — имя машины: процессы одной машины (CLI, TUI, сервер) не
блокируют друг друга, как и раньше, а между машинами работает фенсинг.
``GC_INSTANCE_ID`` задаёт имя явно (несколько инстансов на одной машине,
контейнеры с одинаковым hostname).
"""

from __future__ import annotations

import os
import socket

INSTANCE_ENV = "GC_INSTANCE_ID"


def instance_id() -> str:
    explicit = os.environ.get(INSTANCE_ENV, "").strip()
    return explicit or socket.gethostname() or "gc-instance"
