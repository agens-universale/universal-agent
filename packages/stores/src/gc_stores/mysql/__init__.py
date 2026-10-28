"""Хранилища на MySQL 8: ``MySQLThreadStore``, ``MySQLAgentStore``.

Драйвер — asyncmy (extra ``gc-stores[mysql]``); импорт модуля его не
требует, отказ без драйвера — при первом подключении.
"""

from gc_stores.mysql.store import (
    MySQLAgentStore,
    MySQLThreadStore,
    open_stores,
)

__all__ = ["MySQLAgentStore", "MySQLThreadStore", "open_stores"]
