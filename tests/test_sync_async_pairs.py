"""Пар sync/async в одной области видимости — ровно восемь, и все названы.

Ветка async-only держит одну форму на операцию. Синхронный двойник допустим
только там, где обмен блокирующий по существу и вызывающего с живым loop не
бывает: все восемь обслуживают долгоживущий stdio MCP — их зовёт `atexit`, где
loop'а уже нет. Две пары успели появиться незамеченными уже после того, как
«восемь» было объявлено: счётчик тогда гоняли руками.
"""
import ast
import pathlib
import re

PACKAGES = pathlib.Path(__file__).resolve().parents[1] / "packages"

EXPECTED = {
    "gc_core/tools/connect_mcp/transport.py::<модуль>._readline_with_timeout",
    "gc_core/tools/connect_mcp/transport.py::_LineReader.readline",
    "gc_core/tools/connect_mcp/client_port.py::AgentRuntimeClient.call_tool",
    "gc_core/tools/connect_mcp/client_port.py::AgentRuntimeClient.list_tools",
    "gc_core/tools/connect_mcp/persistent_stdio.py::PersistentStdioSession._close_prologue",
    "gc_core/tools/connect_mcp/persistent_stdio.py::PersistentStdioSession._read_matching_on",
    "gc_core/tools/connect_mcp/persistent_stdio.py::PersistentStdioSession.close",
    "gc_core/tools/connect_mcp/persistent_stdio.py::PersistentStdioRegistry.close_all",
}


def _base_of(name: str):
    m = re.match(r"^(_*)a(.+)$", name)
    return (m.group(1) + m.group(2)) if m else None


def _pairs() -> set:
    rows = set()
    for p in PACKAGES.rglob("*/src/**/*.py"):
        if "vendor" in p.parts:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for scope in [tree] + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            body = getattr(scope, "body", [])
            names = {m.name for m in body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
            asy = {m.name for m in body if isinstance(m, ast.AsyncFunctionDef)}
            for n in asy:
                b = _base_of(n)
                if b and b in names and b not in asy:
                    rel = p.as_posix().split("/src/")[-1]   # str(p) на Windows — с «\», разреза не было бы
                    rows.add(f"{rel}::{getattr(scope, 'name', '<модуль>')}.{b}")
    return rows


def test_only_the_named_pairs_remain():
    found = _pairs()
    assert found - EXPECTED == set(), f"новая пара sync/async: {sorted(found - EXPECTED)}"
    assert EXPECTED - found == set(), (
        f"пара снята — уберите её из EXPECTED и из описания: {sorted(EXPECTED - found)}"
    )
