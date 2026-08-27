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


def _every_entry_carries(c: Dict[str, Any], field: str) -> bool:
    """У КАЖДОЙ записи реестра задано своё `field` — тогда плоский `backend` для
    этого поля не нужен (спека §4: незаданное у записи наследуется от него).

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
            and all((entry or {}).get(field) for entry in models.values()))


def _endpoint_set(c: Dict[str, Any]) -> bool:
    if c.get("completions_url") not in (None, URL_SENTINEL):
        return True
    return _every_entry_carries(c, "url")


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
    когда их несут сами записи (`_every_entry_carries`)."""
    return bool(c.get("models")) or _legacy_model_set(c)


def _agent_auth_set(c: Dict[str, Any]) -> bool:
    auth_cfg = c.get("agent_auth") or {}
    return bool(auth_cfg) and bool(auth_cfg.get("auth_endpoint"))


def _auth_set(c: Dict[str, Any]) -> bool:
    # Auth «не задан» (pending) ТОЛЬКО если в конфиге остался шаблонный плейсхолдер
    # токена (и нет полноценного agent_auth). Реальный токен ИЛИ намеренное отсутствие
    # заголовка (локальный прокси / эндпоинт без auth сам ставит авторизацию) → задан.
    # Гейт — детектор незаполненного шаблона, а не валидатор auth: отсутствие ≠ сентинел.
    # Симметрично адресу: авторизацию может нести и сама запись реестра. Смотрим на
    # `authorization_header` — вычисленное поле снимка (`config._serialize_model_entry`),
    # сырого `token` в загруженном конфиге уже нет.
    auth = (c.get("chat_completion_headers") or {}).get("Authorization")
    if auth == TOKEN_SENTINEL:
        return _agent_auth_set(c) or _every_entry_carries(c, "authorization_header")
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


def _registry_overridden_above(config_path) -> bool:
    """Действующий реестр пришёл НЕ из того файла, который правит мастер: слой выше
    (проектный `.gc/config.yml`, managed) переопределил `models`/`slots.default`.

    Считает тем же `origins`, что и витрина происхождения полей в Settings, —
    отдельного знания о слоях мастеру заводить не нужно."""
    if not config_path:
        return False
    from config_origin import origins
    found = origins(str(config_path), ["models", "slots.default"])
    return any(origin.overlay_kind for origin in found.values())


def overriding_layer_note(config: Dict[str, Any], config_path=None) -> Optional[str]:
    """Спека §11: что перекроет выбор, сделанный в мастере (мастер пишет БАЗОВЫЙ
    config.yml, а конфиг собирается стопкой слоёв — ось 2, §2).

    Профиль (любой источник — базовый указатель, проектный, CLI) лежит над базой →
    предупреждаем. Реестр моделей — только когда он пришёл слоем ВЫШЕ правимого
    файла: свой же реестр config.yml мастер с волны B дописывает сам (решение 10),
    и предупреждать там не о чем, а проектный слой молча оставил бы выбор человека
    без эффекта. Без `config_path` слой не различить — тогда только профиль."""
    profile = config.get("profile") or None
    if profile:
        # Текст — общий для всех поверхностей (экран мастера и подсказка gc-cli),
        # поэтому это чистый факт без адреса действия: звать в «Настройки · Профили»
        # там, где TUI нет, значит врать. Адрес дописывает поверхность (спека §3).
        return f'действует профиль "{profile}" — он перекроет модельные настройки'
    if config.get("models") and _registry_overridden_above(config_path):
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
    нужны для preset-модели; либо укажи свою модель и адрес/токен под неё."""
    profile_note = overriding_layer_note(config, config_path)
    pending = pending_requirements(config)
    if not pending:
        base = f"[gc] Конфиг настроен: {config_path} — можно запускать."
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
        location = f"  Создан файл конфигурации:\n    {config_path}\n\n"
    else:
        header = "  GigaCode — настройка не завершена\n\n"
        location = f"  Конфигурация:\n    {config_path}\n\n"
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


def _registry_ops(values: Dict[str, Any]) -> List[tuple]:
    """Операции реестра для собранных мастером моделей: `models.<alias>` + свой
    слот на каждый ЗАПОЛНЕННЫЙ уровень (незаполненный не пишет ничего — слот на
    несуществующий алиас нерезолвим, §4).

    Один и тот же id на двух уровнях — одна запись и два слота на неё; разные id
    с одинаковым хвостом имени разводятся `unique_alias`. Соответствие «id → уже
    выданный алиас» ищется по РЕЗУЛЬТАТУ, а не по первому расчёту имени: иначе
    третий уровень с уже разведённой моделью получал бы ещё одну запись-близнеца."""
    ops: List[tuple] = []
    planned: Dict[str, str] = {}          # алиас → id, уже запланированный этой записью
    for slot, key in MODEL_SLOT_KEYS.items():
        model_id = values.get(key)
        if not model_id:
            continue
        alias = next((name for name, planned_id in planned.items()
                      if planned_id == model_id), None)
        if alias is None:
            alias = default_alias_for(model_id)
            if alias in planned:
                alias = unique_alias(alias, planned)
            planned[alias] = model_id
            ops.append(("upsert_model", alias, {"model": model_id}))
        ops.append(("write_slot", slot, alias))
    return ops


def write_wizard_values(config_path, values: Dict[str, Any]) -> List[str]:
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

    Возвращает якоря плоских полей, которых в конфиге не нашлось (вызывающий
    решает, показывать ли предупреждение); отказ записи — `ValueError`.
    """
    from pathlib import Path

    import onboarding_config_writer as ocw
    from config import _default_config_path, validate_config_text
    from config_modules.registry_write import apply_registry_ops

    template_path = _default_config_path()
    model_keys = set(MODEL_SLOT_KEYS.values())
    flat = {FIELD_ANCHOR_PATHS.get(key, key): value
            for key, value in values.items() if key not in model_keys}
    missing = ocw.write_values(config_path, flat, template_path=template_path,
                               validate=validate_config_text)
    ops = _registry_ops(values)
    if ops:
        apply_registry_ops(config_path, ops,
                           Path(template_path).read_text(encoding="utf-8"))
    return missing
