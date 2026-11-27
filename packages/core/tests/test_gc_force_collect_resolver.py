"""Тесты режимных резолверов явной сборки мусора.

C1 (после компакции) и C2 (после close_all) — везде, кроме TUI.
C3 — режимная: только CLI; в TUI (там и is_tui, и is_cli_mode) и server нет. Ключа ``gc.force_collect`` в схеме нет,
поэтому конфиг значение не меняет.
См. docs_md/actual/2026-07-29-gc-collect-placement-spec.md.
"""

from gc_core.config_modules.gc import (
    gc_force_collect_enabled,
    gc_run_collect_enabled,
)
from gc_core.settings import default_settings
from gc_testkit.settings import make_settings


class TestDefaultByMode:
    def test_cli_mode_default_on(self):
        assert (
            gc_force_collect_enabled(make_settings(), is_cli_mode=True) is True
        )

    def test_tui_only_flag_off(self):
        assert gc_force_collect_enabled(make_settings(), is_tui=True) is False

    def test_cli_only_on(self):
        assert (
            gc_force_collect_enabled(
                make_settings(), is_tui=False, is_cli_mode=True
            )
            is True
        )

    def test_server_default_off(self):
        assert (
            gc_force_collect_enabled(
                make_settings(), is_tui=False, is_cli_mode=False
            )
            is False
        )

    def test_tui_real_flags_off(self):
        # TUI стартует с system="cli": оба флага истинны.
        assert (
            gc_force_collect_enabled(
                make_settings(), is_tui=True, is_cli_mode=True
            )
            is False
        )


class TestEdgeCases:
    def test_none_settings(self):
        assert gc_force_collect_enabled(None, is_cli_mode=True) is True
        assert (
            gc_force_collect_enabled(None, is_tui=False, is_cli_mode=False)
            is False
        )

    def test_typed_settings_accepted(self):
        settings = default_settings()
        assert gc_force_collect_enabled(settings, is_tui=True) is False
        assert gc_force_collect_enabled(settings) is False


class TestRunCollectByMode:
    """C1 и C2: полная сборка в потоке агента стопает UI-цикл TUI."""

    def test_tui_off(self):
        # TUI стартует с system="cli": решает только is_tui.
        assert gc_run_collect_enabled(is_tui=True) is False

    def test_cli_and_server_on(self):
        assert gc_run_collect_enabled(is_tui=False) is True
