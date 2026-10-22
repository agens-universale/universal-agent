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

Валидация судит СТЕК, который соберёт следующий старт, а не отдельный файл.
Фаза 1 (схема) — по правимому файлу: сломанный соседний слой не повод запрещать
правку этого. Фаза 2 (ссылочная целостность) — по мержу база+managed+project+
профиль по указателю: реестр кросс-слоен по устройству, и вердикт по одному
файлу врёт в обе стороны — пропускает снятие модели, на которую смотрит слот
активного профиля (файл уходит на диск с зелёным «✓», а следующий старт падает),
и отвергает рабочую раскладку «модели в базе, уровни в профиле». Для config.yml
это `config.validate_config_text_with_sections(base_path=)`; для файла профиля —
`validate_profile_text_with_sections`, и только когда указатель базы приводит
именно в него (`stack_base`): файл, которого следующий старт не грузит, остаётся
на фазе 1 (`config_modules.profiles.validate_profile_text`) — блокировать правку
неактивного профиля вреднее, чем предупредить при активации.

Кроме записи модуль отвечает на вопросы О НЕЙ, не трогая диск: `references_to` и
`references_broken_by_removal` — кто ссылается на алиас в своём и в чужих файлах стека,
`needs_default_slot` — оставит ли предполагаемая транзакция реестр без резолвимого
`default`. Живут они здесь по той же причине, по какой UI не собирает YAML: правило
резолвимости и знание о том, где лежат ссылки, второй редакции в интерфейсе не
терпят — написанные дважды, они уже разъезжались.

Форматирование YAML целиком тут и в writer'е — UI не собирает YAML никогда.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import yaml

from gc_core import config_raw
from gc_core import onboarding_config_writer as ocw
from gc_core.config_modules.layers import (
    prospective_registry,
    stack_documents,
    validate_config_text_with_sections,
    validate_profile_text_with_sections,
)
from gc_core.config_modules.model_registry import LEVELS
from gc_core.config_modules.profiles import validate_profile_text
from gc_core.config_origin import error_origins
from gc_core.utils.user_text import read_user_text


class _YamlValue(Enum):
    NULL = "null"


# None в fields исторически удаляет ключ. Явный YAML null — отдельная команда.
YAML_NULL = _YamlValue.NULL


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
_PLAIN_KEY_RE = re.compile(r"^\w[\w.+\-]*$", re.ASCII)


def _yaml_key(key: str) -> str:
    """Ключ записи в том виде, в каком он ляжет в файл.

    Мало быть синтаксически плоским: ключ, который YAML разберёт НЕ строкой
    (`123`, `true`, `null`, `0755` — локальные раздачи такие id дают), обязан
    уехать в кавычках, иначе запись валится схемой («models.123.[key]: input
    should be a valid string») в момент, когда соседние ключи уже записаны.
    Одна функция и на запись, и на поиск — обе стороны видят один вид.

    `str(key)` первой строкой — граница нормализации типа (ревью MR #80, #4):
    алиас `4.1` без кавычек в YAML разбирается `yaml.safe_load` ФЛОУТОМ, и тем
    же флоутом приходит сюда любой код, читающий его из документа (пикер
    значений слота). Без приведения `_PLAIN_KEY_RE.match` падал `TypeError`
    («expected string or bytes-like object»), а не сообщением про алиас —
    механический сбой вместо честной записи "4.1"."""
    key = str(key)
    if not _PLAIN_KEY_RE.match(key):
        return ocw.quote_scalar(key)
    try:
        plain = isinstance(yaml.safe_load(key), str)
    except yaml.YAMLError:
        plain = False
    return key if plain else ocw.quote_scalar(key)


def _read(target) -> str:
    """Текст цели. Нечитаемый файл — отказ ОПЕРАЦИИ (`RegistryWriteError`), а не
    сюрприз вызывающему: голый `OSError` уходил мимо перехвата у одних читателей,
    а `UnicodeDecodeError` (подкласс `ValueError`, не `OSError`) — мимо и того
    `(RegistryWriteError, OSError)`, которым секция ловит отказы записи, и ронял
    экран крэшем. Случай достижим: файл НЕАКТИВНОГО профиля в загрузке не
    участвует, его кодировку никто не проверял, а секция в него пишет."""
    try:
        return read_user_text(target)
    except ValueError as exc:
        raise RegistryWriteError(str(exc)) from exc


def _document(text: str) -> dict:
    """Текущий документ цели — источник СТАРОЙ записи для merge и ссылок для
    отчёта об удалении. Битый YAML отдаём той же ошибкой, что и невалидный
    результат: править запись в файле, который не парсится, нечем.

    Здесь же проверяется ФОРМА секций. Дальше по модулю к ним ходят словарными
    методами (`models.get`, `assign.items`), а файл профиля пишется руками, и
    `slots: [light]` — обычная опечатка: на активном профиле её ловит загрузчик,
    а неактивный открывается в Настройках без единой проверки. Без этого стража
    опечатка прилетала бы `AttributeError` МИМО `RegistryWriteError`, который
    секция ловит и показывает нотисом, — то есть роняла бы всё приложение."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RegistryWriteError(f"невалидный YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise RegistryWriteError("документ должен быть YAML-объектом")
    for name in ("models", "slots", "assign"):
        section = raw.get(name)
        if section is not None and not isinstance(section, dict):
            raise RegistryWriteError(
                f"секция {name} должна быть YAML-объектом"
            )
    subagents = (raw.get("assign") or {}).get("subagents")
    if subagents is not None and not isinstance(subagents, dict):
        raise RegistryWriteError(
            "секция assign.subagents должна быть YAML-объектом"
        )
    return raw


# Слой, где лежит ключ, — словами, которыми человек его знает в интерфейсе; за
# словами всегда полный путь: файлов в стеке несколько, и «в файле выше» не
# называет ни одного (ревизия владельца по живому смоку).
_LAYER_WORDS = {
    "base": "в основном конфиге",
    "managed": "в managed-файле",
    "project": "в проектном конфиге",
    "profile:project": 'в профиле "{name}"',
    "profile:base": 'в профиле "{name}"',
}


def _name_culprits(
    message: str, target, text: str, is_profile: bool, stack_base
) -> str:
    """Текст отказа валидации плюс файл(ы) стека, где лежат названные им ключи:
    «… — поправьте slots.default в профиле "work": <полный путь>».

    Вердикт вынесен по СТЕКУ, и ключ, который он называет, может лежать не в
    правимом файле: мастер пишет реестр в основной конфиг, а «slots.default:
    алиас "strong" не найден» — про активный профиль. Без файла человек читал
    «конфиг не записан: …» и не понимал, про конфиг это или про профиль.

    Виновник считается по тому стеку, что судил валидатор: цель стоит в нём ещё
    не записанным текстом (`candidates`), а не своим дисковым содержимым. По
    диску каскад, снявший ссылку в цели, показывал бы значение «не менявшимся»
    до самого профиля — и виновником выходил бы файл, из которого ссылка как
    раз уходит. Неактивный профиль без `stack_base` стека не имеет — ключи не
    ищутся; ошибка не про ключи — текст остаётся как есть: виновника не
    выдумываем."""
    if is_profile and stack_base is None:
        return message
    culprits = error_origins(
        stack_base if is_profile else target,
        message,
        candidates={target: config_raw.parse_document(text)},
    )
    if not culprits:
        return message
    advice = "; ".join(
        f"{', '.join(culprit.keys)} "
        f"{_LAYER_WORDS[culprit.kind].format(name=Path(culprit.path).stem)}: "
        f"{os.path.abspath(culprit.path)}"
        for culprit in culprits
    )
    return f"{message} — поправьте {advice}"


def _commit(target, text: str, is_profile: bool, stack_base=None) -> None:
    # Локальный импорт: `config` — агрегатор, импортирующий config_modules.*;
    # модульный импорт отсюда замкнул бы цикл.
    if is_profile and stack_base is None:
        validate = validate_profile_text
    elif is_profile:

        def validate(candidate: str) -> None:
            # `lenient_prebroken` — только здесь: реестр правит содержимое УЖЕ
            # стоящего слоя, и чужая поломка стека не должна запирать именно ту
            # секцию, в которой её чинят (layers, развилка «сломан и без нас»).
            validate_profile_text_with_sections(
                candidate,
                target=target,
                base_path=stack_base,
                lenient_prebroken=True,
            )
    else:

        def validate(candidate: str) -> None:
            validate_config_text_with_sections(
                candidate, base_path=target, lenient_prebroken=True
            )

    try:
        ocw.write_text(target, text, validate=validate)
    except RegistryWriteError:
        raise
    except ValueError as exc:
        # Отказ валидации написан для человека — как и осознанный отказ операции
        # (`_apply_ops_to_text`), он доезжает без служебных имён операций: «write_slot
        # default + upsert_model …:» впереди — служебный лог в пользовательском тексте.
        raise RegistryWriteError(
            _name_culprits(str(exc), target, text, is_profile, stack_base)
        ) from exc


def _entry_block_lines(alias_key: str, entry: dict) -> list[str]:
    """Строки блока записи реестра из УЖЕ СМЕРЖЕННОГО словаря. `yaml.safe_dump`
    (не свой построчный сборщик) — чтобы корректно сериализовать произвольные
    `pricing`/`backend`, не только плоские url/token/model."""
    if not entry:
        return [f"{alias_key}: {{}}"]
    body = yaml.safe_dump(
        entry, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    return [f"{alias_key}:"] + [f"  {line}" for line in body.splitlines()]


def _assign_coordinate(consumer: str) -> tuple[str, str]:
    """(родительский dotted-якорь, СЫРОЙ ключ) для идентификатора потребителя:
    `finish_gate` → («assign», «finish_gate»); `subagents.subagent_critic` →
    («assign.subagents», «subagent_critic»). Голый `*` — сокращение для
    `subagents.*`: в схеме `assign` wildcard существует ТОЛЬКО как ключ словаря
    субагентов («все остальные субагенты»), топ-уровневого места у него нет.

    Ключ отдаётся СЫРЫМ, а квотирование остаётся у того, кто пишет строку:
    `_drop_mapping_entry` квотирует сам, и заквотированный дважды `"\\"*\\""` не
    нашёл бы в файле ничего — то есть отказ снятия выглядел бы как «строки нет»
    у всякой привязки подряд."""
    dotted = "subagents.*" if consumer == "*" else consumer
    head, _sep, leaf = dotted.rpartition(".")
    parent = f"assign.{head}" if head else "assign"
    return parent, leaf


def _assign_present(document: dict, consumer: str) -> bool:
    """Есть ли привязка потребителя в РАЗОБРАННОМ документе — спрашивается перед
    снятием: чего нет, то снято (идемпотентный no-op), а что есть, но не снялось
    построчно, — отказ."""
    dotted = "subagents.*" if consumer == "*" else consumer
    node = document.get("assign")
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _mapping(document: dict, name: str) -> dict:
    """Секция документа словарём; негодная форма — пусто, без отказа.

    Форму секций цели сторожит `_document` и отказывает — там опечатка мешает
    писать. Сюда же приходят и ЧУЖИЕ файлы стека (`references_broken_by_removal`),
    которых мы не правим: отказать из-за соседской опечатки значило бы запретить
    правку своего файла, а `slots: [light]` в неактивном профиле не проверял никто —
    указатель резолвится из базы, и файл, который старт не грузил, попадает сюда
    как есть. Негодная секция ссылок не содержит — ровно тем же правилом, каким
    `stack_documents` молча пропускает нечитаемый файл."""
    section = document.get(name)
    return section if isinstance(section, dict) else {}


def _references(document: dict, alias: str) -> list[str]:
    """Координаты слотов и привязок, ссылающихся на алиас реестра."""
    refs: list[str] = []
    slots = _mapping(document, "slots")
    for level in LEVELS:
        if slots.get(level) == alias:
            refs.append(f"slots.{level}")
    assign = _mapping(document, "assign")
    for consumer, value in assign.items():
        if consumer == "subagents":
            for name, nested in _mapping(assign, "subagents").items():
                if nested == alias:
                    refs.append(f"assign.subagents.{name}")
        elif value == alias:
            refs.append(f"assign.{consumer}")
    return refs


def _upsert_model_text(
    text: str, template: str, alias: str, fields: dict
) -> str:
    """MERGE со старой записью, не замена: `fields` несёт только те ключи, о которых
    вызывающий что-то знает, а блок записи переписывается целиком — без наложения
    поля, которых форма не знает вовсе (`pricing`) или не трогала в этот
    раз (`token`), молча терялись бы. Правило: ключ со значением — установить,
    ключ со значением `None` — удалить, `YAML_NULL` — записать YAML null,
    отсутствующий ключ — сохранить
    как был. Старая запись читается из ТЕКУЩЕГО текста — в транзакции это уже
    результат предыдущих операций, а не состояние диска.

    Пересборка блока (reparse+reserialize) несёт две оговорки. Комментарии старой
    записи переносятся `ocw.carry_entry_comments` — шапка записи и трейлинг строк,
    оставшихся с тем же ключом и значением: данные пересборка переживают, а
    комментарии без переноса молча пропадали. Запись с YAML-якорями/merge не
    пересобирается ВООБЩЕ — отказ: `safe_dump` молча инлайнит `<<: *common` и
    роняет объявления якорей, текст остаётся валидным, а запись немедленная и
    отмены нет — предупреждать постфактум поздно."""
    old_entry = (_document(text).get("models") or {}).get(alias)
    entry = dict(old_entry) if isinstance(old_entry, dict) else {}
    for key, value in fields.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = None if value is YAML_NULL else value
    key = _yaml_key(alias)
    text = ocw.ensure_section_anchor_text(text, "models", template)
    if ocw.entry_uses_yaml_anchors(text, "models", key):
        raise RegistryWriteError(
            f"запись «{alias}» использует YAML-якоря/merge — правьте файл руками"
        )
    block = ocw.carry_entry_comments(
        text, "models", key, _entry_block_lines(key, entry)
    )
    return ocw.upsert_mapping_entry(text, "models", key, block)


def _drop_mapping_entry(text: str, parent: str, key: str, subject: str) -> str:
    """Снять запись `<parent>.<key>` из текста, УБЕДИВШИСЬ, что она снята.

    Удаление смотрит на файл двумя разными способами: ЧТО удалять, читается по
    РАЗОБРАННОМУ документу (запись реестра, ссылающиеся слоты и привязки), а
    удаляется оно по СТРОКАМ файла. Формы, у которых отдельной строки нет вовсе
    (рукописный инлайновый мэппинг `models: {flash: {…}}`, `slots: {default: …}`),
    эти взгляды разводят: `remove_mapping_entry` молча отдаёт исходный текст, а
    вызывающий рапортует успех о неслучившемся — «✓ снято» при нетронутом файле;
    у каскада хуже: запись снята, ссылка на несуществующий алиас повисла. Текст
    не изменился — это отказ.

    Проверка стоит на КАЖДОМ снятии по отдельности, а не на итоге транзакции: итог
    каскада меняется от первой же удавшейся правки, и «результат отличается от
    исходного» пропустило бы ровно те случаи, ради которых проверка заведена."""
    new_text = ocw.remove_mapping_entry(text, parent, _yaml_key(key))
    if new_text == text:
        # Отказ называет ФАКТ (строки нет), а причину — гипотезой: строки не бывает
        # у нескольких форм записи ключа, и инлайновый мэппинг лишь самая частая из
        # них. Утверждать её значило бы иногда врать и советовать неисполнимое.
        raise RegistryWriteError(
            f"строки {subject} в файле нет — правки идут построчно; "
            f"проверьте секцию {parent}: похоже, она инлайновый мэппинг"
        )
    return new_text


def _drop_entry_text(text: str, alias: str) -> str:
    """Снять запись реестра — с проверкой `_drop_mapping_entry`."""
    return _drop_mapping_entry(text, "models", alias, f"записи «{alias}»")


def _remove_model_text(
    text: str,
    _template: str,
    alias: str,
    force: bool = False,
    *,
    keep_references: bool = False,
) -> str:
    """Удалить запись реестра вместе со ссылающимися слотами/привязками. Записи нет
    — идемпотентный no-op. Ссылки есть, а `force` не задан — ошибка с их перечнем:
    у операции внутри транзакции нет канала отчёта, каким располагает `remove_model`,
    а молча оставить нерезолвимый файл нельзя.

    `force` — именно флаг: словарь-«опции» (`{"keep_references": True}`) в этой
    позиции читался бы как truthy и молча делал бы РОВНО ОБРАТНОЕ просимому —
    сносил ссылки каскадом. Всё, что в позиции флага не `bool`, — ошибка
    вызывающего."""
    if not isinstance(force, bool):
        raise TypeError(
            f"третий аргумент — флаг force (bool), получено {type(force).__name__}"
        )
    document = _document(text)
    if alias not in (document.get("models") or {}):
        return text
    references = _references(document, alias)
    if references and not force:
        raise RegistryWriteError(
            f"на запись ссылаются {', '.join(references)} — снимите ссылки в этой же "
            "транзакции или удаляйте принудительно"
        )
    # Запись снимается ПЕРВОЙ: не снялась — транзакция обрывается здесь, и ссылки
    # остаются при живой записи, а не наоборот (`_drop_entry_text`).
    text = _drop_entry_text(text, alias)
    if keep_references:
        # Алиас объявляет ещё один слой стека: после снятия ЭТОЙ записи он
        # останется, и ссылки на него продолжат резолвиться — сносить их значит
        # ломать рабочую раскладку. Знание о стеке приходит сверху: текстовая
        # операция его не добудет, а второй редакции правила заводить нельзя —
        # именно так каскад и разошёлся со справкой об отказе.
        return text
    for reference in references:
        # Каждая ссылка — с той же проверкой: снять запись и оставить ссылку на
        # неё висеть значит сделать ровно то, что этот каскад и предотвращает.
        parent, _sep, leaf = reference.rpartition(".")
        text = _drop_mapping_entry(text, parent, leaf, f"ссылки {reference}")
    return text


def _write_slot_text(text: str, template: str, level: str, alias) -> str:
    """Задать/снять `slots.<level>`. Снятие идёт через `_drop_mapping_entry` — тем
    же стражем, что и удаление записи: у рукописного `slots: {default: gc}`
    отдельной строки нет, сырое `remove_mapping_entry` вернуло бы исходный текст,
    а секция отрапортовала бы «✓ Слот «default» снят» о неслучившемся. Слота нет
    вовсе — идемпотентный no-op: пикер открывается на «не задан», и Enter без
    движения курсора штатно шлёт `None`. Прикреплённый комментарий строки слота
    переносится (`carry_entry_comments`) — тот же класс потери, что у записи
    реестра, только запись однострочная.

    `str(alias)` (не-`None` ветка) — та же граница нормализации типа, что у
    `_yaml_key` (ревью MR #80, #4): алиас `4.1` без кавычек в YAML разбирается
    ФЛОУТОМ, тем же флоутом сюда приходит выбор пикера значений слота."""
    if alias is None:
        if level not in (_document(text).get("slots") or {}):
            return text
        return _drop_mapping_entry(
            text, "slots", level, f"слота slots.{level}"
        )
    alias = str(alias)
    text = ocw.ensure_section_anchor_text(text, "slots", template)
    block = ocw.carry_entry_comments(
        text, "slots", level, [f"{level}: {ocw.quote_scalar(alias)}"]
    )
    return ocw.upsert_mapping_entry(text, "slots", level, block)


def _write_assign_text(text: str, _template: str, consumer: str, value) -> str:
    """Задать/снять привязку потребителя. Снятие — под тем же стражем, что и слот
    (`_write_slot_text`): формы без отдельной строки бывают не только инлайновые,
    но и «ключ записан другими кавычками» (`'*'` против `"*"` писателя).

    `str(value)` (не-`None` ветка) — та же граница нормализации типа, что у
    `_write_slot_text`/`_yaml_key` (ревью MR #80, #4): привязка на алиас
    `4.1` приходит тем же нестроковым флоутом, каким его разобрал YAML."""
    parent, key = _assign_coordinate(consumer)
    if value is None:
        if not _assign_present(_document(text), consumer):
            return text
        return _drop_mapping_entry(
            text, parent, key, f"привязки assign.{consumer}"
        )
    value = str(value)
    # Запись одной привязки не применяет остальные привязки шаблона.
    text = ocw.ensure_section_anchor_text(text, "assign", "")
    if parent != "assign":
        # Вложенный якорь (`assign.subagents`) — не топ-уровневый, его
        # `ensure_section_anchor_text` не заводит: заводим записью-словарём.
        nested = parent.split(".", 1)[1]
        try:
            ocw.find_anchor(text, parent)
        except KeyError:
            text = ocw.upsert_mapping_entry(
                text, "assign", nested, [f"{nested}: {{}}"]
            )
    key_yaml = _yaml_key(key)
    block = ocw.carry_entry_comments(
        text, parent, key_yaml, [f"{key_yaml}: {ocw.quote_scalar(value)}"]
    )
    return ocw.upsert_mapping_entry(text, parent, key_yaml, block)


_OPERATIONS = {
    "upsert_model": _upsert_model_text,
    "remove_model": _remove_model_text,
    "write_slot": _write_slot_text,
    "write_assign": _write_assign_text,
}


def _op_label(op: tuple) -> str:
    """Короткое имя операции для сообщения об ошибке: «write_slot light»."""
    return " ".join(str(part) for part in op[:2])


def _surviving_aliases(target, ops, stack_base) -> frozenset:
    """Алиасы снимаемых записей, которые переживут удаление (объявлены ещё одним
    слоем стека). Их ссылки каскад не трогает: они продолжат резолвиться."""
    return frozenset(
        op[1]
        for op in ops
        if op
        and op[0] == "remove_model"
        and alias_survives_removal(target, op[1], stack_base=stack_base)
    )


def _apply_ops_to_text(
    text: str, ops, template: str, survivors=frozenset()
) -> str:
    """Наложить операции на текст последовательно. Отдельно от коммита, потому что об
    ИТОГЕ спрашивают дважды: запись его пишет, а справка `needs_default_slot` только
    смотрит, что из него получилось бы."""
    for op in ops:
        transform = _OPERATIONS.get(op[0])
        if transform is None:
            raise RegistryWriteError(
                f"неизвестная операция реестра: {op[0]} (есть: {', '.join(sorted(_OPERATIONS))})"
            )
        try:
            if op[0] == "remove_model" and op[1] in survivors:
                text = transform(text, template, *op[1:], keep_references=True)
            else:
                text = transform(text, template, *op[1:])
        except RegistryWriteError:
            # Операция отказала ОСОЗНАННО и уже сказала человеку, что не так
            # («строки записи «flash» в файле нет…»). Приписывать к готовой фразе
            # своё внутреннее имя значит вести служебный лог в пользовательском
            # тексте: отказ доезжает до notice секции дословно.
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            # Механический сбой (сломанный аргумент, отсутствующий якорь) фразы для
            # человека не несёт — вот его и подписываем операцией. Нестроковый алиас
            # (YAML-ключ `4.1:` без кавычек парсится float'ом) сюда больше не
            # доезжает — граница нормализации типа (`str(...)` в `_yaml_key`/
            # `_write_slot_text`/`_write_assign_text`, ревью MR #80, #4) чинит
            # ПРИЧИНУ, а не только глушит `AttributeError` из `quote_scalar` на
            # `.replace`. Класс не сужаем: `AttributeError` остаётся живым по
            # другому пути (например, `fields` не словарь у `upsert_model` —
            # `fields.items()` падает тем же классом).
            raise RegistryWriteError(f"{_op_label(op)}: {exc}") from exc
    return text


def apply_registry_ops(
    target, ops, template: str, *, is_profile: bool = False, stack_base=None
) -> None:
    """Применить группу операций ОДНОЙ транзакцией: последовательно к тексту цели,
    валидация — одна на итоговый результат, запись — одна атомарная.

    `ops` — последовательность кортежей `(имя операции, *аргументы)` в порядке
    применения; имена и аргументы — как у одноимённых функций модуля без `target`
    и `template`: `("upsert_model", alias, fields)`, `("remove_model", alias[, force])`,
    `("write_slot", level, alias_or_none)`, `("write_assign", consumer, value_or_none)`.

    Аргументы ПОЗИЦИОННЫЕ: «словарь опций» третьим элементом кортежа лёг бы в
    позицию `force` у `remove_model` и молча включил бы каскад — такая форма
    отвергается ошибкой, а не читается как опции.

    Нужна там, где ни один из промежуточных результатов не валиден сам по себе, а
    итог валиден: вторая модель в реестре обязана прийти вместе со слотом (при 2+
    моделях и незаданных слотах конфиг нерезолвим, спека §4) — поодиночке ни одна
    из двух записей не проходит. Одиночные функции модуля — та же машинка с одной
    операцией.

    Любая операция сломала результат → `RegistryWriteError` (файл не тронут):
    `stack_base` — путь config.yml, когда цель ПРОФИЛЬ: включает фазу 2 на стеке
    (см. докстринг модуля). Для самой config.yml не нужен — там стек строится от
    цели. Не задан у профиля — валидация остаётся фазой 1, как прежде.

    Механический сбой называет свою операцию. Провал итоговой валидации — причину
    от валидатора и файл стека, где лежит названный ею ключ (`_name_culprits`):
    вердикт вынесен по стеку, и виноват может быть не правимый файл. ОСОЗНАННЫЙ
    отказ операции (она сама подняла `RegistryWriteError`) доезжает дословно.
    Ни тому, ни другому имя операции не приписывается: их тексты написаны для
    человека, и служебное имя перед ними — шум в пользовательском сообщении."""
    ops = [tuple(op) for op in ops]
    text = _apply_ops_to_text(
        _read(target),
        ops,
        template,
        _surviving_aliases(target, ops, stack_base),
    )
    _commit(target, text, is_profile, stack_base=stack_base)


def upsert_model(
    target,
    alias: str,
    fields: dict,
    template: str,
    *,
    is_profile: bool = False,
    stack_base=None,
) -> None:
    """Создать/обновить запись `models.<alias>` в `target` (merge-контракт — см.
    `_upsert_model_text`).

    `template` — текст шаблона для якоря секции (`config.default.yml` у config.yml,
    пустая строка у профиля: тогда синтезируется голый `models: {}`)."""
    apply_registry_ops(
        target,
        [("upsert_model", alias, fields)],
        template,
        is_profile=is_profile,
        stack_base=stack_base,
    )


def references_to(target, alias: str) -> list[str]:
    """Координаты слотов/привязок, ссылающихся на алиас, БЕЗ всякой записи.

    Нужна подтверждению удаления в UI: `remove_model` отчёт о ссылках отдаёт, но
    при их отсутствии сразу пишет — спросить «а что сломается?» до confirm-баннера
    им нельзя. Знание о том, где живут ссылки, остаётся здесь: UI не должен
    обходить slots/assign сам."""
    return _references(_document(_read(target)), alias)


def _other_stack_documents(target, stack_base) -> list:
    """Файлы стека, которым судится удаление из `target`, БЕЗ самого `target` —
    `(Path, dict)`.

    Цель — основной конфиг (`stack_base` не задан): стек следующего старта, с
    профилем по указателю — снятая в базе запись повиснет ровно у него. Цель —
    ПРОФИЛЬ: стек «база + этот профиль» независимо от указателя (`stack_documents(…,
    profile_override="")` — нижние слои; сам профиль и есть цель). Профиль по
    указателю в него не входит: правится файл, который старт грузит только вместе с
    нижними слоями, и что объявляет ДРУГОЙ профиль, его стеку не видно. По
    указателю оба вопроса врали для неактивного профиля: запись, которую объявлял
    ещё и активный, «переживала» удаление — каскад оставлял свой слот висеть, а
    отказ молчал о ссылке базы, которую резолвил только этот файл; валидатор до
    неактивного профиля не доходит, и всплывало это лишь при его активации.
    Тот же стек, каким `prospective_registry` отвечает на вопрос о слоте, — иначе
    каскад внутри справки и её итог считались бы по разным стекам."""
    if stack_base is None:
        docs = stack_documents(target)
    else:
        docs = stack_documents(stack_base, profile_override="")
    target_path = Path(target).resolve()
    return [
        (path, document)
        for path, document in docs
        if Path(path).resolve() != target_path
    ]


def alias_survives_removal(target, alias: str, *, stack_base=None) -> bool:
    """Останется ли алиас в стеке после снятия записи из `target` — его объявляет
    ещё один слой (какой стек — `_other_stack_documents`).

    Один предикат на два вопроса: отказ удаления спрашивает «повиснут ли чужие
    ссылки», каскад — «сносить ли свои». Обе редакции правила уже расходились:
    справка знала про переживший алиас, а каскад считал по совпадению имени в
    своём файле и сносил ссылку, которая продолжала резолвиться."""
    for _path, document in _other_stack_documents(target, stack_base):
        models = document.get("models")
        if isinstance(models, dict) and alias in models:
            return True
    return False


def references_broken_by_removal(
    target, alias: str, *, stack_base=None
) -> list:
    """Ссылки в ЧУЖИХ файлах стека, которые повиснут, если снять запись из `target`, —
    `(имя файла, координата)`. Свой файл сюда не попадает, его отдаёт `references_to`.

    Инвариант именно такой, а НЕ «на алиас смотрит другой файл»: алиас может
    объявлять и другой слой. Профиль, перекрывший запись основного конфига тем же
    именем, снимается свободно — запись основного остаётся на своём месте, и слот,
    который на неё смотрит, продолжает резолвиться. Отказ по одному лишь факту чужой
    ссылки запрещал бы возможное.

    Нужна отказу удаления: если ссылка ПОВИСНЕТ, снять запись нельзя — транзакции
    через файлы у слоя записи нет, — и спросить «точно удалить?» значило бы пообещать
    то, чего не случится ни при каком ответе. То же действующее правило, что и для
    унаследованной записи, распространённое на другой слой."""
    if alias_survives_removal(target, alias, stack_base=stack_base):
        return []  # алиас переживёт удаление — ничьи ссылки не повиснут
    refs: list = []
    for path, document in _other_stack_documents(target, stack_base):
        refs += [
            (Path(path).name, coordinate)
            for coordinate in _references(document, alias)
        ]
    return refs


def needs_default_slot(
    target, ops, template: str, *, is_profile: bool = False, stack_base=None
) -> bool:
    """Оставит ли ЭТА транзакция стек без резолвимого `default` — справка для UI,
    задаваемая ДО записи, чтобы форма успела спросить «какая модель по умолчанию» и
    уйти ОДНОЙ транзакцией.

    Спрашивается здесь, а не считается в секции: правило одно
    (`model_sections_validation.default_slot_required`), и вторая его редакция в
    интерфейсе уже разъезжалась с валидатором — считала по своему файлу плюс тень, а
    тень есть только у под-вида профиля.

    `stack_base` — как у `apply_registry_ops`. Не наложилось / реестр не собрался —
    `False`: справка не имеет права ронять форму, а невозможность записи всё равно
    поймает гейт своим отказом."""
    from gc_core.config_modules.model_sections_validation import (
        default_slot_required,
    )

    try:
        prepared = [tuple(op) for op in ops]
        text = _apply_ops_to_text(
            _read(target),
            prepared,
            template,
            _surviving_aliases(target, prepared, stack_base),
        )
    except (RegistryWriteError, OSError):
        return False
    view = prospective_registry(
        text,
        base_path=stack_base if is_profile else target,
        is_profile=is_profile,
    )
    if view is None:
        return False
    return default_slot_required(*view)


def remove_model(
    target,
    alias: str,
    *,
    force: bool = False,
    is_profile: bool = False,
    stack_base=None,
) -> RemovalReport:
    """Удалить запись `models.<alias>`.

    На запись ссылаются слоты/привязки — по умолчанию НИЧЕГО не пишется, вызывающий
    получает отчёт со списком координат и решает, показывать ли подтверждение;
    `force=True` удаляет запись ВМЕСТЕ со ссылками (иначе файл остался бы
    нерезолвимым). Записи нет — отчёт «не удалено, ссылок нет», файл не тронут.

    Отчёт `removed=True` — про СОСТОЯВШЕЕСЯ удаление: запись, которую не удалось
    снять текстом (`_drop_entry_text`), приходит `RegistryWriteError`, а не тихим
    отчётом об успехе."""
    document = _document(_read(target))
    if alias not in (document.get("models") or {}):
        return RemovalReport(removed=False, references=[])
    references = _references(document, alias)
    if references and not force:
        return RemovalReport(removed=False, references=references)
    apply_registry_ops(
        target,
        [("remove_model", alias, True)],
        "",
        is_profile=is_profile,
        stack_base=stack_base,
    )
    return RemovalReport(removed=True, references=references)


def write_slot(
    target,
    level: str,
    alias,
    template: str,
    *,
    is_profile: bool = False,
    stack_base=None,
) -> None:
    """Задать `slots.<level>` алиасом реестра; `alias=None` — снять слот (строка
    удаляется, уровень возвращается к фоллбэку матрицы `LEVEL_FALLBACK`)."""
    apply_registry_ops(
        target,
        [("write_slot", level, alias)],
        template,
        is_profile=is_profile,
        stack_base=stack_base,
    )


def write_assign(
    target,
    consumer: str,
    value,
    template: str,
    *,
    is_profile: bool = False,
    stack_base=None,
) -> None:
    """Задать привязку потребителя (`finish_gate`, `subagents.<имя>`, wildcard `*`)
    уровнем или алиасом; `value=None` — снять привязку (потребитель возвращается
    к активному уровню)."""
    apply_registry_ops(
        target,
        [("write_assign", consumer, value)],
        template,
        is_profile=is_profile,
        stack_base=stack_base,
    )


__all__ = [
    "YAML_NULL",
    "RegistryWriteError",
    "RemovalReport",
    "apply_registry_ops",
    "needs_default_slot",
    "references_broken_by_removal",
    "references_to",
    "remove_model",
    "upsert_model",
    "write_assign",
    "write_slot",
]
