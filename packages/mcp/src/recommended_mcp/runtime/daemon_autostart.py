"""Автозапуск HTTP/SSE MCP-daemon'ов перед запуском агента.

Жил в ядре (`recommended_mcp.runtime.daemon_autostart`) как мост «фронтенд → рецепты», хотя
потребителей в самом ядре у него не было ни одного: зовут его `gc_cli.cli` и
`gc_tui`. Ради этого ядро импортировало `recommended_mcp` — пакет, которого нет в
его зависимостях и не может быть: `gc-mcp` сам зависит от `gc-core`, вышло бы
кольцо дистрибутивов. Переехал сюда 27.08.2026: код принадлежит рецептам (это
автозапуск ИХ daemon'ов), а все три профиля пинят `gc-mcp` жёстко.

Прежней охраны `except ImportError` здесь НЕТ и не нужно: рецепты — соседний
модуль того же дистрибутива. Свойство, ради которого охрана была точной, при этом
только усилилось: ошибка импорта ВНУТРИ рецептов (опечатка, отсутствующая
зависимость) летит как есть, а не выдаётся за «рецептов не установлено». Раньше
это обеспечивала сверка `exc.name`; теперь — отсутствие перехвата вовсе.
Сторож — `packages/mcp/tests/test_recipes_guard.py`.
"""

from __future__ import annotations

from typing import Any

_supervisor: Any = None


def ensure_mcp_daemons(
    settings: Any, *, disable_hint: str | None = None
) -> None:
    """Поднять недостающие MCP-daemon'ы перед запуском агента.

    ``settings`` — настройки агента: всё, что принимает ``settings_of``
    (контекст, ``LoadedSettings``, ``AppConfig``).

    Сбой автозапуска НЕ мешает фронтенду подняться. Автозапуск — удобство:
    агент работает и без него, со встроенными инструментами и с теми серверами,
    что живы. А стоил этот шаг всего: 01.09.2026 подключённый, но не
    установленный `science` уронил `gc-ua` трейсбеком FileNotFoundError ещё до
    первого экрана. В TUI автозапуск включён по умолчанию, так что защиты не
    было ни у кого.

    Внутри `ensure_daemons_for_agent` каждый рецепт уже завёрнут отдельно, чтобы
    один сломанный не мешал соседям. Здесь — второй рубеж: он ловит то, что
    случилось ДО цикла или ВОКРУГ него (битый манифест, недоступный каталог
    рецептов, сбой резолва пути), и делает обещание «фронтенд стартует всегда»
    свойством одной функции, а не дисциплиной каждого вызывающего.

    `disable_hint` — как ИМЕННО выключить автозапуск у вызывающей команды.
    Фраза уезжает в предупреждения ниже. Параметр, а не константа, потому что
    модуль общий: у `gc-ua` это `--no-auto-mcp`, у `gc-cli` такого флага нет
    вовсе — там автозапуск включается `-a`.

    Импорт рецептов оставлен ВНЕ `try` намеренно. По докстрингу модуля ошибка
    импорта внутри рецептов (опечатка, отсутствующая зависимость) обязана лететь
    как есть, а не выдаваться за «просто не завелось»: это поломка пакета, а не
    рабочее состояние. Держит `packages/mcp/tests/test_recipes_guard.py`.
    """
    global _supervisor
    from recommended_mcp.autostart import ensure_daemons_for_agent

    kwargs = {} if disable_hint is None else {"disable_hint": disable_hint}
    try:
        _supervisor = ensure_daemons_for_agent(settings, **kwargs)
    except Exception as exc:  # сознательно широко, см. докстринг
        _supervisor = None
        from recommended_mcp.autostart import _warn

        _warn(
            f"автозапуск MCP пропущен: {exc}. "
            "Агент стартует без daemon-серверов; встроенные инструменты и "
            "остальные MCP работают. Проверить: `gc-mcp list`."
            + (f" {disable_hint}." if disable_hint else ""),
            code="mcp_autostart_skipped",
        )


def shutdown_mcp_daemons() -> None:
    """Остановить daemon'ы, поднятые ensure_mcp_daemons() в этом процессе.
    Гашение упало — супервизор не забываем: `kill_mcp_daemons_now()` добьёт тех,
    кого оно не погасило."""
    global _supervisor
    if _supervisor is None:
        return
    _supervisor.shutdown_managed()
    _supervisor = None


def kill_mcp_daemons_now() -> None:
    """Последний рубеж аварийного выхода TUI: SIGKILL daemon'ам этого процесса
    без ожидания — если штатное `shutdown_mcp_daemons()` упало или не уложилось
    в бюджет."""
    supervisor = _supervisor  # штатное гашение может обнулить его параллельно
    if supervisor is not None:
        supervisor.kill_managed_now()
