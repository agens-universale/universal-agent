from gc_core.config_modules.todo import (
    DEFAULT_TODO_REMINDER_STEPS,
    TodoConfig,
    normalize_todo_config,
    todo_reminder_steps_from_config,
)


def test_default_is_10():
    assert DEFAULT_TODO_REMINDER_STEPS == 10
    assert todo_reminder_steps_from_config({}) == 10
    assert todo_reminder_steps_from_config(None) == 10


def test_reads_todo_params():
    assert (
        todo_reminder_steps_from_config({"todo_params": {"reminder_steps": 5}})
        == 5
    )


def test_zero_disables():
    assert (
        todo_reminder_steps_from_config({"todo_params": {"reminder_steps": 0}})
        == 0
    )


def test_negative_clamped_to_zero():
    assert (
        todo_reminder_steps_from_config(
            {"todo_params": {"reminder_steps": -3}}
        )
        == 0
    )


def test_garbage_falls_back_to_default():
    assert (
        todo_reminder_steps_from_config(
            {"todo_params": {"reminder_steps": "x"}}
        )
        == 10
    )
    assert todo_reminder_steps_from_config({"todo_params": "not a dict"}) == 10


def test_normalize_clamps_negative():
    cfg = TodoConfig(reminder_steps=-1)
    normalize_todo_config(cfg)
    assert cfg.reminder_steps == 0


def test_app_config_accepts_todo_section():
    from gc_core.config_modules.root import AppConfig

    cfg = AppConfig(todo={"reminder_steps": 7})
    assert cfg.todo.reminder_steps == 7
