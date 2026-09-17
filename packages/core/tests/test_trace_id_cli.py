import pytest

from gc_core.config import apply_cli_overrides


class TestApplyCliOverridesTraceId:
    def test_absent_leaves_no_key(self):
        cfg = {}
        apply_cli_overrides(cfg)
        assert "trace_id" not in cfg

    def test_flag_value_is_normalized(self):
        cfg = {}
        apply_cli_overrides(cfg, trace_id="  wf-42  ")
        assert cfg["trace_id"] == "wf-42"

    def test_blank_flag_leaves_no_key(self):
        cfg = {}
        apply_cli_overrides(cfg, trace_id="   ")
        assert "trace_id" not in cfg

    def test_invalid_flag_raises(self):
        with pytest.raises(ValueError, match="управляющие символы"):
            apply_cli_overrides({}, trace_id="wf\n42")

    def test_generic_override_is_normalized_too(self):
        # Generic-роут прогоняет значение через yaml.safe_load, поэтому сюда
        # может приехать int; нормализация обязана его починить.
        cfg = {}
        apply_cli_overrides(cfg, extra_overrides={"trace_id": 12345})
        assert cfg["trace_id"] == "12345"

    def test_flag_wins_over_generic_override(self):
        cfg = {}
        apply_cli_overrides(
            cfg, extra_overrides={"trace_id": "from-generic"}, trace_id="from-flag"
        )
        assert cfg["trace_id"] == "from-flag"

    def test_empty_generic_override_is_dropped(self):
        cfg = {"trace_id": ""}
        apply_cli_overrides(cfg)
        assert "trace_id" not in cfg
