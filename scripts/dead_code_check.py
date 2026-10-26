#!/usr/bin/env python3
"""Мёртвый код в прод-коде — аналог правил Sonar S1144, S1763, S1481/S1854.

Обёртка над vulture, которая оставляет только то, где ложных срабатываний
почти не бывает:

* всё, в чём vulture уверен на ``STRICT_CONFIDENCE`` % и выше — неиспользуемые
  импорты и переменные, недостижимый код после ``return``/``raise`` (S1763);
* неиспользуемые функции, методы, классы и свойства — приватные и публичные
  — от ``DEF_CONFIDENCE`` % (S1144 «Unused private methods» и его публичный
  аналог). Имена, которые зовут фреймворки (Textual, HTMLParser, Temporal,
  pydantic, FastAPI, pytest), отсеиваются правилами ниже; публичный контракт
  для внешних пользователей пакета (SDK, протоколы хранилищ) — в белом списке
  с пояснением.

Где ищем использования: прод-код и белый список
``scripts/dead_code_whitelist.py``. Вызов из теста использованием НЕ считается:
код, который зовут только тесты, — мёртвый вместе со своими тестами.
Исключение — ``gc_testkit``: это инфраструктура тестов, её потребители и есть
наборы тестов (``USAGE_PATHS``).
Где ищем мёртвый код: только прод-код (``PROD_PATHS``).

Имена, которые вызывает фреймворк, а не наш код, отсеиваются по соглашениям
(``IGNORE_NAMES``) и декораторам (``IGNORE_DECORATORS``). Всё остальное, что
живо лишь по неявным причинам, — в белый список с пояснением.

Запуск: ``python scripts/dead_code_check.py`` (без аргументов: анализ
межмодульный, по файлу отдельно смысла не имеет). Ненулевой код выхода при
находках — для CI и pre-commit.
"""

from __future__ import annotations

import sys
from pathlib import Path

from vulture import Vulture

ROOT = Path(__file__).resolve().parent.parent

# Та же область, что у complexipy и sonar_like_check: прод-код всех пакетов.
PROD_PATHS = [
    "packages/cli/src",
    "packages/core/src",
    "packages/mcp/src",
    "packages/server/src",
    "packages/testkit/src",
    "packages/tui/src",
    "packages/temporalio/src",
    "packages/acp/src",
    "packages/chat/src",
    "packages/core/assets/skills",
    "scripts",
    "integrations",
]
# Наборы тестов: места использования ТОЛЬКО для кода gc_testkit
# (``TEST_INFRA_PATHS``); остальной прод-код живым от тестов не становится.
TEST_INFRA_PATHS = ["packages/testkit/src"]
USAGE_PATHS = [
    "packages/cli/tests",
    "packages/core/tests",
    "packages/mcp/tests",
    "packages/server/tests",
    "packages/tui/tests",
    "packages/temporalio/tests",
    "packages/acp/tests",
    "packages/chat/tests",
    "tests",
]
WHITELIST = "scripts/dead_code_whitelist.py"
EXCLUDE = ["*/external/*", "*/__pycache__/*", "*/__snapshots__/*"]

STRICT_CONFIDENCE = 80
DEF_CONFIDENCE = 60
_DEF_KINDS = {"function", "method", "class", "property"}

# Соглашения фреймворков: эти методы зовёт не наш код.
IGNORE_NAMES = [
    # Textual: обработчики сообщений, действия, реактивность, компоновка.
    "on_*",
    "_on_*",
    "action_*",
    "_action_*",
    "watch_*",
    "_watch_*",
    "validate_*",
    "_validate_*",
    "compute_*",
    "_compute_*",
    "compose",
    "render",
    # Textual: переопределения методов App/Widget, их зовёт фреймворк.
    "get_default_screen",
    "check_action",
    "get_content_height",
    "get_selection",
    # html.parser.HTMLParser: обработчики разбора, их зовёт feed().
    "handle_*",
    # PEP 562: ленивые атрибуты модуля, зовёт механизм импорта.
    "__getattr__",
    "__dir__",
    # pytest: хуки плагина gc_testkit.
    "pytest_*",
]
# Декорированное регистрирует фреймворк; вызов по имени в коде не нужен.
IGNORE_DECORATORS = [
    "@field_validator",
    "@model_validator",
    "@*validator",
    "@computed_field",
    "@*.on_event",
    "@*.get",
    "@*.post",
    "@*.put",
    "@*.delete",
    "@*.exception_handler",
    "@*.middleware",
    "@pytest.fixture",
    "@pytest.hookimpl",
    "@*.fixture",
    "@on",
    "@work",
    # Temporal: обработчики и точки входа регистрирует SDK по декоратору.
    "@workflow.signal",
    "@workflow.update",
    "@workflow.query",
    "@workflow.run",
    "@activity.defn",
]


def _reported(item) -> bool:
    if item.confidence >= STRICT_CONFIDENCE:
        return True
    return item.confidence >= DEF_CONFIDENCE and item.typ in _DEF_KINDS


def _rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def _under(rel: str, roots: list[str]) -> bool:
    return any(rel == p or rel.startswith(p + "/") for p in roots)


def _in_prod(path: Path) -> bool:
    return _under(_rel(path), PROD_PATHS)


def _unused(paths: list[str]) -> dict:
    """Находки vulture в прод-коде, когда использования ищутся в ``paths``."""
    vulture = Vulture(
        ignore_names=IGNORE_NAMES, ignore_decorators=IGNORE_DECORATORS
    )
    vulture.scavenge(
        [str(ROOT / p) for p in paths if (ROOT / p).exists()], exclude=EXCLUDE
    )
    if vulture.exit_code:
        raise RuntimeError("vulture could not analyze all input files")
    return {
        (str(i.filename), i.first_lineno, i.name, i.typ): i
        for i in vulture.get_unused_code(min_confidence=DEF_CONFIDENCE)
        if _reported(i) and _in_prod(Path(i.filename))
    }


def find_dead_code() -> list:
    # Два прохода: без тестов — для прод-кода, с тестами — для gc_testkit.
    dead = _unused(PROD_PATHS + [WHITELIST])
    with_tests = _unused(PROD_PATHS + USAGE_PATHS + [WHITELIST])
    return sorted(
        (
            item
            for key, item in dead.items()
            if not _under(_rel(Path(item.filename)), TEST_INFRA_PATHS)
            or key in with_tests
        ),
        key=lambda i: (str(i.filename), i.first_lineno),
    )


def main() -> int:
    try:
        dead = find_dead_code()
    except RuntimeError as exc:
        print(f"dead-code: {exc}", file=sys.stderr)
        return 2
    for item in dead:
        rel = Path(item.filename).resolve().relative_to(ROOT).as_posix()
        print(
            f"{rel}:{item.first_lineno}: {item.message} ({item.confidence}% confidence)"
        )
    print(f"{len(dead)} finding(s)")
    if dead:
        print(
            "Мёртвое — удалите. Живое по неявной причине (вызов по имени, "
            f"реэкспорт, внешний API) — в {WHITELIST} с пояснением."
        )
    return 1 if dead else 0


if __name__ == "__main__":
    sys.exit(main())
