import os
import tempfile
import unittest

from config import apply_cli_command_moderation_overrides, apply_cli_overrides, load_config


class CommandModerationConfigTest(unittest.TestCase):
    def test_defaults_are_applied(self):
        """Safe-by-default-OFF: при отсутствии security блока — все слои выключены."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write("backend:\n  api_mode: chat_completions\n")

            cfg = load_config(cfg_path)
            self.assertEqual(cfg["command_moderation"]["mode"], "allow")
            self.assertFalse(cfg["write_guard"]["enabled"])
            self.assertEqual(cfg["write_guard"]["extra_paths"], [])
            self.assertEqual(cfg["command_moderation"]["allowed_command_examples"], [])
            self.assertFalse(cfg["url_policy"]["enabled"])
            self.assertEqual(cfg["url_policy"]["allowed_schemes"], ["http", "https"])
            self.assertEqual(cfg["url_policy"]["url_blacklist"], [])

    def test_extra_paths_resolve_relative_to_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "security:\n"
                    "  write_guard:\n"
                    "    extra_paths: ['tmp/cache']\n"
                )

            cfg = load_config(cfg_path)
            self.assertEqual(
                cfg["write_guard"]["extra_paths"],
                [os.path.join(tmp_dir, "tmp", "cache")],
            )

    def test_invalid_mode_raises(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "security:\n"
                    "  command_moderation:\n"
                    "    mode: maybe\n"
                )

            with self.assertRaisesRegex(ValueError, "security.command_moderation.mode"):
                load_config(cfg_path)

    def test_cli_overrides_have_priority(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "security:\n"
                    "  command_moderation:\n"
                    "    mode: ask\n"
                    "  write_guard:\n"
                    "    enabled: true\n"
                )

            cfg = load_config(cfg_path)
            apply_cli_command_moderation_overrides(
                cfg,
                write_guard_enabled=False,
                command_moderation_mode="allow",
            )
            self.assertFalse(cfg["write_guard"]["enabled"])
            self.assertEqual(cfg["command_moderation"]["mode"], "allow")

    def test_apply_cli_overrides_is_single_entry_point(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write(
                    "backend:\n  api_mode: chat_completions\n"
                    "agent:\n  model: original-model\n"
                    "security:\n"
                    "  command_moderation:\n"
                    "    mode: ask\n"
                    "  write_guard:\n"
                    "    enabled: true\n"
                )

            cfg = load_config(cfg_path)
            apply_cli_overrides(
                cfg,
                extra_overrides={"model": "cli-model", "max_steps": 77},
                write_guard_enabled=True,
                no_write_guard=True,
                command_moderation_mode="deny",
            )
            self.assertEqual(cfg["model"], "cli-model")
            self.assertEqual(cfg["max_steps"], 77)
            self.assertFalse(cfg["write_guard"]["enabled"])
            self.assertEqual(cfg["command_moderation"]["mode"], "deny")

    def test_apply_cli_overrides_syncs_logging_params(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write("backend:\n  api_mode: chat_completions\n")
            cfg = load_config(cfg_path)
            self.assertTrue(cfg["logging_params"]["save_logs"])
            apply_cli_overrides(cfg, extra_overrides={"save_logs": False})
            self.assertFalse(cfg["save_logs"])
            self.assertFalse(cfg["logging_params"]["save_logs"])


if __name__ == "__main__":
    unittest.main()


def test_denied_command_examples_field():
    from config_modules.security import CommandModerationConfig
    c = CommandModerationConfig(denied_command_examples=["rm -rf /", "curl x | sh"])
    assert c.denied_command_examples == ["rm -rf /", "curl x | sh"]
    assert CommandModerationConfig().denied_command_examples == []
