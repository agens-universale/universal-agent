#!/usr/bin/env python3
"""Сетка защиты для переезда на пакеты: сверяет строковые ссылки на модули.

Зачем. Кодмод правит только узлы import. Но модули называются строками ещё в
354 местах — `monkeypatch.setattr("tools.registry.x")`, `sys.modules["tools"]`,
`types.ModuleType("summarize")`, фильтры `startswith("tools.")`. Из них ~110
при устаревшей строке НЕ падают: цикл выселения просто становится пустым,
заглушки протекают между тестовыми файлами, а набор остаётся зелёным.

Поэтому после каждого шага переезда прогон тестов ничего не доказывает — нужна
эта проверка. Она находит все такие литералы и сверяет с реальностью:
существует ли модуль, существует ли атрибут.

Использование:
    python tools_dev/check_module_literals.py            # отчёт
    python tools_dev/check_module_literals.py --json     # машинный вывод
    python tools_dev/check_module_literals.py --inventory  # снять снимок «до»
    python tools_dev/check_module_literals.py --compare inventory.json
        сравнить с снимком: любое ИСЧЕЗНОВЕНИЕ живого литерала — регрессия

Код возврата: 0 — всё сходится, 1 — есть битые ссылки.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCAN_DIRS = ["packages", "src", "tests", "harbor", "scripts", "recommended_mcp", "meta"]
SCAN_FILES = ["conftest.py"]

# Функции, чей строковый аргумент — путь к модулю или атрибуту модуля.
# Имена логгеров — тоже ссылка на модуль: логгер обычно зовётся __name__, и
# при переезде его полное имя меняется вместе с пакетом. Тест, который ждёт
# старое имя, падает громко, но найти причину без этой проверки долго.
DOTTED_TARGET_CALLS = {
    "assertLogs": 0,
    "assertNoLogs": 0,
    "getLogger": 0,
    "import_module": 0,
    "setattr": 0,      # monkeypatch.setattr("mod.attr", ...) — только строковая форма
    "patch": 0,        # mock.patch("mod.attr")
    "object": None,    # patch.object(mod, "name") — первый арг не строка, пропускаем
    "ModuleType": 0,
    "find_spec": 0,
}


@dataclass(frozen=True)
class Ref:
    file: str
    line: int
    kind: str      # import_module | setattr | patch | ModuleType | sys_modules | prefix
    literal: str

    def key(self) -> str:
        return f"{self.file}:{self.line}:{self.kind}:{self.literal}"


def _top_level_names() -> set[str]:
    """Имена, с которых может начинаться ССЫЛКА на наш модуль.

    Только верхний уровень: сами пакеты (`gc_core`, `gc_tui`, …) плюс —
    во время миграции — дореформенные имена из module_map.json. Брать сюда
    вложенные подпакеты нельзя: `fs`, `exec`, `session` дают ложные
    срабатывания на обычных строках (`f"fs{i:04d}"` — идентификатор сессии).
    """
    names: set[str] = set()
    for pkg_src in (REPO / "packages").glob("*/src"):
        for child in pkg_src.iterdir():
            if child.is_dir() and (child / "__init__.py").exists():
                names.add(child.name)
    legacy = REPO / "tools_dev" / "module_map.json"
    if legacy.is_file():
        names |= set(json.loads(legacy.read_text(encoding="utf-8")))
    src = REPO / "src"
    if src.exists():
        for p in src.iterdir():
            if p.name.startswith(("__", ".")):
                continue
            names.add(p.stem if p.suffix == ".py" else p.name)
    return names


_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\.?$")


def _looks_like_our_module(lit: str, ours: set[str], *, strict: bool = False) -> bool:
    """strict=True — литерал обязан выглядеть как модульный путь целиком.

    Нужно для f-строк: `f"config.yml не перечитан…"` начинается с имени
    модуля `config`, но модулем не является. У обычных строк такой проверки
    нет намеренно — там встречаются пути вида `tools/session/x.py`.
    """
    if lit.split(".")[0] not in ours:
        return False
    return bool(_DOTTED.match(lit)) if strict else True


def collect(paths: list[Path], ours: set[str]) -> list[Ref]:
    refs: list[Ref] = []
    for p in paths:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), str(p))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = str(p.relative_to(REPO))
        for node in ast.walk(tree):
            # 1) вызовы с точечной строкой-целью
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                idx = DOTTED_TARGET_CALLS.get(name)
                if idx is not None and len(node.args) > idx:
                    a = node.args[idx]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        if _looks_like_our_module(a.value, ours):
                            refs.append(Ref(rel, node.lineno, name, a.value))
                # 2) фильтры выселения: startswith("tools.")
                if name == "startswith" and node.args:
                    a = node.args[0]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        if _looks_like_our_module(a.value, ours):
                            refs.append(Ref(rel, node.lineno, "prefix", a.value))
            # 3) sys.modules["literal"] — чтение, запись, удаление
            if isinstance(node, ast.Subscript):
                v = node.value
                if (isinstance(v, ast.Attribute) and v.attr == "modules"
                        and isinstance(node.slice, ast.Constant)
                        and isinstance(node.slice.value, str)
                        and _looks_like_our_module(node.slice.value, ours)):
                    refs.append(Ref(rel, node.lineno, "sys_modules", node.slice.value))

            # 4) monkeypatch.setitem(sys.modules, "literal", …) и sys.modules.get("literal")
            if isinstance(node, ast.Call):
                fn = node.func
                nm = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if nm == "setitem" and len(node.args) >= 2:
                    tgt, key = node.args[0], node.args[1]
                    is_sysmods = isinstance(tgt, ast.Attribute) and tgt.attr == "modules"
                    if (is_sysmods and isinstance(key, ast.Constant)
                            and isinstance(key.value, str)
                            and _looks_like_our_module(key.value, ours)):
                        refs.append(Ref(rel, node.lineno, "sys_modules", key.value))
                if nm == "get" and isinstance(fn, ast.Attribute) and node.args:
                    owner = fn.value
                    if (isinstance(owner, ast.Attribute) and owner.attr == "modules"
                            and isinstance(node.args[0], ast.Constant)
                            and isinstance(node.args[0].value, str)
                            and _looks_like_our_module(node.args[0].value, ours)):
                        refs.append(Ref(rel, node.lineno, "sys_modules", node.args[0].value))
                if nm == "__import__" and node.args:
                    a = node.args[0]
                    if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                            and _looks_like_our_module(a.value, ours)):
                        refs.append(Ref(rel, node.lineno, "import_module", a.value))
                # uvicorn.run("server:app") — форма "модуль:атрибут"
                if nm == "run" and node.args:
                    a = node.args[0]
                    if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                            and ":" in a.value
                            and _looks_like_our_module(a.value.split(":")[0], ours)):
                        refs.append(Ref(rel, node.lineno, "asgi_target", a.value))

            # 4b) f-строки: f"tools.{sub}" — кодмод их НЕ видит (это JoinedStr,
            # а не Constant). Именно на них разъехались регистрация и чтение в
            # tests/{tools,security}/conftest.py: пакет ставился под
            # `tools.session`, а искался под `gc_core.tools.session`.
            if isinstance(node, ast.JoinedStr) and node.values:
                first = node.values[0]
                if (isinstance(first, ast.Constant) and isinstance(first.value, str)
                        and _looks_like_our_module(first.value, ours, strict=True)):
                    refs.append(Ref(rel, node.lineno, "fstring", first.value))

            # 5) "literal" in sys.modules
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant):
                if isinstance(node.left.value, str) and _looks_like_our_module(node.left.value, ours):
                    for cmp_ in node.comparators:
                        if isinstance(cmp_, ast.Attribute) and cmp_.attr == "modules":
                            refs.append(Ref(rel, node.lineno, "sys_modules", node.left.value))
    return refs


def verify(ref: Ref) -> str | None:
    """Вернуть текст проблемы или None, если ссылка валидна."""
    lit = ref.literal.rstrip(".")
    if ref.kind == "prefix":
        lit = lit.rstrip(".")
    if ref.kind == "asgi_target":
        lit = lit.split(":")[0]
    parts = lit.split(".")
    # ищем самый длинный существующий модульный префикс
    mod = None
    for i in range(len(parts), 0, -1):
        cand = ".".join(parts[:i])
        try:
            if importlib.util.find_spec(cand) is not None:
                mod = cand
                rest = parts[i:]
                break
        except Exception:
            # find_spec бросает, а не возвращает None, когда РОДИТЕЛЬСКИЙ пакет
            # не импортируется (ModuleNotFoundError), а импорт родителя может
            # уронить что угодно ещё. Для нашей задачи это просто «не нашли».
            continue
    else:
        return f"модуль не найден: {lit}"
    if mod is None:
        return f"модуль не найден: {lit}"
    if not rest:
        return None
    # остаток должен быть атрибутом — импортировать модуль ради проверки дорого
    # и небезопасно (побочные эффекты), поэтому проверяем только существование
    # модуля; атрибут ловится «громко» самим pytest/mock.
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--inventory", action="store_true", help="снять снимок и вывести в stdout")
    ap.add_argument("--compare", metavar="FILE", help="сравнить с ранее снятым снимком")
    args = ap.parse_args()

    sys.path.insert(0, str(REPO / "src"))
    for pkg in (REPO / "packages").glob("*/src"):
        sys.path.insert(0, str(pkg))

    ours = _top_level_names()
    paths = [p for d in SCAN_DIRS for p in (REPO / d).rglob("*.py")
             if (REPO / d).exists() and "__pycache__" not in p.parts]
    paths += [REPO / f for f in SCAN_FILES if (REPO / f).exists()]

    refs = collect(paths, ours)
    problems = [(r, msg) for r in refs if (msg := verify(r))]

    if args.inventory:
        print(json.dumps([asdict(r) for r in refs], ensure_ascii=False, indent=1))
        return 0

    if args.compare:
        before = {Ref(**d).key() for d in json.loads(Path(args.compare).read_text())}
        now = {r.key() for r in refs}
        lost = before - now
        print(f"снимок: {len(before)} ссылок, сейчас: {len(now)}")
        if lost:
            print(f"ИСЧЕЗЛИ {len(lost)} (проверьте, что это намеренно):")
            for k in sorted(lost)[:30]:
                print("  ", k)
        return 1 if problems else 0

    by_kind: dict[str, int] = {}
    for r in refs:
        by_kind[r.kind] = by_kind.get(r.kind, 0) + 1

    if args.json:
        print(json.dumps({"total": len(refs), "by_kind": by_kind,
                          "problems": [{**asdict(r), "problem": m} for r, m in problems]},
                         ensure_ascii=False, indent=1))
    else:
        print(f"строковых ссылок на наши модули: {len(refs)} в "
              f"{len({r.file for r in refs})} файлах")
        for k, n in sorted(by_kind.items(), key=lambda x: -x[1]):
            print(f"  {k:12s} {n:4d}")
        print()
        if problems:
            print(f"БИТЫХ ССЫЛОК: {len(problems)}")
            for r, m in problems[:40]:
                print(f"  {r.file}:{r.line}  {r.literal!r} — {m}")
        else:
            print("битых ссылок нет")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
