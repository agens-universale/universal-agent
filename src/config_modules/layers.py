"""Наложение слоёв конфига и pre-write валидация по стеку.

Слоёв три оси (спека §3): machine-managed overlay → project-local → профиль по
указателю. Модуль отвечает и за то, ЧТО получится после наложения (`_merge_layers`,
`stack_documents`), и за вопрос «переживёт ли эту запись следующий старт»
(`validate_*_with_sections`, `prospective_registry`).

Дом — здесь, а не в агрегаторе `config`: слой записи реестра
(`config_modules.registry_write`) обязан спрашивать правило перед каждой записью, а
импорт агрегатора замыкал бы цикл — и держался этот цикл только на отложенных
импортах внутри функций, то есть до первой естественной правки.
"""
import os
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional

import yaml

from config_modules.compat import apply_compat
from config_modules.env_interpolation import resolve_env_placeholders
from config_modules import env_vars
from config_modules.merge import deep_merge
from config_modules.model_registry import validate_registry_pairs
from config_modules.model_sections_validation import validate_model_sections
from config_modules.profiles import (
    ProfileRef,
    load_profile_document,
    profile_roots,
    resolve_profile,
    validate_profile_text,
)
from config_modules.root import AppConfig
from mcp_runtime import paths as mcp_paths

# `None` у пути слоя — законное значение («слоя нет»), поэтому «не передано»
# отмечается отдельным маркером: иначе подмена, вернувшая None, читалась бы как
# «спроси владельца сам» — ровно та ошибка, из-за которой глушение слоёв в тестах
# не действовало.
_UNSET = object()


class _Layers(NamedTuple):
    """Итог наложения слоёв поверх базы: сам мерж плюс координаты слоёв — их
    просят и `ProfileMeta`, и снимок свежести."""

    config: Dict[str, Any]
    ref: Optional[ProfileRef]
    active_name: Optional[str]
    roots: list[Path]
    managed_path: Path
    project_path: Optional[str]
    # Сырьё managed-слоя и снимок `voice` ДО его наложения: по ним считается вердикт
    # «батарейка перекрыла ваш voice». Оба — такой же факт наложения слоёв, как
    # координаты файлов, и заново их не добыть: managed пришлось бы перечитывать с
    # диска, а base-voice к этому моменту уже перезаписан мержем.
    managed_raw: Dict[str, Any]
    base_voice: Dict[str, Any]


def _layer_document(path, subject: str) -> Dict[str, Any]:
    """Сырой документ файла-слоя (до env-интерполяции — её вызывающий делает сам,
    ему нужен и документ ДО неё).

    Негодный файл отдаётся `ValueError` — тем же классом, каким `_raw_from_text`
    отвечает за текст. Контракт общий на всё чтение слоёв: слои читает и загрузчик,
    и pre-write валидация, а она ловит `(OSError, ValueError)` и откатывается к
    одному файлу. Голый `yaml.safe_load` возвращал сюда `yaml.YAMLError` на
    неразбираемом файле и `dict`-подобное ничто на «- a\\n- b» — обе ошибки шли
    мимо отката и роняли /settings прямо на рендере меню. Не-модельные секции
    правятся руками, так что негодный слой — рабочий сценарий, а не экзотика."""
    with open(path, "r", encoding="utf-8") as file:
        try:
            raw = yaml.safe_load(file) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"{path}: невалидный YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: {subject} должен быть YAML-объектом")
    return raw


def _merge_layers(
    raw_config: Dict[str, Any],
    path: str,
    *,
    profile_override: Optional[str] = None,
    profile_doc: Optional[Dict[str, Any]] = None,
    profile_doc_path: Optional[Path] = None,
    notices: Optional[list[str]] = None,
    managed_path=_UNSET,
    project_path=_UNSET,
) -> _Layers:
    """Наложить на базу слои, которые накладывает загрузка: managed → project →
    профиль по указателю (оси 1-2 спеки §3). База приходит уже прочитанной и
    интерполированной: этот шаг у вызывающих разный.

    Вынесено из `load_config_ex` ради второго вызывающего — pre-write валидации
    секций. Реестр кросс-слоен ПО УСТРОЙСТВУ (слот профиля вправе ссылаться на
    модель базы), поэтому вердикт по одному файлу врёт в обе стороны: пропускает
    merged, который не загрузится, и отвергает рабочую раскладку «модели в базе,
    уровни в профиле». Судить надо ТОТ стек, который соберёт следующий старт, —
    один и тот же код на обе роли.

    `profile_doc` — готовый документ вместо чтения профиля с диска; подставляется,
    только если резолв указателя привёл ровно в `profile_doc_path`. Так валидатор
    ставит ещё не записанный текст профиля на его место в стеке, не рискуя
    подсунуть его вместо чужого файла-тёзки из другого корня."""
    base_path = Path(path)
    notices = notices if notices is not None else []

    # Сохранить base-voice для дефекта перекрытия (до мержа managed). Тип НЕ приводим:
    # секция ещё не проходила схему, и `dict()` над «voice: строка» падал бы технической
    # ошибкой про sequence element вместо внятного вердикта валидатора (находка ревью).
    _raw_voice = raw_config.get("voice")
    _base_voice = dict(_raw_voice) if isinstance(_raw_voice, dict) else {}

    # Machine-managed overlay (gc-mcp install/remove): ~/.gc/config.local.yml.
    # Мёржится поверх base, под project-local. mcp.servers аккумулируются.
    # Пути слоёв приходят ОТ ВЫЗЫВАЮЩЕГО, если он их знает: загрузчик добывает их
    # своими именами, и подмена этих имён (тесты глушат слои машины разработчика)
    # обязана действовать. Спрашивать владельца самим значило бы читать слой мимо
    # подмены — сьют тогда проверяет не то состояние, которое считает своим.
    if managed_path is _UNSET:
        managed_path = mcp_paths.managed_config_path()
    managed_raw: Dict[str, Any] = {}
    if (managed_path is not None and managed_path.is_file()
            and os.path.abspath(str(managed_path)) != os.path.abspath(path)):
        managed_raw = _layer_document(managed_path, "config.local.yml")
        managed_before_env = managed_raw
        managed_raw = resolve_env_placeholders(managed_raw)
        validate_registry_pairs(managed_raw, source=str(managed_path),
                                raw_source=managed_before_env)
        raw_config = deep_merge(raw_config, managed_raw)

    # Layered loading: project <cwd>/.gc/config.yml поверх глобального.
    if project_path is _UNSET:
        project_path = env_vars.project_local_config_path()
    if project_path is not None and os.path.abspath(project_path) != os.path.abspath(path):
        project_raw = _layer_document(project_path, "проектный config.yml")
        project_before_env = project_raw
        project_raw = resolve_env_placeholders(project_raw)
        validate_registry_pairs(project_raw, source=project_path,
                                raw_source=project_before_env)
        raw_config = deep_merge(raw_config, project_raw)

    # Профиль (спека §3): указатель резолвится из пре-профильного мержа выше (ось 1,
    # анти-рекурсия — профиль не может выбрать профиль), либо из явного override
    # (CLI --profile / сессия). level: НЕ резолвится тут — обычный ключ полного мержа.
    roots = profile_roots(Path(os.getcwd()), base_path)
    pointer = (
        profile_override
        if profile_override is not None
        else str(raw_config.get("profile") or "").strip()
    )
    ref: Optional[ProfileRef] = None
    active_name: Optional[str] = pointer or None
    if pointer:
        ref = resolve_profile(pointer, roots, notices=notices)
        if profile_doc is not None and profile_doc_path is not None and (
            Path(ref.path).resolve() == Path(profile_doc_path).resolve()
        ):
            profile_raw = profile_doc
        else:
            profile_raw = load_profile_document(
                ref.path,
                warn_literal_tokens=(ref.root_kind == "project"),
                notices=notices,
            )
        # Контент профиля мержится ПОВЕРХ base/managed/project (ось 2).
        raw_config = deep_merge(raw_config, profile_raw)

    return _Layers(raw_config, ref, active_name, roots, managed_path, project_path,
                   managed_raw, _base_voice)


def _raw_from_text(text: str, subject: str = "config.yml") -> Dict[str, Any]:
    """Текст → документ → ${ENV:VAR}. Отдельный шаг, потому что стековая фаза 2
    накладывает слои на СЫРОЙ документ и зовёт compat один раз на итог мержа —
    ровно как загрузчик; склеенный с ним `_app_config_from_text` compat применил
    бы к слою поодиночке."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"невалидный YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{subject} должен быть YAML-объектом")
    return resolve_env_placeholders(raw)


def _app_config_from_raw(raw: Dict[str, Any]) -> AppConfig:
    """compat → схема над уже прочитанным документом."""
    apply_compat(raw)
    try:
        return AppConfig.model_validate(raw)
    except Exception as exc:
        raise ValueError(f"конфиг не проходит схему: {exc}") from exc


def _app_config_from_text(text: str) -> AppConfig:
    """Общий шаг фазы 1 (${ENV:VAR} → compat → схема) для validate_config_text и
    validate_config_text_with_sections — не дублировать парсинг/интерполяцию/
    compat на каждый вызов, отдаёт готовый AppConfig для фазы 2 второй функции."""
    return _app_config_from_raw(_raw_from_text(text))


def validate_config_text(text: str) -> None:
    """Контрольная валидация текста config.yml перед записью (writer/sync).

    Конвейер тот же, что в load_config_ex: ${ENV:VAR} → compat → схема. Иначе файл со
    старым (переименованным) ключом или с плейсхолдером в нестроковом поле проходил бы
    загрузку, но отвергался при записи — и юзер оказывался заблокирован в /settings.
    Бросает ValueError с человекочитаемой причиной.
    """
    _app_config_from_text(text)


def _stacked_app_config(text: str, *, target=None, base_path, is_profile: bool = False):
    """Merged AppConfig стека, который соберёт СЛЕДУЮЩИЙ СТАРТ, — или None, если цель
    в этот стек не входит либо он не собрался.

    Один путь сборки на обе pre-write валидации: второй раз выписанный мерж
    разъехался бы с первым на первой же правке загрузчика."""
    try:
        if not is_profile:
            return _app_config_from_raw(
                _merge_layers(_raw_from_text(text), str(base_path)).config)
        base_raw = _raw_from_text(
            Path(base_path).read_text(encoding="utf-8"), "config.yml")
        layers = _merge_layers(
            base_raw, str(base_path),
            profile_doc=_raw_from_text(text, "профиль"),
            profile_doc_path=Path(target))
        if layers.ref is None or (
            Path(layers.ref.path).resolve() != Path(target).resolve()
        ):
            return None
        return _app_config_from_raw(layers.config)
    except (OSError, ValueError):
        return None


def prospective_registry(text: str, *, target=None, base_path=None,
                         is_profile: bool = False):
    """`(models, slots)` реестра, каким он СТАНЕТ, если этот файл пойдёт в дело;
    None — сам текст не разбирается.

    Намеренно НЕ то же, что `_stacked_app_config`: для файла профиля считает
    «база + ЭТОТ файл» независимо от указателя. Гейт по неактивному профилю бить не
    должен, а вопрос «какая модель по умолчанию» задать в нём полезно — собрать
    нерезолвимое состояние человек может сейчас, а активировать позже. Вопрос
    советует, гейт запрещает: полномочия разные, значит и стек разный.

    Стек не собрался — откат к одному файлу: спросить по своему файлу лучше, чем не
    спросить вовсе."""
    subject = "профиль" if is_profile else "config.yml"
    try:
        _raw_from_text(text, subject)
    except ValueError:
        return None
    if base_path is not None:
        try:
            if not is_profile:
                merged = _merge_layers(_raw_from_text(text), str(base_path)).config
            else:
                base_raw = _raw_from_text(
                    Path(base_path).read_text(encoding="utf-8"), "config.yml")
                # profile_override="" — пустой указатель: слой профиля из файлов НЕ
                # берётся, его место занимает наш текст, чьим бы файлом он ни был.
                layers = _merge_layers(base_raw, str(base_path), profile_override="")
                merged = deep_merge(layers.config, _raw_from_text(text, subject))
            cfg = _app_config_from_raw(merged)
            return cfg.models, cfg.slots
        except (OSError, ValueError):
            pass
    try:
        cfg = _app_config_from_raw(_raw_from_text(text, subject))
    except ValueError:
        return None
    return cfg.models, cfg.slots


def stack_documents(base_path) -> list:
    """Файлы стека следующего старта и их СЫРЫЕ документы — `(Path, dict)`, в порядке
    наложения. Нечитаемое и не-словари молча пропускаются: справке важно найденное, а
    не полнота обхода.

    Нужна тому, кому мало слитого результата и важно, В КАКОМ файле лежит найденное:
    отказ удаления обязан назвать файл, иначе человеку некуда идти."""
    def _doc(path):
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return None
        return raw if isinstance(raw, dict) else None

    base = Path(base_path)
    base_raw = _doc(base)
    if base_raw is None:
        return []
    docs = [(base, base_raw)]
    for path in (mcp_paths.managed_config_path(), env_vars.project_local_config_path()):
        if not path or os.path.abspath(str(path)) == os.path.abspath(str(base)):
            continue
        raw = _doc(path)
        if raw is not None:
            docs.append((Path(path), raw))
    pointer = str(base_raw.get("profile") or "").strip()
    if pointer:
        try:
            ref = resolve_profile(pointer, profile_roots(Path(os.getcwd()), base))
        except ValueError:
            return docs
        raw = _doc(ref.path)
        if raw is not None:
            docs.append((Path(ref.path), raw))
    return docs


def validate_config_text_with_sections(text: str, *, base_path=None) -> None:
    """Как validate_config_text (фаза 1), плюс фаза 2 — ссылочная целостность и
    правило резолвимости секций models/slots/assign (config_modules.
    model_sections_validation.validate_model_sections).

    Нужна там, где запись меняет реестр/слоты/привязки (слой записи
    `config_modules/registry_write.py`): иначе честный, но нерезолвимый после
    записи config.yml (например, снят единственный заданный слот при 2+ моделях
    в реестре) ушёл бы на диск, а следующий запуск gc-cli/gc-ua падал бы
    необработанным ValueError при перечитке. Точечные записи плоских полей
    (`_do_save` и т.п.) slots/assign не трогают — им остаётся обычный
    validate_config_text. Бросает ValueError с текстом ошибки валидатора
    (координата ключа).

    `base_path` (путь файла, которым текст вот-вот станет) переводит ФАЗУ 2 на
    стек, который соберёт следующий старт: managed → project → профиль по
    указателю. Без него вердикт по одному файлу врёт в обе стороны — пропускает
    удаление модели, на которую смотрит слот активного профиля (файл пишется с
    зелёным «✓», а следующий старт падает), и отвергает рабочую раскладку
    «модели в базе, уровни в профиле». ФАЗА 1 остаётся на самом файле: сломанный
    соседний слой — не повод запрещать правку этого, а сообщение о чужом файле
    человеку в форме бесполезно. Стек не собрался (нет профиля по указателю,
    нечитаемый слой) — фаза 2 откатывается к однофайловой: хуже, чем было, не
    станет."""
    app_cfg = _app_config_from_text(text)
    if base_path is not None:
        stacked = _stacked_app_config(text, base_path=base_path)
        if stacked is not None:
            app_cfg = stacked
    validate_model_sections(app_cfg)


def validate_profile_text_with_sections(text: str, *, target, base_path) -> None:
    """Фаза 1 профиля (`profiles.validate_profile_text`) плюс фаза 2 на стеке, где
    ЭТОТ текст стоит слоем профиля.

    Профиль частичен, и его собственный файл фазе 2 не подлежит — слот профиля
    законно ссылается на модель нижнего слоя. Но пока профиль АКТИВЕН, его файл —
    часть того самого стека, который соберёт следующий старт: снятое в нём
    значение способно оставить merged нерезолвимым ровно так же, как правка базы.
    Поэтому фаза 2 включается только когда указатель базы приводит в `target`;
    неактивный профиль (и любой случай, когда стек не собрался) остаётся на фазе
    1 — блокировать правку файла, которого никто не грузит, вреднее, чем
    предупредить при активации."""
    validate_profile_text(text)
    app_cfg = _stacked_app_config(text, target=target, base_path=base_path,
                                  is_profile=True)
    if app_cfg is not None:
        validate_model_sections(app_cfg)
