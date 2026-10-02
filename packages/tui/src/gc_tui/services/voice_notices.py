"""Стартовые уведомления голосового ввода.

Жили в `gc_core.startup_notices` и начинались с `if not is_tui: return []` — то есть
`gc-cli` и `gc-server` носили с собой код, который на каждом старте возвращал пустоту,
а ядро ради него импортировало `voice.recorder` и `voice_runtime.state`. Голос
включается только в TUI, поэтому и уведомления о нём собирает TUI и передаёт готовыми
в `collect_startup_notices(..., extra=...)`.

Реестра поставщиков намеренно НЕТ: у сборки один вызывающий на фронтенд, параметр
достаточен, а глобальный список пришлось бы подметать в тестах — как подметают реестр
слушателей runtime-уведомлений (`clear_runtime_notices(listeners=True)`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from gc_core.settings import runtime_of, settings_of
from gc_core.startup_notices import Notice


def _extras(config_dict: Dict[str, Any]) -> List[Notice]:
    """Перекрытие managed-слоем и битая установка. Зовётся в конце `voice_notices`."""
    overridden = runtime_of(config_dict).voice_overridden_by_managed
    extra: List[Notice] = []
    if overridden:
        extra.append(
            Notice(
                "info",
                "voice_overridden",
                f"voice: настройки из config.yml ({overridden}) перекрыты локальным "
                "сервисом (config.local.yml). Вернуть — gc-voice remove.",
                highlights=("voice", "config.local.yml", "gc-voice remove"),
            )
        )
    # Битая установка: state есть, а окружение снесено/повреждено.
    from gc_tui.voice_runtime.state import (
        load_state,
    )  # ленивый: не тянуть при импорте

    st = load_state()
    if st is not None and not Path(st.root).is_dir():
        extra.append(
            Notice(
                "warning",
                "voice_install_broken",
                "voice: локальная установка повреждена (нет каталога сервиса) — выполните gc-voice install заново.",
                highlights=("voice", "gc-voice install"),
            )
        )
    return extra


def voice_notices(config_dict: Dict[str, Any]) -> List[Notice]:
    """Конфиг-проблемы голосового ввода. Пустая секция — не замечание.

    `config_dict` — настройки окна (`LoadedSettings`): признак перекрытия
    managed-слоем живёт в их рантайме, плоский словарь его не переносит."""
    from gc_core.config_modules.field_meta import plain_dump
    from gc_core.config_modules.voice import (
        voice_enabled,
        voice_section_filled,
    )

    voice = plain_dump(settings_of(config_dict).voice)
    if not voice_section_filled(voice):
        return []
    if not voice_enabled(voice):
        # Токен опционален (voice_enabled) — не обещаем юзеру лишнего
        return [
            Notice(
                "warning",
                "voice_incomplete",
                "voice: секция заполнена не до конца — нужен url; голосовой ввод неактивен.",
                highlights=("voice",),
            )
        ]
    from gc_tui.voice.recorder import (
        available,
    )  # ленивый: аудио-стек может отсутствовать

    notices: List[Notice] = []
    if not available():
        notices.append(
            Notice(
                "warning",
                "voice_no_sounddevice",
                "voice: настроен, но аудио-стек sounddevice недоступен — голосовой "
                "ввод неактивен (Linux: установите libportaudio2).",
                highlights=("voice", "sounddevice", "libportaudio2"),
            )
        )
    # Доп. замечания (перекрытие, битая установка) собираем ВСЕГДА, а не только когда
    # аудио-стек на месте: раньше отсутствие sounddevice возвращало свой Notice и
    # перекрывало их: машина без libportaudio2 — ровно та, где полусломанная локальная
    # установка вероятнее всего, а человек о ней не узнавал.
    return notices + _extras(config_dict)
