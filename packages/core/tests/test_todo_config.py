from gc_core.config_modules.todo import (
    DEFAULT_TODO_REMINDER_STEPS,
    TodoConfig,
    normalize_todo_config,
)
from gc_core.settings import settings_of
from gc_testkit.settings import make_settings


def test_default_is_10():
    assert DEFAULT_TODO_REMINDER_STEPS == 10
    assert settings_of(make_settings({})).todo.reminder_steps == 10


def test_reads_todo_params():
    assert (
        settings_of(
            make_settings({"todo": {"reminder_steps": 5}})
        ).todo.reminder_steps
        == 5
    )


def test_garbage_is_rejected_by_schema():
    """Мусор в ``todo`` не доходит до чтения: схема отвергает его строго."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_settings({"todo": {"reminder_steps": "x"}})
    with pytest.raises(ValidationError):
        make_settings({"todo": "not a dict"})


def test_normalize_clamps_negative():
    cfg = TodoConfig(reminder_steps=-1)
    cfg = normalize_todo_config(cfg)
    assert cfg.reminder_steps == 0


def test_app_config_accepts_todo_section():
    from gc_core.config_modules.root import AppConfig

    cfg = AppConfig(todo={"reminder_steps": 7})
    assert cfg.todo.reminder_steps == 7
