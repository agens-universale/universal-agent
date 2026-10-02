"""Per-alias main/auxiliary: replace, не deep-merge и не чужой диалект."""

from types import SimpleNamespace

from gc_core.config import flatten_app_config
from gc_core.config_modules.model_registry import (
    parse_completion_kwargs_text,
    parse_model_backend_text,
)
from gc_core.config_modules.root import AppConfig
from gc_core.model_router import ModelRouter
from gc_core.utils import llm_chat_client
from gc_core.utils.connect_backend import create_call_data_completions
from gc_core.utils.llm_chat_client import _build_chat_completion_payload
from gc_testkit import as_coro, run_coro


def _cfg(raw: dict) -> dict:
    return flatten_app_config(AppConfig.model_validate(raw))


def _router(cfg: dict, level: str = "default") -> ModelRouter:
    return ModelRouter(cfg, level_provider=lambda: level)


def _main_payload(cfg: dict, resolved) -> dict:
    return create_call_data_completions(
        [{"role": "user", "content": "hi"}],
        SimpleNamespace(messages=[], tools=[], default_model="legacy"),
        config_dict=cfg,
        resolved=resolved,
    )


def _aux_payload(cfg: dict, consumer: str, *, level: str = "default") -> dict:
    router = _router(cfg, level)
    return _build_chat_completion_payload(
        ctx=SimpleNamespace(config_dict=cfg),
        state=SimpleNamespace(default_model="legacy"),
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=32,
        resolved=router.resolve(consumer),
    )


_PROFILE = {
    "backend": {
        "chat_completion_kwargs": {
            "temperature": 0.1,
            "top_p": 0.9,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    },
    "models": {
        "glm-5.3-flash-ctx262k": {
            "model": "glm-5.3-flash-ctx262k",
            "url": "http://glm/v1/chat/completions",
            "token": "",
            "main": {
                "temperature": 1.0,
                "chat_template_kwargs": {
                    "thinking": True,
                    "reasoning_effort": "high",
                },
            },
            "auxiliary": {
                "temperature": 0.2,
                "chat_template_kwargs": {
                    "thinking": False,
                    "reasoning_effort": "none",
                },
            },
        },
        "gc_v2": {
            "model": "gc_v2",
            "url": "http://gc/v1/chat/completions",
            "token": "",
            "main": {"temperature": 0.5, "tool_choice": "auto"},
            "auxiliary": {"temperature": 0.3, "tool_choice": "auto"},
        },
        "qwen_38_mlsp": {
            "model": "qwen_38_mlsp",
            "url": "http://qwen/v1/chat/completions",
            "token": "",
            "main": {
                "temperature": 0.4,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            "auxiliary": {
                "temperature": 0.4,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        },
        "quiet": {
            "model": "quiet",
            "url": "http://quiet/v1/chat/completions",
            "token": "",
            "main": {
                "temperature": 0.8,
                "chat_template_kwargs": {"enable_thinking": True},
            },
            "auxiliary": {},
        },
        "same-id-a": {
            "model": "shared-id",
            "url": "http://a/v1/chat/completions",
            "token": "",
            "main": {"temperature": 0.11},
        },
        "same-id-b": {
            "model": "shared-id",
            "url": "http://b/v1/chat/completions",
            "token": "",
            "main": {"temperature": 0.22},
        },
    },
    "slots": {
        "light": "qwen_38_mlsp",
        "default": "gc_v2",
        "max": "glm-5.3-flash-ctx262k",
    },
    "assign": {
        "summary": "gc_v2",
        "finish_gate": "max",
        "intent_scope": "quiet",
        "subagents": {"subagent_code_planner": "glm-5.3-flash-ctx262k"},
    },
}


def test_level_switch_uses_that_alias_main_and_drops_the_other_dialect():
    held = {"level": "light"}
    cfg = _cfg(_PROFILE)
    router = ModelRouter(cfg, level_provider=lambda: held["level"])
    light = _main_payload(cfg, router.resolve("main"))
    held["level"] = "max"
    hot = _main_payload(cfg, router.resolve("main"))
    assert light["chat_template_kwargs"] == {"enable_thinking": False}
    assert light["temperature"] == 0.4
    assert hot["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "high",
    }
    assert "enable_thinking" not in hot["chat_template_kwargs"]
    assert hot["temperature"] == 1.0
    assert "top_p" not in hot


def test_partial_main_replaces_global_temperature():
    cfg = _cfg(
        {
            "backend": {
                "chat_completion_kwargs": {"temperature": 0.7, "top_p": 0.9}
            },
            "models": {
                "only": {
                    "url": "http://only/v1/chat/completions",
                    "token": "",
                    "main": {"top_p": 0.2},
                }
            },
            "slots": {"default": "only"},
        }
    )
    data = _main_payload(cfg, _router(cfg).resolve("main"))
    assert data["top_p"] == 0.2
    assert "temperature" not in data


def test_alias_backend_chat_completion_kwargs_are_not_read():
    cfg = _cfg(
        {
            "backend": {"chat_completion_kwargs": {"temperature": 0.1}},
            "models": {
                "only": {
                    "url": "http://only/v1/chat/completions",
                    "token": "",
                    "backend": {
                        "chat_completion_kwargs": {"temperature": 0.9}
                    },
                }
            },
            "slots": {"default": "only"},
        }
    )
    data = _main_payload(cfg, _router(cfg).resolve("main"))
    assert data["temperature"] == 0.1


def test_unset_main_keeps_the_global_dict():
    cfg = _cfg(
        {
            "backend": {"chat_completion_kwargs": {"temperature": 0.15}},
            "models": {
                "only": {
                    "url": "http://only/v1/chat/completions",
                    "token": "",
                }
            },
            "slots": {"default": "only"},
        }
    )
    data = _main_payload(cfg, _router(cfg).resolve("main"))
    assert data["temperature"] == 0.15


def test_summary_finish_gate_and_subagent_read_different_sections():
    cfg = _cfg(_PROFILE)
    summary = _aux_payload(cfg, "summary")
    assert summary["temperature"] == 0.3
    assert summary["tool_choice"] == "auto"
    assert "chat_template_kwargs" not in summary
    assert "enable_thinking" not in summary

    gate = _aux_payload(cfg, "finish_gate")
    assert gate["chat_template_kwargs"] == {
        "thinking": False,
        "reasoning_effort": "none",
    }
    assert gate["temperature"] == 0.2

    router = _router(cfg)
    planner = _main_payload(
        cfg, router.resolve("subagent:subagent_code_planner")
    )
    assert planner["chat_template_kwargs"]["thinking"] is True
    assert planner["chat_template_kwargs"]["reasoning_effort"] == "high"

    # Черновик роли — тот же text-only клиент. Модель субагента, секция auxiliary.
    role = _build_chat_completion_payload(
        ctx=SimpleNamespace(config_dict=cfg),
        state=SimpleNamespace(default_model="legacy"),
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=32,
        resolved=router.resolve("subagent:subagent_code_planner"),
    )
    assert role["temperature"] == 0.2
    assert role["chat_template_kwargs"] == {
        "thinking": False,
        "reasoning_effort": "none",
    }


def test_unset_auxiliary_keeps_the_global_aux_dict_not_main():
    cfg = _cfg(
        {
            "backend": {
                "chat_completion_kwargs": {
                    "temperature": 0.7,
                    "chat_template_kwargs": {"enable_thinking": True},
                }
            },
            "chat_completions": {
                "auxiliary_llm": {
                    "completion_kwargs": {
                        "separate_reasoning": False,
                        "chat_template_kwargs": {"enable_thinking": False},
                    }
                }
            },
            "models": {
                "only": {
                    "url": "http://only/v1/chat/completions",
                    "token": "",
                }
            },
            "slots": {"default": "only"},
        }
    )
    summary = _aux_payload(cfg, "summary")
    assert summary["separate_reasoning"] is False
    assert summary["chat_template_kwargs"] == {"enable_thinking": False}
    assert "temperature" not in summary

    dialog = _main_payload(cfg, _router(cfg).resolve("main"))
    assert dialog["temperature"] == 0.7
    assert dialog["chat_template_kwargs"] == {"enable_thinking": True}


def test_explicit_empty_auxiliary_sends_no_vendor_keys():
    cfg = _cfg(
        {
            "chat_completions": {
                "auxiliary_llm": {
                    "completion_kwargs": {
                        "chat_template_kwargs": {"enable_thinking": False}
                    }
                }
            },
            "models": {
                "quiet": {
                    "url": "http://quiet/v1/chat/completions",
                    "token": "",
                    "auxiliary": {},
                }
            },
            "slots": {"default": "quiet"},
            "assign": {"summary": "quiet"},
        }
    )
    payload = _aux_payload(cfg, "summary")
    assert "chat_template_kwargs" not in payload
    assert "enable_thinking" not in payload


def test_parse_completion_kwargs_text_blank_is_inherit_and_braces_are_empty():
    assert parse_completion_kwargs_text("") is None
    assert parse_completion_kwargs_text("   \n") is None
    assert parse_completion_kwargs_text("{}") == {}
    assert parse_completion_kwargs_text("temperature: 1\n") == {
        "temperature": 1
    }


def test_empty_auxiliary_does_not_copy_main_or_global_thinking():
    cfg = _cfg(_PROFILE)
    payload = _aux_payload(cfg, "intent_scope")
    assert "chat_template_kwargs" not in payload
    assert "enable_thinking" not in payload
    assert "temperature" not in payload
    assert "top_p" not in payload


def test_open_alias_fallback_sends_active_model_kwargs(monkeypatch):
    """Алиас в предохранителе не оставляет свои kwargs поверх запасной модели."""
    cfg = _cfg(_PROFILE)
    router = _router(cfg, level="light")
    router.breaker.trip("gc_v2", "down")
    captured = {}

    def _fake_policy(*, payload_factory, initial_max_tokens, **kwargs):
        captured["payload"] = payload_factory(initial_max_tokens)
        captured["url"] = kwargs["url"]
        return {"choices": [{"message": {"content": "ok"}}]}, {}

    monkeypatch.setattr(
        llm_chat_client,
        "arequest_chat_completions_with_policy",
        as_coro(_fake_policy),
    )
    monkeypatch.setattr(
        llm_chat_client,
        "acreate_call_headers",
        as_coro(lambda _s, _c, resolved=None: {}),
    )

    run_coro(
        llm_chat_client.arequest_chat_completion_text(
            ctx=SimpleNamespace(
                config_dict=cfg,
                model_router=router,
                logger=None,
                is_tui=False,
            ),
            state=SimpleNamespace(default_model="legacy", usage=None),
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=32,
            consumer="summary",
        )
    )

    payload = captured["payload"]
    assert payload["model"] == "qwen_38_mlsp"
    assert captured["url"] == "http://qwen/v1/chat/completions"
    assert payload["temperature"] == 0.4
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert "tool_choice" not in payload


def test_two_aliases_with_one_model_id_do_not_collapse():
    cfg = _cfg(_PROFILE)
    router = _router(cfg)
    first = router.resolve("summary")
    assert first.alias == "gc_v2"
    held = {"level": "default"}
    direct = ModelRouter(
        cfg,
        level_provider=lambda: held["level"],
    )
    # Оба алиаса с одним id остаются разными записями снимка.
    models = cfg["models"]
    assert models["same-id-a"]["model"] == models["same-id-b"]["model"]
    assert models["same-id-a"]["main"] != models["same-id-b"]["main"]
    assert set(models) >= {"same-id-a", "same-id-b", "quiet"}
    assert "quiet" not in {
        cfg["slots"]["light"],
        cfg["slots"]["default"],
        cfg["slots"]["max"],
    }
    assert direct.resolve("main").alias == "gc_v2"


def test_parse_model_backend_text_overlays_named_keys_only():
    assert parse_model_backend_text("") is None
    assert parse_model_backend_text("{}") == {}
    assert parse_model_backend_text(
        "use_max_tokens_param: false\nrequest_timeout_sec: 120\n"
    ) == {"use_max_tokens_param": False, "request_timeout_sec": 120}
    try:
        parse_model_backend_text(
            "chat_completion_kwargs: {temperature: 0.2}\n"
        )
    except ValueError as exc:
        assert "main" in str(exc)
    else:
        raise AssertionError("kwargs в backend модели должны отвергаться")
    try:
        parse_model_backend_text("api_mode: chat_completions\n")
    except ValueError as exc:
        assert "общем backend" in str(exc)
    else:
        raise AssertionError("api_mode модели должен отвергаться")
