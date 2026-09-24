import os
import tempfile
import unittest

from gc_core.config import load_config


class ChatCompletionsPromptConfigTest(unittest.TestCase):
    def test_default_prompt_path_from_project_root(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write("backend:\n  api_mode: chat_completions\n")

            # prompts/ рядом с конфигом → дефолты резолвятся config_dir-relative
            prompts_dir = os.path.join(tmp_dir, "prompts")
            os.makedirs(prompts_dir, exist_ok=True)
            for name in (
                "chat_completions_system_prompt.md",
                "chat_completions_summary_prompt.md",
            ):
                with open(
                    os.path.join(prompts_dir, name), "w", encoding="utf-8"
                ) as fd:
                    fd.write("x")

            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["chat_completions_system_prompt_path"],
                os.path.join(
                    tmp_dir, "prompts", "chat_completions_system_prompt.md"
                ),
            )
            self.assertEqual(
                cfg["summary_params"]["chat_completions_summary_prompt_path"],
                os.path.join(
                    tmp_dir, "prompts", "chat_completions_summary_prompt.md"
                ),
            )

    def test_relative_prompt_path_resolved_from_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "paths:\n  chat_completions_system_prompt_path: prompts/chat.md\n"
                    "summary:\n"
                    "  chat_completions_summary_prompt_path: prompts/summary.md\n"
                )

            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["chat_completions_system_prompt_path"],
                os.path.join(tmp_dir, "prompts", "chat.md"),
            )
            self.assertEqual(
                cfg["summary_params"]["chat_completions_summary_prompt_path"],
                os.path.join(tmp_dir, "prompts", "summary.md"),
            )

    def test_summary_trigger_chars_defaults_to_zero(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write("backend:\n  api_mode: chat_completions\n")
            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["summary_params"][
                    "chat_completions_summary_trigger_chars"
                ],
                0,
            )
            self.assertEqual(
                cfg["summary_params"][
                    "chat_completions_summary_turn_end_trigger_chars"
                ],
                0,
            )
            self.assertEqual(
                cfg["summary_params"]["incremental_batch_chars"], 0
            )
            self.assertFalse(cfg["logging_params"]["full_log_backend"])
            self.assertTrue(cfg["logging_params"]["console_trace"])
            self.assertTrue(cfg["logging_params"]["save_logs"])

    def test_summary_trigger_chars_is_normalized_to_int(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "summary:\n"
                    "  chat_completions_summary_trigger_chars: '123'\n"
                )
            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["summary_params"][
                    "chat_completions_summary_trigger_chars"
                ],
                123,
            )
            self.assertEqual(
                cfg["summary_params"]["incremental_batch_chars"], 172
            )
            self.assertEqual(
                cfg["summary_params"][
                    "chat_completions_summary_turn_end_trigger_chars"
                ],
                73,
            )

    def test_summary_params_defaults(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write("backend:\n  api_mode: chat_completions\n")
            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["summary_params"]["last_dialog_steps_count"], 5
            )
            self.assertEqual(
                cfg["summary_params"]["last_dialog_step_message_max_chars"],
                1500,
            )
            self.assertEqual(cfg["summary_params"]["max_message_chars"], 50000)
            self.assertEqual(
                cfg["summary_params"]["max_dialogue_chars"], 1000000
            )
            self.assertEqual(
                cfg["summary_params"]["max_accumulated_summary_chars"], 20000
            )
            self.assertEqual(
                cfg["summary_params"]["intent_log_preview_chars"], 10000
            )
            self.assertEqual(
                cfg["summary_params"][
                    "chat_completions_summary_turn_end_trigger_chars"
                ],
                0,
            )
            self.assertNotIn(
                "original_user_messages_max_chars", cfg["summary_params"]
            )
            self.assertTrue(
                cfg["summary_params"][
                    "chat_completions_memory_patch_replace_prompt_path"
                ].endswith("prompts/chat_completions_memory_patch_replace.md")
            )
            self.assertNotIn("llm", cfg["summary_params"])
            from gc_core.config_modules.chat_completions import (
                ChatCompletionsLlmConfig,
                auxiliary_llm_from_config_dict,
            )

            self.assertIsInstance(cfg["auxiliary_llm_params"], dict)
            self.assertNotIn("chat_completions_llm", cfg)
            aux = auxiliary_llm_from_config_dict(cfg)
            self.assertIsInstance(aux, ChatCompletionsLlmConfig)
            self.assertEqual(aux.max_tokens, 24576)
            self.assertEqual(aux.judge_max_tokens, 24576)
            self.assertEqual(aux.completion_kwargs, {})

    def test_auxiliary_llm_loaded_from_yaml(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "chat_completions:\n"
                    "  auxiliary_llm:\n"
                    "    max_tokens: 4096\n"
                    "    judge_max_tokens: 5000\n"
                    "    completion_kwargs:\n"
                    "      enable_thinking: false\n"
                )
            cfg = load_config(cfg_path)
            from gc_core.config_modules.chat_completions import (
                auxiliary_llm_from_config_dict,
            )

            self.assertEqual(cfg["auxiliary_llm_params"]["max_tokens"], 4096)
            self.assertEqual(
                cfg["auxiliary_llm_params"]["judge_max_tokens"], 5000
            )
            self.assertEqual(
                cfg["auxiliary_llm_params"]["completion_kwargs"],
                {"enable_thinking": False},
            )
            self.assertEqual(
                cfg["chat_completions_params"]["auxiliary_llm"]["max_tokens"],
                4096,
            )
            aux = auxiliary_llm_from_config_dict(cfg)
            self.assertEqual(aux.max_tokens, 4096)
            self.assertEqual(aux.judge_max_tokens, 5000)
            self.assertEqual(aux.completion_kwargs, {"enable_thinking": False})

    def test_legacy_llm_alias_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\nchat_completions:\n  llm:\n    max_tokens: 1111\n"
                )
            cfg = load_config(cfg_path)
            from gc_core.config_modules.chat_completions import (
                auxiliary_llm_from_config_dict,
            )

            self.assertEqual(cfg["auxiliary_llm_params"]["max_tokens"], 1111)
            self.assertEqual(
                auxiliary_llm_from_config_dict(cfg).max_tokens, 1111
            )
            self.assertIn("auxiliary_llm", cfg["chat_completions_params"])
            self.assertNotIn("llm", cfg["chat_completions_params"])

    def test_summary_params_normalization(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "summary:\n"
                    "  chat_completions_summary_trigger_chars: '200'\n"
                    "  incremental_batch_chars: '260'\n"
                    "  chat_completions_summary_prompt_path: prompts/summary.md\n"
                    "  max_message_chars: '45000'\n"
                    "  max_dialogue_chars: '900000'\n"
                    "  max_accumulated_summary_chars: '15000'\n"
                    "  last_dialog_steps_count: '7'\n"
                    "  last_dialog_step_message_max_chars: '2500'\n"
                )
            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["summary_params"]["chat_completions_summary_prompt_path"],
                os.path.join(tmp_dir, "prompts", "summary.md"),
            )
            self.assertEqual(
                cfg["summary_params"][
                    "chat_completions_summary_trigger_chars"
                ],
                200,
            )
            self.assertEqual(
                cfg["summary_params"]["incremental_batch_chars"], 260
            )
            self.assertEqual(cfg["summary_params"]["max_message_chars"], 45000)
            self.assertEqual(
                cfg["summary_params"]["max_dialogue_chars"], 900000
            )
            self.assertEqual(
                cfg["summary_params"]["max_accumulated_summary_chars"], 15000
            )
            self.assertEqual(
                cfg["summary_params"]["last_dialog_steps_count"], 7
            )
            self.assertEqual(
                cfg["summary_params"]["last_dialog_step_message_max_chars"],
                2500,
            )

    def test_incremental_batch_chars_derived_from_trigger(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "summary:\n"
                    "  chat_completions_summary_trigger_chars: 50000\n"
                )
            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["summary_params"]["incremental_batch_chars"], 70000
            )
            self.assertEqual(
                cfg["summary_params"][
                    "chat_completions_summary_turn_end_trigger_chars"
                ],
                30000,
            )

    def test_turn_end_trigger_chars_explicit_and_disabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "summary:\n"
                    "  chat_completions_summary_trigger_chars: 200000\n"
                    "  chat_completions_summary_turn_end_trigger_chars: '80000'\n"
                )
            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["summary_params"][
                    "chat_completions_summary_turn_end_trigger_chars"
                ],
                80000,
            )

        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "summary:\n"
                    "  chat_completions_summary_trigger_chars: 200000\n"
                    "  chat_completions_summary_turn_end_trigger_chars: 0\n"
                )
            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["summary_params"][
                    "chat_completions_summary_turn_end_trigger_chars"
                ],
                0,
            )

    def test_chat_completion_authorization_header_loaded_from_backend_section(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n"
                    "  api_mode: chat_completions\n"
                    "  chat_completion_headers:\n"
                    "    Authorization: 'Bearer sk-or-v1-test-token'\n"
                )
            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["chat_completion_headers"]["Authorization"],
                "Bearer sk-or-v1-test-token",
            )

    def test_summary_original_user_messages_max_chars_in_yaml_is_rejected(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\nsummary:\n  original_user_messages_max_chars: 12000\n"
                )
            # отказ схемы приходит завёрнутым (без pydantic-дампа), ключ назван
            with self.assertRaisesRegex(ValueError, "summary"):
                load_config(cfg_path)

    def test_summary_llm_in_yaml_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\nsummary:\n  llm:\n    max_tokens: 4096\n"
                )
            # отказ схемы приходит завёрнутым (без pydantic-дампа), ключ назван
            with self.assertRaisesRegex(ValueError, "summary"):
                load_config(cfg_path)

    def test_chat_completion_kwargs_and_flags_loaded_from_backend_section(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n"
                    "  api_mode: chat_completions\n"
                    "  use_max_tokens_param: true\n"
                    "  openai_strict_tools_schema: true\n"
                    "  chat_completion_kwargs:\n"
                    "    temperature: 0.2\n"
                    "    top_p: 0.9\n"
                    "    vendor_flag: true\n"
                    "logging:\n"
                    "  full_log_backend: true\n"
                )
            cfg = load_config(cfg_path)
            self.assertEqual(cfg["chat_completion_kwargs"]["temperature"], 0.2)
            self.assertEqual(cfg["chat_completion_kwargs"]["top_p"], 0.9)
            self.assertTrue(cfg["chat_completion_kwargs"]["vendor_flag"])
            self.assertTrue(cfg["logging_params"]["full_log_backend"])
            self.assertTrue(cfg["use_max_tokens_param"])
            self.assertTrue(cfg["openai_strict_tools_schema"])

    def test_chat_completion_headers_loaded_from_backend_section(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n"
                    "  api_mode: chat_completions\n"
                    "  chat_completion_headers:\n"
                    "    HTTP-Referer: '  https://example.org/app  '\n"
                    "    X-OpenRouter-Title: 'mini-cli'\n"
                    "    X-Custom: 123\n"
                )
            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["chat_completion_headers"]["HTTP-Referer"],
                "https://example.org/app",
            )
            self.assertEqual(
                cfg["chat_completion_headers"]["X-OpenRouter-Title"],
                "mini-cli",
            )
            self.assertEqual(cfg["chat_completion_headers"]["X-Custom"], "123")

    def test_memory_patch_prompt_paths_resolved_from_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "summary:\n"
                    "  chat_completions_memory_patch_replace_prompt_path: prompts/patch_replace.md\n"
                    "  intent_log_preview_chars: 8000\n"
                )
            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["summary_params"][
                    "chat_completions_memory_patch_replace_prompt_path"
                ],
                os.path.join(tmp_dir, "prompts", "patch_replace.md"),
            )
            self.assertEqual(
                cfg["summary_params"]["intent_log_preview_chars"], 8000
            )

    def test_intent_scope_tools_map_loaded_from_yaml(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "intent_scope:\n"
                    "  tools:\n"
                    "    turn_ask_human:\n"
                    "      record: aggregated\n"
                    "    turn_finish:\n"
                    "      record: raw\n"
                )
            cfg = load_config(cfg_path)
            tools = cfg["intent_scope_params"]["tools"]
            self.assertEqual(tools["turn_ask_human"]["record"], "aggregated")
            self.assertEqual(tools["turn_finish"]["record"], "raw")

    def test_auxiliary_llm_params_survives_deep_merge_over_schema_defaults(
        self,
    ):
        """Бенч: overlay пишет auxiliary_llm_params — typed профиль не остаётся дефолтным."""
        from gc_core.config import flatten_app_config
        from gc_core.config_modules.chat_completions import (
            auxiliary_llm_from_config_dict,
        )
        from gc_core.config_modules.merge import deep_merge
        from gc_core.config_modules.root import AppConfig

        base = flatten_app_config(AppConfig.model_validate({}))
        self.assertIsInstance(base["auxiliary_llm_params"], dict)
        self.assertNotIn("chat_completions_llm", base)

        merged = deep_merge(
            base,
            {
                "chat_completion_kwargs": {
                    "chat_template_kwargs": {"enable_thinking": True},
                },
                "auxiliary_llm_params": {
                    "max_tokens": 8192,
                    "judge_max_tokens": 10000,
                    "completion_kwargs": {
                        "separate_reasoning": False,
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                },
            },
        )
        aux = auxiliary_llm_from_config_dict(merged)
        self.assertEqual(
            aux.completion_kwargs,
            {
                "separate_reasoning": False,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        self.assertEqual(aux.max_tokens, 8192)

    def test_stale_legacy_chat_completions_llm_object_ignored_when_params_present(
        self,
    ):
        """Дефолтный объект-близнец не должен перебивать auxiliary_llm_params."""
        from gc_core.config_modules.chat_completions import (
            ChatCompletionsLlmConfig,
            auxiliary_llm_from_config_dict,
        )

        cfg = {
            "chat_completions_llm": ChatCompletionsLlmConfig(),  # = schema defaults
            "auxiliary_llm_params": {
                "completion_kwargs": {"enable_thinking": False},
            },
        }
        aux = auxiliary_llm_from_config_dict(cfg)
        self.assertEqual(aux.completion_kwargs, {"enable_thinking": False})

    def test_legacy_nested_overlay_survives_when_flat_is_schema_default(self):
        """Старый harbor-оверлей в chat_completions_params.auxiliary_llm не глохнет."""
        from gc_core.config import flatten_app_config
        from gc_core.config_modules.chat_completions import (
            auxiliary_llm_from_config_dict,
        )
        from gc_core.config_modules.merge import deep_merge
        from gc_core.config_modules.root import AppConfig

        base = flatten_app_config(AppConfig.model_validate({}))
        merged = deep_merge(
            base,
            {
                "chat_completion_kwargs": {
                    "chat_template_kwargs": {"enable_thinking": True},
                },
                "chat_completions_params": {
                    "auxiliary_llm": {
                        "max_tokens": 4096,
                        "judge_max_tokens": 10000,
                        "completion_kwargs": {
                            "separate_reasoning": False,
                            "chat_template_kwargs": {"enable_thinking": False},
                        },
                    }
                },
            },
        )
        # flat остаётся schema default после такого оверлея
        self.assertEqual(
            merged["auxiliary_llm_params"]["completion_kwargs"], {}
        )
        self.assertEqual(merged["auxiliary_llm_params"]["max_tokens"], 24576)
        aux = auxiliary_llm_from_config_dict(merged)
        self.assertEqual(aux.max_tokens, 4096)
        self.assertFalse(
            aux.completion_kwargs.get("chat_template_kwargs", {}).get(
                "enable_thinking", True
            )
        )

    def test_flat_wins_over_diverged_nested_defaults(self):
        """Канон flat побеждает устаревший nested dump после одностороннего merge."""
        from gc_core.config_modules.chat_completions import (
            auxiliary_llm_from_config_dict,
        )

        cfg = {
            "chat_completions_params": {
                "auxiliary_llm": {
                    "completion_kwargs": {
                        "chat_template_kwargs": {"enable_thinking": True},
                    },
                },
            },
            "auxiliary_llm_params": {
                "completion_kwargs": {
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            },
        }
        aux = auxiliary_llm_from_config_dict(cfg)
        self.assertEqual(
            aux.completion_kwargs,
            {"chat_template_kwargs": {"enable_thinking": False}},
        )

    def test_empty_aux_overlay_does_not_inject_vendor_keys(self):
        """Schema/default ``{}`` не шлёт chat_template_kwargs на OpenAI-like агента."""
        from types import SimpleNamespace

        from gc_core.config import flatten_app_config
        from gc_core.config_modules.chat_completions import (
            auxiliary_llm_from_config_dict,
        )
        from gc_core.config_modules.merge import deep_merge
        from gc_core.config_modules.root import AppConfig
        from gc_core.utils.llm_chat_client import (
            _build_chat_completion_payload,
        )

        base = flatten_app_config(AppConfig.model_validate({}))
        merged = deep_merge(
            base,
            {
                "chat_completion_kwargs": {
                    "temperature": 1.0,
                    "top_p": 1.0,
                },
                "auxiliary_llm_params": {
                    "max_tokens": 8192,
                    "judge_max_tokens": 10000,
                    "completion_kwargs": {},
                },
            },
        )
        self.assertEqual(
            merged["auxiliary_llm_params"]["completion_kwargs"], {}
        )
        aux = auxiliary_llm_from_config_dict(merged)
        self.assertEqual(aux.completion_kwargs, {})
        payload = _build_chat_completion_payload(
            ctx=SimpleNamespace(config_dict=merged, auxiliary_llm=aux),
            state=SimpleNamespace(default_model="gpt-4o"),
            messages=[{"role": "user", "content": "x"}],
            max_tokens=100,
            payload_overrides=dict(aux.completion_kwargs),
        )
        self.assertNotIn("chat_template_kwargs", payload)
        self.assertNotIn("separate_reasoning", payload)
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["temperature"], 1.0)

    def test_dsv4_style_agent_thinking_does_not_leak_into_aux_payload(self):
        """DeepSeek-style: aux overlay гасит thinking/reasoning_effort агента."""
        from types import SimpleNamespace

        from gc_core.config import flatten_app_config
        from gc_core.config_modules.chat_completions import (
            auxiliary_llm_from_config_dict,
        )
        from gc_core.config_modules.merge import deep_merge
        from gc_core.config_modules.root import AppConfig
        from gc_core.utils.llm_chat_client import (
            _build_chat_completion_payload,
        )

        base = flatten_app_config(AppConfig.model_validate({}))
        merged = deep_merge(
            base,
            {
                "chat_completion_kwargs": {
                    "chat_template_kwargs": {
                        "thinking": True,
                        "reasoning_effort": "high",
                    },
                    "tool_choice": "auto",
                },
                "auxiliary_llm_params": {
                    "max_tokens": 8192,
                    "judge_max_tokens": 10000,
                    "completion_kwargs": {
                        "chat_template_kwargs": {
                            "thinking": False,
                            "reasoning_effort": "none",
                        },
                    },
                },
            },
        )
        aux = auxiliary_llm_from_config_dict(merged)
        payload = _build_chat_completion_payload(
            ctx=SimpleNamespace(config_dict=merged),
            state=SimpleNamespace(default_model="m"),
            messages=[{"role": "user", "content": "x"}],
            max_tokens=100,
            payload_overrides=dict(aux.completion_kwargs),
        )
        self.assertEqual(payload["chat_template_kwargs"]["thinking"], False)
        self.assertEqual(
            payload["chat_template_kwargs"]["reasoning_effort"], "none"
        )


if __name__ == "__main__":
    unittest.main()
