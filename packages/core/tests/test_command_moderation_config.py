import os
import tempfile
import unittest

from gc_core.config import apply_cli_overrides, load_settings


class CommandModerationConfigTest(unittest.TestCase):
    def test_defaults_are_applied(self):
        """On/ask по дефолту (secure-by-default): при отсутствии security блока — все слои включены."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write("backend:\n  api_mode: chat_completions\n")

            security = load_settings(cfg_path).settings.security
            self.assertEqual(security.command_moderation.mode, "ask")
            self.assertTrue(security.write_guard.enabled)
            self.assertEqual(security.write_guard.extra_paths, [])
            self.assertEqual(
                security.command_moderation.allowed_command_examples, []
            )
            self.assertTrue(security.url_policy.enabled)
            self.assertEqual(
                security.url_policy.allowed_schemes, ["http", "https"]
            )
            self.assertEqual(security.url_policy.url_blacklist, [])

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

            security = load_settings(cfg_path).settings.security
            self.assertEqual(
                security.write_guard.extra_paths,
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
                load_settings(cfg_path)

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
            security = overridden.settings.security
            self.assertFalse(security.write_guard.enabled)
            self.assertEqual(security.command_moderation.mode, "allow")
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

            settings = apply_cli_overrides(
                load_settings(cfg_path),
                extra_overrides={
                    "agent.model": "cli-model",
                    "agent.max_steps": 77,
                },
                write_guard_enabled=True,
                no_write_guard=True,
                command_moderation_mode="deny",
            ).settings
            self.assertEqual(settings.agent.model, "cli-model")
            self.assertEqual(settings.agent.max_steps, 77)
            self.assertFalse(settings.security.write_guard.enabled)
            self.assertEqual(settings.security.command_moderation.mode, "deny")

    def test_apply_cli_overrides_reaches_logging_section(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = os.path.join(tmp_dir, "config.yml")
            with open(cfg_path, "w", encoding="utf-8") as fd:
                fd.write("backend:\n  api_mode: chat_completions\n")
            loaded = load_settings(cfg_path)
            self.assertTrue(loaded.settings.logging.save_logs)
            # Прежнее плоское имя `save_logs` доезжает до секции logging.
            overridden = apply_cli_overrides(
                loaded, extra_overrides={"logging.save_logs": False}
            )
            self.assertFalse(overridden.settings.logging.save_logs)
            self.assertEqual(
                overridden.provenance.source("logging.save_logs").layer,
                "cli",
            )


if __name__ == "__main__":
    unittest.main()


def test_denied_command_examples_field():
    from gc_core.config_modules.security import CommandModerationConfig

    c = CommandModerationConfig(
        denied_command_examples=["rm -rf /", "curl x | sh"]
    )
    assert c.denied_command_examples == ["rm -rf /", "curl x | sh"]
    assert CommandModerationConfig().denied_command_examples == []
