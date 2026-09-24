# tests/test_bench_profiles.py
import importlib.util
import sys
from pathlib import Path

import yaml
from gc_testkit.paths import HARBOR

PROFILES = HARBOR / "profiles"


def _build(gc_cli_config):
    # tests/harbor/ (a test-fixture package) shadows the real harbor framework.
    # Reorder sys.path so the real site-packages entry comes first, then evict
    # cached shadow modules — mirrors the same fix in test_harbor_adapter_bundled_paths.py.
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
    for key in list(sys.modules.keys()):
        if key == "harbor" or key.startswith("harbor."):
            del sys.modules[key]
    # Адаптер тянет соседей как `from adapters.…`, поэтому на sys.path нужен
    # каталог harbor/, а не исходники: единого <repo>/src больше нет.
    sys.path.insert(0, str(HARBOR))
    spec = importlib.util.spec_from_file_location(
        "adapters.gc_cli_external", HARBOR / "adapters" / "gc_cli_external.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from gc_core.summarize.config import parse_summary_trigger_chars

    # completions_url: the adapter has no hardcoded default (fail-fast) — supply one.
    cfg = {
        "completions_url": "http://test/v1/chat/completions",
        **gc_cli_config,
    }
    a = mod.GcCliExternal(
        logs_dir=Path("/tmp"), model_name="openai/m", gc_cli_config=cfg
    )
    c = a._build_config()
    return c, parse_summary_trigger_chars(c)


EXPECT = {
    "bench_01_no_summary_no_subagents.yaml": (False, 0),
    "bench_02_summary_no_subagents.yaml": (False, 150000),
    "bench_03_no_summary_subagents.yaml": (True, 0),
    "bench_04_summary_subagents.yaml": (True, 150000),
}


def test_each_profile_toggles_match():
    for fname, (subagents, trigger) in EXPECT.items():
        prof = yaml.safe_load((PROFILES / fname).read_text())
        gcc = prof["agent_kwargs"]["gc_cli_config"]
        c, parsed_trigger = _build(gcc)
        assert c["use_local_subagents"] is subagents, fname
        assert parsed_trigger == trigger, fname
        # common faithful keys present in every profile
        assert c["max_steps"] == 1000, fname
        assert c["no_tool_call_max_count"] == 5, fname
        assert c["strict_turn_finish_validation"] is True, fname
        assert "reason" in c["inject_tool_fields"], fname
        assert c["chat_completion_kwargs"]["separate_reasoning"] is True, fname
