import os
import tempfile
import unittest

from config import load_config  # noqa: E402


class ChatCompletionsPromptConfigTest(unittest.TestCase):
    def test_default_prompt_path_from_project_root(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write("backend_api_mode: chat_completions\n")

            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["chat_completions_system_prompt_path"],
                os.path.join(tmp_dir, "chat_completions_system_prompt.md"),
            )

    def test_relative_prompt_path_resolved_from_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend_api_mode: chat_completions\n"
                    "chat_completions_system_prompt_path: prompts/chat.md\n"
                )

            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["chat_completions_system_prompt_path"],
                os.path.join(tmp_dir, "prompts", "chat.md"),
            )


if __name__ == "__main__":
    unittest.main()
