"""Фаза 2 валидации секций моделей: ссылочная целостность на merged-реестре.

Фаза 1 (`config_modules/model_registry.py`) валидирует каждый слой конфига
изолированно — типы, зарезервированные имена, конфликты настроек одной модели.
Здесь — итог **после мержа** всех слоёв (base/managed/проект/профиль/CLI):
слоты → существующие алиасы `models`, значения `assign` → уровень или алиас
merged-реестра, ключи `assign.subagents` → канонический реестр имён субагентов.
Кросс-слойные ссылки (слот в config.yml на алиас из профиля) легальны и
проверяются только тут — до мержа проверить нечего.
"""
from __future__ import annotations

from config_modules.model_registry import LEVELS
from config_modules.root import AppConfig

# Канонический реестр имён субагентов. Источник истины — SUBAGENT_CLASSES в
# tools/subagents/registry.py; здесь — статическая копия, а не импорт. Импорт
# оттуда развернул бы направление зависимости (tools/subagents уже импортирует
# config_modules.*, см. runner.py/special_agent.py) и завёл бы конфиг на модуль
# с побочными эффектами регистрации инструментов ради одного списка строк.
# Держать в синхроне со SUBAGENT_CLASSES — задача стража (см. план волны A, Task 15).
#
# Кортеж, а не сразу frozenset: ПОРЯДОК реестра (тот же, что у SUBAGENT_CLASSES) —
# витрина для UI, перечисляющего субагентов поимённо (секция «Модели», привязки);
# у множества порядка нет, и строки привязок прыгали бы от запуска к запуску.
# Проверке ключей порядок не нужен — ей достаётся frozenset ниже.
SUBAGENT_NAMES = (
    "subagent_worker",
    "subagent_code_generator",
    "subagent_code_planner",
    "subagent_planner",
    "subagent_critic",
    "subagent_code_reviewer",
    "subagent_special_agent",
)
_SUBAGENT_NAMES = frozenset(SUBAGENT_NAMES)

_ASSIGN_SCALAR_FIELDS = (
    "finish_gate",
    "text_answer_judge",
    "command_judge",
    "summary",
    "intent_scope",
    "memory_compaction",
    "auto_judge",
)


def _check_assign_value(models: dict, coordinate: str, value: str, prefix: str, errors: list[str]) -> None:
    if value == "auto":
        errors.append(
            f'{prefix}{coordinate}: значение "auto" недопустимо для привязки — '
            "auto управляет активным уровнем, а не резолвится в модель"
        )
        return
    if value in LEVELS or value in models:
        return
    errors.append(
        f'{prefix}{coordinate}: значение "{value}" — не уровень ({", ".join(LEVELS)}) '
        "и не алиас merged-реестра models"
    )


def validate_model_sections(cfg: AppConfig, *, source_hint: str = "") -> list[str]:
    """Ссылочная целостность models/slots/assign на итоговом merged-конфиге.

    Кидает `ValueError` с координатой(ами) ключа при первой обнаруженной ошибке
    (или собранными сразу несколькими — сообщение объединяет все). Возвращает
    список warning-строк (например, «привязки без реестра моделей не действуют»)
    — тем же каналом, что и предупреждения compat-слоя (`load_config` мёржит их
    в `config_dict["compat_notices"]`).
    """
    prefix = f"{source_hint}: " if source_hint else ""
    models = cfg.models
    errors: list[str] = []
    assign_touched = False

    for slot_name in ("light", "default", "max"):
        alias = getattr(cfg.slots, slot_name)
        if alias is not None and alias not in models:
            errors.append(f'{prefix}slots.{slot_name}: алиас "{alias}" не найден в merged-реестре models')

    if (
        len(models) >= 2
        and cfg.slots.light is None
        and cfg.slots.default is None
        and cfg.slots.max is None
    ):
        errors.append(
            f"{prefix}slots.default: моделей в реестре ≥2 — укажите slots.default "
            "(или любой другой слот — default резолвится по матрице фоллбэков)"
        )

    for field in _ASSIGN_SCALAR_FIELDS:
        value = getattr(cfg.assign, field)
        if value is not None:
            assign_touched = True
            _check_assign_value(models, f"assign.{field}", value, prefix, errors)

    for subagent_name, value in cfg.assign.subagents.items():
        assign_touched = True
        if subagent_name != "*" and subagent_name not in _SUBAGENT_NAMES:
            errors.append(
                f'{prefix}assign.subagents.{subagent_name}: неизвестный субагент — '
                f'канонический реестр: {", ".join(sorted(_SUBAGENT_NAMES))}, или "*"'
            )
        _check_assign_value(models, f"assign.subagents.{subagent_name}", value, prefix, errors)

    if errors:
        raise ValueError("; ".join(errors))

    if not models and assign_touched:
        return [f"{prefix}assign: привязки без реестра моделей не действуют (models пуст)"]
    return []
