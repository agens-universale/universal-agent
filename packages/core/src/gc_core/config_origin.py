"""Происхождение действующего значения поля конфига: файл, оверлей или окружение.

Экран настроек правит БАЗОВЫЙ файл, но load_config кладёт поверх него оверлеи
(machine-managed ~/.gc/config.local.yml и проектный <cwd>/.gc/config.yml). Без
этого модуля /settings показывает значение базового файла как действующее — и
молчит, когда сохранённая правка не подействует, потому что поле перекрыто.

Слияние слоёв НЕ дублируется: пути берём у тех же функций, что зовёт load_config,
а семантику наложения — у того же deep_merge (config_modules.merge). Своих правил
слияния здесь нет: иначе экран рассказывал бы про конфиг не то, что делает
load_config (пустая секция в оверлее затирает узел, списки склеиваются, …).
Модуль ничего не рисует и ничего не пишет.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from gc_core import config_raw
from gc_core.config_modules.merge import deep_merge

# Подписи слоёв для UI (значение — то, что видит пользователь). Профильные подписи —
# шаблоны с `{name}` (имя профиля = стем пути файла, см. overlay_label ниже): одно
# имя-кандидат на корень (`profile:project`/`profile:base`), а не занятое статикой
# «имя из шаблона» — иначе KeyError на любом активном профиле (спека §12.3).
OVERLAY_LABELS: Dict[str, str] = {
    "managed": "~/.gc/config.local.yml",
    "project": ".gc/config.yml в проекте",
    "profile:project": "профилем {name} (проектным)",
    "profile:base": "профилем {name} (глобальным)",
}


def overlay_label(origin: "Origin") -> str:
    """Человекочитаемая подпись перекрывшего слоя. Профильные подписи (`profile:*`)
    подставляют имя профиля из пути перекрывшего файла (`overlay_path`) — оно не
    хранится отдельно в `Origin`, но однозначно восстанавливается как имя файла
    без расширения (правило именования профилей, спека §1)."""
    label = OVERLAY_LABELS[origin.overlay_kind]
    if origin.overlay_kind.startswith("profile:") and origin.overlay_path:
        from pathlib import Path
        label = label.format(name=Path(origin.overlay_path).stem)
    return label


@dataclass(frozen=True)
class Origin:
    """Откуда взялось действующее значение поля."""

    raw: Any                      # значение в БАЗОВОМ файле, как записано (может быть ${ENV:VAR})
    overlay_kind: str             # "" | "managed" | "project" — кто перекрыл
    overlay_path: Optional[str]   # путь перекрывающего файла (None — не перекрыто)
    effective: Any                # что действует после всех слоёв (до env-резолва)

    @property
    def is_overridden(self) -> bool:
        return self.overlay_kind != ""


def _overlay_paths(base_path: str) -> list[tuple[str, str]]:
    """[(kind, path)] в порядке приоритета load_config: managed, project, затем
    (спека §2, ось 2 — контент профиля ложится ПОВЕРХ всех файловых слоёв) активный
    профиль, если указатель резолвится. Указатель читаем из того же мержа
    base+managed+project, которым его резолвит `load_config_ex` (ось 1) — не из
    `self._config`: у origins() своя копия мержа, отдельная от вызывающего экрана.

    Совпадающий с базовым файл пропускаем — ровно как load_config."""
    from pathlib import Path as _Path
    from gc_core.config import _project_local_config_path
    from gc_core.config_modules.env_vars import managed_config_path
    from gc_core.config_modules.profiles import discover_profiles, profile_roots

    base_abs = os.path.abspath(base_path)
    out: list[tuple[str, str]] = []
    merged = config_raw.read_document(base_path)

    managed = managed_config_path()
    if managed.is_file() and os.path.abspath(str(managed)) != base_abs:
        out.append(("managed", str(managed)))
        merged = deep_merge(merged, config_raw.read_document(str(managed)))

    project = _project_local_config_path()
    if project is not None and os.path.abspath(project) != base_abs:
        out.append(("project", project))
        merged = deep_merge(merged, config_raw.read_document(project))

    pointer = str(merged.get("profile") or "").strip()
    if pointer:
        roots = profile_roots(_Path(os.getcwd()), _Path(base_path))
        ref = discover_profiles(roots).get(pointer)
        if ref is not None:
            kind = "profile:project" if ref.root_kind == "project" else "profile:base"
            out.append((kind, str(ref.path)))

    return out


def origins(base_path: str, dotted_keys: Iterable[str]) -> Dict[str, Origin]:
    """{dotted-путь: Origin} — что в базовом файле, чем перекрыто, что действует.

    Действующее значение считаем ТЕМ ЖЕ deep_merge, что и load_config: накатываем
    слои на сырые документы и смотрим dotted-путь в результате. Перекрывшим слоем
    считается последний, после наложения которого значение изменилось.
    """
    keys = list(dotted_keys)
    merged = config_raw.read_document(base_path)
    raw = {dotted: config_raw.value_at(merged, dotted) for dotted in keys}

    effective = dict(raw)
    kinds: Dict[str, str] = {dotted: "" for dotted in keys}
    paths: Dict[str, Optional[str]] = {dotted: None for dotted in keys}

    for overlay_kind, overlay_path in _overlay_paths(base_path):
        merged = deep_merge(merged, config_raw.read_document(overlay_path))
        for dotted in keys:
            value = config_raw.value_at(merged, dotted)
            if value != effective[dotted]:
                effective[dotted] = value
                kinds[dotted], paths[dotted] = overlay_kind, overlay_path

    return {
        dotted: Origin(raw=raw[dotted], overlay_kind=kinds[dotted],
                       overlay_path=paths[dotted], effective=effective[dotted])
        for dotted in keys
    }
