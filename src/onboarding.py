"""Онбординг первого запуска: политика «настроен ли конфиг» + текст-гайд.

Модуль-владелец презентации/политики первого запуска. Контроллеры (cli, tui)
зовут is_unconfigured()/setup_guide(); сюда же вынесены сентинелы дефолт-шаблона
(единый источник истины, сверяется тестом с config.default.yml).

Требования стратегия-зависимы: auth удовлетворяется токеном ИЛИ блоком agent_auth
(сертификатная авторизация — без статического токена). Подсказка перечисляет только
реально незаполненное; если всё покрыто — позитивное «настроен».
"""
from typing import Any, Callable, Dict, List, NamedTuple, Optional
from urllib.parse import urlparse

import config_raw


def authorization_header(raw: str) -> str:
    """Ensure raw token has Authorization scheme; add Bearer if missing.

    If raw contains no space after strip, prepend 'Bearer '.
    If raw has space, keep as-is (Basic, Bearer, or custom scheme).
    """
    s = raw.strip()
    return s if " " in s else f"Bearer {s}"


def mask_authorization(header: str) -> str:
    """Mask authorization header for logging: show scheme and first 3 chars of secret.

    Format: '<scheme> <first 3 chars>***'
    If no scheme+secret pair, return '***'.
    """
    parts = header.split(" ", 1)
    if len(parts) == 2 and parts[1]:
        scheme, secret = parts
        return f"{scheme} {secret[:3]}***"
    return "***"


def mask_secret(raw: str) -> str:
    """Mask a bare secret (a key with no scheme: `gsk_…`, `sk-…`): first 4 chars + … + last 4.

    mask_authorization masks a HEADER — it shows the scheme, and for a bare key
    (no space) degrades to '***', which no longer identifies anything. Here the
    recognisable edges stay visible instead. Short secrets are not cut at all:
    what remains would exceed what is hidden.

    Threshold is 16, not 10: the visible edges take 8 characters, so anything
    shorter shows more than it hides — a 10-character key rendered 8 of its 10
    on screen, exactly the case this rule exists to exclude.
    """
    s = str(raw)
    return f"{s[:4]}…{s[-4:]}" if len(s) >= 16 else "***"


ENV_REF_RESOLVED = "переменная подставилась"
ENV_REF_MISSING = "переменной нет"


def env_ref_status(raw: Any) -> str:
    """Статус ссылки ${ENV:VAR} в СЫРОМ значении поля ("" — значение не ссылка)."""
    if not config_raw.env_refs(raw):
        return ""
    return ENV_REF_MISSING if config_raw.missing_env_refs(raw) else ENV_REF_RESOLVED


def display_value(raw: Any) -> str:
    """Сырое значение поля для показа: ссылка — ссылкой + её статус, литерал — как есть."""
    status = env_ref_status(raw)
    return f"{raw} · {status}" if status else (raw or "")


def display_secret(raw: Any) -> str:
    """То же для секрета: литерал — под маской. Ссылка секретом не является и
    показывается как есть: маска выдавала бы «токен задан» там, где его нет.

    Маску выбираем по форме значения: заголовок «схема секрет» прячем
    mask_authorization (схему показать осмысленно — Bearer/Basic), голый ключ —
    mask_secret (у него схемы нет, и заголовочная маска вырождалась в «***»)."""
    if env_ref_status(raw):
        return display_value(raw)
    return mask_authorization(raw) if " " in str(raw) else mask_secret(raw)


def _valid_endpoint_url(raw: str) -> bool:
    """Структурная проверка адреса: схема http(s) + непустой хост.
    Путь и достижимость НЕ проверяем (осознанно — обрезанный-но-валидный путь пройдёт)."""
    parsed = urlparse(raw)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)

# Сентинелы из config.default.yml (страж совпадения — tests/test_onboarding.py).
URL_SENTINEL = "http://your-backend:PORT/v1/chat/completions"
TOKEN_SENTINEL = "Bearer <ВПИШИТЕ_ТОКЕН>"
MODEL_SENTINEL = "<ВПИШИТЕ_МОДЕЛЬ>"


def configured_endpoint(raw: Any) -> str:
    """Адрес, ЗАДАННЫЙ человеком, либо пусто: заглушка шаблона — «ещё ничего не
    задано», а не значение.

    Одно правило на всех, кто адрес показывает, И на всех, кто по нему стучится.
    Витрины его знали, сетевой путь — нет: запрос за списком моделей уходил на
    несуществующий `your-backend:PORT`, и мастер печатал «не удалось получить
    список» человеку, у которого в поле виден его рабочий адрес."""
    return "" if not raw or raw == URL_SENTINEL else str(raw)


def _entry_carries_own_connection(entry: Dict[str, Any]) -> bool:
    """Запись ЗАГРУЖЕННОГО реестра несёт своё подключение целиком.

    Что считать адресом и токеном, спрашиваем у ядра (`connection_parts`, §2) — тем
    же определением судят Router и правило атомарной пары. Своё прочтение ключей
    ошибалось бы в обе стороны: `url: ""` — это «своего адреса нет, пара
    наследуется», а не заданный адрес; своё подключение вдобавок пишется вторым
    равноправным написанием (`backend.completions_url` / `backend.proxy` /
    `backend.chat_completion_headers.Authorization`), и заполненный по документации
    реестр объявлялся бы ненастроенным.

    Форма здесь — снимок (`config._serialize_model_entry`): сырого `token` в
    загруженном конфиге уже нет, вместо него вычисленный `authorization_header`
    (None — наследовать, "" — ЗАДАННЫЙ токен «сервер не требует авторизации»), а
    явные оверлеи backend лежат в `backend_overrides`.

    Адрес и токен спрашиваются вместе, одним ответом: правило пары (§3) не пускает
    в загруженный конфиг половину подключения, поэтому «свой адрес» и «свой токен»
    у записи — одно и то же условие."""
    from config_modules.model_registry import connection_parts

    entry = entry or {}
    overrides = entry.get("backend_overrides") or {}
    headers = overrides.get("chat_completion_headers") or {}
    return all(connection_parts(
        url=entry.get("url"),
        token=entry.get("authorization_header"),
        completions_url=overrides.get("completions_url"),
        proxy=overrides.get("proxy"),
        authorization=headers.get("Authorization"),
    ))


def _registry_is_self_sufficient(c: Dict[str, Any]) -> bool:
    """КАЖДАЯ запись реестра работает через СВОЁ подключение — тогда плоский
    `backend` не нужен вовсе (спека §4: незаданное у записи наследуется от него).

    Только для `chat_completions`: в `responses` от записи действует один
    провайдерский id, per-model адрес и авторизация мертвы (§4) — там такой конфиг
    ушёл бы на плоский плейсхолдер. Проверка по ВСЕМ записям, а не по резолвнутой,
    намеренно грубее: резолв (слоты, привязки, фоллбэки уровней) — знание Router'а,
    тащить его в детект первого запуска нельзя, а «самодостаточны все» ошибается
    только в безопасную сторону — лишний раз предложить мастер, но не объявить
    готовым конфиг, который уйдёт на `your-backend:PORT`."""
    models = c.get("models") or {}
    return (c.get("backend_api_mode") == "chat_completions"
            and bool(models)
            and all(_entry_carries_own_connection(entry) for entry in models.values()))


def _endpoint_set(c: Dict[str, Any]) -> bool:
    if c.get("completions_url") not in (None, URL_SENTINEL):
        return True
    return _registry_is_self_sufficient(c)


def _legacy_model_set(c: Dict[str, Any]) -> bool:
    """Плоский `agent.model` заполнен (не плейсхолдер шаблона)."""
    return c.get("model") not in (None, "", MODEL_SENTINEL)


def _model_set(c: Dict[str, Any]) -> bool:
    """Требование модели: непустой merged-реестр (любой слой, спека §11) ИЛИ
    заполненный легаси-ключ.

    Сам факт непустого реестра закрывает ТОЛЬКО это требование: записи, которые
    пишет мастер, наследуют url и авторизацию от плоского `backend` (решение 10),
    поэтому такой реестр поверх плейсхолдерного endpoint'а — «нечем работать», а не
    «настроено». Адрес и токен реестр закрывает отдельно и по другому условию —
    когда их несут сами записи (`_registry_is_self_sufficient`)."""
    return bool(c.get("models")) or _legacy_model_set(c)


def _agent_auth_set(c: Dict[str, Any]) -> bool:
    auth_cfg = c.get("agent_auth") or {}
    return bool(auth_cfg) and bool(auth_cfg.get("auth_endpoint"))


def _auth_set(c: Dict[str, Any]) -> bool:
    # Auth «не задан» (pending) ТОЛЬКО если в конфиге остался шаблонный плейсхолдер
    # токена (и нет полноценного agent_auth). Реальный токен ИЛИ намеренное отсутствие
    # заголовка (локальный прокси / эндпоинт без auth сам ставит авторизацию) → задан.
    # Гейт — детектор незаполненного шаблона, а не валидатор auth: отсутствие ≠ сентинел.
    # Симметрично адресу: авторизацию может нести и сама запись реестра — целой парой
    # со своим адресом, порознь их правило пары (§3) в конфиг не пускает.
    auth = (c.get("chat_completion_headers") or {}).get("Authorization")
    if auth == TOKEN_SENTINEL:
        return _agent_auth_set(c) or _registry_is_self_sufficient(c)
    return True


class _Requirement(NamedTuple):
    is_satisfied: Callable[[Dict[str, Any]], bool]
    hint: str   # что вписать в config.yml, если требование не удовлетворено


# Требования к рабочему конфигу. auth — «либо/либо»: токен ИЛИ agent_auth.
# Чтобы добавить требование: дописать строку (+ при необходимости сентинел и
# плейсхолдер в config.default.yml — тест-страж поймает рассинхрон).
REQUIREMENTS: List[_Requirement] = [
    _Requirement(
        _endpoint_set,
        "backend.completions_url — endpoint OpenAI-совместимого провайдера (chat/completions)",
    ),
    _Requirement(
        _auth_set,
        "backend.chat_completion_headers.Authorization — токен доступа (Bearer …)",
    ),
    _Requirement(
        _model_set,
        "agent.model — имя модели",
    ),
]


# Соответствие ключ-якорь (leaf в config.yml) → предикат «поле задано».
# Переиспользует те же предикаты, что и REQUIREMENTS — единый источник политики.
_FIELD_CHECKS: Dict[str, Callable[[Dict[str, Any]], bool]] = {
    "completions_url": _endpoint_set,
    "Authorization": _auth_set,
    "model": _model_set,
}

# Dotted-пути якорей полей визарда в config.yml (для path-aware записи и вставки).
FIELD_ANCHOR_PATHS: Dict[str, str] = {
    "completions_url": "backend.completions_url",
    "Authorization": "backend.chat_completion_headers.Authorization",
    "model": "agent.model",
    "proxy": "backend.proxy",
}


def pending_field_keys(config: Dict[str, Any]) -> set:
    """Ключи-якоря полей, ещё не удовлетворённых конфигом (для визарда)."""
    return {key for key, is_set in _FIELD_CHECKS.items() if not is_set(config)}


def pending_requirements(config: Dict[str, Any]) -> List[_Requirement]:
    """Требования, ещё не удовлетворённые текущим конфигом."""
    return [r for r in REQUIREMENTS if not r.is_satisfied(config)]


def is_unconfigured(config: Dict[str, Any]) -> bool:
    """True, если конфиг ещё не готов к запуску (есть незакрытое требование).

    Требования проверяются ВСЕ — реестр моделей входит в них через `_model_set`
    (спека §11: «нечем работать» ловится в обоих мирах), а не закорачивает детект
    целиком: иначе конфиг с живым реестром и плейсхолдерным адресом считался бы
    настроенным, мастер отчитывался бы «✓ Готово», следующий старт больше не
    предложил бы настройку, а запрос ушёл бы на `your-backend:PORT`."""
    return bool(pending_requirements(config))


def _registry_overridden_above(config_path, *, ignore_profile: bool = False) -> bool:
    """Действующий реестр пришёл НЕ из того файла, который правит мастер: слой выше
    (проектный `.gc/config.yml`, managed) переопределил `models`/`slots.default`.

    Считает тем же `origins`, что и витрина происхождения полей в Settings, —
    отдельного знания о слоях мастеру заводить не нужно.

    `ignore_profile` — финал мастера деактивирует профиль (ступень
    `without_profile` лестницы починки): перекрыть запись он уже не сможет, а
    `origins` судит по диску, где профиль ещё лежит. Не исключить его значит
    показать на одном экране два предупреждения, противоречащих друг другу
    (живой смок). Прочие слои деактивация не трогает — они судятся как прежде."""
    if not config_path:
        return False
    from config_origin import origins
    found = origins(str(config_path), ["models", "slots.default"])
    return any(
        origin.overlay_kind
        and not (ignore_profile and origin.overlay_kind.startswith("profile:"))
        for origin in found.values()
    )


def effective_model(config: Dict[str, Any]) -> str:
    """Действующая модель конфига — ПРОВАЙДЕРСКИЙ id из резолва `ModelRouter`
    (§6/§7), а не плоский `agent.model`: волна B пишет реестр (`models.<алиас>`
    + `slots.default`), а плоский ключ оставляет шаблонным сентинелом — по нему
    мастер на своём же настроенном конфиге видел бы «модель не задана» и молча
    переставлял слот на первую модель сервера.

    Канон витрин ветки (`config._effective_model_name`,
    `main._footer_model_name`), но берётся `model`, а не `alias`: обе поверхности
    мастера сверяются со списком С СЕРВЕРА, где модели названы провайдерскими id.

    Резолв не удался (полусырой конфиг: пустой реестр без плоского ключа, слот
    на несуществующий алиас) — фоллбэк на плоский ключ: мастер обязан
    открываться на любом конфиге. Сентинел шаблона — «ещё не задано», а не
    значение: отдаём пусто, как и на конфиге без модели вовсе."""
    name = config.get("model")
    try:
        from model_router import ModelRouter
        resolved = ModelRouter(
            config, level_provider=lambda: config.get("level") or "default",
        ).resolve("main", check_breaker=False)
        name = resolved.model
    except Exception:
        pass
    return "" if not name or name == MODEL_SENTINEL else name


def assigned_level_model(config: Dict[str, Any], level: str) -> str:
    """Провайдерский id модели, ЯВНО назначенной уровню, — или пусто, если слот
    не задан.

    Именно явный слот, а не резолв матрицей фоллбэков: незаданный `max` работает
    моделью `default`, но написать против него её имя значило бы сказать
    «уровень назначен» там, где он не назначен, — и следующий шаг закрепил бы
    выдуманное."""
    alias = (config.get("slots") or {}).get(level)
    if not alias:
        return ""
    entry = (config.get("models") or {}).get(alias)
    model = entry.get("model") if isinstance(entry, dict) else None
    return model or alias


def overriding_layer_note(config: Dict[str, Any], config_path=None, *,
                          ignore_profile: bool = False) -> Optional[str]:
    """Спека §11: что перекроет выбор, сделанный в мастере (мастер пишет БАЗОВЫЙ
    config.yml, а конфиг собирается стопкой слоёв — ось 2, §2).

    Профиль (любой источник — базовый указатель, проектный, CLI) лежит над базой →
    предупреждаем. Реестр моделей — только когда он пришёл слоем ВЫШЕ правимого
    файла: свой же реестр config.yml мастер с волны B дописывает сам (решение 10),
    и предупреждать там не о чем, а проектный слой молча оставил бы выбор человека
    без эффекта. Без `config_path` слой не различить — тогда только профиль."""
    profile = config.get("profile") or None
    if profile and not ignore_profile:
        # Текст — общий для всех поверхностей (экран мастера и подсказка gc-cli),
        # поэтому это чистый факт без адреса действия: звать в «Настройки · Профили»
        # там, где TUI нет, значит врать. Адрес дописывает поверхность (спека §3).
        return f'действует профиль "{profile}" — он перекроет модельные настройки'
    if config.get("models") and _registry_overridden_above(config_path,
                                                            ignore_profile=ignore_profile):
        return ("действует реестр моделей из другого слоя конфигурации — ваш выбор "
                "ляжет в основной конфиг, но этот слой его перекроет")
    return None


def setup_guide(config_path: str, config: Dict[str, Any], program: str = "gc-cli",
                just_created: bool = False) -> str:
    """Подсказка по config_path.

    pending непуст → что вписать + как открыть помощник (`{program} --init-config`).
    pending пуст → позитивное «настроен». program — имя бинаря (gc-cli / gc-ua).
    just_created=True → конфиг создан этим запуском (иначе уже существовал).
    Если модель уже задана (а адрес/токен — нет), добавляем ремарку: эти поля
    нужны для preset-модели; либо укажи свою модель и адрес/токен под неё.

    Путь показывается абсолютным (`_display_path`) — той же причиной, что и у
    `explain_broken_config`: относительный бессмыслен вне момента запуска.
    `config_path`, переданный дальше в `overriding_layer_note`, не трогаем —
    у неё своя (нечувствительная к форме пути) нормализация через
    `config_origin._overlay_paths`."""
    import os

    def _display_path(path) -> str:
        return os.path.abspath(path) if path else path

    profile_note = overriding_layer_note(config, config_path)
    pending = pending_requirements(config)
    if not pending:
        base = f"[gc] Конфиг настроен: {_display_path(config_path)} — можно запускать."
        return f"{base}\n[gc] {profile_note}." if profile_note else base
    fields = "\n".join(f"    • {r.hint}" for r in pending)
    note = ""
    if _legacy_model_set(config):
        # Ремарка — про ПЛОСКИЙ ключ (её текст его и называет): при живом реестре
        # с нетронутым плейсхолдером она печатала бы «agent.model = <ВПИШИТЕ_МОДЕЛЬ>».
        note = (
            f"\n  Модель по умолчанию уже задана (agent.model = {config.get('model')}) —\n"
            f"  осталось указать для неё адрес и токен; либо укажите свою модель\n"
            f"  и соответствующие адрес/токен.\n"
        )
    if just_created:
        header = "  GigaCode — конфигурация создана, настройка не завершена\n\n"
        location = f"  Создан файл конфигурации:\n    {_display_path(config_path)}\n\n"
    else:
        header = "  GigaCode — настройка не завершена\n\n"
        location = f"  Конфигурация:\n    {_display_path(config_path)}\n\n"
    if profile_note:
        note += f"\n  {profile_note}.\n"
    return (
        f"\n{header}{location}"
        f"  Не заполнены обязательные поля:\n{fields}\n"
        f"{note}\n"
        f"  Что дальше:\n"
        f"    • Помощник настройки (рекомендуется):\n"
        f"        {program} --init-config\n"
        f"    • Либо впишите поля вручную и запустите снова.\n\n"
        f"  MCP-серверы (веб-поиск, документы и др.): gc-mcp install — поставить, gc-mcp help — все команды\n"
    )


# --- Объяснение вместо трейсбека на нерезолвимом конфиге --------------------

# Подписи участников карты («Участвуют:») — kind из config_origin._overlay_paths
# плюс своя "base" для самой базы (её _overlay_paths не называет — она точка
# отсчёта, а не оверлей).
_BROKEN_CONFIG_LAYER_LABELS = {
    "base": "база",
    "managed": "managed",
    "project": "проект",
    "profile:project": "профиль",
    "profile:base": "профиль",
}


def explain_broken_config(config_path: str, exc: Exception, *, program: str = "gc-cli") -> str:
    """Человекочитаемое объяснение вместо трейсбека, когда `load_config_ex()` не
    смог собрать конфиг. `exc` — пойманный `ValueError` (pydantic `ValidationError`
    входит в него подклассом — единственный исход загрузки после защиты чтения
    базы, см. config.py/layers.py::_layer_document).

    Собирается из трёх частей: сам текст ошибки (pydantic-дамп приводится к тому
    же читаемому виду, что и на путях записи, — `layers.py::_app_config_from_raw`)
    + карта участвующих файлов (сырые документы, переживающие сломанный конфиг:
    `config_origin._overlay_paths` + `layers.resolve_profile_pointer`) + советы
    «что сделать» — от точечной починки к обходу и лишь затем к грубому откату
    (решение владельца: откат последний, он теряет модели, MCP и голос).

    Файл(ы) для починки называет `config_origin.error_origins` — общее с отказом
    записи ядро «текст ошибки → файлы, где лежат названные ею ключи» (закрытый
    список подозреваемых, сверка префиксом "; "-сегмента, `origins()` за файлом).
    Не нашли ни одного ключа (битый YAML, нечитаемый файл, профиль не найден по
    указателю) — сопоставлять нечего, совет становится «поправить файл: <путь>»,
    где путь — тот, что называет сама ошибка (слои пишут «<path>: …», см.
    layers.py::_layer_document), либо базовый, если ни один известный файл в
    тексте не назван.

    Показанные пути — всегда абсолютные (`_display_path`): относительный, как
    из `--config .\\config.yml`, бессмыслен вне момента запуска (другой cwd,
    копипаста в переписку) и рядом с уже абсолютным путём профиля читается
    неряшливо. Нормализация — только на выходе, в f-строках ниже: `config_path`
    и всё, что от него посчитано (`participants`, `named`, `culprit.path`),
    остаются НЕТРОНУТЫМИ — на них стоит сравнение `error_text.startswith(...)`,
    и здесь важно опознать файл ТЕМ же видом пути, каким его назвала сама
    ошибка (см. `_layer_document`/`validate_registry_pairs`, которые пишут путь
    без прогона через `os.path.abspath`)."""
    import os

    from pydantic import ValidationError as _ValidationError

    from config_modules.layers import resolve_profile_pointer
    from config_origin import _overlay_paths, error_origins

    def _display_path(path) -> str:
        return os.path.abspath(path)

    error_text = (
        f"конфиг не проходит схему: {exc}" if isinstance(exc, _ValidationError) else str(exc)
    )

    participants = [("base", config_path)] + _overlay_paths(config_path)
    participant_lines = [
        f"    {_BROKEN_CONFIG_LAYER_LABELS[kind].ljust(10)}{_display_path(path)}"
        + ("   (активен)" if kind.startswith("profile:") else "")
        for kind, path in participants
    ]

    culprits = error_origins(config_path, error_text)

    fix_lines: list[str] = []
    if culprits:
        for culprit in culprits:
            fix_lines.append(
                f"поправить {', '.join(culprit.keys)} в:  {_display_path(culprit.path)}")
    else:
        named = config_path
        for _, candidate in participants:
            if error_text.startswith(f"{candidate}:"):
                named = candidate
                break
        fix_lines.append(f"поправить файл:  {_display_path(named)}")

    if resolve_profile_pointer(config_path):
        fix_lines.append(f'запустить без профиля, ничего не потеряв:  {program} --profile ""')
    fix_lines.append("откатить конфиг к шаблону (старый уйдёт в бэкап):  gc-cli --reset-config")

    error_block = "\n".join(f"  {line}" for line in error_text.splitlines())
    fix_block = "\n".join(f"    • {line}" for line in fix_lines)
    participants_block = "\n".join(participant_lines)

    return (
        f"[gc] Конфиг не загружается: {_display_path(config_path)}\n\n"
        f"{error_block}\n\n"
        f"  Участвуют:\n{participants_block}\n\n"
        f"  Что сделать:\n{fix_block}\n"
    )


# --- Загрузка конфига «чинилками» (--init-config) --------------------------

REPAIR_STAGE_FULL = "full"
REPAIR_STAGE_WITHOUT_PROFILE = "without_profile"
REPAIR_STAGE_TEMPLATE = "template"


class RepairLoad(NamedTuple):
    """Итог лестницы `load_config_for_repair`: конфиг, метаданные профиля,
    ступень приземления и готовое сообщение человеку (None — деградации не было)."""
    config: Dict[str, Any]
    meta: Any                      # ProfileMeta загрузчика
    stage: str
    message: Optional[str]


def _repair_message(stage: str, exc: Exception, profile: str) -> str:
    """Текст деградации — ОДИН на все поверхности (stderr gc-cli, stderr gc-ua без
    TTY, runtime-notice мастера). Говорит не только «сломано», но и что именно
    отброшено: человек обязан понимать, почему поля мастера выглядят иначе, чем
    его файл."""
    if stage == REPAIR_STAGE_WITHOUT_PROFILE:
        named = f' "{profile}"' if profile else ""
        return (
            f"config.yml не читается с профилем{named} ({exc}) — профиль снят на этот "
            f"запуск, остальные ваши настройки (адрес, токен, реестр моделей) действуют."
        )
    return (
        f"config.yml не читается ({exc}) — продолжаю на пакетном шаблоне: ваших "
        f"настроек в мастере не видно, он перепишет ваш файл."
    )


def load_config_for_repair() -> RepairLoad:
    """Загрузка конфига для «чинилки» (`gc-cli --init-config`, `gc-ua --init-config`)
    — лестница сверху вниз: полная загрузка → БЕЗ ПРОФИЛЯ → пакетный шаблон.

    Дом — здесь, а не в `config.py`, потому что деградация загрузки — свойство
    ПЕРВОГО ЗАПУСКА и починки, а не загрузчика: обычный старт обязан падать громко
    (`explain_broken_config` + код 1), и лестница рядом с ним в config.py читалась
    бы как «конфиг всегда как-нибудь да загрузится». Здесь же живёт родня —
    политика «настроен ли конфиг» и весь текст, который эти же две поверхности
    показывают человеку.

    Ступень 2 — не украшение: поломка почти всегда сидит в профиле (слот на
    несуществующий алиас), а не в базе, и падение сразу на шаблон стоило человеку
    рабочих адреса и токена — мастер уходил в сеть на `your-backend:PORT` и не
    получал список моделей при живом токене.

    Ступень 3 отдаёт ШАБЛОН, а не пустой `{}`: рантайм TUI требует полей вроде
    `threads_data_path`, и на пустом словаре `resolve_threads_dir` падает сразу
    следом. Шаблон валиден по построению; мастер всё равно пишет в
    ПОЛЬЗОВАТЕЛЬСКИЙ файл, а не в него.

    Про поломку рассказывает ПЕРВАЯ ошибка (та, что человек видит на обычном
    старте), а не отказ промежуточной ступени: вторая — диагноз уже урезанного
    конфига."""
    from config import _default_config_path, load_config_ex, resolve_config_path

    try:
        config, meta = load_config_ex(None)
        return RepairLoad(config, meta, REPAIR_STAGE_FULL, None)
    except ValueError as exc:
        broken = exc

    try:
        config, meta = load_config_ex(None, profile_override="")
    except ValueError:
        config, meta = load_config_ex(_default_config_path(), profile_override="")
        return RepairLoad(config, meta, REPAIR_STAGE_TEMPLATE,
                          _repair_message(REPAIR_STAGE_TEMPLATE, broken, ""))

    from config_modules.layers import resolve_profile_pointer

    profile = resolve_profile_pointer(resolve_config_path(None))
    return RepairLoad(config, meta, REPAIR_STAGE_WITHOUT_PROFILE,
                      _repair_message(REPAIR_STAGE_WITHOUT_PROFILE, broken, profile))


# --- Запись собранного мастером (волна B, спека §11) ----------------------

def default_alias_for(model_id: str) -> str:
    """Алиас `models.<alias>` из провайдерского id модели: последний path-сегмент
    до опций (`qwen/qwen-2.5-flash` → `qwen-2.5-flash`; `qwen/m:free` → `m`),
    короткое имя без `/` — как есть. Имени алиаса мастер не спрашивает: на первом
    запуске лишний вопрос дороже, чем предсказуемое имя, а переименовать запись
    можно в секции «Модели».

    Коллизия с зарезервированным словом уровня (`RESERVED_WORDS`, §4) → суффикс
    `-model` (`light` → `light-model`): иначе запись не прошла бы валидацию.
    Пустой id (или после нормализации — пустая строка) → `"model"`.
    """
    from config_modules.model_registry import RESERVED_WORDS

    if not model_id:
        return "model"
    slug = model_id.strip().rsplit("/", 1)[-1].split(":", 1)[0].strip()
    if not slug:
        return "model"
    return f"{slug}-model" if slug in RESERVED_WORDS else slug


def unique_alias(alias: str, taken) -> str:
    """`alias`, либо `alias-2`/`alias-3`/… — первое имя, не занятое в `taken`.

    Нужен шагу уровней: два РАЗНЫХ провайдерских id могут свернуться в одно имя
    (`vendor-a/qwen-plus` и `vendor-b/qwen-plus`), и без разведения вторая запись
    затёрла бы первую по merge-контракту — оба слота увели бы уровни на одну
    модель, а выбор человека потерялся бы молча."""
    taken = set(taken)
    if alias not in taken:
        return alias
    n = 2
    while f"{alias}-{n}" in taken:
        n += 1
    return f"{alias}-{n}"


# Модели, собранные мастером: ключ в `values` → слот реестра. `model` — базовый шаг,
# `model_light`/`model_max` — опциональный скипаемый шаг уровней (TUI, решение 8
# плана волны B). Имена ключей — контракт между шагами и записью, поэтому живут
# здесь, у политики записи, а не в экране.
MODEL_SLOT_KEYS: Dict[str, str] = {
    "default": "model",
    "light": "model_light",
    "max": "model_max",
}


def _aliases_in_file(config_path) -> Dict[str, str]:
    """Алиасы реестра, УЖЕ лежащие в правимом файле → провайдерский id каждого
    (`model` не задан — id равен самому алиасу, §4).

    Нужны уникализации имён: `upsert_model` МЕРЖИТ в существующую запись, поэтому
    занятое имя — это чужая пара «адрес + токен», которая переживёт прогон. Читаем
    именно правимый файл, а не merged-конфиг: домержится запись только здесь.

    Файл нечитаем или не парсится — считаем реестр пустым: сказать об этом человеку
    всё равно нечем, а следом идёт `apply_registry_ops`, который тот же файл читает
    сам и отказывает понятной ошибкой."""
    from pathlib import Path

    import yaml

    try:
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, dict):
        return {}
    return {alias: ((entry or {}).get("model") if isinstance(entry, dict) else None) or alias
            for alias, entry in models.items()}


def _registry_ops(values: Dict[str, Any], existing: Optional[Dict[str, str]] = None
                  ) -> List[tuple]:
    """Операции реестра для собранных мастером моделей: `models.<alias>` + свой
    слот на каждый ЗАПОЛНЕННЫЙ уровень (незаполненный не пишет ничего — слот на
    несуществующий алиас нерезолвим, §4).

    Один и тот же id на двух уровнях — одна запись и два слота на неё; разные id
    с одинаковым хвостом имени разводятся `unique_alias`. Соответствие «id → уже
    выданный алиас» ищется по РЕЗУЛЬТАТУ, а не по первому расчёту имени: иначе
    третий уровень с уже разведённой моделью получал бы ещё одну запись-близнеца.

    `existing` (алиас → id, уже лежащие в файле — `_aliases_in_file`) участвует в
    той же уникализации: имя за ТОЙ ЖЕ моделью переиспользуется (upsert домержится
    в неё, и тариф записи переживает повторный прогон), а имя за ДРУГОЙ моделью
    занято — взяв его, мастер увёл бы слот на чужую пару «адрес + токен», хотя
    человек только что ввёл новое подключение."""
    ops: List[tuple] = []
    existing = existing or {}
    planned: Dict[str, str] = {}          # алиас → id, уже запланированный этой записью
    for slot, key in MODEL_SLOT_KEYS.items():
        model_id = values.get(key)
        if not model_id:
            continue
        # Имя ищется и среди УЖЕ ЛЕЖАЩИХ в файле, а не только среди
        # запланированного этой записью: иначе повторный прогон мастера не узнавал
        # записи, названные человеком (`fast`, `brain`), и заводил рядом близнецов
        # под расчётным именем. Слоты уезжали на новые записи — без своей пары
        # «адрес и токен», — а прежние оставались осиротевшими: уровень, ходивший
        # на локальный сервер, уходил на общий endpoint с id локальной модели.
        # Порядок словарей важен: своё запланированное имя побеждает существующее.
        alias = next((name for name, known_id in {**existing, **planned}.items()
                      if known_id == model_id), None)
        if alias is None:
            alias = default_alias_for(model_id)
            if alias in planned or existing.get(alias, model_id) != model_id:
                alias = unique_alias(alias, set(planned) | set(existing))
            planned[alias] = model_id
            ops.append(("upsert_model", alias, {"model": model_id}))
        ops.append(("write_slot", slot, alias))
    return ops


class PartialWriteError(ValueError):
    """Отказ ВТОРОЙ записи мастера — записи реестра при этом уже на диске.

    Подкласс `ValueError`: вызывающие ловят его прежними `except ValueError`, и
    ветку «конфиг не записан» разбирают те, кому есть что сказать иначе."""


def write_wizard_values(config_path, values: Dict[str, Any], *,
                        deactivate_profile: bool = False) -> List[str]:
    """Записать собранное мастером в `config_path` — ОДИН путь записи на обе
    поверхности (CLI-визард и TUI-мастер), политика живёт здесь.

    Подключение (`completions_url`/`Authorization`/`proxy`) пишется плоскими
    ключами `backend.*`, КАК РАНЬШЕ: это база наследования записей реестра (§4) и
    живой источник пробы соединения. Модели — записями реестра `models.<alias>:
    {model: <id>}` + своими слотами вместо легаси `agent.model` (спека §11):
    базовый шаг даёт `slots.default`, опциональный шаг уровней — `slots.light`/
    `slots.max`. Per-model `url`/`token` мастер НЕ пишет намеренно: в режиме
    `responses` они не действуют (§4), и запись, самодостаточная в
    `chat_completions`, увела бы responses-конфиг с рабочего endpoint'а.

    Все записи реестра уходят ОДНОЙ транзакцией: вторая запись без слота
    нерезолвима (§4) и была бы отвергнута валидацией отдельной операцией.

    Записей всё же ДВЕ (реестр и плоские `backend.*`), и порядок между ними —
    часть политики: реестр идёт ПЕРВЫМ. Его отказ тогда не пишет ничего, и «конфиг
    не записан» — правда, включая секрет: плоским ключом уходит ТОКЕН, и оставить
    его на диске от несостоявшегося прогона нельзя. Отказ второй записи оставляет
    модели без подключения — `is_unconfigured` вернёт человека в мастер, а секрета
    на диске нет.

    `deactivate_profile` — лестница приземлилась на ступень
    `REPAIR_STAGE_WITHOUT_PROFILE`: конфиг собрался ТОЛЬКО потому, что профиль сняли
    на этот запуск. Тогда финал снимает и указатель — иначе чинилка не может
    записать собственную починку: обе записи ниже судятся по стеку СЛЕДУЮЩЕГО
    старта, а там указатель снова поднимает сломанный профиль, и запись честно
    отвергается («slots.default: алиас ... не найден» — живой тупик стенда). Файл
    профиля не трогаем: человек чинит его позже и включает обратно.

    Снятие указателя идёт ПЕРЕД обеими записями, и это не вопрос удобства. Порядок
    наоборот невозможен физически: пока указатель на месте, гейт отвергает всё, что
    мастер собрал. А частичный исход честен: ступень `without_profile` значит, что
    без профиля база грузится, — то есть после одного лишь снятия конфиг ПОДНИМЕТСЯ
    (было: не грузился вовсе), настройки человека останутся прежними и рабочими, а
    токен этого прогона на диск ещё не уходил.

    Возвращает якоря плоских полей, которых в конфиге не нашлось (вызывающий
    решает, показывать ли предупреждение); отказ записи — `ValueError`.
    """
    from pathlib import Path

    import onboarding_config_writer as ocw
    from config import _default_config_path, validate_config_text
    from config_modules.layers import profile_pointer_write_target
    from config_modules.registry_write import apply_registry_ops

    if deactivate_profile:
        # Куда ложится `profile: ""` и как — общие с секцией «Профили» правила
        # (`profile_pointer_write_target` + `ocw.write_profile_pointer`): своя
        # редакция писала бы в слой, который следующий старт всё равно перекроет.
        ocw.write_profile_pointer(
            profile_pointer_write_target(config_path), "", base_path=config_path)
    template_path = _default_config_path()
    try:
        template = Path(template_path).read_text(encoding="utf-8")
    except OSError:
        # Шаблон недоступен — это не повод уронить мастер трейсбеком (вызывающие
        # ловят `ValueError`): якорь отсутствующей секции писатель синтезирует сам
        # (`ensure_section_anchor_text`), а плоскому ключу без якоря останется
        # отчёт «не записан». Путь недочитанного шаблона дальше не отдаём: тот же
        # отказ ждал бы и `write_values`, только уже трейсбеком.
        template = ""
    model_keys = set(MODEL_SLOT_KEYS.values())
    flat = {FIELD_ANCHOR_PATHS.get(key, key): value
            for key, value in values.items() if key not in model_keys}
    ops = _registry_ops(values, _aliases_in_file(config_path))
    registry_written = False
    if ops:
        apply_registry_ops(config_path, ops, template)
        registry_written = True
    try:
        return ocw.write_values(config_path, flat,
                                template_path=template_path if template else None,
                                validate=validate_config_text)
    except ValueError as exc:
        # Какая из двух записей отказала — знает только этот код, а решают судьбу
        # сообщения обе поверхности мастера. Отказ ПЕРВОЙ уходит наружу как был:
        # файл не тронут, «конфиг не записан» — правда. Отказ ВТОРОЙ после
        # состоявшейся первой правдой уже не будет, и молча выдать его за неё
        # значит отправить человека искать нетронутый файл, где лежит новый реестр.
        if registry_written:
            raise PartialWriteError(str(exc)) from exc
        raise
