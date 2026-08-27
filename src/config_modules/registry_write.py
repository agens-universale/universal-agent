"""Слой записи секций моделей: `models` (реестр), `slots` (уровни), `assign`
(привязки потребителей) — в config.yml либо в файл профиля.

Единственный вход записи для UI: операции накладываются на текст цели в ПАМЯТИ
(writer-примитивы `onboarding_config_writer`), валидируется РЕЗУЛЬТАТ и только
потом идёт одна атомарная запись. Невалидно — `RegistryWriteError`, файл
байт-в-байт не тронут (включая якоря секций: их дописывание — часть той же
несостоявшейся записи, а не отдельный побочный эффект).

Базовая единица — ТРАНЗАКЦИЯ `apply_registry_ops`: одна валидация и одна запись
на группу операций. Иначе взаимозависимые правки невыразимы — вторая модель в
реестре обязана прийти вместе со слотом (спека §4: при 2+ моделях и незаданных
слотах конфиг нерезолвим), а поодиночке ни одна из двух записей не проходит.
Одиночные `upsert_model`/`remove_model`/`write_slot`/`write_assign` — та же
машинка с транзакцией из одной операции.

Валидация зависит от цели: config.yml — фаза 1 схемы + фаза 2 ссылочной
целостности (`config.validate_config_text_with_sections`), чтобы на диск не ушёл
честный, но нерезолвимый конфиг; файл профиля — только фаза 1
(`config_modules.profiles.validate_profile_text`): профиль частичен, его слот
вправе ссылаться на алиас нижнего слоя.

Форматирование YAML целиком тут и в writer'е — UI не собирает YAML никогда.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

import onboarding_config_writer as ocw
from config_modules.model_registry import LEVELS
from config_modules.profiles import validate_profile_text


class RegistryWriteError(ValueError):
    """Результат записи не прошёл валидацию цели — файл остался прежним."""


@dataclass(frozen=True)
class RemovalReport:
    """Итог `remove_model`: удалена ли запись и какие ссылки на неё найдены
    (координаты вида `slots.default`, `assign.subagents.subagent_critic`)."""

    removed: bool
    references: list[str]


# Ключ, который в YAML можно писать без кавычек. Всё прочее (wildcard `*`
# привязок субагентов) — через `quote_scalar`: literal-сравнение записей в
# writer'е идёт по тому же виду, в каком ключ лежит в файле.
_PLAIN_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+\-]*$")


def _yaml_key(key: str) -> str:
    """Ключ записи в том виде, в каком он ляжет в файл.

    Мало быть синтаксически плоским: ключ, который YAML разберёт НЕ строкой
    (`123`, `true`, `null`, `0755` — локальные раздачи такие id дают), обязан
    уехать в кавычках, иначе запись валится схемой («models.123.[key]: input
    should be a valid string») в момент, когда соседние ключи уже записаны.
    Одна функция и на запись, и на поиск — обе стороны видят один вид."""
    if not _PLAIN_KEY_RE.match(key):
        return ocw.quote_scalar(key)
    try:
        plain = isinstance(yaml.safe_load(key), str)
    except yaml.YAMLError:
        plain = False
    return key if plain else ocw.quote_scalar(key)


def _read(target) -> str:
    return Path(target).read_text(encoding="utf-8")


def _document(text: str) -> dict:
    """Текущий документ цели — источник СТАРОЙ записи для merge и ссылок для
    отчёта об удалении. Битый YAML отдаём той же ошибкой, что и невалидный
    результат: править запись в файле, который не парсится, нечем."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RegistryWriteError(f"невалидный YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise RegistryWriteError("документ должен быть YAML-объектом")
    return raw


def _commit(target, text: str, is_profile: bool, label: str) -> None:
    if is_profile:
        validate = validate_profile_text
    else:
        # Локальный импорт: `config` — агрегатор, импортирующий config_modules.*;
        # модульный импорт отсюда замкнул бы цикл.
        from config import validate_config_text_with_sections
        validate = validate_config_text_with_sections
    try:
        ocw.write_text(target, text, validate=validate)
    except RegistryWriteError:
        raise
    except ValueError as exc:
        raise RegistryWriteError(f"{label}: {exc}") from exc


def _entry_block_lines(alias_key: str, entry: dict) -> list[str]:
    """Строки блока записи реестра из УЖЕ СМЕРЖЕННОГО словаря. `yaml.safe_dump`
    (не свой построчный сборщик) — чтобы корректно сериализовать произвольные
    `pricing`/`backend`, не только плоские url/token/model."""
    if not entry:
        return [f"{alias_key}: {{}}"]
    body = yaml.safe_dump(entry, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return [f"{alias_key}:"] + [f"  {line}" for line in body.splitlines() if line.strip()]


def _assign_coordinate(consumer: str) -> tuple[str, str]:
    """(родительский dotted-якорь, YAML-ключ) для идентификатора потребителя:
    `finish_gate` → («assign», «finish_gate»); `subagents.subagent_critic` →
    («assign.subagents», «subagent_critic»). Голый `*` — сокращение для
    `subagents.*`: в схеме `assign` wildcard существует ТОЛЬКО как ключ словаря
    субагентов («все остальные субагенты»), топ-уровневого места у него нет."""
    dotted = "subagents.*" if consumer == "*" else consumer
    head, _sep, leaf = dotted.rpartition(".")
    parent = f"assign.{head}" if head else "assign"
    return parent, _yaml_key(leaf)


def _references(document: dict, alias: str) -> list[str]:
    """Координаты слотов и привязок, ссылающихся на алиас реестра."""
    refs: list[str] = []
    slots = document.get("slots") or {}
    for level in LEVELS:
        if slots.get(level) == alias:
            refs.append(f"slots.{level}")
    assign = document.get("assign") or {}
    for consumer, value in assign.items():
        if consumer == "subagents":
            for name, nested in (value or {}).items():
                if nested == alias:
                    refs.append(f"assign.subagents.{name}")
        elif value == alias:
            refs.append(f"assign.{consumer}")
    return refs


def _upsert_model_text(text: str, template: str, alias: str, fields: dict) -> str:
    """MERGE со старой записью, не замена: `fields` несёт только те ключи, о которых
    вызывающий что-то знает, а блок записи переписывается целиком — без наложения
    поля, которых форма не знает вовсе (`pricing`/`backend`) или не трогала в этот
    раз (`token`), молча терялись бы. Правило: ключ со значением — установить,
    ключ со значением `None` — удалить из записи, отсутствующий ключ — сохранить
    как был. Старая запись читается из ТЕКУЩЕГО текста — в транзакции это уже
    результат предыдущих операций, а не состояние диска."""
    old_entry = (_document(text).get("models") or {}).get(alias)
    entry = dict(old_entry) if isinstance(old_entry, dict) else {}
    for key, value in fields.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    key = _yaml_key(alias)
    text = ocw.ensure_section_anchor_text(text, "models", template)
    return ocw.upsert_mapping_entry(text, "models", key, _entry_block_lines(key, entry))


def _remove_model_text(text: str, _template: str, alias: str, force: bool = False) -> str:
    """Удалить запись реестра вместе со ссылающимися слотами/привязками. Записи нет
    — идемпотентный no-op. Ссылки есть, а `force` не задан — ошибка с их перечнем:
    у операции внутри транзакции нет канала отчёта, каким располагает `remove_model`,
    а молча оставить нерезолвимый файл нельзя."""
    document = _document(text)
    if alias not in (document.get("models") or {}):
        return text
    references = _references(document, alias)
    if references and not force:
        raise RegistryWriteError(
            f"на запись ссылаются {', '.join(references)} — снимите ссылки в этой же "
            "транзакции или удаляйте принудительно"
        )
    text = ocw.remove_mapping_entry(text, "models", _yaml_key(alias))
    for reference in references:
        parent, _sep, leaf = reference.rpartition(".")
        text = ocw.remove_mapping_entry(text, parent, _yaml_key(leaf))
    return text


def _write_slot_text(text: str, template: str, level: str, alias) -> str:
    if alias is None:
        return ocw.remove_mapping_entry(text, "slots", level)
    text = ocw.ensure_section_anchor_text(text, "slots", template)
    return ocw.upsert_mapping_entry(
        text, "slots", level, [f"{level}: {ocw.quote_scalar(alias)}"])


def _write_assign_text(text: str, template: str, consumer: str, value) -> str:
    parent, key = _assign_coordinate(consumer)
    if value is None:
        return ocw.remove_mapping_entry(text, parent, key)
    text = ocw.ensure_section_anchor_text(text, "assign", template)
    if parent != "assign":
        # Вложенный якорь (`assign.subagents`) — не топ-уровневый, его
        # `ensure_section_anchor_text` не заводит: заводим записью-словарём.
        nested = parent.split(".", 1)[1]
        try:
            ocw.find_anchor(text, parent)
        except KeyError:
            text = ocw.upsert_mapping_entry(text, "assign", nested, [f"{nested}: {{}}"])
    return ocw.upsert_mapping_entry(
        text, parent, key, [f"{key}: {ocw.quote_scalar(value)}"])


_OPERATIONS = {
    "upsert_model": _upsert_model_text,
    "remove_model": _remove_model_text,
    "write_slot": _write_slot_text,
    "write_assign": _write_assign_text,
}


def _op_label(op: tuple) -> str:
    """Короткое имя операции для сообщения об ошибке: «write_slot light»."""
    return " ".join(str(part) for part in op[:2])


def apply_registry_ops(target, ops, template: str, *, is_profile: bool = False) -> None:
    """Применить группу операций ОДНОЙ транзакцией: последовательно к тексту цели,
    валидация — одна на итоговый результат, запись — одна атомарная.

    `ops` — последовательность кортежей `(имя операции, *аргументы)` в порядке
    применения; имена и аргументы — как у одноимённых функций модуля без `target`
    и `template`: `("upsert_model", alias, fields)`, `("remove_model", alias[, force])`,
    `("write_slot", level, alias_or_none)`, `("write_assign", consumer, value_or_none)`.

    Нужна там, где ни один из промежуточных результатов не валиден сам по себе, а
    итог валиден: вторая модель в реестре обязана прийти вместе со слотом (при 2+
    моделях и незаданных слотах конфиг нерезолвим, спека §4) — поодиночке ни одна
    из двух записей не проходит. Одиночные функции модуля — та же машинка с одной
    операцией.

    Любая операция сломала результат → `RegistryWriteError` (файл не тронут):
    механический сбой называет свою операцию, провал итоговой валидации — состав
    транзакции и причину от валидатора."""
    ops = [tuple(op) for op in ops]
    text = _read(target)
    for op in ops:
        transform = _OPERATIONS.get(op[0])
        if transform is None:
            raise RegistryWriteError(
                f"неизвестная операция реестра: {op[0]} "
                f"(есть: {', '.join(sorted(_OPERATIONS))})"
            )
        try:
            text = transform(text, template, *op[1:])
        except (KeyError, TypeError, ValueError) as exc:   # RegistryWriteError — тоже ValueError
            raise RegistryWriteError(f"{_op_label(op)}: {exc}") from exc
    _commit(target, text, is_profile, label=" + ".join(_op_label(op) for op in ops))


def upsert_model(target, alias: str, fields: dict, template: str, *,
                 is_profile: bool = False) -> None:
    """Создать/обновить запись `models.<alias>` в `target` (merge-контракт — см.
    `_upsert_model_text`).

    `template` — текст шаблона для якоря секции (`config.default.yml` у config.yml,
    пустая строка у профиля: тогда синтезируется голый `models: {}`)."""
    apply_registry_ops(target, [("upsert_model", alias, fields)], template,
                       is_profile=is_profile)


def references_to(target, alias: str) -> list[str]:
    """Координаты слотов/привязок, ссылающихся на алиас, БЕЗ всякой записи.

    Нужна подтверждению удаления в UI: `remove_model` отчёт о ссылках отдаёт, но
    при их отсутствии сразу пишет — спросить «а что сломается?» до confirm-баннера
    им нельзя. Знание о том, где живут ссылки, остаётся здесь: UI не должен
    обходить slots/assign сам."""
    return _references(_document(_read(target)), alias)


def remove_model(target, alias: str, *, force: bool = False,
                 is_profile: bool = False) -> RemovalReport:
    """Удалить запись `models.<alias>`.

    На запись ссылаются слоты/привязки — по умолчанию НИЧЕГО не пишется, вызывающий
    получает отчёт со списком координат и решает, показывать ли подтверждение;
    `force=True` удаляет запись ВМЕСТЕ со ссылками (иначе файл остался бы
    нерезолвимым). Записи нет — отчёт «не удалено, ссылок нет», файл не тронут."""
    document = _document(_read(target))
    if alias not in (document.get("models") or {}):
        return RemovalReport(removed=False, references=[])
    references = _references(document, alias)
    if references and not force:
        return RemovalReport(removed=False, references=references)
    apply_registry_ops(target, [("remove_model", alias, True)], "", is_profile=is_profile)
    return RemovalReport(removed=True, references=references)


def write_slot(target, level: str, alias, template: str, *,
               is_profile: bool = False) -> None:
    """Задать `slots.<level>` алиасом реестра; `alias=None` — снять слот (строка
    удаляется, уровень возвращается к фоллбэку матрицы `LEVEL_FALLBACK`)."""
    apply_registry_ops(target, [("write_slot", level, alias)], template,
                       is_profile=is_profile)


def write_assign(target, consumer: str, value, template: str, *,
                 is_profile: bool = False) -> None:
    """Задать привязку потребителя (`finish_gate`, `subagents.<имя>`, wildcard `*`)
    уровнем или алиасом; `value=None` — снять привязку (потребитель возвращается
    к активному уровню)."""
    apply_registry_ops(target, [("write_assign", consumer, value)], template,
                       is_profile=is_profile)


__all__ = [
    "RegistryWriteError",
    "RemovalReport",
    "apply_registry_ops",
    "references_to",
    "upsert_model",
    "remove_model",
    "write_slot",
    "write_assign",
]
