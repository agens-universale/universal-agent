import copy
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_FILE_NAME = "config.yml"

from pydantic import ValidationError as _PydanticValidationError

from gc_core.bundled_assets import bundled_root
from gc_core.config_modules import env_vars

# managed_config_path живёт в config_modules.env_vars, а не в mcp_runtime.paths:
# путь принадлежит конфигу, и держать его в MCP-подсистеме значило бы вернуть
# статическую зависимость ядра конфига от MCP, снятую при разбиении на пакеты.
from gc_core.config_modules import env_vars as mcp_paths
from gc_core.config_modules.backend import (
    parse_chat_completions_max_tokens,  # noqa: F401  # re-export: processing_responses импортирует из config
)
from gc_core.config_modules.common import (
    swarm_env_updates,
)
from gc_core.config_modules.compat import apply_compat
from gc_core.config_modules.cross_validation import cross_field_notices
from gc_core.config_modules.env_interpolation import (
    UnresolvedPlaceholdersError,
    raise_unresolved,
    resolve_env_placeholders,
    resolve_env_placeholders_strict,
    settle_unresolved,
    strip_unresolved_marks,
    tag_layer,
)
from gc_core.config_modules.env_vars import project_gc_dir

# Наложение слоёв и pre-write валидация живут в `config_modules.layers` (слой записи
# реестра спрашивает их напрямую, минуя агрегатор). Здесь — ре-экспорт: снаружи их
# зовут как `config.<имя>`, и второй адрес у одного правила заводить незачем.
from gc_core.config_modules.layers import (  # noqa: F401
    _app_config_from_raw,
    _app_config_from_text,
    _layer_document,
    _Layers,
    _merge_layers,
    _raw_from_text,
    _stacked_app_config,
    prospective_registry,
    resolve_profile_pointer,
    stack_documents,
    validate_config_text,
    validate_config_text_with_sections,
    validate_profile_text_with_sections,
)
from gc_core.config_modules.migrations import migrate_document
from gc_core.config_modules.model_registry import (
    validate_registry_pairs,
)
from gc_core.config_modules.model_sections_validation import (
    validate_model_sections,
)
from gc_core.config_modules.profiles import (
    ProfileMeta,
    mtime_or_none,
    profile_roots,
)
from gc_core.config_modules.root import (
    SCHEMA_ERROR_PREFIX,
    AppConfig,
    resolve_paths,
    schema_error_details,
)
from gc_core.config_modules.summary import (
    DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS,
)
from gc_core.config_modules.unknown_keys import (
    drop_unknown_keys,
    unknown_key_notice,
)
from gc_core.trace_id import normalize_trace_id
from gc_core.utils.user_text import unreadable_reason

# Backward compatibility for existing imports.
_DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS = (
    DEFAULT_LAST_DIALOG_STEP_MESSAGE_MAX_CHARS
)


def _default_config_path() -> str:
    # Floor: bundled дефолт-конфиг (едет в wheel через force-include).
    return os.path.join(str(bundled_root()), "config.default.yml")


def _xdg_config_path() -> Optional[str]:
    """
    Вернуть абсолютный путь к <gc_paths()["config"]>/config.yml, если файл
    существует, иначе None.

    Базовый tier между --config и пакетным дефолтом. Каталог берётся из
    gc_paths()["config"] (XDG-config: ~/.config/gc/ на Linux/macOS,
    %USERPROFILE%\\.gc\\ на Windows; учитывает GC_CONFIG_DIR / XDG_CONFIG_HOME
    / GC_HOME). Полная база-замена, НЕ слой: частичный файл здесь теряет
    пакетные дефолты (project-local <cwd>/.gc/config.yml по-прежнему
    мерджится поверх выбранной базы).
    """
    candidate = os.path.abspath(
        os.path.join(str(env_vars.effective_config_dir()), CONFIG_FILE_NAME)
    )
    if os.path.isfile(candidate):
        return candidate
    return None


def user_config_path() -> Path:
    """Путь дефолтного пользовательского конфига: <gc config>/config.yml.
    На залоченном ~/.config (корп-Mac/Linux) автоматически уезжает в ~/.gc."""
    return env_vars.effective_config_dir() / CONFIG_FILE_NAME


def _bootstrap_user_config() -> bool:
    """Первый запуск без --config и без ~/.gc/config.yml: скопировать floor
    в ~/.gc/config.yml, чтобы у пользователя был редактируемый конфиг.

    Возвращает True, если файл создан сейчас (иначе уже существовал). Презентация
    (онбординг-гайд) — на стороне контроллеров (cli/tui), не здесь."""
    dest = user_config_path()
    if dest.is_file():
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_default_config_path(), dest)
    except OSError as exc:
        print(
            f"[gc] не удалось создать пользовательский конфиг {dest} ({exc}); "
            f"работаю на дефолте. Задайте GC_HOME на доступную папку или используйте --config.",
            file=sys.stderr,
        )
        return False
    return True


def resolve_base_config_path(config_path: Optional[str] = None) -> str:
    """Резолвить путь к base config.yml без загрузки YAML.

    Использует ту же логику что `load_config`: явный путь → XDG → пакетный
    дефолт. Нужен swarm-runner'у (см. шаг M4/1.10), чтобы материализовать
    runtime config.yml внутри песочницы на основании хостового base YAML.
    """
    return config_path or _xdg_config_path() or _default_config_path()


def _cli_updates(
    extra_overrides: Dict[str, Any],
) -> tuple[Dict[str, Any], Optional[str], list[str]]:
    """``--param`` → точечные пути настроек; ``trace_id`` — не настройка, а рантайм.

    Плоские имена прежнего ``config_dict`` (``max_steps``) переводятся по таблице,
    путь с точкой (``agent.max_steps``) берётся как есть.

    ``${ENV:…}``/``${FILE:…}`` — строго, как у файлов при старте: незаданная
    обязательная переменная → ``UnresolvedPlaceholdersError`` (``ValueError``) с
    ключом ``--<param>``, а не тихая пустая строка.

    Перекрытия (``--ключ`` CLI, ``agent_overrides`` сервера) ложатся мимо compat
    загрузки, поэтому проходят его здесь: переименованный ключ иначе оседал бы
    мёртвым. Третье значение — факты compat для стартовых уведомлений.
    """
    from gc_core.config_modules.compat import map_legacy_path
    from gc_core.config_modules.flat_keys import setting_path

    resolved = resolve_env_placeholders_strict(
        extra_overrides, source="--param"
    )
    trace_id = resolved.pop("trace_id", None)
    if "models" in resolved:
        # `--models` задаёт весь реестр строкой: то же правило пары, что у файлов.
        validate_registry_pairs(
            {"models": resolved["models"]}, source="--models"
        )
    updates: Dict[str, Any] = {}
    facts: list[str] = []
    for key, value in resolved.items():
        path = setting_path(key)
        mapped = map_legacy_path(path)
        if mapped is None:
            facts.append(f"перекрытие: {key} устарел и игнорируется")
            continue
        if mapped != path and not isinstance(value, dict):
            # Секцию-словарь проведёт compat ниже — со своими фактами.
            facts.append(f"перекрытие: {key} переименован в {mapped}")
        if isinstance(value, dict):
            # Внутри секции могут быть старые листья (`diff_tui.cache_max_orig`).
            document: Dict[str, Any] = {}
            _set_nested(document, path, copy.deepcopy(value))
            facts.extend(
                f"перекрытие: {fact}" for fact in apply_compat(document)
            )
            value = _get_nested(document, mapped)
            if value is None:
                continue
        updates[mapped] = value
    return updates, trace_id, facts


def _set_nested(document: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        document = document.setdefault(part, {})
    document[parts[-1]] = value


def _get_nested(document: Dict[str, Any], dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(document, dict) or part not in document:
            return None
        document = document[part]
    return document


def apply_cli_overrides(
    loaded,
    *,
    extra_overrides: Optional[Dict[str, Any]] = None,
    write_guard_enabled: Optional[bool] = None,
    no_write_guard: bool = False,
    command_moderation_mode: Optional[str] = None,
    read_guard_enabled: Optional[bool] = None,
    no_read_guard: bool = False,
    read_guard_mode: Optional[str] = None,
    agentsmd_override: bool = False,
    trace_id: Optional[str] = None,
):
    """CLI поверх загруженного: новый ``LoadedSettings`` (слой ``cli``).

    Значения проходят валидацию схемы; источник каждой правки помечается в
    происхождении — по нему, например, видно легаси-форс модели.
    """
    from gc_core.config_modules.security import (
        validate_command_moderation_mode,
        validate_read_guard_mode,
    )

    updates, param_trace_id, override_facts = _cli_updates(
        dict(extra_overrides or {})
    )
    write_guard = False if no_write_guard else write_guard_enabled
    read_guard = False if no_read_guard else read_guard_enabled
    if write_guard is not None:
        updates["security.write_guard.enabled"] = bool(write_guard)
    if command_moderation_mode is not None:
        updates["security.command_moderation.mode"] = (
            validate_command_moderation_mode(command_moderation_mode)
        )
    if read_guard is not None:
        updates["security.read_guard.enabled"] = bool(read_guard)
    if read_guard_mode is not None:
        updates["security.read_guard.mode"] = validate_read_guard_mode(
            read_guard_mode
        )
    if agentsmd_override:
        updates["tui.agentsmd_override"] = True
    loaded = loaded.with_overrides(updates, layer="cli")
    # Внешний trace_id: значение могло приехать и флагом, и `--param`
    # (через yaml.safe_load — не обязательно строкой). Пусто → отсутствует.
    raw_trace = trace_id if trace_id is not None else param_trace_id
    normalized = normalize_trace_id(
        raw_trace if raw_trace is not None else loaded.runtime.trace_id
    )
    if override_facts:
        loaded = loaded.with_runtime(
            notices=(*loaded.runtime.notices, *override_facts)
        )
    return loaded.with_runtime(trace_id=normalized or None)


def cli_forces_legacy_model(loaded) -> bool:
    """Легаси-форс (спека §2, §12.1): CLI явно передал `model`/`completions_url`
    поверх непустого merged-реестра моделей — резолв уровней/алиасов у
    ``ModelRouter`` отключается на этот запуск (``force_legacy=True``).
    Профиль-слой обычных секций (гварды, суммаризация и пр.) при этом остаётся
    в стопке — это решает исключительно резолв модели, не сборку конфига.
    ``--model``/``--completions_url`` при пустом реестре — обычный легаси, без форса.
    """
    from_cli = any(
        loaded.provenance.source(path).layer == "cli"
        for path in ("agent.model", "backend.completions_url")
    )
    return from_cli and bool(loaded.settings.models)


def resolve_config_path(config_path: Optional[str] = None) -> str:
    """Путь config.yml, использованный сессией (та же цепочка, что в load_config)."""
    return config_path or _xdg_config_path() or _default_config_path()


def _build_mtime_set(
    base_path: Path,
    managed_path: Path,
    project_path: Optional[str],
    active_name: Optional[str],
    profile_roots_list: list[Path],
) -> Dict[str, Optional[float]]:
    """Снимок mtime базового/managed/проектного конфига + обоих кандидатных путей
    активного профиля (спека §3.3): отсутствующий файл — ключ есть, значение None."""
    project_config_path = (
        Path(project_path)
        if project_path is not None
        else project_gc_dir() / CONFIG_FILE_NAME
    )
    mtime_set: Dict[str, Optional[float]] = {
        str(base_path): mtime_or_none(base_path),
        str(managed_path): mtime_or_none(managed_path),
        str(project_config_path): mtime_or_none(project_config_path),
    }
    if active_name:
        for root in profile_roots_list:
            candidate = Path(root) / f"{active_name}.yml"
            mtime_set[str(candidate)] = mtime_or_none(candidate)
    return mtime_set


def profile_mtime_set(
    config_path: Optional[str], active_name: Optional[str] = None
) -> Dict[str, Optional[float]]:
    """Тот же снимок, что строит `load_settings` (спека §3.3), но БЕЗ парсинга/
    валидации файлов — только `stat()`. Точка входа для TUI (`set_config_path`):
    связывание пути с окном не обязано гонять полную загрузку конфига ради снимка
    свежести."""
    path = resolve_config_path(config_path)
    base_path = Path(path)
    # Через модуль-дом, а не через импортированные сюда имена: адрес чтения
    # один на весь загрузчик (`_merge_layers` спрашивает там же), и подмена
    # дома в тестах действует на обе точки разом.
    managed_path = mcp_paths.managed_config_path()
    project_path = env_vars.project_local_config_path()
    roots = profile_roots(Path(os.getcwd()), base_path)
    return _build_mtime_set(
        base_path, managed_path, project_path, active_name, roots
    )


def load_settings(
    config_path: Optional[str] = None,
    *,
    profile_override: Optional[str] = None,
):
    """Загрузить настройки: ``LoadedSettings(settings, runtime, provenance)``.

    Конвейер загрузки: слои → ${ENV} → миграции → compat → схема → пути.
    """
    from gc_core.config_modules.provenance import Provenance
    from gc_core.settings import LoadedSettings, RuntimeEnv

    """Загрузить эффективный конфиг + метаданные резолва профиля (спека §2, §3).

    Слои: base → managed → project (deep-merge, каждый слой интерполируется
    ${ENV:VAR} сразу после чтения) → указатель `profile:` резолвится из этого мержа
    (либо `profile_override`) → контент профиля мержится ПОВЕРХ → compat →
    `AppConfig.model_validate` → `validate_model_sections` → resolve_paths/flatten.
    `load_config` (ниже) — тонкая обёртка для обратной совместимости.

    Сбор побочек на процесс не имеет: глушение TLS-предупреждений ушло вместе с
    urllib3, глобальный конвейер проекций — в `754b8910`. Поэтому витрина чужого
    профиля в Настройках зовёт ту же функцию, что старт и reload; отдельный
    флаг `apply_to_process` для неё больше не нужен и снят."""
    if config_path is None and _xdg_config_path() is None:
        _bootstrap_user_config()
    path = resolve_config_path(config_path)
    config_dir = os.path.dirname(os.path.abspath(path))
    base_path = Path(path)
    notices: list[str] = []

    # Чтение базы — тем же приёмом, что и слои (`_layer_document`): невалидный YAML и
    # не-словарь на верхнем уровне дают ValueError с именем файла и причиной, а не
    # сырые yaml.YAMLError/AttributeError из глубины мержа. Голый open() снаружи
    # `_layer_document` — сам он не оборачивает OSError/UnicodeDecodeError (см. его
    # докстринг), это остаётся на вызывающем; здесь единственный вызывающий, которому
    # нужен непрочитанный файл тем же ValueError, что и остальные исходы базы.
    # `UnicodeDecodeError` — подкласс `ValueError`, а не `OSError` (файл, сохранённый
    # «блокнотом» в UTF-16), и без отдельного перехвата уходил голой жалобой кодека
    # мимо этой обёртки и мимо классового сторожа (ревью MR #80, L40).
    try:
        raw_config = _layer_document(base_path, CONFIG_FILE_NAME)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(unreadable_reason(base_path, exc)) from exc
    # Дорезолвный вид держим для отказа правила пары: после подстановки от `${ENV:X}`
    # остаётся пустая строка, и назвать незаданную переменную уже нечем.
    #
    # `${ENV:…}`/`${FILE:…}` на старте — СТРОГО: неразрешённые обязательные
    # ссылки всех слоёв (файл + dotted-ключ) копятся в `unresolved`, и загрузка
    # отказывает списком сразу (`UnresolvedPlaceholdersError`) — вместо токена,
    # молча ставшего пустой строкой и всплывшего потом 401-м от бэкенда.
    # Необязательное значение помечается умолчанием: `${ENV:VAR:-}`.
    unresolved: list = []
    base_before_env = raw_config
    raw_config = resolve_env_placeholders(
        raw_config, unresolved=unresolved, source=str(base_path)
    )
    tag_layer(unresolved, 0, layer="base")
    try:
        # Формат файла (`config_version`) — каждый слой поднимается до текущей
        # версии сам (`migrations.migrate_document`), до мержа и схемы.
        raw_config, migration_notices = migrate_document(
            raw_config, source=str(base_path)
        )
        notices.extend(migration_notices)
        raw_base_after_env = raw_config
        if not unresolved:
            validate_registry_pairs(
                raw_config, source=str(base_path), raw_source=base_before_env
            )

        (
            raw_config,
            ref,
            active_name,
            roots,
            managed_path,
            project_path,
            managed_raw,
            _base_voice,
            layer_documents,
        ) = _merge_layers(
            raw_config,
            path,
            profile_override=profile_override,
            notices=notices,
            # Пути слоёв добывает ЗАГРУЗЧИК — своими именами, теми же, что зовёт
            # `profile_mtime_set` ниже и что подменяют тесты, глуша слои машины
            # разработчика. Спроси их `_merge_layers` сам у модулей-владельцев —
            # подмена бы не действовала, и «изолированный» тест читал бы реальный
            # `~/.gc/config.local.yml` и проектный `.gc/config.yml`.
            managed_path=mcp_paths.managed_config_path(),
            project_path=env_vars.project_local_config_path(),
            unresolved=unresolved,
        )
    except ValueError as exc:
        # Отказ, случившийся ПОСЛЕ незаданной переменной (пустая строка не
        # прошла схему профиля, профиль по указателю не нашёлся…), — следствие;
        # человеку нужна причина, и она уже собрана.
        if not isinstance(exc, UnresolvedPlaceholdersError):
            raise_unresolved(unresolved)
        raise
    # Отказ — только по ссылкам, значения которых дошли до итогового мержа:
    # перекрытая верхним слоем незаданная переменная ни на что не влияет.
    raw_config, surviving = settle_unresolved(raw_config, unresolved)
    raise_unresolved(surviving)
    if unresolved:
        raw_base_after_env = strip_unresolved_marks(raw_base_after_env)
        layer_documents = strip_unresolved_marks(list(layer_documents))
        managed_raw = strip_unresolved_marks(managed_raw)
        _base_voice = strip_unresolved_marks(_base_voice)

    mtime_set = _build_mtime_set(
        base_path, managed_path, project_path, active_name, roots
    )

    # Перекрыт ли юзерский voice managed-слоем (gc-voice install) — для notices. Правило
    # общее с установкой: она предупреждает о том же факте, и вердикт обязан совпадать.
    #
    # Считаем ПОСЛЕ ВСЕХ слоёв и только когда адрес батарейки реально победил. Сперва
    # расчёт стоял между managed и project — и агент предупреждал «managed перекрыл ваш
    # voice» там, где работал адрес из <cwd>/.gc/config.yml (находка ревью коллеги).
    # Профиль — такой же слой поверх: если voice.url задан в нём, батарейка тоже ничего
    # не решает, поэтому место расчёта — ниже профильного мержа, а не выше.
    _voice_overridden: Optional[str] = None
    _mv = managed_raw.get("voice") or {}
    if _mv:
        from gc_core.config_modules.voice import overridden_user_voice

        _managed_url = str(_mv.get("url") or "")
        _merged_voice = raw_config.get("voice")
        _effective_url = (
            str((_merged_voice or {}).get("url") or "")
            if isinstance(_merged_voice, dict)
            else ""
        )
        if _effective_url == _managed_url:
            _voice_overridden = overridden_user_voice(
                _base_voice, _managed_url
            )

    # Compat-слой (A4): rename/remove устаревших ключей in-memory, файл не трогаем.
    # Применяется к итоговому мержу — профильный слой проходит его так же, как
    # остальные (единый вызов после всех merge-слоёв, включая профиль).
    _compat_notices = apply_compat(raw_config)
    # Лишние ключи — предупреждение, а не отказ: файл пишет человек, и
    # опечатка или поле из новой версии не должны лишать его агента.
    raw_config, _unknown_keys = drop_unknown_keys(raw_config, AppConfig)

    # Отказ схемы — из `exc.errors()`, не `str(exc)`: pydantic-дамп несёт
    # `input_value=<repr записи>` — токен из файла уезжал в хинт пикера профилей
    # и в статус «не применится» дословным текстом ошибки (MR #80, класс L28).
    try:
        app_cfg = AppConfig.model_validate(raw_config)
    except _PydanticValidationError as exc:
        raise ValueError(
            f"{SCHEMA_ERROR_PREFIX}{schema_error_details(exc)}"
        ) from exc
    _model_section_notices = validate_model_sections(app_cfg)
    # Межполевые предупреждения — до `resolve_paths`: там явные значения
    # подменяются выведенными умолчаниями (см. `cross_validation`).
    _model_section_notices += cross_field_notices(app_cfg)
    app_cfg = resolve_paths(app_cfg, config_dir)

    meta = ProfileMeta(
        active_name=active_name, ref=ref, mtime_set=mtime_set, notices=notices
    )
    provenance = Provenance.from_layers(
        [("base", str(base_path), base_before_env, raw_base_after_env)]
        + list(layer_documents)
    )
    # SWARM_* окружения — последний слой файлов: сервер swarm-раннера задаёт
    # ими каталоги тредов/скиллов и AGENTS.md поверх конфига.
    swarm_updates = swarm_env_updates(config_dir)
    if swarm_updates:
        from gc_core.settings import override_settings

        app_cfg = override_settings(
            app_cfg, swarm_updates, config_dir=config_dir
        )
        provenance = provenance.with_layer("env", swarm_updates)
    _unknown_notices = [
        unknown_key_notice(
            unknown, provenance.source(unknown.dotted).path or None
        )
        for unknown in _unknown_keys
    ]
    runtime = RuntimeEnv(
        config_path=os.path.abspath(path),
        config_dir=config_dir,
        active_profile=active_name or "",
        notices=tuple(
            _compat_notices
            + _unknown_notices
            + _model_section_notices
            + notices
        ),
        voice_overridden_by_managed=_voice_overridden,
    )
    return LoadedSettings(
        settings=app_cfg,
        runtime=runtime,
        provenance=provenance,
        profile_meta=meta,
    )
