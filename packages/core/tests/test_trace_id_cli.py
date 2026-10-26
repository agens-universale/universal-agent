import pytest
from gc_core.config import apply_cli_overrides
from gc_testkit.settings import make_settings


def _apply(base: dict | None = None, **kwargs):
    """CLI поверх настроек из плоского словаря: новый объект, исходный не трогается."""
    return apply_cli_overrides(make_settings(base or {}), **kwargs)


class TestApplyCliOverridesTraceId:
    def test_absent_leaves_no_trace_id(self):
        loaded = _apply()
        assert loaded.runtime.trace_id is None

    def test_flag_value_is_normalized(self):
        loaded = _apply(trace_id="  wf-42  ")
        assert loaded.runtime.trace_id == "wf-42"

    def test_blank_flag_leaves_no_trace_id(self):
        loaded = _apply(trace_id="   ")
        assert loaded.runtime.trace_id is None

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

    def test_source_settings_are_not_mutated(self):
        # Новый API не мутирует вход: trace_id уезжает только в возвращённый объект.
        base = make_settings({})
        loaded = apply_cli_overrides(base, trace_id="wf-1")
        assert base.runtime.trace_id is None
        assert loaded.runtime.trace_id == "wf-1"
