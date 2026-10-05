"""Фаза 2 валидации секций моделей: ссылочная целостность на merged-реестре.

Фаза 1 (`config_modules/model_registry.py`) валидирует каждый слой конфига
изолированно — типы, зарезервированные имена, конфликты настроек одной модели.
Здесь — итог **после мержа** всех слоёв (base/managed/проект/профиль/CLI):
слоты → существующие алиасы `models`, значения `assign` → уровень или алиас
merged-реестра, ключи `assign.subagents` → канонический реестр имён субагентов.
Кросс-слойные ссылки (слот в config.yml на алиас из профиля) легальны и
проверяются только тут — до мержа проверить нечего.

Отсюда же уходят стартовые notices про сами записи (§7 спеки пары: ослабленный
транспорт под общим токеном) — канал у предупреждений один, и второго заводить
незачем: `load_settings` кладёт их в `runtime.notices`.
"""

from __future__ import annotations

from gc_core.config_modules.model_registry import LEVELS
from gc_core.config_modules.root import AppConfig

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
    "vision",
)


def _check_assign_value(
    models: dict, coordinate: str, value: str, prefix: str, errors: list[str]
) -> None:
    if value == "auto":
        errors.append(
            f'{prefix}{coordinate}: значение "auto" недопустимо для привязки — '
            "auto управляет активным уровнем, а не резолвится в модель"
        )
        return
    if value in LEVELS or value in models:
        return
    errors.append(
        f'{prefix}{coordinate}: значение "{value}" — не уровень ({", ".join(LEVELS)}) и не алиас merged-реестра models'
    )


def default_slot_required(models, slots) -> bool:
    """Нужен ли ЯВНЫЙ слот, чтобы реестр резолвился (спека §4): при 2+ моделях и ни
    одном заданном слоте вывести `default` не из чего.

    Отдельным предикатом, потому что спрашивают его двое: валидатор — чтобы
    пожаловаться, и слой записи — чтобы форма успела спросить человека ДО записи.
    Написанное дважды от руки, это правило уже разъезжалось: одна копия считала по
    слитому стеку, другая по одному файлу, и вопрос «какая модель по умолчанию» не
    задавался там, где запись потом отвергал гейт."""
    return (
        len(models) >= 2
        and slots.light is None
        and slots.default is None
        and slots.max is None
    )


def _slot_errors(cfg: AppConfig, prefix: str) -> list[str]:
    models = cfg.models
    errors: list[str] = []
    for slot_name in ("light", "default", "max"):
        alias = getattr(cfg.slots, slot_name)
        if alias is not None and alias not in models:
            errors.append(
                f'{prefix}slots.{slot_name}: алиас "{alias}" не найден в merged-реестре models'
            )
    if default_slot_required(models, cfg.slots):
        errors.append(
            f"{prefix}slots.default: моделей в реестре ≥2 — укажите slots.default "
            "(или любой другой слот — default резолвится по матрице фоллбэков)"
        )
    return errors


def _assign_errors(cfg: AppConfig, prefix: str) -> tuple[list[str], bool]:
    """Ошибки привязок assign.* и признак «привязки вообще заданы»."""
    models = cfg.models
    errors: list[str] = []
    assign_touched = False
    for field in _ASSIGN_SCALAR_FIELDS:
        value = getattr(cfg.assign, field)
        if value is not None:
            assign_touched = True
            _check_assign_value(
                models, f"assign.{field}", value, prefix, errors
            )
    for subagent_name, value in cfg.assign.subagents.items():
        assign_touched = True
        if subagent_name != "*" and subagent_name not in _SUBAGENT_NAMES:
            errors.append(
                f"{prefix}assign.subagents.{subagent_name}: неизвестный субагент — "
                f'канонический реестр: {", ".join(sorted(_SUBAGENT_NAMES))}, или "*"'
            )
        _check_assign_value(
            models, f"assign.subagents.{subagent_name}", value, prefix, errors
        )
    return errors, assign_touched


def validate_model_sections(
    cfg: AppConfig, *, source_hint: str = ""
) -> list[str]:
    """Ссылочная целостность models/slots/assign на итоговом merged-конфиге.

    Кидает `ValueError` с координатой(ами) ключа при первой обнаруженной ошибке
    (или собранными сразу несколькими — сообщение объединяет все). Возвращает
    список warning-строк (например, «привязки без реестра моделей не действуют»)
    — тем же каналом, что и предупреждения compat-слоя (`load_config` мёржит их
    в `runtime.notices`).
    """
    prefix = f"{source_hint}: " if source_hint else ""
    models = cfg.models
    errors: list[str] = []

    errors += _slot_errors(cfg, prefix)
    assign_errors, assign_touched = _assign_errors(cfg, prefix)
    errors += assign_errors

    if errors:
        raise ValueError("; ".join(errors))

    # §7 спеки пары: запись, не задавшая своего подключения, работает ОБЩИМ токеном —
    # и вправе при этом ослабить транспорт под собой (`use_ssl: false`, клиентский
    # сертификат). Отказом это не считается (решение владельца 2), но и молчать нельзя:
    # что общий ключ уходит по ослабленному каналу, видно только отсюда — в самой
    # записи это выглядит одним сетевым квирком. Предикат — у записи
    # (`ModelEntry.weakens_shared_transport`), здесь только СЛОВА предупреждения.
    notices = [
        f"{prefix}запись {alias} ослабляет защиту запроса, который идёт с общим токеном"
        for alias, entry in models.items()
        if entry.weakens_shared_transport()
    ]
    if not models and assign_touched:
        notices.append(
            f"{prefix}assign: привязки без реестра моделей не действуют (models пуст)"
        )
    return notices
