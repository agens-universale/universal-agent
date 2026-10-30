"""gc-acp собирает снимок набора плагинов после правок из флагов (§8.3)."""

from gc_acp.main import main
from gc_core.settings import runtime_of
from gc_testkit.plugin_store import install_plugin
from gc_testkit.settings import make_settings


def _started(monkeypatch, tmp_path, argv):
    held = {}

    def load(_path, **_kwargs):
        return make_settings(
            {"os_backend": "local", "paths.threads_data_path": str(tmp_path)}
        )

    async def serve(loaded, _protocol_out):
        held["loaded"] = loaded

    monkeypatch.setattr("gc_acp.main.load_settings", load)
    monkeypatch.setattr("gc_acp.main.serve", serve)
    assert main(argv) == 0
    return tuple(p.name for p in runtime_of(held["loaded"]).plugins.plugins)


def test_acp_snapshot_lists_installed(monkeypatch, tmp_path):
    install_plugin(tmp_path, monkeypatch)

    assert _started(monkeypatch, tmp_path, []) == ("inst",)


def test_acp_entry_overrides_applied_before_snapshot(monkeypatch, tmp_path):
    install_plugin(tmp_path, monkeypatch)

    names = _started(
        monkeypatch, tmp_path, ["--plugins.use_installed", "false"]
    )

    assert names == ()
