import pytest
from gc_core.config import apply_cli_overrides, flat_config
from gc_core.settings import settings_from_flat


def _apply(base: dict | None = None, **kwargs):
    """CLI поверх настроек из плоского словаря: новый объект, исходный не трогается."""
    return apply_cli_overrides(settings_from_flat(base or {}), **kwargs)


class TestApplyCliOverridesTraceId:
    def test_absent_leaves_no_key(self):
        loaded = _apply()
        assert loaded.runtime.trace_id is None
        assert "trace_id" not in flat_config(loaded)

    def test_flag_value_is_normalized(self):
        loaded = _apply(trace_id="  wf-42  ")
        assert loaded.runtime.trace_id == "wf-42"
        assert flat_config(loaded)["trace_id"] == "wf-42"

    def test_blank_flag_leaves_no_key(self):
        loaded = _apply(trace_id="   ")
        assert loaded.runtime.trace_id is None
        assert "trace_id" not in flat_config(loaded)

    def test_invalid_flag_raises(self):
        with pytest.raises(ValueError, match="управляющие символы"):
            _apply(trace_id="wf\n42")

    def test_generic_override_is_normalized_too(self):
        # Generic-роут прогоняет значение через yaml.safe_load, поэтому сюда
        # может приехать int; нормализация обязана его починить.
        loaded = _apply(extra_overrides={"trace_id": 12345})
        assert loaded.runtime.trace_id == "12345"

    def test_flag_wins_over_generic_override(self):
        loaded = _apply(
            extra_overrides={"trace_id": "from-generic"},
            trace_id="from-flag",
        )
        assert loaded.runtime.trace_id == "from-flag"

    def test_empty_generic_override_is_dropped(self):
        loaded = _apply({"trace_id": ""})
        assert loaded.runtime.trace_id is None
        assert "trace_id" not in flat_config(loaded)

    def test_source_settings_are_not_mutated(self):
        # Новый API не мутирует вход: trace_id уезжает только в возвращённый объект.
        base = settings_from_flat({})
        loaded = apply_cli_overrides(base, trace_id="wf-1")
        assert base.runtime.trace_id is None
        assert loaded.runtime.trace_id == "wf-1"
