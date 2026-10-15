"""Хранилище тредов в файле SQLite: ``SQLiteThreadStore``.

Драйвер — ``sqlite3`` из стандартной библиотеки, extra не нужен.
"""

from gc_stores.sqlite.store import SQLiteThreadStore

__all__ = ["SQLiteThreadStore"]
