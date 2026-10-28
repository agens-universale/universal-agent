"""Перевод meta develop ↔ части записи — только внутри файлового хранилища.

Ядро, фронтенды и сервер читают типизированную запись треда
(``ThreadRecord``: ``info``, ``state``, ``usage``, ``runtime``,
``frontend``, ``extra``). meta-словарь develop — формат файлов на диске, его
знает ``FileThreadStore``; любой другой читатель meta означал бы второй
источник правды о том, где лежит поле (так жили ``Threads.legacy_meta`` и
``FileTaskStore.get_task_meta``). ``gc_testkit`` — тестовый инструментарий,
ему переводить можно: тесты совместимости сверяют формат на диске.
"""

import ast

from gc_testkit.paths import PACKAGE_SRC_DIRS, REPO

TRANSLATORS = {"meta_from_record", "meta_from_parts", "parts_from_meta"}
ALLOWED = {
    "packages/core/src/gc_core/threads/meta_map.py",
    "packages/core/src/gc_core/threads/file_store.py",
}


def _translator_imports(path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "gc_core.threads.meta_map"
        ):
            names = {alias.name for alias in node.names} & TRANSLATORS
            found += [f"{node.lineno}: {name}" for name in sorted(names)]
        if isinstance(node, ast.Attribute) and node.attr in TRANSLATORS:
            found.append(f"{node.lineno}: .{node.attr}")
    return found


def test_meta_translation_lives_only_in_the_file_store():
    offenders, scanned = [], 0
    for src in PACKAGE_SRC_DIRS:
        if src.parent.name == "testkit":
            continue
        for path in sorted(src.rglob("*.py")):
            scanned += 1
            rel = path.relative_to(REPO).as_posix()
            hits = _translator_imports(path)
            if hits and rel not in ALLOWED:
                offenders += [f"{rel}:{hit}" for hit in hits]
    assert scanned > 100, f"подозрительно мало исходников: {scanned}"
    assert offenders == [], (
        "meta develop читают мимо файлового хранилища — читайте части "
        "записи (ThreadRecord):\n  " + "\n  ".join(offenders)
    )


def test_meta_readers_are_gone():
    from gc_core.threads.facade import Threads
    from gc_tui.services.stores.file_store import FileTaskStore

    assert not hasattr(Threads, "legacy_meta")
    assert not hasattr(FileTaskStore, "get_task_meta")
