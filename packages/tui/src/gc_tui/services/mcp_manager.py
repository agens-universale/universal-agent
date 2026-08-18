"""Инъецируемый фасад управления MCP для TUI (как settings_probe/model_picker):
мгновенные строки из manifest ∪ wired ∪ ручные; медленные installed-статусы —
отдельным методом (фоновый воркер); операции делегируют в mcp_runtime.cli."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from recommended_mcp.runtime import cli as _cli


@dataclass(frozen=True)
class McpRow:
    name: str
    wired: bool
    manual: bool
    description: str = ""


@dataclass(frozen=True)
class McpState:
    installed: Optional[bool]
    version: Optional[str]
    update: Optional[str]


class McpManager:
    def __init__(self, *, config: Optional[str] = None, user_config: Optional[dict] = None) -> None:
        self._config = config
        self._user_config = user_config
        self._manifest = None       # (entries, root), кэш на сессию: манифест статичен

    def rows(self) -> list:
        wired = set(_cli.wired_names(self._config))
        installable = _cli.installable_names(self._config)
        descs = _cli.descriptions(self._config)
        managed_names = list(dict.fromkeys(installable + list(wired)))
        managed = [McpRow(n, wired=n in wired, manual=False, description=descs.get(n, ""))
                   for n in managed_names]
        # «Ручные» = серверы из user config.yml, которых НЕТ среди управляемых
        # (manifest ∪ wired). Иначе подключённые дублируются: рантайм-ключ
        # mcp-servers — это merge config.yml + config.local.yml (куда gc-mcp пишет wired).
        seen = set(managed_names)
        manual = [McpRow(n, wired=False, manual=True)
                  for n in _cli.manual_server_names(self._user_config) if n not in seen]
        return managed + manual

    def state(self, name: str) -> McpState:
        if self._manifest is None:      # грузим манифест один раз, а не per-строка воркером
            self._manifest = _cli.load_manifest_entries(self._config)
        st = _cli.installed_state(name, self._config, manifest=self._manifest)
        return McpState(st["installed"], st["version"], st["update"])

    def wire(self, name: str) -> bool:
        return _cli.wire(name, self._config)

    def unwire(self, name: str) -> bool:
        return _cli.unwire(name)

    def install(self, name: str) -> int:
        return _cli.install(name, self._config)

    def remove(self, name: str) -> int:
        return _cli.remove(name, self._config)
