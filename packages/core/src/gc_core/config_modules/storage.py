"""Yaml ``storage`` — где живут треды и ресурсы агента.

Ядро знает только схему: тип и параметры каждой части. Открывает хранилища
фронтенд (``gc_stores.open_configured``): ядро от реализаций не зависит.
Нет секции или части — файлы, как раньше.

Параметры части — словарь без схемы ядра (``dsn``, ``path``, ``url``,
``bucket``…): их проверяет реализация. Секреты в нём скрываются по имени
ключа (``field_meta.redact_free_form``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

FILES = "files"
PARTS = ("threads", "agent")


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Хранилище тредов: ``{type: postgres, dsn: …}``; пусто — файлы.
    threads: dict[str, Any] = Field(default_factory=dict)
    #: Хранилище агента: ``{type: …, agent_id: …, …}``; пусто — файлы.
    agent: dict[str, Any] = Field(default_factory=dict)
    #: ``True`` — при открытии применить недостающие миграции (нужны права
    #: на DDL); иначе — только сверка версии схемы.
    ensure_schema: bool = False

    @field_validator("threads", "agent")
    @classmethod
    def _part(cls, value: dict[str, Any]) -> dict[str, Any]:
        kind = value.get("type", FILES)
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError(
                "type должен быть непустой строкой: files, postgres, mysql, "
                "sqlite, redis, s3 или пакет.модуль:фабрика"
            )
        return value

    def part(self, name: str) -> tuple[str, dict[str, Any]]:
        """``(тип, параметры)`` части ``threads`` или ``agent``."""
        if name not in PARTS:
            raise ValueError(f"нет части хранилища {name!r}")
        raw = dict(getattr(self, name))
        kind = str(raw.pop("type", FILES)).strip()
        return kind, raw

    def is_files(self) -> bool:
        return all(self.part(name)[0] == FILES for name in PARTS)
