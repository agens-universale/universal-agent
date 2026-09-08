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
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from gc_core import config_raw
from gc_core.config_modules.merge import deep_merge
from gc_core.config_modules.root import PROFILE_SCHEMA_ERROR_PREFIX, SCHEMA_ERROR_PREFIX

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


def _overlay_paths(base_path: str, *, profile_override: Optional[str] = None
                   ) -> list[tuple[str, str]]:
    """[(kind, path)] в порядке приоритета load_config: managed, project, затем
    (спека §2, ось 2 — контент профиля ложится ПОВЕРХ всех файловых слоёв) активный
    профиль, если указатель резолвится. Указатель — общим резолвом
    `config_modules.layers.resolve_profile_pointer`: тот же мерж base+managed+
    project, каким его резолвит `load_config_ex` (ось 1), а не `self._config`
    вызывающего экрана — у origins() своя, независимая от него копия. Раньше эта
    функция мержила слои сама (четвёртая по счёту редакция того же правила —
    находка ревью), и её собственная копия молчала о профиле на битой базе иначе,
    чем резолвит следующий старт; теперь обе стороны — один код.

    `profile_override` — как у `load_config_ex`: имя побеждает указатель, пустая
    строка снимает слой профиля вовсе, None — указатель из файлов. Сессия с
    `--profile` грузит не тот профиль, что назван в файле, и карта слоёв обязана
    считаться по тому же стеку (ревью: объяснение сломанного конфига называло
    базу вместо профиля из флага).

    Совпадающий с базовым файл пропускаем — ровно как load_config."""
    from pathlib import Path as _Path
    from gc_core.config_modules.env_vars import (
        managed_config_path,
        project_local_config_path,
    )
    from gc_core.config_modules.layers import resolve_profile_pointer
    from gc_core.config_modules.profiles import discover_profiles, profile_roots

    base_abs = os.path.abspath(base_path)
    out: list[tuple[str, str]] = []

    managed = managed_config_path()
    if managed.is_file() and os.path.abspath(str(managed)) != base_abs:
        out.append(("managed", str(managed)))

    project = project_local_config_path()
    if project is not None and os.path.abspath(project) != base_abs:
        out.append(("project", project))

    pointer = (profile_override if profile_override is not None
               else resolve_profile_pointer(base_path))
    if pointer:
        roots = profile_roots(_Path(os.getcwd()), _Path(base_path))
        ref = discover_profiles(roots).get(pointer)
        if ref is not None:
            kind = "profile:project" if ref.root_kind == "project" else "profile:base"
            out.append((kind, str(ref.path)))

    return out


def origins(base_path: str, dotted_keys: Iterable[str], *,
            candidates: Optional[Dict[Any, Dict[str, Any]]] = None,
            profile_override: Optional[str] = None) -> Dict[str, Origin]:
    """{dotted-путь: Origin} — что в базовом файле, чем перекрыто, что действует.

    Действующее значение считаем ТЕМ ЖЕ deep_merge, что и load_config: накатываем
    слои на сырые документы и смотрим dotted-путь в результате. Перекрывшим слоем
    считается последний, после наложения которого значение изменилось.

    `candidates` — {путь: документ} вместо чтения этих файлов с диска (базы или
    любого оверлея), тем же приёмом, что `layers._merge_layers`. Нужен отказу
    записи: его вердикт вынесен по стеку, где цель стоит ЕЩЁ НЕ ЗАПИСАННЫМ
    текстом, и ответ по диску назвал бы слой, которого валидатор не видел.
    `profile_override` — как у `_overlay_paths`.
    """
    keys = list(dotted_keys)
    _candidates = {Path(p).resolve(): doc for p, doc in (candidates or {}).items()}

    def _document(path):
        # `{}` — настоящий (пустой) кандидат, а не «кандидата нет»: `or` их бы спутал.
        doc = _candidates.get(Path(path).resolve())
        return config_raw.read_document(path) if doc is None else doc

    merged = _document(base_path)
    raw = {dotted: config_raw.value_at(merged, dotted) for dotted in keys}

    effective = dict(raw)
    kinds: Dict[str, str] = {dotted: "" for dotted in keys}
    paths: Dict[str, Optional[str]] = {dotted: None for dotted in keys}

    for overlay_kind, overlay_path in _overlay_paths(base_path, profile_override=profile_override):
        merged = deep_merge(merged, _document(overlay_path))
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


# Закрытый список ключей-подозреваемых (спека решения по нерезолвимому конфигу):
# текст ошибки не парсим, а сверяем с этими строками. Порядок задаёт порядок
# ответа `error_origins`, когда поломка размазана по нескольким файлам.
#
# Координата берётся у "; "-сегмента ЦЕЛИКОМ (голова до ": "), а подозреваемый —
# её секция или она сама: `assign.subagents.<имя>` ищется в файлах именно этим
# путём. Голая секция `assign` называла бы файл, где привязок больше, а не тот,
# где лежит сломанная (ревью: смешанная ошибка теряла строку про привязки).
ERROR_SUSPECT_KEYS = ("slots.default", "slots.light", "slots.max", "assign", "models", "profile")

# Отказ резолва указателя (`profiles.resolve_profile`) ключа не называет — он
# по-русски: 'профиль "<имя>" не найден; искал: …'. Без этой сверки файл с
# указателем не назывался вовсе и виновником выходила база (ревью).
_POINTER_NOT_FOUND = ('профиль "', '" не найден')


def error_keys(error_text: str) -> list:
    """Координаты ключей, названные текстом ошибки, — в порядке `ERROR_SUSPECT_KEYS`,
    внутри одного подозреваемого — в порядке текста. Ошибка не про ключи — пусто.

    Вводная схемы снимается до разбора — ОБЕ редакции, у конфига и у профиля
    (`SCHEMA_ERROR_PREFIX`/`PROFILE_SCHEMA_ERROR_PREFIX`): первый сегмент склейки
    `конфиг не проходит схему: slots.default: …` (или её профильный близнец
    `профиль не проходит схему: …`) иначе прятал бы свой ключ за вводной — и
    виновник первой (нередко единственной) ошибки терялся (ревью MR #80, #3:
    снималась только конфиговая вводная, профильная — нет). `removeprefix` двух
    подряд безопасен: текст начинается максимум с одной из них."""
    error_text = error_text.removeprefix(SCHEMA_ERROR_PREFIX).removeprefix(
        PROFILE_SCHEMA_ERROR_PREFIX)
    heads = [segment.split(": ", 1)[0] for segment in error_text.split("; ")]
    heads = ["profile" if head.startswith(_POINTER_NOT_FOUND[0])
             and head.endswith(_POINTER_NOT_FOUND[1]) else head for head in heads]
    keys: list = []
    for suspect in ERROR_SUSPECT_KEYS:
        for head in heads:
            if (head == suspect or head.startswith(f"{suspect}.")) and head not in keys:
                keys.append(head)
    return keys


@dataclass(frozen=True)
class ErrorOrigin:
    """Файл стека, где лежат ключи, названные ошибкой: `kind` — слой («base» для
    самой базы, иначе kind оверлея из `_overlay_paths`), `path` — его файл,
    `keys` — какие из подозреваемых он задаёт, в порядке `ERROR_SUSPECT_KEYS`."""

    kind: str
    path: str
    keys: tuple


def error_origins(base_path: str, error_text: str, *,
                  candidates: Optional[Dict[Any, Dict[str, Any]]] = None,
                  profile_override: Optional[str] = None) -> list:
    """Текст ошибки → файлы, где лежат названные ею ключи (`ErrorOrigin`, по одному
    на файл). `candidates` и `profile_override` — как у `origins`.

    Ключи ищутся не парсингом текста, а сверкой с закрытым списком
    `ERROR_SUSPECT_KEYS` (`error_keys`): голова "; "-сегмента до ": "
    (`model_sections_validation.py` склеивает несколько находок этим
    разделителем) — и есть координата, если её секция в списке; для найденных
    спрашивается `origins()` — она называет слой, чьё наложение последним
    изменило значение. Голова сегмента, а не вхождение где угодно в тексте:
    хвост «не найден в merged-реестре models», общий для ЛЮБОЙ ошибки слота,
    ложно приплетал бы секцию models к каждой поломке. Ключ, которого не задал ни
    один оверлей, — на базе: чинить его там. Ни один ключ не назван (битый YAML,
    нечитаемый файл) — сопоставлять нечего, ответ пуст: файл-виновник не
    выдумывается. Ошибка схемы (pydantic) координаты несёт не текстом, а `loc`
    — для неё тот же ответ даёт `key_origins`.

    Общий дом для объяснения нерезолвимого конфига при старте
    (`onboarding.explain_broken_config`) и отказа записи, вынесенного по стеку
    (`config_modules.registry_write`): вторая редакция правила «какой файл
    виноват» разошлась бы с первой на первой же правке списка подозреваемых."""
    return key_origins(base_path, error_keys(error_text), candidates=candidates,
                       profile_override=profile_override)


def key_origins(base_path: str, keys: Iterable[str], *,
                candidates: Optional[Dict[Any, Dict[str, Any]]] = None,
                profile_override: Optional[str] = None) -> list:
    """Координаты ключей → файлы стека, где они лежат (`ErrorOrigin`, по одному на
    файл, ключи — в порядке `keys`). Вторая половина `error_origins`, вынесенная
    ради ошибок, у которых координаты уже есть без текста (pydantic `loc`).

    Под `profile_override` указатель `profile` в файлах инертен — загрузчик его
    не читает, имя пришло из флага — и виновником не называется: человека
    послали бы править ключ, который в этой сессии ничего не решал."""
    keys = [key for key in keys if not (profile_override is not None and key == "profile")]
    if not keys:
        return []
    found = origins(base_path, keys, candidates=candidates, profile_override=profile_override)
    by_file: Dict[str, tuple] = {}
    for key in keys:
        origin = found[key]
        kind, path = ((origin.overlay_kind, origin.overlay_path)
                      if origin.overlay_path else ("base", str(base_path)))
        by_file.setdefault(path, (kind, []))[1].append(key)
    return [ErrorOrigin(kind=kind, path=path, keys=tuple(file_keys))
            for path, (kind, file_keys) in by_file.items()]
