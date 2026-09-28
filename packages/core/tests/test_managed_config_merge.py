from gc_core import config
from gc_core.config_modules import env_vars


def test_managed_local_config_merges_mcp_servers(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "gc"
    cfg_dir.mkdir(parents=True)
    # managed overlay добавляет один mcp-сервер
    (cfg_dir / "config.local.yml").write_text(
        "mcp:\n  servers:\n    science:\n      transport: stream_http\n"
        "      url: http://127.0.0.1:8765/mcp\n      server-name: science\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        env_vars,
        "gc_paths",
        lambda: {
            "config": cfg_dir,
            "state": tmp_path / "s",
            "data": tmp_path / "d",
            "cache": tmp_path / "c",
        },
    )
    monkeypatch.setattr(env_vars, "project_slug", lambda: "p")
    base = tmp_path / "base.yml"
    base.write_text(
        "backend:\n  api_mode: chat_completions\n", encoding="utf-8"
    )

    cfg = config.load_config(str(base))
    assert "science" in cfg["mcp-servers"]
    assert cfg["mcp-servers"]["science"]["transport"] == "stream_http"
