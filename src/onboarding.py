"""Онбординг первого запуска: политика «настроен ли конфиг» + текст-гайд.

Модуль-владелец презентации/политики первого запуска. Контроллеры (cli, tui)
зовут is_unconfigured()/setup_guide(); сюда же вынесены сентинелы дефолт-шаблона
(единый источник истины, сверяется тестом с config.default.yml).

Требования стратегия-зависимы: auth удовлетворяется токеном ИЛИ блоком agent_auth
(сертификатная авторизация — без статического токена). Подсказка перечисляет только
реально незаполненное; если всё покрыто — позитивное «настроен».
"""
from typing import Any, Callable, Dict, List, NamedTuple

# Сентинелы из config.default.yml (страж совпадения — tests/test_onboarding.py).
URL_SENTINEL = "http://your-backend:PORT/v1/chat/completions"
TOKEN_SENTINEL = "Bearer <ВПИШИТЕ_ТОКЕН>"
MODEL_SENTINEL = "<ВПИШИТЕ_МОДЕЛЬ>"


def _endpoint_set(c: Dict[str, Any]) -> bool:
    return c.get("completions_url") not in (None, URL_SENTINEL)


def _model_set(c: Dict[str, Any]) -> bool:
    return c.get("model") not in (None, "", MODEL_SENTINEL)


def _token_set(c: Dict[str, Any]) -> bool:
    auth = (c.get("chat_completion_headers") or {}).get("Authorization")
    return auth not in (None, "", TOKEN_SENTINEL)


def _agent_auth_set(c: Dict[str, Any]) -> bool:
    auth_cfg = c.get("agent_auth") or {}
    return bool(auth_cfg) and bool(auth_cfg.get("auth_endpoint"))


def _auth_set(c: Dict[str, Any]) -> bool:
    return _token_set(c) or _agent_auth_set(c)


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


def pending_requirements(config: Dict[str, Any]) -> List[_Requirement]:
    """Требования, ещё не удовлетворённые текущим конфигом."""
    return [r for r in REQUIREMENTS if not r.is_satisfied(config)]


def is_unconfigured(config: Dict[str, Any]) -> bool:
    """True, если конфиг ещё не готов к запуску (есть незакрытое требование)."""
    return bool(pending_requirements(config))


def setup_guide(config_path: str, config: Dict[str, Any]) -> str:
    """Подсказка по config_path.

    pending непуст → что вписать. pending пуст → позитивное «настроен».
    Если модель уже задана (а адрес/токен — нет), добавляем ремарку: эти поля
    нужны для preset-модели; либо укажи свою модель и адрес/токен под неё."""
    pending = pending_requirements(config)
    if not pending:
        return f"[gc] Конфиг настроен: {config_path} — можно запускать."
    lines = "\n".join(f"  • {r.hint}" for r in pending)
    note = ""
    if _model_set(config):
        note = (
            f"     Модель по умолчанию уже задана (agent.model = {config.get('model')}) — "
            f"осталось указать для неё адрес и токен; либо укажите свою модель и "
            f"соответствующие адрес/токен.\n"
        )
    return (
        f"[gc] Конфиг ещё не настроен: {config_path}\n"
        f"     Впишите перед запуском:\n{lines}\n"
        f"{note}"
        f"     затем запустите снова."
    )
