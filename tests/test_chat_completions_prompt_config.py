import os
import tempfile
import unittest

from pydantic import ValidationError

from config import load_config  # noqa: E402


class ChatCompletionsPromptConfigTest(unittest.TestCase):
    def test_default_prompt_path_from_project_root(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write("backend:\n  api_mode: chat_completions\n")

            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["chat_completions_system_prompt_path"],
                os.path.join(tmp_dir, "prompts", "chat_completions_system_prompt.md"),
            )
            self.assertTrue(
                cfg["summary_params"]["chat_completions_summary_prompt_path"].endswith(
                    "prompts/chat_completions_summary_prompt.md"
                )
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
                cfg["summary_params"]["chat_completions_summary_trigger_chars"], 0
            )
            self.assertEqual(cfg["summary_params"]["incremental_batch_chars"], 0)
            self.assertFalse(cfg["logging_params"]["full_log_backend"])
            self.assertTrue(cfg["logging_params"]["console_trace"])
            self.assertFalse(cfg["logging_params"]["dump_storage"])
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
                cfg["summary_params"]["chat_completions_summary_trigger_chars"], 123
            )
            self.assertEqual(cfg["summary_params"]["incremental_batch_chars"], 172)

    def test_summary_params_defaults(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write("backend:\n  api_mode: chat_completions\n")
            cfg = load_config(cfg_path)
            self.assertEqual(cfg["summary_params"]["last_dialog_steps_count"], 5)
            self.assertEqual(
                cfg["summary_params"]["last_dialog_step_message_max_chars"],
                1500,
            )
            self.assertEqual(cfg["summary_params"]["max_message_chars"], 50000)
            self.assertEqual(cfg["summary_params"]["max_dialogue_chars"], 1000000)
            self.assertEqual(
                cfg["summary_params"]["max_accumulated_summary_chars"], 20000
            )
            self.assertEqual(cfg["summary_params"]["intent_log_preview_chars"], 10000)
            self.assertNotIn("original_user_messages_max_chars", cfg["summary_params"])
            self.assertTrue(
                cfg["summary_params"][
                    "chat_completions_memory_patch_append_prompt_path"
                ].endswith("prompts/chat_completions_memory_patch_append.md")
            )
            self.assertTrue(
                cfg["summary_params"][
                    "chat_completions_memory_patch_replace_prompt_path"
                ].endswith("prompts/chat_completions_memory_patch_replace.md")
            )
            self.assertNotIn("llm", cfg["summary_params"])
            from config_modules.chat_completions import ChatCompletionsLlmConfig

            self.assertIsInstance(cfg["chat_completions_llm"], ChatCompletionsLlmConfig)
            self.assertEqual(cfg["chat_completions_llm"].max_tokens, 8192)
            self.assertEqual(cfg["chat_completions_llm"].completion_kwargs, {})

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
                cfg["summary_params"]["chat_completions_summary_trigger_chars"], 200
            )
            self.assertEqual(cfg["summary_params"]["incremental_batch_chars"], 260)
            self.assertEqual(cfg["summary_params"]["max_message_chars"], 45000)
            self.assertEqual(cfg["summary_params"]["max_dialogue_chars"], 900000)
            self.assertEqual(
                cfg["summary_params"]["max_accumulated_summary_chars"], 15000
            )
            self.assertEqual(cfg["summary_params"]["last_dialog_steps_count"], 7)
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

    def test_chat_completion_authorization_header_loaded_from_backend_section(self):
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

    def test_summary_original_user_messages_max_chars_in_yaml_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "summary:\n"
                    "  original_user_messages_max_chars: 12000\n"
                )
            with self.assertRaises(ValidationError):
                load_config(cfg_path)

    def test_summary_llm_in_yaml_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "summary:\n"
                    "  llm:\n"
                    "    max_tokens: 4096\n"
                )
            with self.assertRaises(ValidationError):
                load_config(cfg_path)

    def test_chat_completion_kwargs_and_flags_loaded_from_backend_section(self):
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
                    "  chat_completions_memory_patch_append_prompt_path: prompts/patch_append.md\n"
                    "  chat_completions_memory_patch_replace_prompt_path: prompts/patch_replace.md\n"
                    "  intent_log_preview_chars: 8000\n"
                )
            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["summary_params"]["chat_completions_memory_patch_append_prompt_path"],
                os.path.join(tmp_dir, "prompts", "patch_append.md"),
            )
            self.assertEqual(
                cfg["summary_params"]["chat_completions_memory_patch_replace_prompt_path"],
                os.path.join(tmp_dir, "prompts", "patch_replace.md"),
            )
            self.assertEqual(cfg["summary_params"]["intent_log_preview_chars"], 8000)

    def test_intent_scope_tools_map_loaded_from_yaml(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "intent_scope:\n"
                    "  tools:\n"
                    "    ask_human:\n"
                    "      record: aggregated\n"
                    "    submit_solution:\n"
                    "      record: raw\n"
                )
            cfg = load_config(cfg_path)
            tools = cfg["intent_scope_params"]["tools"]
            self.assertEqual(tools["ask_human"]["record"], "aggregated")
            self.assertEqual(tools["submit_solution"]["record"], "raw")

if __name__ == "__main__":
    unittest.main()
