#!/usr/bin/env python3
"""Кодмод: плоские импорты -> gc_core / gc_tui / gc_server.

Работает по AST (находит узлы Import/ImportFrom и их точные позиции), а не
регуляркой по всему тексту: иначе задевает строки, комментарии и совпадающие
идентификаторы (`config = ...`, `tools = [...]`).

Правила:
  from X import a, b        ->  from PKG.X import a, b
  from X.Y import a         ->  from PKG.X.Y import a
  import X                  ->  from PKG import X          (сохраняет привязку X)
  import X.Y as Z           ->  from PKG.X import Y as Z    (привязка на ПОДМОДУЛЬ)
  import X as Z             ->  from PKG import X as Z
Относительные импорты (level > 0) не трогаем.

Использование:
  codemod.py --map split_map.json --root <dir> [--apply]
Без --apply печатает план и ничего не меняет.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from pathlib import Path


def build_lookup(split: dict) -> dict[str, str]:
    """Карта {старое_имя_верхнего_уровня: новый_пакет}.

    Принимает либо плоский вид {"config": "gc_core", ...}, либо старый
    сгруппированный {"core": [...], "tui": [...]}.
    """
    if all(isinstance(v, str) for v in split.values()):
        return dict(split)
    pkg_of: dict[str, str] = {}
    for key, names in split.items():
        pkg = key if key.startswith("gc_") else f"gc_{key}"
        for n in names:
            pkg_of[n] = pkg
    return pkg_of


def rewrite_source(text: str, pkg_of: dict[str, str]) -> tuple[str, int, list[str]]:
    """Вернуть (новый текст, число правок, список предупреждений)."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return text, 0, [f"SYNTAX: {exc}"]

    lines = text.splitlines(keepends=True)
    edits: list[tuple[int, int, int, str]] = []   # (lineno, col, end_lineno, new_segment)
    warns: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:                      # относительный — не трогаем
                continue
            mod = node.module or ""
            top = mod.split(".")[0]
            pkg = pkg_of.get(top)
            if not pkg:
                continue
            names = ", ".join(
                a.name + (f" as {a.asname}" if a.asname else "") for a in node.names
            )
            new = f"from {pkg}.{mod} import {names}"
            edits.append((node.lineno, node.col_offset, node.end_lineno, new))

        elif isinstance(node, ast.Import):
            parts, touched = [], False
            for a in node.names:
                top = a.name.split(".")[0]
                pkg = pkg_of.get(top)
                if not pkg:
                    parts.append(("plain", a))
                    continue
                touched = True
                if "." in a.name and not a.asname:
                    warns.append(
                        f"line {node.lineno}: `import {a.name}` — точечный импорт без "
                        f"as; после переезда привязывается только {top}, проверить руками"
                    )
                parts.append(("moved", a, pkg))
            if not touched:
                continue
            if any(p[0] == "plain" for p in parts):
                warns.append(
                    f"line {node.lineno}: смешанный import (свои + чужие модули) — "
                    f"разбить руками"
                )
                continue
            segs = []
            for p in parts:
                a, pkg = p[1], p[2]
                top, _, sub = a.name.partition(".")
                if sub and a.asname:
                    # `import updater.runner as runner` обязан привязать ПОДМОДУЛЬ.
                    # Наивное `from PKG import updater as runner` привязывает пакет —
                    # и всё, что дальше зовёт runner.<что-то>, падает AttributeError.
                    # Этот случай стоил 619 упавших тестов, поэтому он здесь явный.
                    segs.append(f"from {pkg}.{top} import {sub} as {a.asname}")
                    continue
                asname = a.asname or top
                segs.append(
                    f"from {pkg} import {top}"
                    + (f" as {asname}" if asname != top else "")
                )
            edits.append((node.lineno, node.col_offset, node.end_lineno, "; ".join(segs)))

    if not edits:
        return text, 0, warns

    # применяем с конца, чтобы не сбить номера строк
    for lineno, col, end_lineno, new in sorted(edits, key=lambda e: -e[0]):
        indent = lines[lineno - 1][:col]
        lines[lineno - 1:end_lineno] = [indent + new + "\n"]
    return "".join(lines), len(edits), warns


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True)
    ap.add_argument("--root", required=True, action="append")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    pkg_of = build_lookup(json.loads(Path(args.map).read_text()))
    total_files = total_edits = 0
    allwarns: list[str] = []
    per_pkg: Counter = Counter()

    for root in args.root:
        for p in sorted(Path(root).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            src = p.read_text(encoding="utf-8")
            new, n, warns = rewrite_source(src, pkg_of)
            if warns:
                allwarns += [f"{p}: {w}" for w in warns]
            if n:
                total_files += 1
                total_edits += n
                for pkg in ("gc_core", "gc_tui", "gc_server"):
                    per_pkg[pkg] += new.count(f"from {pkg}.") + new.count(f"from {pkg} import")
                if args.apply:
                    p.write_text(new, encoding="utf-8")

    print(f"файлов затронуто: {total_files}, импорт-узлов переписано: {total_edits}")
    print(f"  по пакетам: {dict(per_pkg)}")
    if allwarns:
        print(f"\nТРЕБУЮТ РУЧНОЙ ПРОВЕРКИ ({len(allwarns)}):")
        for w in allwarns[:40]:
            print("  ", w)
        if len(allwarns) > 40:
            print(f"   ... ещё {len(allwarns) - 40}")
    if not args.apply:
        print("\n(сухой прогон — файлы не изменены; добавьте --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
