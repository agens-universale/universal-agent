"""Хранилища на MySQL 8: ``MySQLThreadStore``.

Драйвер — asyncmy (extra ``gc-stores[mysql]``); импорт модуля его не
требует, отказ без драйвера — при первом подключении.
"""

from gc_stores.mysql.store import MySQLThreadStore

__all__ = ["MySQLThreadStore"]
