"""Тесты резолвера точки C3 (collect после батча тулов).

C1 (после компакции) и C2 (после close_all) — всегда включены и от флага не зависят.
C3 — режимная: CLI/TUI да, server нет. Ключа ``gc.force_collect`` в схеме нет,
поэтому конфиг значение не меняет.
См. docs_md/actual/2026-07-29-gc-collect-placement-spec.md.
"""

from gc_core.config_modules.gc import gc_force_collect_enabled
from gc_core.settings import default_settings


class TestDefaultByMode:
    def test_cli_mode_default_on(self):
        assert gc_force_collect_enabled({}, is_cli_mode=True) is True

    def test_tui_default_on(self):
        assert gc_force_collect_enabled({}, is_tui=True) is True

    def test_server_default_off(self):
        assert (
            gc_force_collect_enabled({}, is_tui=False, is_cli_mode=False)
            is False
        )

    def test_cli_and_tui_both_on(self):
        assert (
            gc_force_collect_enabled({}, is_tui=True, is_cli_mode=True) is True
        )


class TestEdgeCases:
    def test_none_config_dict(self):
        assert gc_force_collect_enabled(None, is_cli_mode=True) is True
        assert (
            gc_force_collect_enabled(None, is_tui=False, is_cli_mode=False)
            is False
        )

    def test_typed_settings_accepted(self):
        settings = default_settings()
        assert gc_force_collect_enabled(settings, is_tui=True) is True
        assert gc_force_collect_enabled(settings) is False
