"""Откуда взялась каждая настройка: слой файла, переменная окружения, CLI, сессия.

Основа ``config explain``. Слои накладываются по порядку (base → managed →
project → профиль → CLI → сессия), поэтому источник листа — последний слой,
который его задал. Лист, которого нет ни в одном слое, — значение по умолчанию.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping

_ENV = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class Source:
    """Источник значения: слой (default/base/managed/project/profile:<имя>/cli/
    session/runtime), путь файла и переменные окружения, если значение из них."""

    layer: str
    path: str | None = None
    env: tuple[str, ...] = ()

    def describe(self) -> str:
        where = (
            self.layer if self.path is None else f"{self.layer} ({self.path})"
        )
        if self.env:
            return f"{where} ← ${{ENV:{', '.join(self.env)}}}"
        return where


DEFAULT = Source("default")


def leaves(
    document: Any, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Листья документа: словари раскрываются, списки и скаляры — листья."""
    if isinstance(document, Mapping) and document:
        for key, value in document.items():
            yield from leaves(value, (*prefix, str(key)))
    elif prefix:
        yield prefix, document


def _env_names(value: Any) -> tuple[str, ...]:
    found: list[str] = []
    for _, leaf in (
        leaves(value) if isinstance(value, Mapping) else [((), value)]
    ):
        if isinstance(leaf, str):
            found.extend(_ENV.findall(leaf))
        elif isinstance(leaf, list):
            for item in leaf:
                if isinstance(item, str):
                    found.extend(_ENV.findall(item))
    return tuple(dict.fromkeys(found))


def _lookup(document: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(document, Mapping) or key not in document:
            return None
        document = document[key]
    return document


class Provenance:
    """Источник каждого листа настроек; неизменяем после сборки."""

    def __init__(
        self, sources: Mapping[tuple[str, ...], Source] | None = None
    ):
        self._sources = dict(sources or {})

    @classmethod
    def from_layers(
        cls, layers: Iterable[tuple[str, str | None, Any, Any]]
    ) -> Provenance:
        """``layers`` — ``(слой, путь, документ до ${ENV}, документ после)``."""
        sources: dict[tuple[str, ...], Source] = {}
        for layer, path, before, after in layers:
            for leaf, _value in leaves(after or {}):
                env = _env_names(_lookup(before, leaf)) if before else ()
                sources[leaf] = Source(layer, path, env)
        return cls(sources)

    def with_layer(self, layer: str, updates: Mapping[str, Any]) -> Provenance:
        """Добавить слой точечных правок (``{"agent.max_steps": 5}``)."""
        sources = dict(self._sources)
        for dotted in updates:
            sources[tuple(dotted.split("."))] = Source(layer)
        return Provenance(sources)

    def source(self, dotted: str) -> Source:
        """Источник листа; для секции — источник ближайшего заданного предка."""
        path = tuple(dotted.split("."))
        while path:
            if path in self._sources:
                return self._sources[path]
            path = path[:-1]
        return DEFAULT

    def explicit(self, dotted: str) -> bool:
        """Задано ли значение явно (любым слоем, кроме умолчания)."""
        prefix = tuple(dotted.split("."))
        return (
            any(key[: len(prefix)] == prefix for key in self._sources)
            or self.source(dotted) is not DEFAULT
        )

    def items(self) -> list[tuple[str, Source]]:
        return sorted((".".join(k), v) for k, v in self._sources.items())


__all__ = ["DEFAULT", "Provenance", "Source", "leaves"]
