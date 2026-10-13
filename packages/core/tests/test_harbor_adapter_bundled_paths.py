# tests/test_harbor_adapter_bundled_paths.py
# Requires the `harbor` framework (dependency-group `test`).
import importlib.util
import os
import sys
from pathlib import Path

import pytest
from gc_testkit.paths import HARBOR

_TEST_URL = "http://test-backend/v1/chat/completions"


def _load_adapter():
    # tests/harbor/ (a test-fixture package with __init__.py) is added to sys.path by
    # pytest before site-packages, shadowing the real harbor framework.  Ensure the
    # harbor venv site-packages directory appears first so the adapter's framework
    # imports ("from harbor.agents.base import BaseAgent" etc.) resolve correctly.
    _harbor_sp = next(
        (
            p
            for p in sys.path
            if (Path(p) / "harbor" / "agents" / "base.py").exists()
        ),
        None,
    )
    if _harbor_sp:
        sys.path.remove(_harbor_sp)
        sys.path.insert(0, _harbor_sp)
    # Evict any cached shadow from tests/harbor/ so the re-inserted site-packages wins.
    for key in list(sys.modules.keys()):
        if key == "harbor" or key.startswith("harbor."):
            del sys.modules[key]
    # Import as harbor does at runtime (`adapters.gc_cli_external:GcCliExternal`), so the
    # adapter's own `from adapters.gc_cli_config import ...` resolves.
    sys.path.insert(0, str(HARBOR))
    spec = importlib.util.spec_from_file_location(
        "adapters.gc_cli_external", HARBOR / "adapters" / "gc_cli_external.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.GcCliExternal


def _agent(GcCliExternal, **gc_cli_config):
    cfg = {"backend.completions_url": _TEST_URL, **gc_cli_config}
    return GcCliExternal(
        logs_dir=Path("/tmp"), model_name="openai/m", gc_cli_config=cfg
    )


def test_adapter_points_dependency_paths_to_existing_files(monkeypatch):
    monkeypatch.delenv("GC_CLI_BACKEND_URL", raising=False)
    GcCliExternal = _load_adapter()
    a = _agent(GcCliExternal)
    settings = a._build_settings().settings
    paths = settings.paths
    # system prompt is the BASE prompt, and exists
    assert (
        Path(paths.chat_completions_system_prompt_path)
        .as_posix()
        .endswith("prompts/chat_completions_system_prompt.md")
    )
    assert os.path.isfile(paths.chat_completions_system_prompt_path)
    # skills dir set + exists
    assert os.path.isdir(paths.skills_data_path)
    # summary sub-prompt paths exist
    summary = settings.summary
    for k in (
        "chat_completions_summary_prompt_path",
        "chat_completions_memory_patch_replace_prompt_path",
    ):
        assert os.path.isfile(getattr(summary, k)), k
    # intent_guard prompt path is bundled + exists (activates the finish gate in the
    # image; without it the gate fail-opens because its default path is source-only)
    assert (
        Path(paths.chat_completions_intent_guard_prompt_path)
        .as_posix()
        .endswith("prompts/chat_completions_intent_guard_prompt.md")
    )
    assert os.path.isfile(paths.chat_completions_intent_guard_prompt_path)
    assert paths.chat_completions_routing_pair_judge_prompt_path.endswith(
        "prompts/chat_completions_routing_pair_judge_prompt.md")
    assert os.path.isfile(paths.chat_completions_routing_pair_judge_prompt_path)


def test_security_guards_pinned_off_for_benches(monkeypatch):
    # Fuse: if a release flips guard defaults to enabled/ask, benches must still get them
    # explicitly OFF. The rest of each guard's schema keys ride along from the defaults —
    # only the switch is pinned.
    monkeypatch.delenv("GC_CLI_BACKEND_URL", raising=False)
    GcCliExternal = _load_adapter()
    security = _agent(GcCliExternal)._build_settings().settings.security
    assert security.read_guard.enabled is False
    assert security.write_guard.enabled is False
    assert security.url_policy.enabled is False
    assert security.command_moderation.mode == "allow"


def test_no_backend_url_fails_fast(monkeypatch):
    # No api_base / env / profile url -> RuntimeError, no silent default.
    monkeypatch.delenv("GC_CLI_BACKEND_URL", raising=False)
    GcCliExternal = _load_adapter()
    a = GcCliExternal(logs_dir=Path("/tmp"), model_name="openai/m")
    with pytest.raises(RuntimeError, match="No LLM backend configured"):
        a._build_settings()


def test_no_model_fails_fast(monkeypatch):
    monkeypatch.delenv("GC_CLI_BACKEND_URL", raising=False)
    monkeypatch.delenv("GC_CLI_MODEL", raising=False)
    GcCliExternal = _load_adapter()
    a = GcCliExternal(
        logs_dir=Path("/tmp"),
        model_name=None,
        gc_cli_config={"backend.completions_url": _TEST_URL},
    )
    with pytest.raises(RuntimeError, match="No model configured"):
        a._build_settings()


def test_url_and_model_sources_resolve(monkeypatch):
    GcCliExternal = _load_adapter()
    # env beats profile for the URL (preserves the old force-override precedence)
    monkeypatch.setenv(
        "GC_CLI_BACKEND_URL", "http://env-backend/v1/chat/completions"
    )
    a = _agent(GcCliExternal)
    assert (
        a._build_settings().settings.backend.completions_url
        == "http://env-backend/v1/chat/completions"
    )
    # profile is used when env is absent
    monkeypatch.delenv("GC_CLI_BACKEND_URL", raising=False)
    assert (
        _agent(GcCliExternal)
        ._build_settings()
        .settings.backend.completions_url
        == _TEST_URL
    )
    # model comes from profile when the runner passed none
    b = GcCliExternal(
        logs_dir=Path("/tmp"),
        model_name=None,
        gc_cli_config={
            "backend.completions_url": _TEST_URL,
            "agent.model": "openai/VendorX/M1",
        },
    )
    assert b._build_settings().settings.agent.model == "VendorX/M1"


def test_profile_overrides_still_win(monkeypatch):
    monkeypatch.delenv("GC_CLI_BACKEND_URL", raising=False)
    GcCliExternal = _load_adapter()
    a = _agent(
        GcCliExternal,
        **{
            "agent.use_local_subagents": True,
            "summary": {"chat_completions_summary_trigger_chars": 150000},
        },
    )
    c = a._build_settings().settings
    assert c.agent.use_local_subagents is True
    # merged: profile trigger + adapter-provided summary prompt path coexist
    assert c.summary.chat_completions_summary_trigger_chars == 150000
    assert os.path.isfile(c.summary.chat_completions_summary_prompt_path)
