"""Секции kwargs сохраняют replace/null/пустой mapping во всей стопке."""
from types import SimpleNamespace

import pytest
import yaml

from gc_core.config import load_config_ex
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars
from gc_core.model_router import ModelRouter
from gc_core.utils.connect_backend import create_call_data_completions
from gc_core.utils.llm_chat_client import _build_chat_completion_payload


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path / "home"))
    project = tmp_path / "project"
    (project / ".gc").mkdir(parents=True)
    monkeypatch.chdir(project)
    base = tmp_path / "config.yml"
    base.write_text(yaml.safe_dump({
        "backend": {"completions_url": "https://example.test/v1/chat/completions", "chat_completion_kwargs": {"temperature": 0.1}},
        "chat_completions": {"auxiliary_llm": {"completion_kwargs": {"temperature": 0.2}}},
        "models": {"sample": {
            "main": {"temperature": 0.9, "chat_template_kwargs": {"enable_thinking": True}},
            "auxiliary": {"temperature": 0.8, "chat_template_kwargs": {"enable_thinking": False}},
            "backend": {"request_timeout_sec": 100},
        }},
        "slots": {"default": "sample"},
    }))
    return base, project


@pytest.mark.parametrize("layer", ["project", "profile"])
@pytest.mark.parametrize("section", ["main", "auxiliary"])
@pytest.mark.parametrize("value", [{}, {"top_p": 0.2}, None])
def test_model_kwargs_replace_the_lower_layer(stack, layer, section, value):
    base, project = stack
    overlay = {"models": {"sample": {section: value}}}
    if layer == "project":
        (project / ".gc" / "config.yml").write_text(yaml.safe_dump(overlay))
        cfg, _ = load_config_ex(str(base))
    else:
        profile = project / ".gc" / "profiles" / "test.yml"
        profile.parent.mkdir()
        profile.write_text(yaml.safe_dump(overlay))
        cfg, _ = load_config_ex(str(base), profile_override="test")
    model = ModelRouter(cfg, level_provider=lambda: "default").resolve("main")
    state = SimpleNamespace(messages=[], tools=[], default_model="sample")
    messages = [{"role": "user", "content": "hi"}]
    if section == "main":
        data = create_call_data_completions(messages, state, config_dict=cfg, resolved=model)
    else:
        data = _build_chat_completion_payload(ctx=SimpleNamespace(config_dict=cfg), state=state, messages=messages, max_tokens=32, resolved=model)
    extras = {k: v for k, v in data.items() if k not in {"model", "messages", "tools", "max_completion_tokens", "max_tokens"}}
    assert extras == (value if value is not None else {"temperature": 0.1 if section == "main" else 0.2})
    assert model.backend_overrides["request_timeout_sec"] == 100


def test_legacy_model_kwargs_are_migrated_with_notice(stack):
    base, _ = stack
    doc = yaml.safe_load(base.read_text())
    doc["models"]["sample"].pop("main")
    doc["models"]["sample"]["backend"]["chat_completion_kwargs"] = {"temperature": 0.7}
    base.write_text(yaml.safe_dump(doc))
    cfg, _ = load_config_ex(str(base))
    model = ModelRouter(cfg, level_provider=lambda: "default").resolve("main")
    assert model.main == {"temperature": 0.7}
    assert any("chat_completion_kwargs" in n and "main" in n for n in cfg["compat_notices"])
    assert "chat_completion_kwargs" in base.read_text()


def test_explicit_main_wins_over_legacy_kwargs(stack):
    base, _ = stack
    doc = yaml.safe_load(base.read_text())
    doc["models"]["sample"]["main"] = {}
    doc["models"]["sample"]["backend"]["chat_completion_kwargs"] = {"temperature": 0.7}
    base.write_text(yaml.safe_dump(doc))
    cfg, _ = load_config_ex(str(base))
    assert ModelRouter(cfg, level_provider=lambda: "default").resolve("main").main == {}
    assert any("chat_completion_kwargs" in n for n in cfg["compat_notices"])


def test_model_kwargs_origin_uses_replacement(stack):
    from gc_core.config_origin import origins

    base, project = stack
    (project / '.gc' / 'config.yml').write_text(
        yaml.safe_dump({'models': {'sample': {'main': {}}}})
    )
    origin = origins(str(base), ['models.sample.main'])['models.sample.main']
    assert origin.effective == {}
    assert origin.overlay_kind == 'project'


def test_legacy_overlay_origin_matches_runtime(stack):
    from gc_core.config_origin import origins

    base, project = stack
    (project / ".gc" / "config.yml").write_text(yaml.safe_dump({"models": {"sample": {
        "backend": {"chat_completion_kwargs": {"top_p": 0.2}},
    }}}))
    origin = origins(str(base), ["models.sample.main"])["models.sample.main"]
    assert origin.effective == {"top_p": 0.2}
