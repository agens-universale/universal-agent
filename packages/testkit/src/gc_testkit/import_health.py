#!/usr/bin/env python3
"""Сетка против двух классов поломок импорта, переживших кодмод.

Оба класса вылезли при вливании develop 19.08.2026 и оба были найдены не
тестами, а разбором 138 падений постфактум — значит порог обнаружения низкий и
нужна проверка, которая гоняется сама.

**Класс 1: `import a.b` вместо `from a import b`.** Кодмод переписывал плоский
`import arg_parser` в `import gc_cli.arg_parser`. Но `import a.b` связывает имя
`a`, а не `b`: после такой правки 24 обращения к голому `arg_parser.` в
`cli.py` стали NameError. Живой запуск падал, а набор — нет, потому что тесты
на этот путь патчили модуль целиком.

**Класс 2: имя, которого нет в модуле.** develop приватизировал
`default_config_path` → `_default_config_path`, а разрешение конфликта
сохранило наш путь модуля со старым именем. Ловится только тем, что кто-то
дойдёт до этой строки.

Обе проверки — чисто по AST, без импорта проверяемых модулей: импорт ради
проверки дорог и небезопасен (побочные эффекты, разбор argv, сетевые
клиенты). Ровно по той же причине, по которой `module_literals.verify()` не
проверяет атрибуты.

ГРАНИЦА ПРИМЕНИМОСТИ. Класс 1 проверяется точно. Класс 2 — по объявлениям
модульного уровня, поэтому модуль, который лепит имена динамически
(`globals().update`, `setattr(sys.modules[...])`), даст ложное срабатывание;
такие модули пропускаются по признаку `__getattr__` или `import *`. Имя,
приходящее из ветки `try/except ImportError`, видно — оба варианта попадают в
объявления.

Использование:
    uv run python -m gc_testkit.import_health           # отчёт
    uv run python -m gc_testkit.import_health --json    # машинный вывод

Код возврата: 0 — чисто, 1 — есть находки.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

from gc_testkit.paths import REPO, PACKAGE_SRC_DIRS

#: Каталоги, где ищем питон. Тот же принцип, что в module_literals: список
#: исключений, а не включений, — новый каталог с кодом подхватывается сам.
SKIP_DIRS = {
    ".git", ".venv", ".idea", ".vscode", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "__pycache__", "dist", "build", "node_modules", "docs",
}


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    kind: str      # dotted_bind | missing_name
    detail: str


def _our_prefixes() -> set[str]:
    """Верхнеуровневые имена наших дистрибутивов: `gc_core`, `gc_tui`, ..."""
    names: set[str] = set()
    for src in PACKAGE_SRC_DIRS:
        for child in src.iterdir():
            if child.is_dir() and (child / "__init__.py").exists():
                names.add(child.name)
    return names


def _python_files() -> list[Path]:
    out: list[Path] = []
    for base in (REPO / "packages", REPO / "tests", REPO / "integrations"):
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if SKIP_DIRS & set(p.parts):
                continue
            out.append(p)
    return sorted(out)


def check_dotted_binds(path: Path, tree: ast.AST) -> list[Finding]:
    """`import a.b` связывает `a`; голое `b.` ниже по файлу — NameError."""
    bound: set[str] = set()
    dotted: dict[str, tuple[str, int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound.add(alias.asname)
                elif "." in alias.name:
                    dotted.setdefault(alias.name.split(".")[-1], (alias.name, node.lineno))
                else:
                    bound.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.arg,)):
            bound.add(node.arg)

    found: list[Finding] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
            continue
        if node.id in bound or node.id not in dotted:
            continue
        full, line = dotted.pop(node.id)
        found.append(Finding(
            str(path.relative_to(REPO)), line, "dotted_bind",
            f"`import {full}` связывает `{full.split('.')[0]}`, "
            f"а строка {node.lineno} зовёт голое `{node.id}` — нужно "
            f"`from {full.rsplit('.', 1)[0]} import {node.id}`",
        ))
    return found


def _target_names(target: ast.AST) -> set[str]:
    """Имена в цели присваивания, включая распаковку `a, b = ...`."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return {n for t in target.elts for n in _target_names(t)}
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return set()


def _module_bindings(mod_name: str) -> set[str] | None:
    """Имена модульного уровня по AST. None — модуль непрозрачен, не судим."""
    try:
        spec = importlib.util.find_spec(mod_name)
    except Exception:
        return None
    if spec is None or not spec.origin or not spec.origin.endswith(".py"):
        return None
    try:
        tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None

    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == "__getattr__":
                return None          # модуль сам решает, что у него есть
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    return None      # реэкспорт россыпью, состав неизвестен
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                names |= _target_names(t)
        elif isinstance(node, ast.AnnAssign):
            names |= _target_names(node.target)
        elif isinstance(node, (ast.If, ast.Try)):
            # try/except ImportError и `if TYPE_CHECKING` — объявления внутри
            # всё равно попадают в модуль, обходим их тела на один уровень.
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Import):
                    for alias in sub.names:
                        names.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(sub, ast.ImportFrom):
                    for alias in sub.names:
                        if alias.name != "*":
                            names.add(alias.asname or alias.name)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        names |= _target_names(t)
    return names


def check_missing_names(path: Path, tree: ast.AST, ours: set[str]) -> list[Finding]:
    """`from gc_x.y import name` — существует ли `name` в модуле."""
    found: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        if node.module.split(".")[0] not in ours:
            continue
        bindings = _module_bindings(node.module)
        if bindings is None:
            continue
        for alias in node.names:
            if alias.name == "*" or alias.name in bindings:
                continue
            try:                      # имя может быть подмодулем пакета
                if importlib.util.find_spec(f"{node.module}.{alias.name}") is not None:
                    continue
            except Exception:
                pass
            found.append(Finding(
                str(path.relative_to(REPO)), node.lineno, "missing_name",
                f"в `{node.module}` нет имени `{alias.name}`",
            ))
    return found


def scan() -> list[Finding]:
    """Общая точка для CLI и теста."""
    for src in PACKAGE_SRC_DIRS:
        if (entry := str(src)) not in sys.path:
            sys.path.insert(0, entry)

    ours = _our_prefixes()
    findings: list[Finding] = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        findings += check_dotted_binds(path, tree)
        findings += check_missing_names(path, tree, ours)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args()

    findings = scan()
    if args.json:
        print(json.dumps([asdict(f) for f in findings], ensure_ascii=False, indent=1))
        return 1 if findings else 0

    if not findings:
        print("импорты здоровы: битых имён и потерянных связываний нет")
        return 0
    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
    print(f"НАХОДОК: {len(findings)}")
    for kind, n in sorted(by_kind.items()):
        print(f"  {kind:14s} {n:3d}")
    print()
    for f in findings:
        print(f"  {f.file}:{f.line}  {f.detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
