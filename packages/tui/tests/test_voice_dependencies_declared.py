"""Голос ходит в сеть httpx — клиентом ядра, и requests ему не нужен.

История: requests приезжал в голос транзитом из gc-core. Ядро перешло на httpx и
зависимость сняло — чистая установка `gc-voice` стала падать на импорте. Сначала
requests объявили в gc-tui, но это лечило симптом: POST голоса к тому времени уже
шёл клиентом ядра (`NO_NETRC_SESSION`, httpx), а requests оставался в коде ровно
ради `except requests.RequestException` — веток, которые не могли совпасть НИ РАЗУ,
и человеку в тост летел сырой `httpx.ConnectError` вместо «STT недоступен».
Поэтому голос доведён до httpx, а зависимость снята.

Сторож двусторонний: вернётся requests в импорты — его придётся либо снова
объявить, либо (правильнее) дописать переезд.
"""
import ast
import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
VOICE_DIRS = (ROOT / "src" / "gc_tui" / "voice", ROOT / "src" / "gc_tui" / "voice_runtime")


def _declared() -> set:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    out = set()
    for d in deps:
        name = d
        for sep in ("[", ">", "<", "=", "!", "~", ";", " "):
            name = name.split(sep)[0]
        out.add(name.strip().lower())
    return out


def _top_level_imports(path: pathlib.Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            names.add(n.module.split(".")[0])
    return names


def _voice_files() -> list:
    return [p for d in VOICE_DIRS for p in d.rglob("*.py")]


def test_voice_does_not_import_requests():
    importers = [p for p in _voice_files() if "requests" in _top_level_imports(p)]
    assert not importers, (
        "голос снова импортирует requests: "
        + ", ".join(str(p.relative_to(ROOT)) for p in importers)
        + ". Либо переведите на httpx (клиент ядра), либо объявите зависимость в gc-tui"
    )


def test_requests_is_not_declared_while_nobody_imports_it():
    assert "requests" not in _declared(), (
        "requests объявлен в gc-tui, но голос его не импортирует — лишняя зависимость"
    )


def test_voice_speaks_httpx():
    """Чтобы проверка выше не выродилась в пустую: сеть у голоса есть, и она httpx."""
    users = [p for p in _voice_files() if "httpx" in _top_level_imports(p)]
    assert users, "никто в голосе не импортирует httpx — проверка устарела"
