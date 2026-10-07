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

Вторая сторона той же истории — ниже: что голос импортирует НА УРОВНЕ МОДУЛЯ,
обязано стоять в его манифесте. Транзит из чужого дистрибутива гарантией не
считается — он и подвёл с requests. Импорты внутри функций сторож не судит:
такой отказ приходит при вызове, а не на загрузке пакета.
"""

import ast
import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
VOICE_DIRS = (
    ROOT / "src" / "gc_tui" / "voice",
    ROOT / "src" / "gc_tui" / "voice_runtime",
)

#: Модуль → дистрибутив, который его привозит: имя модуля и имя пакета
#: совпадают не всегда (`recommended_mcp` едет в gc-mcp). ``None`` — свой код.
OWN_DISTRIBUTION = {
    "gc_tui": None,
    "gc_core": "gc-core",
    "gc_mcp": "gc-mcp",
    "recommended_mcp": "gc-mcp",
}


def _canon(name: str) -> str:
    """Имя пакета по PEP 503: `huggingface_hub` и `huggingface-hub` — одно и то же."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _declared() -> set:
    deps = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["dependencies"]
    out = set()
    for d in deps:
        name = d
        for sep in ("[", ">", "<", "=", "!", "~", ";", " "):
            name = name.split(sep)[0]
        out.add(_canon(name))
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


def _module_level_imports(path: pathlib.Path) -> set:
    """Импорты, которые исполняются при ЗАГРУЗКЕ модуля.

    В счёт идёт и то, что стоит под `if`/`try` верхнего уровня: отсутствие
    пакета роняет импорт всё равно. Внутрь функций и классов не заходим —
    ленивый импорт падает при вызове, и объявления он не требует.
    """
    names = set()

    def walk(body) -> None:
        for node in body:
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module
            ):
                names.add(node.module.split(".")[0])
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                for attr in ("body", "orelse", "finalbody", "handlers"):
                    for item in getattr(node, attr, []) or []:
                        walk(
                            item.body
                            if isinstance(item, ast.ExceptHandler)
                            else [item]
                        )

    walk(ast.parse(path.read_text(encoding="utf-8")).body)
    return names


def _voice_files() -> list:
    return [p for d in VOICE_DIRS for p in d.rglob("*.py")]


def test_voice_does_not_import_requests():
    importers = [
        p for p in _voice_files() if "requests" in _top_level_imports(p)
    ]
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


def test_what_voice_imports_at_module_level_is_declared():
    """Зеркало проверки про requests: там пакет был объявлен, но не нужен, — здесь
    нужен, но не объявлен. Так и ломалась чистая установка голоса: `import` в
    коде остался, а пакет приезжал транзитом и уехал вместе с чужой правкой."""
    declared = _declared()
    missing: dict = {}
    for path in _voice_files():
        for name in _module_level_imports(path):
            if name in sys.stdlib_module_names:
                continue
            if name in OWN_DISTRIBUTION:
                dist = OWN_DISTRIBUTION[name]
                if dist is None or _canon(dist) in declared:
                    continue
                missing.setdefault(f"{name} (дистрибутив {dist})", []).append(
                    path.name
                )
                continue
            if _canon(name) not in declared:
                missing.setdefault(name, []).append(path.name)
    assert not missing, (
        "голос импортирует на уровне модуля то, чего нет в зависимостях gc-tui: "
        + "; ".join(
            f"{name} — {', '.join(sorted(files))}"
            for name, files in sorted(missing.items())
        )
        + ". Объявите пакет в packages/tui/pyproject.toml — транзит из чужого "
        "дистрибутива не гарантия"
    )
