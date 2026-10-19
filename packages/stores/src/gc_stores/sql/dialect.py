"""Диалект базы для SQL-движка: то, чем базы различаются.

Тексты запросов движок собирает сам (``SqlQueries``) — так, что каждый
параметр встречается один раз и по порядку: тогда позиционные плейсхолдеры
``%s`` (MySQL) и ``?`` (SQLite) работают так же, как ``$n`` (Postgres).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from gc_stores.sql.database import Conn


class Dialect(ABC):
    """Различия баз. ``prefix`` — префикс имён таблиц (схема Postgres)."""

    name: str = ""
    #: Коллация для сравнения ``system``/``thread_id`` по кодам символов
    #: (порядок списка тредов — как ``rules.page_of``).
    collate: str = ""
    #: Блокировка строки треда в транзакции записи.
    lock_suffix: str = " FOR UPDATE"

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix

    def table(self, name: str) -> str:
        return self.prefix + name

    def ident(self, name: str) -> str:
        """Имя колонки в кавычках диалекта (``usage`` у MySQL — слово)."""
        return f'"{name}"'

    @abstractmethod
    def params(self) -> Callable[[], str]:
        """Плейсхолдеры одного запроса по порядку: ``$1, $2…`` или ``?``."""

    def cast_text(self, placeholder: str) -> str:
        """Параметр-строка в списке SELECT (Postgres требует тип)."""
        return placeholder

    @abstractmethod
    def time_in(self, moment: datetime) -> Any:
        """Время в параметр запроса."""

    @abstractmethod
    def time_out(self, value: Any) -> datetime:
        """Время из строки результата — aware, UTC."""

    @abstractmethod
    async def now(self, conn: Conn) -> datetime:
        """Часы хранилища (базы или машины) в момент вызова."""

    @abstractmethod
    def insert_ignore(self, table: str, columns: Sequence[str]) -> str:
        """``INSERT`` строки; есть строка с тем же ключом — ничего (затронуто
        0 строк)."""

    @abstractmethod
    def upsert(
        self, table: str, columns: Sequence[str], keys: Sequence[str]
    ) -> str:
        """``INSERT``; ключ занят — обновить остальные колонки."""

    def check_ids(self, *values: str) -> None:
        """Ключи (id треда, алиас, op_id) влезают в колонки базы; иначе —
        ``ValueError``, а не обрезка."""
        del values


def numbered(template: str) -> Callable[[], Callable[[], str]]:
    """Генератор плейсхолдеров с номером: ``$1``, ``$2``…"""

    def params() -> Callable[[], str]:
        counter = iter(range(1, 10_000))
        return lambda: template.format(next(counter))

    return params
