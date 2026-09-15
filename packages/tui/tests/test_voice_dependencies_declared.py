"""Что голос gc-tui импортирует из сторонних пакетов — объявлено в gc-tui.

`requests` приезжал транзитом из gc-core; ядро перешло на httpx и зависимость
сняло. В окружении разработки это не видно — requests приезжает через
dev-зависимости, — а чистая установка `gc-voice` падала на импорте.
"""
import ast
import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _declared() -> set:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    out = set()
    for d in deps:
        name = d
        for sep in ("[", ">", "<", "=", "!", "~", ";", " "):
            name = name.split(sep)[0]
        out.add(name.strip().lower())
    return out


def _imports_requests(path: pathlib.Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, ast.Import) and any(a.name.split(".")[0] == "requests" for a in n.names):
            return True
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "requests":
            return True
    return False


def test_requests_is_declared_where_it_is_imported():
    importers = [p for p in (ROOT / "src").rglob("*.py") if _imports_requests(p)]
    assert importers, "requests никто не импортирует — проверка устарела"
    assert "requests" in _declared(), (
        "gc-tui импортирует requests, но не объявляет: "
        + ", ".join(str(p.relative_to(ROOT)) for p in importers)
    )
