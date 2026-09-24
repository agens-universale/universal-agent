"""Страж: модуль не ссылается на имя, которое нигде не связывает.

Ловится класс ошибок «переписали блок импортов, а обращения не переименовали»:
`from gc_tui.palette import INK as _INK` свернули в `INK`, а в теле остались
`_INK` — модуль импортируется молча и падает `NameError` только в рантайме, на
конкретном экране. Тестами такое ловится случайно, а пользователем — сразу.

Проверка статическая и намеренно грубая: собираем ВСЕ связывания модуля
(импорты, def/class, присваивания, аргументы, except-as, global) и сверяем с
именами, читаемыми на любом уровне. Грубость в безопасную сторону: лишнее
связывание молчит, а вот прочитать несвязанное имя невозможно ни при каком
порядке исполнения.
"""
from __future__ import annotations

import ast
import builtins

from gc_testkit.paths import REPO

_ALWAYS_BOUND = set(dir(builtins)) | {
    "__file__",
    "__name__",
    "__doc__",
    "__package__",
    "__spec__",
    "__loader__",
    "__builtins__",
    "__debug__",
}


def _bound_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)          # `case X as y` / `case [*rest]`
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)          # `case {..., **rest}`
    return names


def _read_names(tree: ast.AST) -> set[str]:
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def test_no_source_module_reads_an_unbound_name():
    offenders: dict[str, list[str]] = {}
    for path in sorted((REPO / "packages").rglob("*.py")):
        parts = path.parts
        if "__pycache__" in parts or "external" in parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # `from x import *` приносит имена, которых в дереве не видно — такой
        # модуль этой проверке не по зубам, и врать о нём мы не станем.
        if any(
            isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
            for n in ast.walk(tree)
        ):
            continue
        missing = sorted(_read_names(tree) - _bound_names(tree) - _ALWAYS_BOUND)
        if missing:
            offenders[str(path.relative_to(REPO))] = missing
    assert offenders == {}


def test_match_captures_and_nonlocal_are_recognised_as_bindings():
    """Страж обязан молчать на КОРРЕКТНОМ коде — иначе он мина, а не проверка.

    Захваты structural pattern matching и `nonlocal` связывают имена, которых
    нет ни в присваиваниях, ни в аргументах: без их разбора первый же `match`
    под packages/ уронил бы общерепозиторный страж на работающем коде.
    """
    src = """
def handle(event, acc):
    def bump():
        nonlocal acc
        acc = acc + 1
    match event:
        case {"kind": kind, **rest}:
            return kind, rest, bump
        case [first, *tail]:
            return first, tail
        case object() as other:
            return other
"""
    tree = ast.parse(src)
    assert _read_names(tree) - _bound_names(tree) - _ALWAYS_BOUND == set()
