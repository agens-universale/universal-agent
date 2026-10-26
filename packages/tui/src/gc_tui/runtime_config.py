"""Что вычисляет рантайм TUI поверх загруженных настроек — в файле конфига этого нет.

Ставится один раз при старте (tui/__init__) и ОБЯЗАНО переживать hot-reload
(TuiApp.reload_config): тот перечитывает файл с диска, где этих значений нет, —
поэтому перечитка отдаёт сюда прежний ``launch_root``.

`launch_root` — снапшот cwd на старте. Из него живут периметр path_policy (границы
записи), project_data и Diff-TUI. Пересчитывать его из live cwd нельзя: агент мог
сделать cd внутри сессии, и периметр съехал бы вместе с ним.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from gc_core.settings import LoadedSettings


def apply_tui_runtime(
    loaded: LoadedSettings, *, launch_root: Optional[str] = None
) -> LoadedSettings:
    """Настройки под TUI: alt-screen без консольной трассы, снапшот cwd.

    ``launch_root`` — снапшот прежней загрузки (hot-reload); без него — cwd сейчас.
    """
    root = launch_root or str(Path.cwd().resolve())
    return loaded.with_runtime(launch_root=root, is_tui=True).with_overrides(
        {"logging.console_trace": False}, layer="runtime"
    )
