import os
import tempfile
import unittest

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
            self.assertEqual(
                cfg["summary_params"]["original_user_messages_max_chars"], 20000
            )

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
                    "  original_user_messages_max_chars: '12000'\n"
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
            self.assertEqual(
                cfg["summary_params"]["original_user_messages_max_chars"], 12000
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
                )
            cfg = load_config(cfg_path)
            self.assertEqual(cfg["chat_completion_kwargs"]["temperature"], 0.2)
            self.assertEqual(cfg["chat_completion_kwargs"]["top_p"], 0.9)
            self.assertTrue(cfg["chat_completion_kwargs"]["vendor_flag"])
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


if __name__ == "__main__":
    unittest.main()
