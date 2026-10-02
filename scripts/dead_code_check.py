#!/usr/bin/env python3
"""Мёртвый код в прод-коде — аналог правил Sonar S1144, S1763, S1481/S1854.

Обёртка над vulture, которая оставляет только то, где ложных срабатываний
почти не бывает:

* всё, в чём vulture уверен на ``STRICT_CONFIDENCE`` % и выше — неиспользуемые
  импорты и переменные, недостижимый код после ``return``/``raise`` (S1763);
* неиспользуемые ПРИВАТНЫЕ функции, методы, классы и свойства (``_name``,
  не dunder) от ``PRIVATE_CONFIDENCE`` % — это S1144 «Unused private methods».
  Публичные имена vulture помечает десятками: их зовут фреймворки (Textual,
  pydantic, FastAPI, pytest) и внешние пользователи пакета, поэтому как гейт
  они шумят сильнее, чем помогают.

Где ищем использования: прод-код, тесты (функция, которую зовёт только тест,
— живая: её держит тест) и белый список ``scripts/dead_code_whitelist.py``.
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
    "packages/acp/src",
    "packages/core/assets/skills",
    "scripts",
    "integrations",
]
# Только как места использования: сами не проверяются.
USAGE_PATHS = [
    "packages/cli/tests",
    "packages/core/tests",
    "packages/mcp/tests",
    "packages/server/tests",
    "packages/tui/tests",
    "packages/acp/tests",
    "tests",
]
WHITELIST = "scripts/dead_code_whitelist.py"
EXCLUDE = ["*/external/*", "*/__pycache__/*", "*/__snapshots__/*"]

STRICT_CONFIDENCE = 80
PRIVATE_CONFIDENCE = 60
_PRIVATE_KINDS = {"function", "method", "class", "property"}

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
]


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (
        name.startswith("__") and name.endswith("__")
    )


def _reported(item) -> bool:
    if item.confidence >= STRICT_CONFIDENCE:
        return True
    return (
        item.confidence >= PRIVATE_CONFIDENCE
        and item.typ in _PRIVATE_KINDS
        and _is_private(item.name)
    )


def _in_prod(path: Path) -> bool:
    rel = path.resolve().relative_to(ROOT).as_posix()
    return any(rel == p or rel.startswith(p + "/") for p in PROD_PATHS)


def find_dead_code() -> list:
    vulture = Vulture(
        ignore_names=IGNORE_NAMES, ignore_decorators=IGNORE_DECORATORS
    )
    paths = [
        str(ROOT / p)
        for p in PROD_PATHS + USAGE_PATHS + [WHITELIST]
        if (ROOT / p).exists()
    ]
    vulture.scavenge(paths, exclude=EXCLUDE)
    if vulture.exit_code:
        raise RuntimeError("vulture could not analyze all input files")
    items = vulture.get_unused_code(min_confidence=PRIVATE_CONFIDENCE)
    return sorted(
        (i for i in items if _reported(i) and _in_prod(Path(i.filename))),
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
