"""Поля config_dict, которые вычисляет рантайм TUI — в файле конфига их нет.

Ставятся один раз при старте (tui/__init__) и ОБЯЗАНЫ переживать hot-reload
(TuiApp.reload_config): тот перечитывает файл с диска, где этих полей нет, — без
явного переноса они молча исчезают.

`launch_root` — снапшот cwd на старте. Из него живут периметр path_policy (границы
записи), project_data и Diff-TUI. Пересчитывать его из live cwd нельзя: агент мог
сделать cd внутри сессии, и периметр съехал бы вместе с ним.
"""

from __future__ import annotations

from pathlib import Path

# Ключи, которых нет в файле конфига: их считает TUI. Новое такое поле — добавлять сюда,
# иначе оно потеряется при первом же сохранении настроек.
RUNTIME_KEYS = ("launch_root", "console_trace")


def apply_runtime_fields(config_dict: dict) -> None:
    """Проставить поля рантайма TUI — один раз, на старте."""
    config_dict["console_trace"] = False  # TUI владеет alt-screen выводом
    config_dict["launch_root"] = str(Path.cwd().resolve())  # снапшот, не live cwd


def carry_over_runtime_fields(old: dict, new: dict) -> None:
    """Перенести поля рантайма в свежеперечитанный конфиг (hot-reload)."""
    for key in RUNTIME_KEYS:
        if key in old:
            new[key] = old[key]
