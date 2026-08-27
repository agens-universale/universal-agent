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
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml

# Синтаксис ${ENV:VAR} — единый источник истины (модуль-владелец резолва).
from config_modules.env_interpolation import _ENV_MARKER


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
    try:
        return parse_document(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return {}


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


def missing_env_refs(value: Any) -> List[str]:
    """Из env_refs — те, которых нет в окружении (ссылка не подставится)."""
    return [name for name in env_refs(value) if name not in os.environ]
