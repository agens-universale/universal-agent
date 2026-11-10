"""Субагенты плагинов под воркером: набор хоста воркера в ребёнка не
попадает."""

import json

from gc_core.config_modules.root import AppConfig
from gc_core.context_preparation import settings_from_snapshot
from gc_core.plugins import catalog, installed
from gc_core.plugins.catalog import with_plugin_snapshot
from gc_core.settings import LoadedSettings, RuntimeEnv, runtime_of
from gc_core.tools.subagents.registry import resolve_subagent_definitions
from gc_core_temporalio.public_config import public_config_snapshot

ROLE = "subagent_p_r"


def _plugin(tmp_path):
    root = tmp_path / "p"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p", "version": "1.0.0"}), encoding="utf-8"
    )
    (root / "agents").mkdir()
    (root / "agents" / "r.md").write_text(
        "---\nname: r\ndescription: d\ntools: Read\n---\nРоль.",
        encoding="utf-8",
    )
    return root


def test_worker_host_plugin_role_not_in_child_definitions(
    tmp_path, monkeypatch
):
    root = _plugin(tmp_path)
    monkeypatch.setattr(installed, "installed_dir", lambda: tmp_path / "i")
    monkeypatch.setattr(
        installed, "enabled_paths", lambda root_dir=None: [str(root)]
    )
    catalog.reset_plugin_cache()
    loaded = LoadedSettings(
        settings=AppConfig.model_validate(
            {"agent": {"use_local_subagents": True}}
        )
    )
    # Контроль: со снимком хоста субагент есть — тест не пустой.
    host = with_plugin_snapshot(loaded, clone=False)
    assert ROLE in resolve_subagent_definitions(host)

    snap = public_config_snapshot(host)
    worker = settings_from_snapshot(snap.settings, snap.runtime)

    assert runtime_of(worker).plugins.roles == ()
    assert ROLE not in resolve_subagent_definitions(worker)


def test_mcp_allow_round_trips_through_public_snapshot():
    runtime = RuntimeEnv(
        subagent_tools_allow=(),
        subagent_mcp_allow=(("p_gh", "create"), ("p_db", None)),
    )
    snap = public_config_snapshot(
        LoadedSettings(settings=AppConfig(), runtime=runtime)
    )
    assert snap.runtime["subagent_mcp_allow"] == [
        ["p_gh", "create"],
        ["p_db", None],
    ]
    restored = settings_from_snapshot(snap.settings, snap.runtime)
    assert runtime_of(restored).subagent_mcp_allow == (
        ("p_gh", "create"),
        ("p_db", None),
    )
