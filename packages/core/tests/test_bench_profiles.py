# tests/test_bench_profiles.py
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml
from gc_testkit.paths import HARBOR, REPO

PROFILES = HARBOR / "profiles"
BENCH_CONFIGS = REPO / "integrations" / "bench_configs"


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
        "backend.completions_url": "http://test/v1/chat/completions",
        **gc_cli_config,
    }
    a = mod.GcCliExternal(
        logs_dir=Path("/tmp"), model_name="openai/m", gc_cli_config=cfg
    )
    loaded = a._build_settings()
    return loaded.settings, parse_summary_trigger_chars(loaded)


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
        assert c.agent.use_local_subagents is subagents, fname
        assert parsed_trigger == trigger, fname
        # common faithful keys present in every profile
        assert c.agent.max_steps == 1000, fname
        assert c.agent.no_tool_call_max_count == 5, fname
        assert c.agent.strict_turn_finish_validation is True, fname
        assert "reason" in c.agent.inject_tool_fields, fname
        assert (
            c.backend.chat_completion_kwargs["separate_reasoning"] is True
        ), fname


def _gc_cli_configs(doc):
    """gc_cli_config harbor-прогона: плоский профиль (``agent_kwargs``) или
    Hydra-обёртка флита (``harbor_job.agents[i].kwargs``)."""
    if "gc_cli_config" in (doc.get("agent_kwargs") or {}):
        yield doc["agent_kwargs"]["gc_cli_config"]
    for agent in (doc.get("harbor_job") or {}).get("agents") or []:
        if "gc_cli_config" in (agent.get("kwargs") or {}):
            yield agent["kwargs"]["gc_cli_config"]


#: Определения harbor-прогонов в bench_configs (конфиги агента `0*.yml` судит
#: tests/test_bench_configs_resolve.py). Страж там их только перечисляет, и
#: профили uag_pre_v0.4_s200k_* дожили до develop с плоскими ключами
#: (`backend_api_mode`), которые схема отвергает с 3f4ea089b.
HARBOR_RUNS = sorted(
    p
    for p in BENCH_CONFIGS.glob("*.yml")
    if list(_gc_cli_configs(yaml.safe_load(p.read_text()) or {}))
)


def test_harbor_runs_found():
    """Страховка от вакуума: без файлов параметризация ниже пуста и зелена."""
    assert len(HARBOR_RUNS) >= 6, [p.name for p in HARBOR_RUNS]


@pytest.mark.parametrize("path", HARBOR_RUNS, ids=lambda p: p.stem)
def test_bench_config_harbor_run_builds(path):
    """Каждый gc_cli_config harbor-прогона собирается адаптером (схема develop)."""
    for gcc in _gc_cli_configs(yaml.safe_load(path.read_text())):
        _build(gcc)


#: thinking в основном запросе, strip_think — три варианта эксперимента 2026-10-08.
UAG_EXPECT = {
    "uag_pre_v0.4_s200k_no_think.yml": (False, True),
    "uag_pre_v0.4_s200k_think_strip.yml": (True, True),
    "uag_pre_v0.4_s200k_think_keep.yml": (True, False),
}


@pytest.mark.parametrize("fname", sorted(UAG_EXPECT))
def test_uag_s200k_profiles_keep_their_setup(fname):
    thinking, strip = UAG_EXPECT[fname]
    doc = yaml.safe_load((BENCH_CONFIGS / fname).read_text())
    (gcc,) = _gc_cli_configs(doc)
    c, _ = _build(gcc)
    kwargs = c.backend.chat_completion_kwargs
    assert kwargs["chat_template_kwargs"]["enable_thinking"] is thinking
    assert c.strip_tags_projection.strip_think is strip
    assert c.summary.chat_completions_summary_trigger_tokens == 200000
    assert c.summary.chat_completions_summary_trigger_chars == 0
    assert c.agent.harbor_exec_init == "bashrc"
