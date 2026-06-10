"""Тесты автозапуска MCP-daemon'ов по конфигу агента."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from mcp_runtime.manifest_resolve import resolve_agent_manifest_path
from recommended_mcp.autostart import ensure_daemons_for_agent


def _write_manifest(tmp_path: Path, content: str) -> Path:
    manifest = tmp_path / "recommended_mcp" / "config.local.yml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(dedent(content), encoding="utf-8")
    return manifest


def test_resolve_manifest_from_gc_config(tmp_path: Path, monkeypatch) -> None:
    gc_config = tmp_path / "gc-config"
    manifest = gc_config / "recommended_mcp" / "config.local.yml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("mcps: []\n", encoding="utf-8")
    from config_modules import env_vars

    monkeypatch.setattr(env_vars, "env", env_vars.EnvVars(gc_config_dir=gc_config))
    path = resolve_agent_manifest_path({})
    assert path == manifest.resolve()


def test_ensure_starts_only_configured_http_mcp(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path,
        """
        mcps:
          - name: science
            path: ./science
            install: {type: source, source: ../external/science-mcp}
            run:
              type: command
              command: science-mcp
              args: ["--transport", "http"]
          - name: duckduckgo_search
            path: ./duckduckgo_search
            install: {type: pypi, package: ddgs-mcp==0.1.0}
        """,
    )
    config_dict = {
        "mcp-servers": {
            "science": {"transport": "stream_http", "url": "http://127.0.0.1:8765/mcp"},
            "duckduckgo_search": {"transport": "stdio", "command": "ddgs-mcp"},
        },
    }
    with patch(
        "mcp_runtime.manifest_resolve.resolve_agent_manifest_path",
        return_value=manifest,
    ):
        with patch("recommended_mcp.autostart.run_entry") as run_entry:
            ensure_daemons_for_agent(config_dict)
    run_entry.assert_called_once()
    assert run_entry.call_args.args[0].name == "science"
