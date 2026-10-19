"""Миграции схемы хранилищ на SQL: пронумерованные файлы и версия в базе.

Миграции базы лежат в пакете диалекта: ``<пакет>/migrations/NNNN_<имя>.sql``
— только вперёд, номера подряд с 1. Применённая версия — одна строка
таблицы ``gc_schema_version``. Хранилище сверяет её перед первой операцией:
ниже, чем у кода, — отказ «выполните ``gc-stores migrate``», выше — «база от
более новой gc-stores». ``ensure_schema()`` (и ``gc-stores migrate``)
применяет недостающие под блокировкой: два инстанса, стартующие
одновременно, не применяют одну миграцию дважды.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gc_stores.sql.database import Conn, Database
    from gc_stores.sql.dialect import Dialect

VERSION_TABLE = "gc_schema_version"
_NAME = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")


class SchemaMismatch(RuntimeError):
    """Версия схемы базы не та, что у кода."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    def statements(self) -> list[str]:
        """Операторы файла: каждый кончается «;» в конце строки (в файлах
        миграций нет «;» внутри литералов). Строки-комментарии — с ним."""
        found, chunk = [], []
        for line in self.sql.splitlines():
            chunk.append(line)
            if line.rstrip().endswith(";"):
                statement = "\n".join(chunk).strip()
                chunk = []
                if _meaningful(statement):
                    found.append(statement.rstrip().rstrip(";"))
        rest = "\n".join(chunk).strip()
        if _meaningful(rest):
            raise ValueError(f"миграция {self.name}: оператор без «;»")
        return found


def _meaningful(statement: str) -> bool:
    return any(
        line.strip() and not line.strip().startswith("--")
        for line in statement.splitlines()
    )


def check_sequence(versions: list[int]) -> None:
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError(
            f"номера миграций — подряд с 1, а не {versions}: разрыв или повтор"
        )


def load(package: str) -> list[Migration]:
    """Миграции пакета диалекта по возрастанию номера."""
    found = []
    for entry in resources.files(package).joinpath("migrations").iterdir():
        match = _NAME.fullmatch(entry.name)
        if match is None:
            continue
        found.append(
            Migration(
                int(match.group(1)),
                match.group(2),
                entry.read_text(encoding="utf-8"),
            )
        )
    found.sort(key=lambda m: m.version)
    check_sequence([m.version for m in found])
    return found


def latest(found: list[Migration]) -> int:
    return found[-1].version if found else 0


async def applied_version(conn: Conn, dialect: Dialect) -> int:
    """Версия схемы базы; таблицы версии нет — 0."""
    if not await dialect.table_exists(conn, VERSION_TABLE):
        return 0
    value = await conn.fetchval(
        f"SELECT version FROM {dialect.table(VERSION_TABLE)}"
    )
    return int(value or 0)


def mismatch(current: int, code: int) -> SchemaMismatch | None:
    if current > code:
        return SchemaMismatch(
            f"схема базы версии {current}, а этот код знает {code}: база от "
            "более новой gc-stores — обновите gc-stores"
        )
    if current == 0:
        return SchemaMismatch(
            "в базе нет схемы хранилищ: выполните await store.ensure_schema() "
            "или gc-stores migrate"
        )
    if current < code:
        return SchemaMismatch(
            f"схема базы версии {current}, код — {code}: выполните "
            "gc-stores migrate (или await store.ensure_schema())"
        )
    return None


async def check(
    db: Database, dialect: Dialect, found: list[Migration]
) -> None:
    """Сверить версию схемы базы с кодом; расхождение — ``SchemaMismatch``."""
    async with db.connection() as conn:
        current = await applied_version(conn, dialect)
    error = mismatch(current, latest(found))
    if error is not None:
        raise error


async def _apply(
    conn: Conn, dialect: Dialect, found: list[Migration], current: int
) -> None:
    table = dialect.table(VERSION_TABLE)
    await conn.execute(
        f"CREATE TABLE IF NOT EXISTS {table} (version integer NOT NULL)"
    )
    for migration in found[current:]:
        for statement in migration.statements():
            await conn.execute(statement)
        await conn.execute(f"DELETE FROM {table}")
        p = dialect.params()
        await conn.execute(
            f"INSERT INTO {table} (version) VALUES ({p()})",
            migration.version,
        )


async def ensure(
    db: Database, dialect: Dialect, found: list[Migration]
) -> int:
    """Применить недостающие миграции; вернуть версию схемы.

    Всё на месте — DDL не выполняется (пользователь приложения без права
    ``CREATE`` проходит). Иначе — под блокировкой диалекта, с перечиткой
    версии после неё."""
    code = latest(found)
    async with db.connection() as conn:
        current = await applied_version(conn, dialect)
    if current == code:
        return current
    if current > code:
        raise mismatch(current, code)
    async with dialect.migration_session(db) as conn:
        current = await applied_version(conn, dialect)
        if current > code:
            raise mismatch(current, code)
        if current < code:
            await _apply(conn, dialect, found, current)
    return code
