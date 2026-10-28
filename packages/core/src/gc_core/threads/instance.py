"""Имя инстанса агента и владелец аренды треда.

Инстанс — по умолчанию имя машины; ``GC_INSTANCE_ID`` задаёт его явно
(несколько инстансов на одной машине, контейнеры с одинаковым hostname).
Владелец аренды — сессия (``session_owner``): два хода одного треда не
«свои» друг другу, даже в одном процессе.
"""

from __future__ import annotations

import os
import socket
import uuid

INSTANCE_ENV = "GC_INSTANCE_ID"


def instance_id() -> str:
    explicit = os.environ.get(INSTANCE_ENV, "").strip()
    return explicit or socket.gethostname() or "gc-instance"


def session_owner() -> str:
    """Владелец аренды одной сессии треда: инстанс, процесс и случайный
    хвост — по нему в отказе видно, кто держит тред."""
    return f"{instance_id()}/{os.getpid()}/{uuid.uuid4().hex[:8]}"
