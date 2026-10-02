"""Версия формата файла конфига (`config_version`) и миграции между версиями.

Каждый файл-слой (база, managed, проектный `.gc/config.yml`, профиль) несёт свою
версию: ключа нет — версия 1. Загрузчик поднимает КАЖДЫЙ документ до текущей
версии (`migrate_document`) после `${ENV:…}` и ДО мержа и схемы — файлы на диске
не трогаются. Версия считается по
файлу, а не по итогу мержа: слои пишутся разными версиями приложения, и
поднимать надо каждый от его собственной.

Реестр `MIGRATIONS` — `{from_version: migrate}`; `migrate(raw) -> (raw, facts)`
поднимает документ РОВНО на одну версию (from → from + 1). Документ приходит
копией — мутировать его можно; `facts` — человекочитаемые строки для стартовых
notices (`runtime.notices`), тем же каналом, что и факты compat-слоя.

Compat-слой (`compat.py`: переименования/удаления ключей ВНУТРИ версии 1)
остаётся отдельным шагом поверх итогового мержа, как и был: он чинит конфиги без
`config_version`, то есть версии 1, и потому не может быть «миграцией в v1».
Когда понадобится настоящая смена формата — поднимается
`CURRENT_CONFIG_VERSION`, в `MIGRATIONS` регистрируется шаг `{старая: функция}`,
а в шаблонах (`assets/config.default.yml`, `demo_config.yml`) правится
`config_version`.

Файл с версией БОЛЬШЕ текущей — отказ загрузки: его писала более новая версия
приложения, и читать его по старой схеме значит молча потерять или исказить
настройки.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Tuple

CONFIG_VERSION_KEY = "config_version"
# Текущая версия формата. Поднимать ТОЛЬКО вместе с регистрацией миграции.
CURRENT_CONFIG_VERSION = 1

Migration = Callable[[Dict[str, Any]], Tuple[Dict[str, Any], List[str]]]
# {from_version: migrate} — шаг from → from + 1.
MIGRATIONS: Dict[int, Migration] = {}


def current_config_version() -> int:
    """Текущая версия формата (через функцию — тесты подменяют модульное имя)."""
    return CURRENT_CONFIG_VERSION


def _prefix(source: str) -> str:
    return f"{source}: " if source else ""


def document_version(raw: Dict[str, Any], *, source: str = "") -> int:
    """Версия формата документа: ключа нет — 1. Негодное значение или версия
    новее поддерживаемой — `ValueError` с путём файла."""
    if not isinstance(raw, dict) or CONFIG_VERSION_KEY not in raw:
        return 1
    value = raw[CONFIG_VERSION_KEY]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"{_prefix(source)}{CONFIG_VERSION_KEY}: ожидается целое число ≥ 1, "
            f"получено {value!r}"
        )
    current = current_config_version()
    if value > current:
        raise ValueError(
            f"{_prefix(source)}{CONFIG_VERSION_KEY}: {value} — конфиг записан более "
            f"новой версией приложения (эта понимает формат до {current}); "
            "обновите приложение или возьмите конфиг, записанный этой версией"
        )
    return value


def migrate_document(
    raw: Dict[str, Any], *, source: str = ""
) -> Tuple[Dict[str, Any], List[str]]:
    """Поднять документ одного файла до текущей версии формата.

    Возвращает `(документ, факты)`. Документ уже текущей версии отдаётся ТЕМ ЖЕ
    объектом (без копии): это обычный путь, и копировать каждый слой на каждой
    загрузке незачем. Иначе — глубокая копия, пошаговые миграции, и
    `config_version` выставляется в текущую."""
    version = document_version(raw, source=source)
    current = current_config_version()
    if version == current:
        return raw, []
    doc = copy.deepcopy(raw)
    facts: List[str] = []
    prefix = _prefix(source)
    for step in range(version, current):
        migrate = MIGRATIONS.get(step)
        if migrate is None:
            raise ValueError(
                f"{prefix}{CONFIG_VERSION_KEY}: нет миграции формата {step} → "
                f"{step + 1} (внутренняя ошибка приложения)"
            )
        doc, step_facts = migrate(doc)
        facts += [f"{prefix}{fact}" for fact in step_facts]
    # Штамп ставится и там, где ключа в файле не было: поднятый документ обязан
    # читаться текущей версией и при повторном проходе (кандидаты pre-write
    # валидации проходят `migrate_document` дважды — в `_raw_from_text` и при
    # наложении слоя), иначе миграция применилась бы второй раз.
    doc[CONFIG_VERSION_KEY] = current
    facts.append(
        f"{prefix}формат конфига {version} устарел и прочитан как {current} — "
        "файл на диске не изменён"
    )
    return doc, facts


__all__ = [
    "CONFIG_VERSION_KEY",
    "CURRENT_CONFIG_VERSION",
    "MIGRATIONS",
    "current_config_version",
    "document_version",
    "migrate_document",
]
