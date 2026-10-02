import os
import tempfile
import unittest

from gc_core.config import (
    apply_cli_overrides,
    flat_config,
    load_config,
    load_settings,
)


class CommandModerationConfigTest(unittest.TestCase):
    def test_defaults_are_applied(self):
        """On/ask по дефолту (secure-by-default): при отсутствии security блока — все слои включены."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write("backend:\n  api_mode: chat_completions\n")

            cfg = load_config(cfg_path)
            self.assertEqual(cfg["command_moderation"]["mode"], "ask")
            self.assertTrue(cfg["write_guard"]["enabled"])
            self.assertEqual(cfg["write_guard"]["extra_paths"], [])
            self.assertEqual(
                cfg["command_moderation"]["allowed_command_examples"], []
            )
            self.assertTrue(cfg["url_policy"]["enabled"])
            self.assertEqual(
                cfg["url_policy"]["allowed_schemes"], ["http", "https"]
            )
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
                    "backend:\n  api_mode: chat_completions\nsecurity:\n  command_moderation:\n    mode: maybe\n"
                )

            with self.assertRaisesRegex(
                ValueError, "security.command_moderation.mode"
            ):
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

            loaded = load_settings(cfg_path)
            overridden = apply_cli_overrides(
                loaded,
                write_guard_enabled=False,
                command_moderation_mode="allow",
            )
            cfg = flat_config(overridden)
            self.assertFalse(cfg["write_guard"]["enabled"])
            self.assertEqual(cfg["command_moderation"]["mode"], "allow")
            # Источник правок — слой cli; исходный объект не изменён.
            self.assertEqual(
                overridden.provenance.source(
                    "security.write_guard.enabled"
                ).layer,
                "cli",
            )
            self.assertTrue(loaded.settings.security.write_guard.enabled)

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

            cfg = flat_config(
                apply_cli_overrides(
                    load_settings(cfg_path),
                    extra_overrides={"model": "cli-model", "max_steps": 77},
                    write_guard_enabled=True,
                    no_write_guard=True,
                    command_moderation_mode="deny",
                )
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
            loaded = load_settings(cfg_path)
            self.assertTrue(flat_config(loaded)["logging_params"]["save_logs"])
            cfg = flat_config(
                apply_cli_overrides(
                    loaded, extra_overrides={"save_logs": False}
                )
            )
            self.assertFalse(cfg["save_logs"])
            self.assertFalse(cfg["logging_params"]["save_logs"])


if __name__ == "__main__":
    unittest.main()


def test_denied_command_examples_field():
    from gc_core.config_modules.security import CommandModerationConfig

    c = CommandModerationConfig(
        denied_command_examples=["rm -rf /", "curl x | sh"]
    )
    assert c.denied_command_examples == ["rm -rf /", "curl x | sh"]
    assert CommandModerationConfig().denied_command_examples == []
