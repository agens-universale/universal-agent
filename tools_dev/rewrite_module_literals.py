#!/usr/bin/env python3
"""Переписать строковые ссылки на модули под новые пакеты.

Кодмод импортов правит только узлы `import`. Модули называются строками ещё в
353 местах — `monkeypatch.setattr("tools.registry.x")`, `sys.modules["tools"]`,
`types.ModuleType("summarize")`, фильтры `startswith("tools.")`. Около 110 из
них при устаревшей строке не падают, а молча становятся no-op, поэтому править
их обязательно и обязательно проверять (tools_dev/check_module_literals.py).

Правит ТОЧЕЧНО по позициям AST-узлов (`col_offset`/`end_col_offset`), а не
регуляркой: иначе задело бы совпадающие подстроки в тексте и комментариях.

    python tools_dev/rewrite_module_literals.py --map map.json [--apply]
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCAN = ["packages", "tests", "harbor", "scripts", "conftest.py"]

DOTTED_CALLS = {"import_module": 0, "setattr": 0, "patch": 0, "ModuleType": 0,
                "find_spec": 0, "__import__": 0}


def _targets(tree: ast.AST, known: dict[str, str]) -> list[ast.Constant]:
    """Константы-строки в позициях, где строка означает модуль."""
    out: list[ast.Constant] = []

    def take(node) -> None:
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value.split(".")[0].split(":")[0] in known):
            out.append(node)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            nm = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if nm in DOTTED_CALLS and node.args:
                take(node.args[DOTTED_CALLS[nm]])
            if nm == "startswith" and node.args:
                take(node.args[0])
            if nm == "setitem" and len(node.args) >= 2:
                tgt = node.args[0]
                if isinstance(tgt, ast.Attribute) and tgt.attr == "modules":
                    take(node.args[1])
            if nm == "get" and isinstance(fn, ast.Attribute) and node.args:
                owner = fn.value
                if isinstance(owner, ast.Attribute) and owner.attr == "modules":
                    take(node.args[0])
            if nm == "run" and node.args:
                a = node.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str) and ":" in a.value:
                    take(a)
        if isinstance(node, ast.Subscript):
            v = node.value
            if isinstance(v, ast.Attribute) and v.attr == "modules":
                take(node.slice)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant):
            for cmp_ in node.comparators:
                if isinstance(cmp_, ast.Attribute) and cmp_.attr == "modules":
                    take(node.left)
    return out


# Пакеты, чьё содержимое РАЗВЁРНУТО в корень нового пакета, а не вложено в
# одноимённый подпакет. Для них старое имя ЗАМЕНЯЕТСЯ, а не префиксуется:
# `tui.app` -> `gc_tui.app`, а не `gc_tui.tui.app`.
FLATTENED = {"tui": "gc_tui"}


def _new_literal(lit: str, known: dict[str, str]) -> str:
    """`tools.registry.x` -> `gc_core.tools.registry.x`; `server:app` -> `gc_server.server:app`.

    Для схлопнутых пакетов: `tui.app` -> `gc_tui.app`, `tui.` -> `gc_tui.`.
    """
    head, sep, tail = lit.partition(":")
    parts = head.split(".")
    top = parts[0]
    if top in FLATTENED:
        parts[0] = FLATTENED[top]
        return ".".join(parts) + sep + tail
    pkg = known.get(top)
    if not pkg:
        return lit
    return f"{pkg}.{head}{sep}{tail}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    known: dict[str, str] = json.loads(Path(args.map).read_text())

    files = []
    for s in SCAN:
        p = REPO / s
        files += [p] if p.is_file() else [q for q in p.rglob("*.py")
                                          if "__pycache__" not in q.parts] if p.exists() else []

    total = touched = 0
    for p in sorted(set(files)):
        text = p.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        nodes = _targets(tree, known)
        if not nodes:
            continue
        lines = text.splitlines(keepends=True)
        # правим с конца файла, чтобы не сбить смещения
        edits = sorted(nodes, key=lambda n: (-n.lineno, -n.col_offset))
        n_here = 0
        for node in edits:
            if node.lineno != node.end_lineno:      # многострочный литерал — пропуск
                continue
            new = _new_literal(node.value, known)
            if new == node.value:
                continue
            # col_offset в AST — БАЙТОВОЕ смещение в UTF-8, а не символьное.
            # Резать str напрямую нельзя: на строках с кириллицей или юникодом
            # (а их здесь много) срез уезжает и портит файл молча.
            raw = lines[node.lineno - 1].encode("utf-8")
            seg = raw[node.col_offset:node.end_col_offset].decode("utf-8")
            quote = seg[0] if seg and seg[0] in "\"'" else '"'
            lines[node.lineno - 1] = (
                raw[:node.col_offset].decode("utf-8")
                + f"{quote}{new}{quote}"
                + raw[node.end_col_offset:].decode("utf-8")
            )
            n_here += 1
        if n_here:
            touched += 1
            total += n_here
            if args.apply:
                p.write_text("".join(lines), encoding="utf-8")

    print(f"литералов переписано: {total} в {touched} файлах")
    if not args.apply:
        print("(сухой прогон — файлы не изменены)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
