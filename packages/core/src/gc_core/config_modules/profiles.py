"""Лоадер профилей-файлов. Пользовательская сторона — docs/configuration.md,
раздел «Профили и уровни моделей».

Профиль — YAML-файл в одном из двух корней (проектный/`рядом с базой`), мержится
поверх проектного конфига под CLI-оверрайдами (см. `config.py::load_settings`).
Этот модуль — только механика поиска/чтения/пофайловой валидации одного профиля;
оркестрация мержа и итоговая (фаза 2) ссылочная целостность — в `config.py` /
`config_modules/model_sections_validation.py` соответственно.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import ValidationError

from gc_core.config_modules.compat import apply_compat, manual_edit_fact
from gc_core.config_modules.env_interpolation import (
    resolve_env_placeholders,
    tag_layer,
)
from gc_core.config_modules.migrations import migrate_document
from gc_core.config_modules.model_registry import (
    RESERVED_WORDS,
    validate_registry_pairs,
)
from gc_core.config_modules.root import (
    PROFILE_SCHEMA_ERROR_PREFIX,
    AppConfig,
    schema_error_details,
)
from gc_core.config_modules.unknown_keys import drop_unknown_keys
from gc_core.utils.user_text import unreadable_reason

# Бланк нового файла профиля (`onboarding_config_writer.create_profile_file`):
# ТОЛЬКО шапка-инструкция, без якорей секций (ревизия владельца по смоку:
# `models: {}` / `slots: {}` заставляли свежий профиль «переопределять» секции,
# которых он не трогал, — витрина честно перечисляла ключи файла и путала).
# Секции допишет writer при первом реальном действии (`ensure_section_anchor_text`
# синтезирует якорь сам). Именно бланк, а не учебный пример с готовой записью:
# пример нёс бы литеральный токен и висячие ссылки слотов на алиас, которого у
# пользователя нет.
PROFILE_SKELETON = (
    "# Профиль — отдельный файл-конфиг: переключаемый набор любых настроек\n"
    "# (модели, уровни, гварды и другие секции). Впишите здесь только то, что\n"
    "# должно отличаться, — незаданное наследуется от основного конфига.\n"
)


@dataclass(frozen=True)
class ProfileRef:
    name: str
    path: Path
    root_kind: Literal["project", "base"]


@dataclass
class ProfileMeta:
    """Метаданные резолва профиля для одного вызова `load_settings` (спека §3.3)."""

    active_name: Optional[str]
    ref: Optional[ProfileRef]
    mtime_set: dict[str, Optional[float]]
    notices: list[str] = field(default_factory=list)


def profile_roots(project_dir: Path, base_config_path: Path) -> list[Path]:
    """Два корня поиска профилей, проектный первым (спека §3.1).

    При явном `--config` второй корень — каталог этого файла, не `~/.gc/profiles`
    (следствие того, что `base_config_path` тут — уже резолвленный путь запуска).
    """
    return [
        Path(project_dir) / ".gc" / "profiles",
        Path(base_config_path).resolve().parent / "profiles",
    ]


def _profile_files(root: Path) -> list[Path]:
    """Файлы-кандидаты профиля в корне: имя `*.yml` И ФАКТ ФАЙЛА.

    Отбор по `isfile` — то же правило, каким загрузчик отбирает managed-слой
    (`layers._merge_layers`: `managed_path.is_file()`). Голый глоб отдавал сюда и
    КАТАЛОГ с именем `work.yml`, а дальше он вредил дважды: открытие такого
    «профиля» падало `OSError` мимо всех сторожей (трейсбек вместо объяснения), а
    тёзка из ПРИОРИТЕТНОГО корня ещё и заслонял настоящий профиль из второго —
    дедупликация ниже идёт по имени, и победитель уже не пересматривается. Первое
    чинится и обёрткой чтения (она есть, `_read_yaml_document`), второе — только
    здесь: витрина (`discover_all_refs`) и список «есть: …» тоже обязаны не
    называть профилем то, что им не является.

    Дом общий на оба обхода корней: вторая редакция того же отбора разъехалась бы
    с первой ровно так, как разъехались управляемые слои и профили."""
    return [path for path in sorted(root.glob("*.yml")) if path.is_file()]


def _register_profile(
    result: dict, path: Path, root_kind, notices: Optional[list[str]]
) -> None:
    name = path.stem
    if name in RESERVED_WORDS:
        if notices is not None:
            notices.append(
                f'{path}: имя профиля "{name}" зарезервировано — файл проигнорирован'
            )
        return
    if name in result:
        return  # тёзка из менее приоритетного корня — проектный уже выиграл
    result[name] = ProfileRef(name=name, path=path, root_kind=root_kind)


def discover_profiles(
    roots: list[Path], *, notices: Optional[list[str]] = None
) -> dict[str, ProfileRef]:
    """Дискавери профилей по корням (в порядке приоритета — первый найденный побеждает).

    Зарезервированные имена (`RESERVED_WORDS`) пропускаются + warning в `notices`,
    если список передан (спека §3.2).
    """
    result: dict[str, ProfileRef] = {}
    for idx, root in enumerate(roots):
        root_kind: Literal["project", "base"] = (
            "project" if idx == 0 else "base"
        )
        root = Path(root)
        if not root.is_dir():
            continue
        for path in _profile_files(root):
            _register_profile(result, path, root_kind, notices)
    return result


def _format_available(discovered: dict[str, ProfileRef]) -> str:
    base_names = sorted(
        n for n, ref in discovered.items() if ref.root_kind == "base"
    )
    project_names = sorted(
        n for n, ref in discovered.items() if ref.root_kind == "project"
    )
    segments = []
    if base_names:
        segments.append(f"{', '.join(base_names)} (глобальные)")
    if project_names:
        segments.append(f"{', '.join(project_names)} (проектный)")
    return ", ".join(segments) if segments else "нет"


def resolve_profile(
    name: str, roots: list[Path], *, notices: Optional[list[str]] = None
) -> ProfileRef:
    """Найти профиль `name` в `roots`. Не найден / имя зарезервировано → `ValueError`
    (спека §3.1, §3.2) — тот же класс, каким `load_config` уже падает на невалидном
    (ссылочно) конфиге, см. `model_sections_validation.validate_model_sections`.

    `notices` пробрасывается в `discover_profiles` — иначе warning на зарезервированное
    имя файла, обнаруженное дискавери попутно с резолвом указателя, не долетал бы до
    вызывающего кода (спека §3.2)."""
    if name in RESERVED_WORDS:
        raise ValueError(
            f'profile: имя "{name}" зарезервировано ({", ".join(RESERVED_WORDS)})'
        )
    discovered = discover_profiles(roots, notices=notices)
    if name in discovered:
        return discovered[name]
    candidates = ", ".join(str(Path(root) / f"{name}.yml") for root in roots)
    raise ValueError(
        f'профиль "{name}" не найден; искал: {candidates}; есть: {_format_available(discovered)}'
    )


def _read_yaml_document(path: Path) -> dict:
    # Неразбираемый файл — `ValueError`, как и не-словарь ниже: `validate_profile_text`
    # тот же перевод уже держит для текста, а на файл его не распространили, и
    # `yaml.YAMLError` уходил мимо `except (OSError, ValueError)` у pre-write
    # валидации стека — то есть ронял UI вместо отката к одному файлу.
    #
    # `OSError` — та же граница, что у слоёв (`layers._readable_layer_document`):
    # путь НА МЕСТЕ (его отобрал `_profile_files`), но не открывается — права,
    # каталог на месте файла, отвалившийся сетевой диск. Голым он уходил мимо
    # `except ValueError` ОБЕИХ точек входа (`cli._run_init`, `tui.main`) и мимо
    # всех трёх ступеней лестницы починки (`onboarding.load_config_for_repair`):
    # человек получал трейсбек ровно там, где заводили объяснение. Формат
    # сообщения — тот же «<path>: …», по которому `explain_broken_config`
    # опознаёт виноватый файл. Слой профиля читается ОТСЮДА, а не через
    # `_readable_layer_document`, и своей границы у него до сих пор не было.
    # `UnicodeDecodeError` (профиль сохранён в UTF-16) — тот же «не прочитался»,
    # только подкласс `ValueError`: без пути в сообщении объяснение винило базу.
    try:
        with open(path, encoding="utf-8") as file:
            try:
                raw = yaml.safe_load(file) or {}
            except yaml.YAMLError as exc:
                raise ValueError(f"{path}: невалидный YAML: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(unreadable_reason(path, exc)) from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: профиль должен быть YAML-документом (словарём)"
        )
    return raw


def find_literal_tokens(raw: dict) -> list[str]:
    """Алиасы `models.<alias>` с литеральным (не `${ENV:...}`) `token` в документе
    ДО env-интерполяции — секрет-политика проектных профилей (спека §3.5)."""
    aliases: list[str] = []
    models = raw.get("models")
    if not isinstance(models, dict):
        return aliases
    for alias, entry in models.items():
        if not isinstance(entry, dict):
            continue
        token = entry.get("token")
        if isinstance(token, str) and token and "${ENV:" not in token:
            aliases.append(alias)
    return aliases


def _validate_profile_document(
    raw: dict, *, source: Optional[str] = None
) -> None:
    """Общая фаза-1 валидация документа профиля: схема `AppConfig` (резерв-имена
    алиасов моделей проверяет её field_validator) + запрет ключа `profile`
    (анти-рекурсия, спека §3.2). Переиспользуется `load_profile_document`
    (документ уже прочитан с диска, `source` — путь файла для сообщения) и
    `validate_profile_text` (документ ещё не записан на диск, `source` не задан —
    сообщение без привязки к пути, как `config.validate_config_text`)."""
    prefix = f"{source}: " if source else ""
    try:
        # Лишние ключи профиля отбросит общая загрузка (с уведомлением и
        # файлом); здесь они документ не отвергают.
        AppConfig.model_validate(drop_unknown_keys(raw, AppConfig)[0])
    except ValidationError as exc:
        # Не сырой `str(exc)`: pydantic-дамп несёт `input_value=<repr записи>` —
        # токен НЕАКТИВНОГО профиля уезжал в нотис секции «Модели» этим, вторым
        # путём (первый — `layers._app_config_from_raw`; ревью MR #80, L28).
        raise ValueError(
            f"{prefix}{PROFILE_SCHEMA_ERROR_PREFIX}{schema_error_details(exc)}"
        ) from exc
    if "profile" in raw:
        raise ValueError(
            f'{prefix}ключ "profile" внутри профиля запрещён — цепочки профилей запрещены'
        )


def load_profile_document(
    path: Path,
    *,
    warn_literal_tokens: bool = False,
    notices: Optional[list[str]] = None,
    unresolved: Optional[list] = None,
    layer: str = "profile",
) -> dict:
    """Прочитать + провалидировать документ одного профиля (спека §3.4):

    YAML → (опционально) warning на литеральные токены → per-file env-интерполяция →
    миграция формата (`config_version`) → compat-слой → частичная валидация
    `AppConfig` (фаза 1, ссылочная целостность не проверяется) → запрет ключа
    `profile:` внутри (анти-рекурсия, спека §3.2).

    `unresolved` — строгий режим `${ENV:…}`/`${FILE:…}` (`config.load_settings`):
    неразрешённые ссылки копятся туда с путём файла и слоем `layer`, а проверки
    пары и схемы для такого документа пропускаются — загрузчик всё равно
    откажет списком незаданных переменных, и вердикт по подставленным пустым
    строкам был бы следствием, а не причиной.
    """
    raw = _read_yaml_document(path)
    if warn_literal_tokens and notices is not None:
        for alias in find_literal_tokens(raw):
            notices.append(
                f"{path}: models.{alias}.token — литеральный токен в проектном профиле; "
                f"используйте ${{ENV:VAR}} или наследование из базового конфига"
            )
    start = len(unresolved) if unresolved is not None else 0
    raw = resolve_env_placeholders(
        raw, unresolved=unresolved, source=str(path)
    )
    if unresolved is not None:
        tag_layer(unresolved, start, layer=layer)
    raw, migration_facts = migrate_document(raw, source=str(path))
    if notices is not None:
        notices.extend(migration_facts)
    # Compat ДО схемы — как `load_settings` делает для базы. Схема с
    # extra="forbid" иначе роняет ВЕСЬ профиль на устаревшем ключе, который в
    # config.yml тихо выбрасывается с нотисом; а в профиль такой ключ попадает
    # ровно копипастой секции из старого config.yml. Факты — в тот же канал
    # notices, с путём файла: иначе непонятно, в каком из документов ключ.
    for fact in apply_compat(raw):
        if notices is not None:
            notices.append(f"{path}: {manual_edit_fact(fact)}")
    if unresolved is not None and len(unresolved) > start:
        return raw
    # Пары «адрес+токен» — по СЫРОМУ документу и ДО схемы: слои сливаются позже,
    # и половина пары, наложенная на чужую полную, даёт корректный на вид итог,
    # пока токен базы уходит на адрес этого файла. Здесь же видно, чей это файл.
    validate_registry_pairs(raw, source=str(path))
    _validate_profile_document(raw, source=str(path))
    return raw


def validate_profile_text(text: str) -> None:
    """Контрольная валидация текста профиля перед записью (writer/Settings).

    Тот же конвейер, что `load_profile_document`, но от строки, ещё не записанной
    на диск: YAML → env-интерполяция → compat → фаза-1 схема → запрет ключа
    `profile`. Факты compat здесь отбрасываются (канала notices у pre-write
    валидатора нет, а докладывает их загрузчик при следующем чтении файла) —
    важно, что вердикт «валидно» совпадает с вердиктом загрузчика. Инъецируется
    в `onboarding_config_writer.write_text/write_values(..., validate=)` — та же
    роль, что `config.validate_config_text` у config.yml. Бросает `ValueError`
    (на кривом YAML — с координатой строки/столбца из парсера).
    """
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"невалидный YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("профиль должен быть YAML-документом (словарём)")
    raw, _facts = migrate_document(resolve_env_placeholders(raw))
    apply_compat(raw)
    _validate_profile_document(raw)


def mtime_or_none(path: Path) -> Optional[float]:
    """`stat().st_mtime` или `None`, если файла нет — не падает на отсутствующем пути
    (спека §3.3: отсутствие файла тоже «состояние», не ошибка)."""
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def discover_all_refs(roots: list[Path]) -> dict[str, list[ProfileRef]]:
    """Все найденные ссылки на профиль по имени, БЕЗ дедупликации тёзок (в отличие
    от `discover_profiles`, победившим первым идёт проектный корень) — витрине
    Settings (спека §12.3) нужен весь список тёзок, чтобы показать перекрытый файл
    в hint, не только победившее имя. Зарезервированные имена по-прежнему пропускаются
    молча (витрина — не место для повторного warning'а дискавери загрузчика)."""
    result: dict[str, list[ProfileRef]] = {}
    for idx, root in enumerate(roots):
        root_kind: Literal["project", "base"] = (
            "project" if idx == 0 else "base"
        )
        root = Path(root)
        if not root.is_dir():
            continue
        for path in _profile_files(root):
            name = path.stem
            if name in RESERVED_WORDS:
                continue
            result.setdefault(name, []).append(
                ProfileRef(name=name, path=path, root_kind=root_kind)
            )
    return result


def find_active_ref(
    name: Optional[str], project_dir: Path, base_config_path: Path
) -> Optional[ProfileRef]:
    """`ProfileRef` действующего имени профиля или `None` (нет имени / указатель
    не резолвится — санация уже случилась выше по стеку, здесь просто нечего
    показать). Общий источник для витрин `/config`, `/status`, Settings —
    единая точка резолва имени в файл, не размазанная по вызывающим модулям."""
    if not name:
        return None
    roots = profile_roots(project_dir, base_config_path)
    return discover_profiles(roots).get(name)


_MODEL_SECTION_KEYS = frozenset(
    {"models", "slots", "assign", "model", "level"}
)


@dataclass(frozen=True)
class ProfileDisplaySummary:
    """Витринные факты о содержимом файла профиля (спека §12.3): состав слотов
    строкой + сырые имена секций-оверлеев сверх секций моделей. Презентацию
    (усечение, суффикс «+имя») собирает вызывающий экран — здесь только то, что
    механически лежит в документе."""

    slots: dict[str, str]
    overlay_sections: list[str]
    error: Optional[str] = None


def profile_display_summary(path: Path) -> ProfileDisplaySummary:
    """Прочитать сырой документ профиля для витрины: `slots` как есть + топ-уровневые
    ключи документа МИНУС `models`/`slots`/`assign`/`level`, отсортированные (спека
    §12.3 — «механически, сырыми именами»). Ошибка чтения/YAML — не бросается
    (профиль всё равно должен быть виден в списке), а возвращается как `error`."""
    try:
        raw = _read_yaml_document(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return ProfileDisplaySummary(
            slots={}, overlay_sections=[], error=str(exc)
        )
    slots = raw.get("slots")
    overlay = sorted(k for k in raw if k not in _MODEL_SECTION_KEYS)
    return ProfileDisplaySummary(
        slots=dict(slots) if isinstance(slots, dict) else {},
        overlay_sections=overlay,
        error=None,
    )


__all__ = [
    "PROFILE_SKELETON",
    "ProfileDisplaySummary",
    "ProfileMeta",
    "ProfileRef",
    "discover_all_refs",
    "discover_profiles",
    "find_active_ref",
    "find_literal_tokens",
    "load_profile_document",
    "mtime_or_none",
    "profile_display_summary",
    "profile_roots",
    "resolve_profile",
    "validate_profile_text",
]
