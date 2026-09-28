"""Merge-blocking invariants for working_memory cutover (spec steps 0 and 6)."""

from pathlib import Path

from gc_testkit.paths import PACKAGE_SRC_DIRS, REPO

# Единственное допустимое вхождение memory_files — fail-fast reject старого поля
# при загрузке. Сам тул MemoryFiles удалён вместе с tools/deprecated.
# Пути даны от корня репозитория: после разбиения на пакеты исходники лежат в
# packages/<дистрибутив>/src, а не в едином src/.
_ALLOWED_MEMORY_FILES_RELPATHS = frozenset(
    {
        "packages/core/src/gc_core/persistence/schema.py",
    }
)


def _relpath(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def test_memory_files_only_in_allowed_locations() -> None:
    """rg "memory_files" по исходникам — только schema reject."""
    unexpected: list[str] = []
    for src_dir in PACKAGE_SRC_DIRS:
        for path in sorted(src_dir.rglob("*.py")):
            if "memory_files" not in path.read_text(encoding="utf-8"):
                continue
            rel = _relpath(path)
            if rel not in _ALLOWED_MEMORY_FILES_RELPATHS:
                unexpected.append(rel)
    assert unexpected == []


def test_scan_actually_covers_sources() -> None:
    """Страховка от холостого прогона.

    До разбиения на пакеты тест сканировал `<repo>/src`; каталог исчез, rglob
    стал возвращать пустоту, и проверка молча проходила на любом коде. Теперь
    сначала убеждаемся, что сканировать вообще есть что.
    """
    assert PACKAGE_SRC_DIRS, "не найдено ни одного каталога packages/*/src"
    total = sum(1 for d in PACKAGE_SRC_DIRS for _ in d.rglob("*.py"))
    assert total > 100, f"подозрительно мало исходников в обходе: {total}"
