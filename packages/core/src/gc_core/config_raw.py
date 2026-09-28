"""Сырые (нерезолвнутые) значения config.yml + распознавание ссылок ${ENV:VAR}.

Технический слой, без бизнес-логики. Нужен редакторам конфига: загруженный конфиг
уже резолвнут (config_modules.env_interpolation), и запись его значения обратно в
файл подменила бы ссылку на переменную её значением — секрет открытым текстом.
Редактор должен видеть ровно то, что лежит в файле.

Статус ссылки определяется по os.environ, а НЕ по резолвнутому значению: пустой
резолв («переменной нет») неотличим от «значение не задано».
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List

import yaml

# Синтаксис ${ENV:VAR} — единый источник истины (модуль-владелец резолва).
from gc_core.config_modules.env_interpolation import _ENV_MARKER
from gc_core.utils.user_text import read_user_text_or_none


def parse_document(text: str) -> Dict[str, Any]:
    """YAML-документ из УЖЕ прочитанного текста; битый/пустой → {}.

    Отдельно от `read_document` ради тех, кому нужен и текст, и документ (валидация
    записи спрашивает текст, витрина — документ): второе чтение того же файла ради
    того же содержимого — лишний поход на диск, а не отдельный факт."""
    try:
        document = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}
    return document if isinstance(document, dict) else {}


def read_document(path) -> Dict[str, Any]:
    """YAML-документ файла как есть (без резолва ${ENV:VAR}).

    Нечитаемый/битый/пустой файл → {} (ровно как трактует его load_config). Туда же
    идёт документ, чей верхний уровень не словарь (список, скаляр): конфигом он не
    является, а вызывающие и `value_at` ждут словарь.
    """
    # Витрина профилей читает этим ЧУЖИЕ файлы корня, в том числе неактивные: такой
    # профиль в загрузке не участвует, кодировку его никто не проверял, и отказ
    # ронял экран настроек на файле, который в этом запуске даже не действует.
    return parse_document(read_user_text_or_none(path) or "")


def value_at(document: Any, dotted: str) -> Any:
    """Значение по dotted-пути в документе; ключа нет (или узел не dict) → None."""
    node: Any = document
    for part in dotted.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def read_values(path, dotted_keys: Iterable[str]) -> Dict[str, Any]:
    """{dotted-путь: значение из файла} — как записано, без резолва ${ENV:VAR}.

    Нечитаемый/битый файл и отсутствующий ключ → None (редактор покажет «не задано»).
    """
    document = read_document(path)
    return {dotted: value_at(document, dotted) for dotted in dotted_keys}


def env_refs(value: Any) -> List[str]:
    """Имена переменных, на которые ссылается сырое значение (в порядке появления)."""
    if not isinstance(value, str):
        return []
    return _ENV_MARKER.findall(value)


def env_refs_overwritten(
    new_values: Dict[str, Any], old_values: Dict[str, Any]
) -> tuple[List[str], List[str]]:
    """Переменные окружения, чьи ссылки затрёт запись `new_values` поверх
    `old_values` (сырые значения тех же ключей), разложенные по тому, ЧТО с ними
    произойдёт: (перезапись литералом, удаление пустым значением). Оба списка
    пусты — затирать нечего либо новое значение само ссылка; ключ, которого нет
    в `new_values`, не трогается и ссылок не затирает.

    Общий дом гейтов записи «ссылка не может стать литералом молча» — плоские
    поля /settings и форма записи реестра: у литерала последствие — секрет
    открытым текстом в файле, у пустого значения — исчезновение ссылки. Вторая
    редакция того же правила в одном из гейтов уже разъехалась с первой: форма
    реестра писала токен поверх `${ENV:…}` без единого вопроса."""
    written: List[str] = []
    erased: List[str] = []
    for key, value in new_values.items():
        if env_refs(value):
            continue
        refs = env_refs(old_values.get(key))
        (written if str(value or "").strip() else erased).extend(refs)
    return written, erased


def missing_env_refs(value: Any) -> List[str]:
    """Из env_refs — те, которых нет в окружении (ссылка не подставится)."""
    return [name for name in env_refs(value) if name not in os.environ]
