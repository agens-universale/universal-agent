"""Онбординг первого запуска: политика «настроен ли конфиг» + текст-гайд.

Модуль-владелец презентации/политики первого запуска. Контроллеры (cli, tui)
зовут is_unconfigured()/setup_guide(); сюда же вынесены сентинелы дефолт-шаблона
(единый источник истины, сверяется тестом с config.default.yml).

Детект идёт по ИТОГОВОМУ (после merge) плоскому config_dict из load_config:
если в нём остался наш плейсхолдер — конфиг ещё не настроен. Свой конфиг
пользователя (реальные значения) сюда не попадает — плейсхолдеров там нет.
"""
from typing import Any, Callable, Dict, List, NamedTuple

# Сентинелы из config.default.yml (страж совпадения — tests/test_onboarding.py).
URL_SENTINEL = "http://your-backend:PORT/v1/chat/completions"
TOKEN_SENTINEL = "Bearer <ВПИШИ_ТОКЕН>"


class _Field(NamedTuple):
    get: Callable[[Dict[str, Any]], Any]  # достать значение из плоского config_dict
    sentinel: str                          # значение-плейсхолдер «не заполнено»
    yaml_path: str                         # путь в config.yml (что правит пользователь)
    hint: str                              # человекочитаемая подсказка


# Поля, которые пользователь обязан заполнить под себя. Чтобы добавить новое:
# дописать строку сюда + плейсхолдер в config.default.yml (тест-страж поймает
# рассинхрон). И детект, и текст гайда обновятся автоматически.
REQUIRED_FIELDS: List[_Field] = [
    _Field(
        get=lambda c: c.get("completions_url"),
        sentinel=URL_SENTINEL,
        yaml_path="backend.completions_url",
        hint="endpoint OpenAI-совместимого провайдера (chat/completions)",
    ),
    _Field(
        get=lambda c: (c.get("chat_completion_headers") or {}).get("Authorization"),
        sentinel=TOKEN_SENTINEL,
        yaml_path="backend.chat_completion_headers.Authorization",
        hint="токен доступа (Bearer ...)",
    ),
]


def unconfigured_fields(config: Dict[str, Any]) -> List[_Field]:
    """Поля, всё ещё равные своему плейсхолдеру (не заполнены)."""
    return [f for f in REQUIRED_FIELDS if f.get(config) == f.sentinel]


def is_unconfigured(config: Dict[str, Any]) -> bool:
    """True, если конфиг ещё не настроен (остался хоть один плейсхолдер)."""
    return bool(unconfigured_fields(config))


def setup_guide(config_path: str, config: Dict[str, Any]) -> str:
    """Инструкция: какие поля заполнить в config_path, чтобы агент заработал.

    Перечисляет ровно те поля, что ещё плейсхолдеры (на свежем конфиге — все)."""
    pending = unconfigured_fields(config) or list(REQUIRED_FIELDS)
    lines = "\n".join(f"  • {f.yaml_path} — {f.hint}" for f in pending)
    return (
        f"[gc] Конфиг ещё не настроен: {config_path}\n"
        f"     Впиши перед запуском:\n{lines}\n"
        f"     (проверь также agent.model), затем запусти снова."
    )
