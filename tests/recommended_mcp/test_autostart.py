"""Тесты автозапуска MCP-daemon'ов по конфигу агента."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from gc_core.mcp_runtime.manifest_resolve import resolve_agent_manifest_path
from recommended_mcp.autostart import ensure_daemons_for_agent, wait_daemon_url_ready


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
    from gc_core.config_modules import env_vars

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
        "gc_core.mcp_runtime.manifest_resolve.resolve_agent_manifest_path",
        return_value=manifest,
    ):
        with patch("recommended_mcp.autostart.run_entry", return_value=True) as run_entry:
            with patch("recommended_mcp.autostart.wait_daemon_url_ready", return_value=True) as wait_ready:
                ensure_daemons_for_agent(config_dict)
    run_entry.assert_called_once()
    assert run_entry.call_args.args[0].name == "science"
    wait_ready.assert_called_once_with("http://127.0.0.1:8765/mcp")


def test_ensure_skips_ready_wait_when_daemon_already_running(tmp_path: Path) -> None:
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
        """,
    )
    config_dict = {
        "mcp-servers": {
            "science": {"transport": "stream_http", "url": "http://127.0.0.1:8765/mcp"},
        },
    }
    with patch(
        "gc_core.mcp_runtime.manifest_resolve.resolve_agent_manifest_path",
        return_value=manifest,
    ):
        with patch("recommended_mcp.autostart.run_entry", return_value=False):
            with patch("recommended_mcp.autostart.wait_daemon_url_ready") as wait_ready:
                ensure_daemons_for_agent(config_dict)
    wait_ready.assert_not_called()


def test_wait_daemon_url_ready_accepts_open_port() -> None:
    import socket
    from threading import Thread

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    host, port = sock.getsockname()

    def _serve() -> None:
        conn, _ = sock.accept()
        conn.close()

    thread = Thread(target=_serve, daemon=True)
    thread.start()
    try:
        assert wait_daemon_url_ready(f"http://{host}:{port}/mcp", timeout=2.0) is True
    finally:
        sock.close()
