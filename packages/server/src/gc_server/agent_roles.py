"""Роли поверхностей при хранилище агента в базе.

Один агент на сервер (``storage.agent.agent_id``): промпты, скиллы,
субагенты и память проекта — общие, как на файлах. Роль поверхности —
вариант L0 и специнструкций внутри агента (``L0@<роль>``,
``special_instructions@<роль>``): ``<поверхность>.role``, по умолчанию имя
поверхности. Поверхность может вести и свой агент со своей памятью —
``<поверхность>.agent_id`` (своё подключение к той же базе).

Старт проверяет, что роли поверхностей на месте: молча подняться без них
значило бы отвечать без системных инструкций роли.
"""

from __future__ import annotations

from typing import Any

from gc_core.agent_store import Prompt
from gc_core.agent_store.roles import role_variant
from gc_core.config_modules.storage import FILES
from gc_core.settings import LoadedSettings, settings_of

from gc_server.config import ServerConfig, ServerConfigError

#: Поверхности с ролью: песочница swarm ведёт агента на своих файлах.
ROLE_SURFACES = ("webhook", "chat", "a2a")
#: Вариант специнструкций фасада Chat Completions: ``<роль>.completions``.
COMPLETIONS = "completions"

_SYNC_HINT = (
    "gc-stores sync <каталог агента> --config <config.yml> ({files}) или "
    "gc-stores sync --with-defaults --config <config.yml> (роли по умолчанию: "
    "webhook, chat)"
)


def agent_in_store(settings: LoadedSettings) -> bool:
    """Хранилище агента — не файлы (база или своя фабрика)."""
    return settings_of(settings).storage.part("agent")[0] != FILES


def surface_role(cfg: ServerConfig, name: str) -> str:
    return getattr(cfg, name).role or name


async def aopen_surface_agents(
    base: LoadedSettings, cfg: ServerConfig, names: list[str]
) -> dict[str, Any]:
    """Свои агенты поверхностей (``<поверхность>.agent_id``), отличные от
    общего. На файлах свой агент не задаётся — ошибка конфигурации."""
    own = {
        name: getattr(cfg, name).agent_id
        for name in names
        if name in ROLE_SURFACES and getattr(cfg, name).agent_id
    }
    if not own:
        return {}
    if not agent_in_store(base):
        first = sorted(own)[0]
        raise ServerConfigError(
            f"{first}.agent_id: свой агент поверхности — только с хранилищем "
            "агента в базе (storage.agent в config.yml); на файлах ресурсы "
            "агента задают пути"
        )
    from gc_stores.configured import aopen_agent

    common = settings_of(base).storage.part("agent")[1].get("agent_id")
    opened: dict[str, Any] = {}
    try:
        for name, agent_id in own.items():
            if agent_id != common:
                opened[name] = await aopen_agent(base, agent_id)
    except BaseException:
        for store in opened.values():
            await store.close()
        raise
    return opened


def _required(
    name: str, cfg: ServerConfig, role: str
) -> list[tuple[str, str, str]]:
    """Что нужно роли: (вид, ресурс в хранилище, файл раскладки sync в
    ``roles/``). Вебхук без роли (``webhook.agent_md_path: null``) и a2a
    (на файлах роли нет по умолчанию) — ничего."""
    section = getattr(cfg, name)
    if name == "webhook":
        if (
            "agent_md_path" in section.model_fields_set
            and section.agent_md_path is None
        ):
            return []
        return [("instructions", role_variant("L0", role), f"{role}/L0.md")]
    if name == "chat":
        special = Prompt.SPECIAL_INSTRUCTIONS.value
        return [
            (
                "prompt",
                role_variant(special, variant),
                f"{variant}/{special}.md",
            )
            for variant in (role, f"{role}.{COMPLETIONS}")
        ]
    return []


async def acheck_roles(runtime: Any, names: list[str]) -> None:
    """Роли поверхностей ``names`` на месте; иначе ``ServerConfigError`` с
    подсказкой ``gc-stores sync``. На файлах — ничего не проверяется."""
    if not agent_in_store(runtime.base):
        return
    cfg = runtime.config
    for name in names:
        if name not in ROLE_SURFACES:
            continue
        role = surface_role(cfg, name)
        _reject_role_path(cfg, name, role)
        store = runtime.store_kwargs(name)["agent_store"]
        for kind, resource, file in _required(name, cfg, role):
            if await store.read(kind, resource) is None:
                raise ServerConfigError(
                    f"{name}: роли {role!r} нет в хранилище агента "
                    f"({store.label(kind, resource)}) — "
                    + _SYNC_HINT.format(files=f"roles/{file}")
                )


def _reject_role_path(cfg: ServerConfig, name: str, role: str) -> None:
    section = getattr(cfg, name)
    if section.agent_md_path and "agent_md_path" in section.model_fields_set:
        raise ServerConfigError(
            f"{name}.agent_md_path: с хранилищем агента в базе роль "
            f"поверхности — ресурс L0@{role}: уберите путь и положите роль — "
            + _SYNC_HINT.format(files=f"roles/{role}/L0.md")
        )


__all__ = [
    "COMPLETIONS",
    "ROLE_SURFACES",
    "acheck_roles",
    "agent_in_store",
    "aopen_surface_agents",
    "surface_role",
]
