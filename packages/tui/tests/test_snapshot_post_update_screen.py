"""Пост-апдейт — основной вид: факт обновления + чеклист «осталось обновить».

Экран поднимается тем же путём, что в проде: `_upgrade_info` (его ставит
`tui/__init__.py` из `upgrade_gate.detect_upgrade`) → `on_mount` →
`TuiApp._show_post_update_if_needed`.

Оба пункта чеклиста приходят по своим настоящим путям: config.yml — из файла
(`config_sync.needs_sync` сверяет его с пакетным шаблоном, а минимальный файл
шаблону правда отстаёт), MCP — из фонового скана версий. Сам скан к сети и
subprocess не подпускаем: `installed_state` в проде опрашивает установленные
пакеты, и на разных машинах ответ разный.
"""

import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from gc_testkit.tui_support import tui_settings, wait_until


def _write_config_file(tmp_path):
    """Минимальный config.yml — ему правда есть что перенести из шаблона текущей
    версии, поэтому пункт «Обновить config.yml» появляется по-настоящему."""
    p = tmp_path / "config.yml"
    p.write_text(
        "backend:\n"
        '  completions_url: "https://api.example-host/v1/chat/completions"\n'
        "agent:\n"
        '  model: "CodeAgent"\n',
        encoding="utf-8",
    )
    return p


def _app(tmp_path, monkeypatch):
    import recommended_mcp.runtime.cli as mcp_cli
    from gc_core import upgrade_gate
    from gc_core.config_modules import env_vars
    from gc_tui.app import TuiApp

    # GC_HOME → tmp: там живут managed-конфиг (подключённые MCP) и стамп апгрейда.
    monkeypatch.setattr(
        env_vars, "env", env_vars.EnvVars(gc_home=tmp_path / "gc")
    )
    monkeypatch.setattr(
        mcp_cli, "wired_names", lambda config=None: ["office", "duckduckgo"]
    )
    monkeypatch.setattr(
        mcp_cli, "load_manifest_entries", lambda config=None: ([], None)
    )
    # Версии установленных MCP — без опроса пакетов: office отстал, duckduckgo нет
    # (в строку пункта попадают только те, кому правда есть что обновить).
    monkeypatch.setattr(
        mcp_cli,
        "installed_state",
        lambda name, config=None, manifest=None: {
            "installed": True,
            "version": "0.1.0",
            "update": "0.3.1" if name == "office" else None,
        },
    )
    monkeypatch.setattr(TuiApp, "_start_auto_update_check", lambda self: None)

    cfg_file = _write_config_file(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"model": "CodeAgent", "backend_api_mode": "chat_completions"}
        ),
    )
    app.set_config_path(str(cfg_file))
    app._upgrade_info = upgrade_gate.UpgradeInfo(
        old_version="0.3.0", new_version="0.4.2"
    )
    return app


def test_post_update_snapshot(snap_compare, tmp_path, monkeypatch):
    """Оба пункта отмечены по умолчанию, курсор на первом; рамка обнимает
    карточку, полоса подсказок — сразу под её нижней гранью."""
    app = _app(tmp_path, monkeypatch)

    async def setup(pilot):
        from gc_tui.screens.post_update import PostUpdateScreen

        await wait_until(
            pilot,
            lambda: isinstance(pilot.app.screen, PostUpdateScreen),
            "post-update screen pushed",
        )
        # Пункт MCP дорисовывается фоновым сканом версий — без ожидания снимок
        # фиксировал бы карточку с одним config.yml.
        await wait_until(
            pilot, lambda: "mcp" in pilot.app.screen._items, "mcp scan applied"
        )
        await pilot.pause()
        await pilot.pause()

    assert snap_compare(app, terminal_size=(100, 30), run_before=setup)
