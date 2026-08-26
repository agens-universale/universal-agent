"""Лоадер профилей-файлов. Пользовательская сторона — docs/configuration.md,
раздел «Профили и уровни моделей».

Профиль — YAML-файл в одном из двух корней (проектный/`рядом с базой`), мержится
поверх проектного конфига под CLI-оверрайдами (см. `config.py::load_config_ex`).
Этот модуль — только механика поиска/чтения/пофайловой валидации одного профиля;
оркестрация мержа и итоговая (фаза 2) ссылочная целостность — в `config.py` /
`config_modules/model_sections_validation.py` соответственно.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import yaml

from config_modules.compat import apply_compat
from config_modules.env_interpolation import resolve_env_placeholders
from config_modules.model_registry import RESERVED_WORDS, validate_registry_pairs
from config_modules.root import AppConfig


@dataclass(frozen=True)
class ProfileRef:
    name: str
    path: Path
    root_kind: Literal["project", "base"]


@dataclass
class ProfileMeta:
    """Метаданные резолва профиля для одного вызова `load_config_ex` (спека §3.3)."""

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


def discover_profiles(
    roots: list[Path], *, notices: Optional[list[str]] = None
) -> dict[str, "ProfileRef"]:
    """Дискавери профилей по корням (в порядке приоритета — первый найденный побеждает).

    Зарезервированные имена (`RESERVED_WORDS`) пропускаются + warning в `notices`,
    если список передан (спека §3.2).
    """
    result: dict[str, ProfileRef] = {}
    for idx, root in enumerate(roots):
        root_kind: Literal["project", "base"] = "project" if idx == 0 else "base"
        root = Path(root)
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.yml")):
            name = path.stem
            if name in RESERVED_WORDS:
                if notices is not None:
                    notices.append(
                        f'{path}: имя профиля "{name}" зарезервировано — файл проигнорирован'
                    )
                continue
            if name in result:
                continue  # тёзка из менее приоритетного корня — проектный уже выиграл
            result[name] = ProfileRef(name=name, path=path, root_kind=root_kind)
    return result


def _format_available(discovered: dict[str, ProfileRef]) -> str:
    base_names = sorted(n for n, ref in discovered.items() if ref.root_kind == "base")
    project_names = sorted(n for n, ref in discovered.items() if ref.root_kind == "project")
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
    with open(path, "r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: профиль должен быть YAML-документом (словарём)")
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


def load_profile_document(
    path: Path,
    *,
    warn_literal_tokens: bool = False,
    notices: Optional[list[str]] = None,
) -> dict:
    """Прочитать + провалидировать документ одного профиля (спека §3.4):

    YAML → (опционально) warning на литеральные токены → per-file env-интерполяция →
    compat-слой → частичная валидация `AppConfig` (фаза 1, ссылочная целостность не
    проверяется) → запрет ключа `profile:` внутри (анти-рекурсия, спека §3.2).
    """
    raw = _read_yaml_document(path)
    if warn_literal_tokens and notices is not None:
        for alias in find_literal_tokens(raw):
            notices.append(
                f'{path}: models.{alias}.token — литеральный токен в проектном профиле; '
                f"используйте ${{ENV:VAR}} или наследование из базового конфига"
            )
    raw = resolve_env_placeholders(raw)
    # Compat ДО схемы — как `load_config_ex` делает для базы. Схема с
    # extra="forbid" иначе роняет ВЕСЬ профиль на устаревшем ключе, который в
    # config.yml тихо выбрасывается с нотисом; а в профиль такой ключ попадает
    # ровно копипастой секции из старого config.yml. Факты — в тот же канал
    # notices, с путём файла: иначе непонятно, в каком из документов ключ.
    for fact in apply_compat(raw):
        if notices is not None:
            notices.append(f"{path}: {fact}")
    # Пары «адрес+токен» — по СЫРОМУ документу и ДО схемы: слои сливаются позже,
    # и половина пары, наложенная на чужую полную, даёт корректный на вид итог,
    # пока токен базы уходит на адрес этого файла. Здесь же видно, чей это файл.
    validate_registry_pairs(raw, source=str(path))
    AppConfig.model_validate(raw)
    if "profile" in raw:
        raise ValueError(
            f'{path}: ключ "profile" внутри профиля запрещён — цепочки профилей запрещены'
        )
    return raw


def mtime_or_none(path: Path) -> Optional[float]:
    """`stat().st_mtime` или `None`, если файла нет — не падает на отсутствующем пути
    (спека §3.3: отсутствие файла тоже «состояние», не ошибка)."""
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def discover_all_refs(roots: list[Path]) -> dict[str, list["ProfileRef"]]:
    """Все найденные ссылки на профиль по имени, БЕЗ дедупликации тёзок (в отличие
    от `discover_profiles`, победившим первым идёт проектный корень) — витрине
    Settings (спека §12.3) нужен весь список тёзок, чтобы показать перекрытый файл
    в hint, не только победившее имя. Зарезервированные имена по-прежнему пропускаются
    молча (витрина — не место для повторного warning'а дискавери загрузчика)."""
    result: dict[str, list[ProfileRef]] = {}
    for idx, root in enumerate(roots):
        root_kind: Literal["project", "base"] = "project" if idx == 0 else "base"
        root = Path(root)
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.yml")):
            name = path.stem
            if name in RESERVED_WORDS:
                continue
            result.setdefault(name, []).append(
                ProfileRef(name=name, path=path, root_kind=root_kind))
    return result


def find_active_ref(
    name: Optional[str], project_dir: Path, base_config_path: Path
) -> Optional["ProfileRef"]:
    """`ProfileRef` действующего имени профиля или `None` (нет имени / указатель
    не резолвится — санация уже случилась выше по стеку, здесь просто нечего
    показать). Общий источник для витрин `/config`, `/status`, Settings —
    единая точка резолва имени в файл, не размазанная по вызывающим модулям."""
    if not name:
        return None
    roots = profile_roots(project_dir, base_config_path)
    return discover_profiles(roots).get(name)


_MODEL_SECTION_KEYS = frozenset({"models", "slots", "assign", "level"})


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
        return ProfileDisplaySummary(slots={}, overlay_sections=[], error=str(exc))
    slots = raw.get("slots")
    overlay = sorted(k for k in raw.keys() if k not in _MODEL_SECTION_KEYS)
    return ProfileDisplaySummary(
        slots=dict(slots) if isinstance(slots, dict) else {},
        overlay_sections=overlay,
        error=None,
    )


__all__ = [
    "ProfileRef",
    "ProfileMeta",
    "ProfileDisplaySummary",
    "mtime_or_none",
    "profile_roots",
    "discover_profiles",
    "discover_all_refs",
    "find_active_ref",
    "resolve_profile",
    "find_literal_tokens",
    "load_profile_document",
    "profile_display_summary",
]
