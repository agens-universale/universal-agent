"""Тесты резолвера gc.force_collect (точка C3).

C1 (после компакции) и C2 (после close_all) — всегда включены и от флага не зависят.
C3 (collect после батча тулов) — управляется этим резолвером.
См. docs_md/actual/2026-07-29-gc-collect-placement-spec.md.
"""

from gc_core.config_modules.gc import gc_force_collect_enabled


class TestExplicitFlagOverrides:
    def test_explicit_true_enables_even_for_server(self):
        assert gc_force_collect_enabled(
            {"gc": {"force_collect": True}},
            is_tui=False,
            is_cli_mode=False,
        ) is True

    def test_explicit_false_disables_even_for_tui(self):
        assert gc_force_collect_enabled(
            {"gc": {"force_collect": False}},
            is_tui=True,
            is_cli_mode=True,
        ) is False

    def test_explicit_zero_is_falsy(self):
        assert gc_force_collect_enabled(
            {"gc": {"force_collect": 0}},
            is_tui=True,
        ) is False


class TestDefaultByMode:
    def test_cli_mode_default_on(self):
        assert gc_force_collect_enabled({}, is_cli_mode=True) is True

    def test_tui_default_on(self):
        assert gc_force_collect_enabled({}, is_tui=True) is True

    def test_server_default_off(self):
        assert gc_force_collect_enabled({}, is_tui=False, is_cli_mode=False) is False

    def test_cli_and_tui_both_on(self):
        assert gc_force_collect_enabled({}, is_tui=True, is_cli_mode=True) is True


class TestEdgeCases:
    def test_none_config_dict(self):
        assert gc_force_collect_enabled(None, is_cli_mode=True) is True
        assert gc_force_collect_enabled(None, is_tui=False, is_cli_mode=False) is False

    def test_empty_config(self):
        assert gc_force_collect_enabled({}, is_tui=True) is True

    def test_gc_key_not_dict(self):
        assert gc_force_collect_enabled(
            {"gc": "unexpected_string"},
            is_tui=True,
        ) is True

    def test_gc_key_without_force_collect(self):
        assert gc_force_collect_enabled(
            {"gc": {"other_key": 123}},
            is_tui=False,
            is_cli_mode=False,
        ) is False

    def test_force_collect_none_falsy(self):
        assert gc_force_collect_enabled(
            {"gc": {"force_collect": None}},
            is_tui=True,
        ) is False
