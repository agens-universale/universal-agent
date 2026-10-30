"""Смена набора плагинов посреди сессии TUI (§8.3).

Окно сверяет отпечаток снимка на границе хода и, если набор сменился,
собирает новый снимок. Клонов при этом нет: цикл Textual уже идёт,
недостающий источник уходит в диагностику до перезапуска.
"""

from __future__ import annotations

from gc_core.plugins.catalog import build_plugin_snapshot, plugin_fingerprint
from gc_core.settings import LoadedSettings


def plugin_set_changed(loaded: LoadedSettings) -> bool:
    """Отпечаток снимка в настройках разошёлся с диском."""
    return loaded.runtime.plugins.fingerprint != plugin_fingerprint(loaded)


def refresh_plugins(loaded: LoadedSettings) -> LoadedSettings:
    """Настройки с новым снимком набора.

    В ``RuntimeEnv.notices`` остаются только стартовые уведомления снимка,
    которые всё ещё в силе; новые туда не дописываются — окно показывает
    их уведомлением рантайма, иначе они стояли бы в списке дважды."""
    old = loaded.runtime
    snap = build_plugin_snapshot(loaded, clone=False)
    notices = tuple(
        n
        for n in old.notices
        if n not in old.plugins.notices or n in snap.notices
    )
    return loaded.with_runtime(plugins=snap, notices=notices)
