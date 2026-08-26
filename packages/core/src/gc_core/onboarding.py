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

from gc_core import config_raw
from gc_core.app_version import REPO_GIT_URL


def _mcp_hint() -> str:
    """Подсказка про MCP: команда управления или как её доставить.

    Команды `gc-mcp` объявляет дистрибутив gc-mcp, а `uv tool install` кладёт в
    PATH скрипты только запрошенного дистрибутива. Поэтому в узком профиле
    (`gc-cli` / `gc-tui` / `gc-server`) менеджер установлен как зависимость, но
    в PATH его нет — советовать `gc-mcp install` там значит советовать
    command not found.
    """
    import shutil

    if shutil.which("gc-mcp"):
        return "gc-mcp install — поставить, gc-mcp help — все команды"
    return (
        "менеджер ставится отдельно: "
        'uv tool install "gc-mcp @ git+' + REPO_GIT_URL + '#subdirectory=packages/mcp"'
    )


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


# Публичное имя для tui/ (живёт за extra [tui] и обязано зависеть от ядра только
# через публичный контракт). Делегирующая обёртка, а не алиас: алиас захватил бы
# объект функции на импорте и сломал monkeypatch приватного имени в тестах.
def valid_endpoint_url(raw: str) -> bool:
    """Публичное имя для `_valid_endpoint_url`."""
    return _valid_endpoint_url(raw)

# Сентинелы из config.default.yml (страж совпадения — tests/test_onboarding.py).
URL_SENTINEL = "http://your-backend:PORT/v1/chat/completions"
TOKEN_SENTINEL = "Bearer <ВПИШИТЕ_ТОКЕН>"
MODEL_SENTINEL = "<ВПИШИТЕ_МОДЕЛЬ>"


def _endpoint_set(c: Dict[str, Any]) -> bool:
    return c.get("completions_url") not in (None, URL_SENTINEL)


def _model_set(c: Dict[str, Any]) -> bool:
    return c.get("model") not in (None, "", MODEL_SENTINEL)


def _agent_auth_set(c: Dict[str, Any]) -> bool:
    auth_cfg = c.get("agent_auth") or {}
    return bool(auth_cfg) and bool(auth_cfg.get("auth_endpoint"))


def _auth_set(c: Dict[str, Any]) -> bool:
    # Auth «не задан» (pending) ТОЛЬКО если в конфиге остался шаблонный плейсхолдер
    # токена (и нет полноценного agent_auth). Реальный токен ИЛИ намеренное отсутствие
    # заголовка (локальный прокси / эндпоинт без auth сам ставит авторизацию) → задан.
    # Гейт — детектор незаполненного шаблона, а не валидатор auth: отсутствие ≠ сентинел.
    auth = (c.get("chat_completion_headers") or {}).get("Authorization")
    if auth == TOKEN_SENTINEL:
        return _agent_auth_set(c)
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

    Спека §11: заполненный merged-реестр моделей (`models` непуст, любой источник —
    базовый конфиг, профиль, проектный слой) закрывает требование самостоятельно —
    легаси-плейсхолдеры (`backend.completions_url`/`agent.model`) тогда не блокируют,
    они лишь база наследования (`active_profile_or_registry_note`)."""
    if config.get("models"):
        return False
    return bool(pending_requirements(config))


def active_profile_or_registry_note(config: Dict[str, Any]) -> Optional[str]:
    """Спека §11: активный merged-реестр моделей ИЛИ выбранный профиль (любой
    источник — базовый указатель, проектный, CLI) — мастер онбординга пишет
    плоские легаси-поля (`backend.completions_url`/`agent.model`), но при живом
    реестре/профиле они станут только БАЗОЙ НАСЛЕДОВАНИЯ для незаданного, а не
    рабочей моделью (Router решает резолв через слоты/привязки, §6). Профиль —
    более конкретная причина, чем голый реестр (частный случай §1), поэтому
    проверяется первым."""
    profile = config.get("profile") or None
    if profile:
        return (f'действует профиль "{profile}" — эти поля станут базой наследования, '
                'а не рабочей моделью')
    if config.get("models"):
        return ("действует реестр моделей — эти поля станут базой наследования, "
                "а не рабочей моделью")
    return None


def setup_guide(config_path: str, config: Dict[str, Any], program: str = "gc-cli",
                just_created: bool = False) -> str:
    """Подсказка по config_path.

    pending непуст → что вписать + как открыть помощник (`{program} --init-config`).
    pending пуст → позитивное «настроен». program — имя бинаря (gc-cli / gc-ua).
    just_created=True → конфиг создан этим запуском (иначе уже существовал).
    Если модель уже задана (а адрес/токен — нет), добавляем ремарку: эти поля
    нужны для preset-модели; либо укажи свою модель и адрес/токен под неё."""
    profile_note = active_profile_or_registry_note(config)
    pending = pending_requirements(config)
    if not pending:
        base = f"[gc] Конфиг настроен: {config_path} — можно запускать."
        return f"{base}\n[gc] {profile_note}." if profile_note else base
    fields = "\n".join(f"    • {r.hint}" for r in pending)
    note = ""
    if _model_set(config):
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
        f"  MCP-серверы (веб-поиск, документы и др.): {_mcp_hint()}\n"
    )
