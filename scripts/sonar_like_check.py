#!/usr/bin/env python3
"""Локальный аналог двух правил Sonar, у которых нет эквивалента в ruff.

* S1192 «Define a constant instead of duplicating this literal»: строковый
  литерал длиной >= MIN_LEN, встречающийся в одном файле >= THRESHOLD раз.
  Докстроки, куски f-строк и литералы вида идентификатора (``[\\w-]*``)
  не считаются, как и у Sonar.
* S6035/S6019 подсказки по регуляркам: ``[A-Za-z0-9_]`` вместо ``\\w``,
  ``[0-9]`` вместо ``\\d``, ленивый квантификатор ``.*?x`` вместо ``[^x]*``.
  Проверяются только строковые литералы, переданные в ``re.<func>(...)``.

Запуск: ``python scripts/sonar_like_check.py <пути...>``. Ненулевой код выхода
при находках — для CI и pre-commit. Порог сложности проверяет complexipy,
остальные правила — ruff (см. [tool.ruff] в pyproject.toml).
"""
from __future__ import annotations

import ast
import re
import sys
from collections import Counter
from pathlib import Path

THRESHOLD = 3
MIN_LEN = 5
# Как у Sonar для Python: литералы из одних букв/цифр/подчёркиваний/дефисов
# (ключи словарей, идентификаторы, «utf-8») дублями не считаются.
_IDENTIFIER_LIKE = re.compile(r"[\w-]*")

_WORD_CLASS = re.compile(r"\[(?=[^\]]*A-Z)(?=[^\]]*a-z)(?=[^\]]*0-9)(?=[^\]]*_)(?:A-Z|a-z|0-9|_){4}\]")
_DIGIT_CLASS = re.compile(r"\[0-9\]")
_RELUCTANT = re.compile(r"\.[*+]\?(\\?[^\\()\[\]{}|^$*+?.])(?![*+?{])")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _fstring_parts(tree: ast.AST) -> set[int]:
    return {id(v) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for v in node.values}


def _annotation_strings(tree: ast.AST) -> set[int]:
    """Строковые forward-ref аннотации (``-> "Foo | None"``) — не литералы данных."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        anns = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            anns.append(node.returns)
            anns += [a.annotation for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs]
            anns += [a.annotation for a in (node.args.vararg, node.args.kwarg) if a is not None]
        elif isinstance(node, ast.AnnAssign):
            anns.append(node.annotation)
        for ann in anns:
            if ann is not None:
                ids.update(id(n) for n in ast.walk(ann) if isinstance(n, ast.Constant))
    return ids


def check_duplicate_literals(path: Path, tree: ast.AST) -> list[str]:
    skip = _docstring_nodes(tree) | _fstring_parts(tree) | _annotation_strings(tree)
    seen: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            if len(node.value) >= MIN_LEN and not _IDENTIFIER_LIKE.fullmatch(node.value):
                seen.setdefault(node.value, []).append(node.lineno)
    out = []
    for value, lines in seen.items():
        if len(lines) >= THRESHOLD:
            out.append(
                f"{path}:{lines[0]}: S1192 literal {value!r} duplicated {len(lines)} times "
                f"(lines {', '.join(map(str, lines))}); define a constant"
            )
    return out


def _regex_call_patterns(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        is_re = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "re"
        )
        if is_re and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            yield node.args[0].lineno, node.args[0].value


def check_regex_hints(path: Path, tree: ast.AST) -> list[str]:
    out = []
    for lineno, pattern in _regex_call_patterns(tree):
        if _WORD_CLASS.search(pattern):
            out.append(f"{path}:{lineno}: S6035 use '\\w' instead of '[A-Za-z0-9_]' in {pattern!r}")
        if _DIGIT_CLASS.search(pattern):
            out.append(f"{path}:{lineno}: S6035 use '\\d' instead of '[0-9]' in {pattern!r}")
        m = _RELUCTANT.search(pattern)
        if m:
            ch = m.group(1).lstrip("\\")
            out.append(
                f"{path}:{lineno}: S6019 replace reluctant quantifier before {ch!r} with '[^{ch}]*' in {pattern!r}"
            )
    return out


def iter_py_files(paths: list[str]):
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            yield from sorted(x for x in p.rglob("*.py") if ".venv" not in x.parts and "__pycache__" not in x.parts)
        elif p.suffix == ".py":
            yield p


def main(argv: list[str]) -> int:
    findings: list[str] = []
    for path in iter_py_files(argv or ["."]):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            findings.append(f"{path}: cannot parse: {exc}")
            continue
        findings += check_duplicate_literals(path, tree)
        findings += check_regex_hints(path, tree)
    for line in findings:
        print(line)
    print(f"{len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
