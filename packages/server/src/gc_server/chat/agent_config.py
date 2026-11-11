"""Настройки хода: свой каталог и своя ролевая вставка."""

from __future__ import annotations

from gc_core.settings import LoadedSettings, settings_of


def prepare_agent_settings(
    base: LoadedSettings,
    *,
    cwd: str,
    instructions_path: str,
    variant: str | None = None,
) -> LoadedSettings:
    """Общие настройки процесса не меняются: правка даёт новый объект.

    У Responses и Chat Completions разные вставки, поэтому путь ставится
    на копию перед ``AsyncAgent.create``, а не один раз на процесс. С
    хранилищем агента в базе вставка — вариант роли: ``variant``
    (``completions``) даёт ``special_instructions@<роль>.<variant>``.
    """
    updates = {
        "paths.chat_completions_special_instructions_path": instructions_path
    }
    role = settings_of(base).agent.role
    if role and variant:
        updates["agent.special_instructions_role"] = f"{role}.{variant}"
    return base.with_overrides(updates, layer="runtime").with_runtime(
        launch_root=cwd
    )


def agent_io(settings, cwd, backend, cache):
    """Локальный I/O — на диалог; каталог MCP-тулов — общий на поверхность."""
    from gc_core.backends.factory import parse_os_backend_kind
    from gc_core.backends.local import LocalFsBackend

    if type(backend) is LocalFsBackend or (
        backend is None and parse_os_backend_kind(settings) == "local"
    ):
        cap = (
            backend.exec_output_capture_max_chars
            if backend is not None
            else settings_of(settings).agent.exec_output_capture_max_chars
        )
        scoped = LocalFsBackend(cwd=cwd, exec_output_capture_max_chars=cap)
        # Для local MCP запускается в процессе агента, а не через scoped:
        # смена cwd файловых инструментов не требует нового discovery.
        return scoped, cache
    return backend, cache
