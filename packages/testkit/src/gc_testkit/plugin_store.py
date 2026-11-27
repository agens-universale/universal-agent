"""Установленный плагин в изолированном хранилище теста.

Фронтенды проверяют, что снимок набора плагинов собран после их
оверрайдов (``plugins.use_installed: false`` выключает установленные);
для этого им нужен один настоящий установленный плагин, не из ``~/.gc``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def install_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "inst"
) -> str:
    """Поставить плагин ``name`` в хранилище под ``tmp_path``; имя плагина."""
    from gc_core.plugins import catalog, installed

    monkeypatch.setattr(installed, "installed_dir", lambda: tmp_path / "inst")
    catalog.reset_plugin_cache()
    root = tmp_path / "src" / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
    )
    skill = root / "skills" / "s"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    installed.install(str(root))
    return name
