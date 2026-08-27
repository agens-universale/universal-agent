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
            raise RegistryWriteError(f"секция {name} должна быть YAML-объектом")
    subagents = (raw.get("assign") or {}).get("subagents")
    if subagents is not None and not isinstance(subagents, dict):
        raise RegistryWriteError("секция assign.subagents должна быть YAML-объектом")
    return raw


def _commit(target, text: str, is_profile: bool, label: str, stack_base=None) -> None:
    # Локальный импорт: `config` — агрегатор, импортирующий config_modules.*;
    # модульный импорт отсюда замкнул бы цикл.
    if is_profile and stack_base is None:
        validate = validate_profile_text
    elif is_profile:
        from config import validate_profile_text_with_sections

        def validate(candidate: str) -> None:
            validate_profile_text_with_sections(
                candidate, target=target, base_path=stack_base)
    else:
        from config import validate_config_text_with_sections

        def validate(candidate: str) -> None:
            validate_config_text_with_sections(candidate, base_path=target)
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


def _remove_model_text(text: str, _template: str, alias: str, force: bool = False) -> str:
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
    движения курсора штатно шлёт `None`."""
    if alias is None:
        if level not in (_document(text).get("slots") or {}):
            return text
        return _drop_mapping_entry(text, "slots", level, f"слота slots.{level}")
    text = ocw.ensure_section_anchor_text(text, "slots", template)
    return ocw.upsert_mapping_entry(
        text, "slots", level, [f"{level}: {ocw.quote_scalar(alias)}"])


def _write_assign_text(text: str, template: str, consumer: str, value) -> str:
    """Задать/снять привязку потребителя. Снятие — под тем же стражем, что и слот
    (`_write_slot_text`): формы без отдельной строки бывают не только инлайновые,
    но и «ключ записан другими кавычками» (`'*'` против `"*"` писателя)."""
    parent, key = _assign_coordinate(consumer)
    if value is None:
        if not _assign_present(_document(text), consumer):
            return text
        return _drop_mapping_entry(text, parent, key, f"привязки assign.{consumer}")
    text = ocw.ensure_section_anchor_text(text, "assign", template)
    if parent != "assign":
        # Вложенный якорь (`assign.subagents`) — не топ-уровневый, его
        # `ensure_section_anchor_text` не заводит: заводим записью-словарём.
        nested = parent.split(".", 1)[1]
        try:
            ocw.find_anchor(text, parent)
        except KeyError:
            text = ocw.upsert_mapping_entry(text, "assign", nested, [f"{nested}: {{}}"])
    key_yaml = _yaml_key(key)
    return ocw.upsert_mapping_entry(
        text, parent, key_yaml, [f"{key_yaml}: {ocw.quote_scalar(value)}"])


_OPERATIONS = {
    "upsert_model": _upsert_model_text,
    "remove_model": _remove_model_text,
    "write_slot": _write_slot_text,
    "write_assign": _write_assign_text,
}


def _op_label(op: tuple) -> str:
    """Короткое имя операции для сообщения об ошибке: «write_slot light»."""
    return " ".join(str(part) for part in op[:2])


def _apply_ops_to_text(text: str, ops, template: str) -> str:
    """Наложить операции на текст последовательно. Отдельно от коммита, потому что об
    ИТОГЕ спрашивают дважды: запись его пишет, а справка `needs_default_slot` только
    смотрит, что из него получилось бы."""
    for op in ops:
        transform = _OPERATIONS.get(op[0])
        if transform is None:
            raise RegistryWriteError(
                f"неизвестная операция реестра: {op[0]} "
                f"(есть: {', '.join(sorted(_OPERATIONS))})"
            )
        try:
            text = transform(text, template, *op[1:])
        except RegistryWriteError:
            # Операция отказала ОСОЗНАННО и уже сказала человеку, что не так
            # («строки записи «flash» в файле нет…»). Приписывать к готовой фразе
            # своё внутреннее имя значит вести служебный лог в пользовательском
            # тексте: отказ доезжает до notice секции дословно.
            raise
        except (KeyError, TypeError, ValueError) as exc:
            # Механический сбой (сломанный аргумент, отсутствующий якорь) фразы для
            # человека не несёт — вот его и подписываем операцией.
            raise RegistryWriteError(f"{_op_label(op)}: {exc}") from exc
    return text


def apply_registry_ops(target, ops, template: str, *, is_profile: bool = False,
                       stack_base=None) -> None:
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

    механический сбой называет свою операцию, провал итоговой валидации — состав
    транзакции и причину от валидатора. ОСОЗНАННЫЙ отказ операции (она сама
    подняла `RegistryWriteError`) доезжает дословно: его текст написан для
    человека, и служебное имя операции перед ним — шум в пользовательском
    сообщении."""
    ops = [tuple(op) for op in ops]
    text = _apply_ops_to_text(_read(target), ops, template)
    _commit(target, text, is_profile, label=" + ".join(_op_label(op) for op in ops),
            stack_base=stack_base)


def upsert_model(target, alias: str, fields: dict, template: str, *,
                 is_profile: bool = False, stack_base=None) -> None:
    """Создать/обновить запись `models.<alias>` в `target` (merge-контракт — см.
    `_upsert_model_text`).

    `template` — текст шаблона для якоря секции (`config.default.yml` у config.yml,
    пустая строка у профиля: тогда синтезируется голый `models: {}`)."""
    apply_registry_ops(target, [("upsert_model", alias, fields)], template,
                       is_profile=is_profile, stack_base=stack_base)


def references_to(target, alias: str) -> list[str]:
    """Координаты слотов/привязок, ссылающихся на алиас, БЕЗ всякой записи.

    Нужна подтверждению удаления в UI: `remove_model` отчёт о ссылках отдаёт, но
    при их отсутствии сразу пишет — спросить «а что сломается?» до confirm-баннера
    им нельзя. Знание о том, где живут ссылки, остаётся здесь: UI не должен
    обходить slots/assign сам."""
    return _references(_document(_read(target)), alias)


def references_broken_by_removal(target, alias: str, *, stack_base=None) -> list:
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
    from config import stack_documents
    target_path = Path(target).resolve()
    others = [(path, document)
              for path, document in stack_documents(stack_base or target)
              if Path(path).resolve() != target_path]
    for _path, document in others:
        models = document.get("models")
        if isinstance(models, dict) and alias in models:
            return []           # алиас переживёт удаление — ничьи ссылки не повиснут
    refs: list = []
    for path, document in others:
        refs += [(Path(path).name, coordinate)
                 for coordinate in _references(document, alias)]
    return refs


def needs_default_slot(target, ops, template: str, *, is_profile: bool = False,
                       stack_base=None) -> bool:
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
    from config import prospective_registry
    from config_modules.model_sections_validation import default_slot_required
    try:
        text = _apply_ops_to_text(_read(target), [tuple(op) for op in ops], template)
    except (RegistryWriteError, OSError):
        return False
    view = prospective_registry(
        text,
        target=target,
        base_path=stack_base if is_profile else target,
        is_profile=is_profile,
    )
    if view is None:
        return False
    return default_slot_required(*view)


def remove_model(target, alias: str, *, force: bool = False,
                 is_profile: bool = False, stack_base=None) -> RemovalReport:
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
    apply_registry_ops(target, [("remove_model", alias, True)], "",
                       is_profile=is_profile, stack_base=stack_base)
    return RemovalReport(removed=True, references=references)


def write_slot(target, level: str, alias, template: str, *,
               is_profile: bool = False, stack_base=None) -> None:
    """Задать `slots.<level>` алиасом реестра; `alias=None` — снять слот (строка
    удаляется, уровень возвращается к фоллбэку матрицы `LEVEL_FALLBACK`)."""
    apply_registry_ops(target, [("write_slot", level, alias)], template,
                       is_profile=is_profile, stack_base=stack_base)


def write_assign(target, consumer: str, value, template: str, *,
                 is_profile: bool = False, stack_base=None) -> None:
    """Задать привязку потребителя (`finish_gate`, `subagents.<имя>`, wildcard `*`)
    уровнем или алиасом; `value=None` — снять привязку (потребитель возвращается
    к активному уровню)."""
    apply_registry_ops(target, [("write_assign", consumer, value)], template,
                       is_profile=is_profile, stack_base=stack_base)


__all__ = [
    "RegistryWriteError",
    "RemovalReport",
    "apply_registry_ops",
    "references_to",
    "needs_default_slot",
    "references_broken_by_removal",
    "upsert_model",
    "remove_model",
    "write_slot",
    "write_assign",
]
