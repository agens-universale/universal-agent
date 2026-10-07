"""Сторожи слоя записи: отказ вместо тихого успеха и вместо крэша.

Два класса, которые модуль уже объявил дефектом для удаления записи
(`_drop_mapping_entry`: «текст не изменился — это отказ»), но не довёл до
остальных путей: снятие слота/привязки и секция, оказавшаяся не словарём.
"""

from pathlib import Path

import pytest
import yaml
from gc_core.config_modules import registry_write as rw
from gc_core.config_modules.registry_write import RegistryWriteError


def _target(tmp_path: Path, text: str, name: str = "config.yml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- Снятие слота/привязки: «✓ снято» обязано означать снятое ---


def test_unsetting_a_slot_in_an_inline_mapping_is_refused(tmp_path):
    """Рукописный `slots: {default: gc}` отдельной строки слота не имеет:
    построчное удаление молча вернуло бы исходный текст, а секция отрапортовала
    бы «✓ Слот «default» снят» о неслучившемся."""
    text = 'models:\n  gc:\n    model: "x"\nslots: {default: gc}\n'
    target = _target(tmp_path, text)

    with pytest.raises(RegistryWriteError, match="slots"):
        rw.write_slot(target, "default", None, "")
    assert target.read_text(encoding="utf-8") == text


def test_unsetting_a_slot_absent_from_the_file_is_a_no_op(tmp_path):
    """Слота и так нет — идемпотентный успех, а не отказ: пикер открывается на
    «не задан», и Enter без движения курсора шлёт `None` штатно."""
    text = 'models:\n  gc:\n    model: "x"\nslots:\n  light: "gc"\n'
    target = _target(tmp_path, text)

    rw.write_slot(target, "default", None, "")
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["slots"] == {
        "light": "gc"
    }


def test_unsetting_a_slot_written_as_a_line_still_works(tmp_path):
    text = 'models:\n  gc:\n    model: "x"\nslots:\n  default: "gc"\n'
    target = _target(tmp_path, text)

    rw.write_slot(target, "default", None, "")
    assert (
        yaml.safe_load(target.read_text(encoding="utf-8")).get("slots") or {}
    ) == {}


def test_unsetting_an_assign_quoted_differently_still_finds_it(tmp_path):
    """Ключ wildcard написан одинарными кавычками, писатель пишет свои двойные —
    и всё же строка обязана найтись: ключи сравниваются как YAML-скаляры, а не
    побайтово. Иначе снятие «удавалось» бы на нетронутом файле."""
    text = "models:\n  gc:\n    model: \"x\"\nassign:\n  subagents:\n    '*': gc\n"
    target = _target(tmp_path, text)

    rw.write_assign(target, "*", None, "")
    assert (
        yaml.safe_load(target.read_text(encoding="utf-8"))["assign"].get(
            "subagents"
        )
        or {}
    ) == {}


def test_unsetting_an_assign_in_an_inline_mapping_is_refused(tmp_path):
    """Форма, у которой строки нет ВООБЩЕ: рукописный инлайновый мэппинг. Здесь
    построчное снятие невозможно, и отказ обязан быть громким."""
    text = 'models:\n  gc:\n    model: "x"\nassign: {finish_gate: gc}\n'
    target = _target(tmp_path, text)

    with pytest.raises(RegistryWriteError, match="assign"):
        rw.write_assign(target, "finish_gate", None, "")
    assert target.read_text(encoding="utf-8") == text


def test_unsetting_an_assign_absent_from_the_file_is_a_no_op(tmp_path):
    text = 'models:\n  gc:\n    model: "x"\nassign:\n  finish_gate: "gc"\n'
    target = _target(tmp_path, text)

    rw.write_assign(target, "summary", None, "")
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["assign"] == {
        "finish_gate": "gc"
    }


# --- Многострочные значения соседних полей переживают точечную правку ---


def test_editing_one_field_keeps_a_multiline_value_of_a_sibling_field_intact(
    tmp_path,
):
    """`yaml.safe_dump` кодирует перенос строки внутри скаляра пустой строкой-
    продолжением (`'\\n\\n'` → две пустые строки блока) — построчный фильтр
    «непустых» строк принимал их за мусор и склеивал соседние переносы:
    `token: "\\n\\nUser:"` → `" User:"`. Правка ЧУЖОГО поля (`model`) не должна
    портить значение, которого вообще не касалась."""
    text = 'models:\n  gc:\n    model: "old-model"\n    url: "http://host/v1"\n    token: "\\n\\nUser:"\n'
    target = _target(tmp_path, text)

    rw.upsert_model(target, "gc", {"model": "new-model"}, "")

    doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert doc["models"]["gc"]["model"] == "new-model"
    assert doc["models"]["gc"]["token"] == "\n\nUser:"


def test_unsetting_a_nested_assign_written_as_a_line_still_works(tmp_path):
    text = 'models:\n  gc:\n    model: "x"\nassign:\n  subagents:\n    "*": "gc"\n'
    target = _target(tmp_path, text)

    rw.write_assign(target, "*", None, "")
    assert (
        yaml.safe_load(target.read_text(encoding="utf-8"))["assign"].get(
            "subagents"
        )
        or {}
    ) == {}


# --- Секция не-словарь: отказ, который UI уже умеет показать, вместо AttributeError ---


@pytest.mark.parametrize("section", ["models", "slots", "assign"])
def test_a_section_shaped_as_a_list_is_refused_not_crashed(tmp_path, section):
    """Файл профиля правится руками, и `slots: [x]` — обычная опечатка. Сегодня
    dict-методы на списке дают AttributeError мимо `RegistryWriteError`, который
    секция ловит, — и приложение падает целиком."""
    text = 'models:\n  gc:\n    model: "x"\n' + f"{section}: [x]\n"
    target = _target(tmp_path, text, "work.yml")

    with pytest.raises(RegistryWriteError, match=section):
        rw.references_to(target, "gc")
    with pytest.raises(RegistryWriteError, match=section):
        rw.upsert_model(target, "new", {"model": "y"}, "", is_profile=True)
    with pytest.raises(RegistryWriteError, match=section):
        rw.remove_model(target, "gc", force=True, is_profile=True)
    assert target.read_text(encoding="utf-8") == text


def test_nested_subagents_shaped_as_a_list_is_refused(tmp_path):
    text = 'models:\n  gc:\n    model: "x"\nassign:\n  subagents: [x]\n'
    target = _target(tmp_path, text, "work.yml")

    with pytest.raises(RegistryWriteError, match="subagents"):
        rw.references_to(target, "gc")


def test_an_empty_section_anchor_is_not_mistaken_for_a_broken_one(tmp_path):
    """Пустой якорь (`slots:` без детей) писатель оставляет сам — витрины и
    запись обязаны считать его отсутствием секции, а не поломкой."""
    text = 'models:\n  gc:\n    model: "x"\nslots:\nassign:\n'
    target = _target(tmp_path, text)

    assert rw.references_to(target, "gc") == []


# --- Гейт записи судит стек следующего старта, а не отдельный файл ---


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """База + каталог профилей рядом, изолированные от машины: иначе managed-оверлей
    и проектный `.gc/config.yml` разработчика попали бы в стек (см.
    tests/config/test_stacked_validation.py)."""
    from gc_core.config_modules import env_vars
    from gc_core.config_modules.env_vars import EnvVars

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path / "gc_home"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    return tmp_path / "config.yml", profiles


def test_removing_a_model_the_active_profile_points_at_is_refused(
    tmp_path, stack
):
    """Секция «Модели» на основном файле не видит слотов профиля — но следующий
    старт видит: без стекового гейта запись уходила бы с зелёным «✓», а старт
    падал бы `ValueError` на фазе 2 загрузки."""
    base, profiles = stack
    text = 'profile: "work"\nmodels:\n  gc:\n    model: "x"\n'
    base.write_text(text, encoding="utf-8")
    (profiles / "work.yml").write_text(
        'slots:\n  default: "gc"\n', encoding="utf-8"
    )

    with pytest.raises(RegistryWriteError, match=r"gc"):
        rw.remove_model(base, "gc", force=True)
    assert base.read_text(encoding="utf-8") == text


def test_a_base_resolvable_only_through_the_profile_still_accepts_writes(
    tmp_path, stack
):
    """Обратная сторона: сам по себе основной файл нерезолвим (2 модели, ни одного
    слота), а стек валиден — правка записи обязана проходить."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  a:\n    model: "x"\n  b:\n    model: "y"\n',
        encoding="utf-8",
    )
    (profiles / "work.yml").write_text(
        'slots:\n  default: "a"\n', encoding="utf-8"
    )

    rw.upsert_model(base, "a", {"model": "x2"}, "")
    assert (
        yaml.safe_load(base.read_text(encoding="utf-8"))["models"]["a"][
            "model"
        ]
        == "x2"
    )


def test_an_active_profile_write_is_judged_on_the_stack(tmp_path, stack):
    """Профиль-цель со `stack_base`: снятие своего слота оставило бы merged
    нерезолвимым — отказ. Без `stack_base` (неактивный профиль) — фаза 1, запись
    проходит."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  a:\n    model: "x"\n  b:\n    model: "y"\n',
        encoding="utf-8",
    )
    target = profiles / "work.yml"
    text = 'slots:\n  default: "a"\n'
    target.write_text(text, encoding="utf-8")

    with pytest.raises(RegistryWriteError, match="slots.default"):
        rw.write_slot(
            target, "default", None, "", is_profile=True, stack_base=base
        )
    assert target.read_text(encoding="utf-8") == text

    rw.write_slot(target, "default", None, "", is_profile=True)
    assert (
        yaml.safe_load(target.read_text(encoding="utf-8")).get("slots") or {}
    ) == {}


# --- Справка «оставит ли транзакция реестр без слота» ---


def test_needs_default_slot_sees_the_active_profile_from_the_base(stack):
    """Сценарий, из-за которого всё затевалось: легаси-база без реестра + активный
    профиль с моделью. Первая запись в базу делает merged неоднозначным — вопрос
    обязан быть задан, иначе гейт отвергнет запись, а предложить слот будет негде."""
    base, profiles = stack
    base.write_text('profile: "work"\n', encoding="utf-8")
    (profiles / "work.yml").write_text(
        'models:\n  p1:\n    model: "x"\n', encoding="utf-8"
    )

    assert (
        rw.needs_default_slot(
            base, [("upsert_model", "gc", {"model": "y"})], ""
        )
        is True
    )
    # и ответ на него разрешает запись одной транзакцией — тупика нет
    rw.apply_registry_ops(
        base,
        [
            ("upsert_model", "gc", {"model": "y"}),
            ("write_slot", "default", "gc"),
        ],
        "",
    )


def test_needs_default_slot_is_quiet_when_the_profile_sets_the_slot(stack):
    """Обратная сторона: слот задан в профиле — merged однозначен, лишнего вопроса
    быть не должно."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  a:\n    model: "x"\n', encoding="utf-8"
    )
    (profiles / "work.yml").write_text(
        'slots:\n  default: "a"\n', encoding="utf-8"
    )

    assert (
        rw.needs_default_slot(
            base, [("upsert_model", "gc", {"model": "y"})], ""
        )
        is False
    )


def test_needs_default_slot_for_a_profile_counts_the_base_under_it(stack):
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  a:\n    model: "x"\n', encoding="utf-8"
    )
    target = profiles / "work.yml"
    target.write_text("models: {}\n", encoding="utf-8")

    assert (
        rw.needs_default_slot(
            target,
            [("upsert_model", "b", {"model": "y"})],
            "",
            is_profile=True,
            stack_base=base,
        )
        is True
    )


def test_needs_default_slot_does_not_raise_on_ops_that_cannot_apply(stack):
    """Справка не имеет права ронять форму: не наложилось — вопроса нет, а
    невозможность записи всё равно поймает гейт своим отказом."""
    base, _profiles = stack
    base.write_text(
        'models: {a: {model: "x"}}\n', encoding="utf-8"
    )  # инлайновый мэппинг

    assert (
        rw.needs_default_slot(
            base, [("upsert_model", "b", {"model": "y"})], ""
        )
        is False
    )


# --- Ссылки из ДРУГИХ файлов стека ---


def test_broken_references_name_the_file_and_the_coordinate(stack):
    """Отказ удаления обязан назвать файл и координату: без этого человеку некуда
    идти — свой файл он видит, чужой нет."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  gc:\n    model: "x"\n', encoding="utf-8"
    )
    (profiles / "work.yml").write_text(
        'slots:\n  default: "gc"\n', encoding="utf-8"
    )

    assert rw.references_broken_by_removal(base, "gc") == [
        ("work.yml", "slots.default")
    ]


def test_broken_references_ignore_the_targets_own_file(stack):
    """Свой файл сюда не попадает — его по-прежнему отдаёт `references_to`, и
    удалению своей ссылки каскадом ничего не мешает."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  gc:\n    model: "x"\nslots:\n  default: "gc"\n',
        encoding="utf-8",
    )
    (profiles / "work.yml").write_text("slots: {}\n", encoding="utf-8")

    assert rw.references_broken_by_removal(base, "gc") == []


def test_broken_references_of_a_profile_see_the_base(stack):
    base, profiles = stack
    base.write_text(
        'profile: "work"\nslots:\n  default: "p1"\n', encoding="utf-8"
    )
    target = profiles / "work.yml"
    target.write_text('models:\n  p1:\n    model: "x"\n', encoding="utf-8")

    assert rw.references_broken_by_removal(target, "p1", stack_base=base) == [
        ("config.yml", "slots.default")
    ]


def test_a_shadowed_alias_survives_its_own_removal_and_breaks_nothing(stack):
    """Точный инвариант — не «на алиас смотрит другой файл», а «после удаления алиас
    в стеке НЕ останется». Профиль перекрывает запись базы тем же именем: снять
    профильную можно, запись базы остаётся на её месте, и слот базы резолвится.
    Отказать здесь значило бы запретить возможное."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  gc:\n    model: "base-x"\nslots:\n  default: "gc"\n',
        encoding="utf-8",
    )
    target = profiles / "work.yml"
    target.write_text(
        'models:\n  gc:\n    model: "profile-y"\n', encoding="utf-8"
    )

    assert rw.references_broken_by_removal(target, "gc", stack_base=base) == []
    rw.remove_model(target, "gc", force=True, is_profile=True, stack_base=base)


# --- Указатель профиля из ПРОЕКТНОГО слоя, а не только из базы ---
#
# `stack_documents` резолвил `profile:` из СЫРОГО документа базы, тогда как
# `_merge_layers` берёт его из мержа база+managed+project (§3). Раскладка
# «указатель лежит в `.gc/config.yml`» не покрыта ни одним тестом выше — все
# они кладут `profile:` прямо в базу, — и живёт именно там.


def test_broken_references_see_a_profile_pointed_from_the_project_layer(stack):
    """Ложное «можно»: указатель — в проектном `.gc/config.yml`, база о профиле
    молчит. Справка обязана всё равно найти файл профиля и его ссылку — иначе
    баннер обещал бы удаление, которое отвергнет запись (реальный валидатор
    мержит слои правильно и откажет на фазе 2, а справка соврала бы раньше)."""
    base, profiles = stack
    base.write_text('models:\n  gc:\n    model: "base-x"\n', encoding="utf-8")
    gc_dir = Path.cwd() / ".gc"
    gc_dir.mkdir()
    (gc_dir / "config.yml").write_text('profile: "work"\n', encoding="utf-8")
    (profiles / "work.yml").write_text(
        'slots:\n  default: "gc"\n', encoding="utf-8"
    )

    assert rw.references_broken_by_removal(base, "gc") == [
        ("work.yml", "slots.default")
    ]


def test_an_alias_surviving_via_a_project_pointed_profile_is_not_refused(
    stack,
):
    """Обратная сторона: алиас переживает удаление через профиль, назначенный
    ПРОЕКТНЫМ указателем, а видимая (проектная) ссылка на него — та же. Не
    зная о профиле, справка решила бы, что ссылка повиснет, и заблокировала бы
    легальное удаление наглухо, без обхода в UI."""
    base, profiles = stack
    base.write_text('models:\n  gc:\n    model: "base-x"\n', encoding="utf-8")
    gc_dir = Path.cwd() / ".gc"
    gc_dir.mkdir()
    (gc_dir / "config.yml").write_text(
        'profile: "work"\nslots:\n  default: "gc"\n', encoding="utf-8"
    )
    (profiles / "work.yml").write_text(
        'models:\n  gc:\n    model: "profile-y"\n', encoding="utf-8"
    )

    assert rw.references_broken_by_removal(base, "gc") == []
    rw.remove_model(base, "gc", force=True)


def test_a_pointer_conflict_between_base_and_project_is_resolved_like_the_loader(
    stack,
):
    """Указатель есть в ОБОИХ слоях и они расходятся: `_merge_layers` мержит
    managed, ПОТОМ project — последний побеждает при конфликте одноимённых ключей.
    `resolve_profile_pointer` (общий резолв `stack_documents`) обязан называть ТО ЖЕ
    победившее имя, а не то, что лежит в самой базе."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  gc:\n    model: "base-x"\n',
        encoding="utf-8",
    )
    gc_dir = Path.cwd() / ".gc"
    gc_dir.mkdir()
    (gc_dir / "config.yml").write_text('profile: "corp"\n', encoding="utf-8")
    (profiles / "work.yml").write_text(
        'models:\n  ghost:\n    model: "z"\n', encoding="utf-8"
    )
    (profiles / "corp.yml").write_text(
        'models:\n  gc:\n    model: "corp-y"\n', encoding="utf-8"
    )

    # "gc" переживёт удаление ТОЛЬКО если резолвится "corp" (он её объявляет);
    # "work" её не знает вовсе.
    assert rw.alias_survives_removal(base, "gc") is True
    assert rw.references_broken_by_removal(base, "gc") == []


def test_resolve_profile_pointer_interpolates_env_placeholders(
    stack, monkeypatch
):
    """Указатель `profile: "${ENV:…}"` (ревью MR #80, M4): загрузчик интерполирует
    ДО чтения указателя, а общий резолв отдавал сырую строку — слой профиля
    пропадал у ВСЕХ потребителей резолва разом (stack_documents → справки
    удаления, витрина происхождения, ключ кэша вердикта), при том что гейт
    записи отвечал верно: две редакции резолва разъехались. Интерполируется
    ИТОГ мержа — ${ENV:…}-only резолвером (`resolve_env_only`), не тем же
    `resolve_env_placeholders`, что у загрузчика: указатель зовётся на каждый
    кадр UI, и файловый резолв/warning там неуместны (ревью MR #80, #12)."""
    from gc_core.config_modules.layers import (
        resolve_profile_pointer,
        stack_documents,
    )

    base, profiles = stack
    monkeypatch.setenv("GC_TEST_PROFILE_PTR", "work")
    base.write_text(
        'profile: "${ENV:GC_TEST_PROFILE_PTR}"\nmodels:\n  gc:\n    model: "x"\n',
        encoding="utf-8",
    )
    (profiles / "work.yml").write_text(
        'slots:\n  default: "gc"\n', encoding="utf-8"
    )

    assert resolve_profile_pointer(base) == "work"
    assert any(
        Path(p).resolve() == (profiles / "work.yml").resolve()
        for p, _doc in stack_documents(base)
    )


def test_a_broken_file_marker_logs_once_not_every_frame(
    stack, tmp_path, caplog
):
    """[ревью N7, находка 4] Указатель ${FILE:битый-путь}: полный резолвер зовётся
    на КАЖДОЕ разрешение указателя (за кадр, с UI-потока) — без кэша нечитаемый
    файл заливал лог предупреждением на каждый вызов. Кэш по (значение, mtime):
    повторный вызов при неизменном файле не ходит в полный резолвер вовсе;
    появившийся файл (mtime сменился) честно пересчитывается."""
    from gc_core.config_modules.layers import resolve_profile_pointer

    base, _profiles = stack
    marker = tmp_path / "no-such-pointer.txt"  # файла нет
    base.write_text(
        'profile: "${{FILE:{}}}"\n'.format(str(marker).replace("\\", "/")),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        resolve_profile_pointer(base)
        first = len(caplog.records)
        resolve_profile_pointer(base)
        resolve_profile_pointer(base)

    assert len(caplog.records) == first, "повторные вызовы шумят в лог"

    marker.write_text("work", encoding="utf-8")  # файл появился
    (_profiles / "work.yml").write_text("slots: {}\n", encoding="utf-8")
    assert resolve_profile_pointer(base) == "work"  # кэш не залип на пустоте


def test_resolve_profile_pointer_follows_a_file_marker_like_the_loader(
    stack, tmp_path
):
    """[ревью волны F, находки 2-3] Указатель `profile: "${FILE:…}"`: загрузчик
    разворачивает весь документ полным резолвером и активирует названный
    профиль, а env-only резолв возвращал ЛИТЕРАЛ — все потребители резолва
    расходились с работающим стеком, удаление/переименование оставляло
    указатель висячим. `resolve_pointer_value` дорезолвит `${FILE:…}` полным
    резолвером — редчайший указатель-из-файла платит за себя сам."""
    from gc_core.config_modules.layers import resolve_profile_pointer

    base, profiles = stack
    marker = tmp_path / "pointer.txt"
    marker.write_text("work", encoding="utf-8")
    base.write_text(
        'profile: "${{FILE:{}}}"\nmodels:\n  gc:\n    model: "x"\n'.format(
            str(marker).replace("\\", "/")
        ),
        encoding="utf-8",
    )
    (profiles / "work.yml").write_text(
        'slots:\n  default: "gc"\n', encoding="utf-8"
    )

    assert resolve_profile_pointer(base) == "work"


def test_resolve_profile_pointer_does_not_warn_on_an_unset_env_var(
    stack, caplog, monkeypatch
):
    """[ревью #12] `resolve_profile_pointer` — best-effort и зовётся на КАЖДОЕ
    разрешение указателя (витрина происхождения, ключ кэша вердикта, guard
    ссылок — с UI-потока, за кадр). Полный `resolve_env_placeholders` пишет
    `warning` на каждую незаданную `${ENV:…}` — с указателем это шумело бы на
    каждый кадр, хотя незаданная переменная в ИМЕНИ профиля — штатное
    best-effort «пусто», не повод жаловаться (файловый секрет в имени профиля
    — небыль, ей и подавно предупреждать не о чем)."""
    from gc_core.config_modules.layers import resolve_profile_pointer

    monkeypatch.delenv("GC_TEST_PROFILE_PTR_UNSET", raising=False)
    base, _profiles = stack
    base.write_text(
        'profile: "${ENV:GC_TEST_PROFILE_PTR_UNSET}"\n', encoding="utf-8"
    )

    with caplog.at_level("WARNING"):
        assert resolve_profile_pointer(base) == ""

    assert caplog.records == []


def test_resolve_profile_pointer_is_lenient_to_a_broken_base(stack):
    """База невалидна (внешняя порча, недописанный YAML) — указатель всё равно
    ищется в managed/project, тем же правилом, каким `config_raw.read_document`
    трактует битый файл (пусто, а не отказ): конфиг уже деградировал, и молчать о
    профиле, назначенном другим слоем, значило бы прятать ровно ту подсказку,
    которая нужна для починки. Строгая редакция ничего не защищает — она не
    сообщает о поломке, а тихо возвращает пусто."""
    base, _profiles = stack
    base.write_text(": : : битый yml : : :\n  - [оборван\n", encoding="utf-8")
    gc_dir = Path.cwd() / ".gc"
    gc_dir.mkdir()
    (gc_dir / "config.yml").write_text('profile: "corp"\n', encoding="utf-8")

    from gc_core.config_modules.layers import resolve_profile_pointer

    assert resolve_profile_pointer(base) == "corp"


# --- Негодная секция в ЧУЖОМ файле стека: пропуск, а не крэш ---

# Зеркало `test_a_section_shaped_as_a_list_is_refused_not_crashed`: там правило
# объявлено для файла, в который пишем (`_document` сторожит форму секций), здесь —
# для остальных файлов стека, куда то же правило не дошло. Отказ тут был бы неверен:
# чужой файл мы не правим, и запрещать правку своего из-за соседской опечатки — то
# же самое, что блокировать правку профиля, которого никто не грузит. Негодная
# секция просто не содержит ссылок.


@pytest.mark.parametrize("section", ["slots", "assign"])
def test_a_broken_section_in_another_stack_file_yields_no_references(
    stack, section
):
    """`slots: [x]` в профиле роняло `references_broken_by_removal` `AttributeError`
    мимо `(RegistryWriteError, OSError)`, который ловит секция, — то есть Del на
    строке модели убивал приложение. Достижимо и без чужих правок: указатель
    профиля берётся из БАЗЫ, так что сюда попадает и файл, который старт не
    грузил и схемой не проверял."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  gc:\n    model: "x"\n', encoding="utf-8"
    )
    (profiles / "work.yml").write_text(f"{section}: [x]\n", encoding="utf-8")

    assert rw.references_broken_by_removal(base, "gc") == []


def test_broken_nested_subagents_in_another_stack_file_yield_no_references(
    stack,
):
    """Третья форма того же: `assign.subagents` не словарь — `.items()` на списке."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  gc:\n    model: "x"\n', encoding="utf-8"
    )
    (profiles / "work.yml").write_text(
        "assign:\n  subagents: [x]\n", encoding="utf-8"
    )

    assert rw.references_broken_by_removal(base, "gc") == []


def test_a_broken_section_does_not_hide_the_sound_one_beside_it(stack):
    """Пропускается негодная СЕКЦИЯ, а не весь файл: иначе одна опечатка в slots
    прятала бы живую ссылку из assign — и удаление уходило бы с зелёным «✓»,
    оставив привязку висеть."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  gc:\n    model: "x"\n', encoding="utf-8"
    )
    (profiles / "work.yml").write_text(
        'slots: [x]\nassign:\n  summarizer: "gc"\n', encoding="utf-8"
    )

    assert rw.references_broken_by_removal(base, "gc") == [
        ("work.yml", "assign.summarizer")
    ]


def test_cascade_keeps_a_reference_to_an_alias_that_survives_the_removal(
    stack,
):
    """Каскад снимает ТОЛЬКО те ссылки, что повиснут. Перекрывшая запись профиля
    снимается свободно: запись базы остаётся на месте, и слот, который на неё
    смотрит, продолжает резолвиться.

    Инвариант уже сформулирован соседней справкой (`references_broken_by_removal`:
    «алиас переживёт удаление — ничьи ссылки не повиснут»), но каскад считал по
    совпадению ИМЕНИ в своём файле — вторая, разъехавшаяся редакция одного правила.
    Соседний страж ставит ту же конфигурацию, но кладёт слот в базу и проходит
    мимо дефекта: снести каскадом можно лишь то, что лежит в правимом файле."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  gc:\n    model: "base-x"\n',
        encoding="utf-8",
    )
    target = profiles / "work.yml"
    target.write_text(
        'models:\n  gc:\n    model: "profile-y"\nslots:\n  default: "gc"\n',
        encoding="utf-8",
    )

    assert rw.references_broken_by_removal(target, "gc", stack_base=base) == []
    rw.remove_model(target, "gc", force=True, is_profile=True, stack_base=base)

    doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert (doc.get("models") or {}) == {}
    assert (doc.get("slots") or {}) == {
        "default": "gc"
    }  # ссылка осталась валидной


# --- Удаление из НЕАКТИВНОГО профиля судится стеком «база + этот профиль» ---
#
# Стек вопросов об удалении строился по действующему указателю, а не по тому
# профилю, который открыт в редакторе. Активен профиль A, у обоих профилей есть
# запись `x`, у неактивного B на неё смотрит `slots.default`: открыть B, удалить
# `x` — «алиас переживёт» (он есть в A), каскад пропущен, в B остаётся висячий
# слот; валидатор до неактивного профиля не доходит, а активация B потом падает.
# Все тесты выше ставят на цель АКТИВНЫЙ профиль и мимо дефекта проходят.


def _two_profiles(stack, *, active: str = "a"):
    """База с указателем на `active` и два профиля: `a` и `b` оба объявляют `x`,
    у `b` на неё смотрит `slots.default`. Отдаёт (база, файл b)."""
    base, profiles = stack
    base.write_text(f'profile: "{active}"\n', encoding="utf-8")
    (profiles / "a.yml").write_text(
        'models:\n  x:\n    model: "a-x"\n', encoding="utf-8"
    )
    other = profiles / "b.yml"
    other.write_text(
        'models:\n  x:\n    model: "b-x"\nslots:\n  default: "x"\n',
        encoding="utf-8",
    )
    return base, other


def test_an_alias_declared_only_by_the_active_profile_does_not_survive_removal_from_another(
    stack,
):
    """Из стека открытого профиля B активный A не виден: снятая в B запись `x`
    в стеке «база + B» не останется, что бы ни объявлял A."""
    base, other = _two_profiles(stack)

    assert rw.alias_survives_removal(other, "x", stack_base=base) is False


def test_removal_from_an_inactive_profile_cascades_its_own_dangling_slot(
    stack,
):
    """Каскад в B обязан снять `slots.default: x` вместе с записью: иначе файл
    остаётся с висячей ссылкой, о которой не скажет никто до самой активации."""
    base, other = _two_profiles(stack)

    rw.remove_model(other, "x", force=True, is_profile=True, stack_base=base)

    doc = yaml.safe_load(other.read_text(encoding="utf-8"))
    assert (doc.get("models") or {}) == {}
    assert (doc.get("slots") or {}) == {}


def test_removal_from_an_inactive_profile_protects_a_base_reference_it_alone_resolves(
    stack,
):
    """Тот же стек и у отказа по чужим ссылкам: слот БАЗЫ смотрит на `p`, которую
    объявляют только профили. Снять `p` из неактивного B — значит сделать стек
    «база + B» нерезолвимым: по указателю (база + A) справка отвечала «ничего не
    повиснет» и отказ молчал."""
    base, profiles = stack
    base.write_text('profile: "a"\nslots:\n  default: "p"\n', encoding="utf-8")
    (profiles / "a.yml").write_text(
        'models:\n  p:\n    model: "a-p"\n', encoding="utf-8"
    )
    other = profiles / "b.yml"
    other.write_text('models:\n  p:\n    model: "b-p"\n', encoding="utf-8")

    assert rw.references_broken_by_removal(other, "p", stack_base=base) == [
        ("config.yml", "slots.default")
    ]


def test_a_base_namesake_survives_removal_from_an_inactive_profile(stack):
    """Обратная сторона: запись базы под тем же именем переживает удаление из
    любого профиля — активного или нет, — и своя ссылка B остаётся на месте:
    она продолжит резолвиться записью базы."""
    base, profiles = stack
    base.write_text(
        'profile: "a"\nmodels:\n  x:\n    model: "base-x"\n', encoding="utf-8"
    )
    (profiles / "a.yml").write_text("slots: {}\n", encoding="utf-8")
    other = profiles / "b.yml"
    other.write_text(
        'models:\n  x:\n    model: "b-x"\nslots:\n  default: "x"\n',
        encoding="utf-8",
    )

    assert rw.alias_survives_removal(other, "x", stack_base=base) is True
    assert rw.references_broken_by_removal(other, "x", stack_base=base) == []
    rw.remove_model(other, "x", force=True, is_profile=True, stack_base=base)
    doc = yaml.safe_load(other.read_text(encoding="utf-8"))
    assert (doc.get("slots") or {}) == {"default": "x"}


def test_needs_default_slot_for_an_inactive_profile_counts_its_own_cascade(
    stack,
):
    """Справка о слоте и без того считала «база + этот файл», но каскад внутри неё
    шёл по указателю: в B две записи, `default` смотрит на `x`, которую объявляет
    ещё и активный A. Удаление `x` из B каскадом снимает единственный слот — при
    оставшихся двух моделях (`y` в B и `x` базы) стек «база + B» без `default`
    нерезолвим, и справка обязана ответить «да»."""
    base, profiles = stack
    base.write_text(
        'profile: "a"\nmodels:\n  z:\n    model: "base-z"\n', encoding="utf-8"
    )
    (profiles / "a.yml").write_text(
        'models:\n  x:\n    model: "a-x"\n', encoding="utf-8"
    )
    other = profiles / "b.yml"
    other.write_text(
        'models:\n  x:\n    model: "b-x"\n  y:\n    model: "b-y"\nslots:\n  default: "x"\n',
        encoding="utf-8",
    )

    assert (
        rw.needs_default_slot(
            other,
            [("remove_model", "x", True)],
            "",
            is_profile=True,
            stack_base=base,
        )
        is True
    )


# --- Отказ валидации называет файл, где лежит ключ ---
#
# Вердикт фазы 2 вынесен по СТЕКУ, и ключ, который он называет, может лежать не в
# том файле, который правится. Живой тупик стенда: мастер пишет реестр в основной
# конфиг, а отказ «write_slot default + upsert_model … + write_slot max:
# slots.default: алиас "strong" не найден в merged-реестре models» — про активный
# профиль. Человек не мог понять, про конфиг это или про профиль, а служебные
# имена операций впереди ему ничего не говорили.


def test_a_refusal_caused_by_the_profile_names_the_profile_and_its_file(stack):
    """Текст валидатора остаётся дословно; за ним — слой словами («в профиле
    "work"») и ПОЛНЫЙ путь его файла: файлов в стеке несколько, и «в файле выше»
    не называет ни одного.

    Стенд — запись, ломающая ЗДОРОВЫЙ стек сама (снятие записи базы, на которую
    смотрит слот активного профиля): с починки L19 стек, не проходивший вопрос
    гейта и ДО записи, правку своего файла больше не запирает — прежний стенд с
    уже висячей ссылкой профиля до отказа теперь не доходит."""
    base, profiles = stack
    text = (
        'profile: "work"\n'
        'models:\n  fast:\n    model: "qwen/qwen3-8b"\n'
        '  strong:\n    model: "m-strong"\n'
        'slots:\n  default: "fast"\n'
    )
    base.write_text(text, encoding="utf-8")
    profile = profiles / "work.yml"
    profile.write_text('slots:\n  max: "strong"\n', encoding="utf-8")

    with pytest.raises(RegistryWriteError) as exc:
        rw.apply_registry_ops(base, [("remove_model", "strong", True)], "")

    message = str(exc.value)
    assert message.startswith(
        'slots.max: алиас "strong" не найден в merged-реестре models'
    )
    assert f'поправьте slots.max в профиле "work": {profile}' in message
    assert base.read_text(encoding="utf-8") == text


def test_a_refusal_caused_by_the_base_names_the_base_file(stack):
    """Обратная раскладка: правится АКТИВНЫЙ профиль, запись рвёт ссылку, а
    названный вердиктом ключ лежит в основном конфиге — назван он, а не файл,
    который человек правит. Стенд — запись сама ломает здоровый стек: снимается
    профильная модель, которую резолвит только слот базы (прежний стенд с уже
    висячим слотом базы с починки L19 до отказа не доходит)."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nslots:\n  default: "p1"\n', encoding="utf-8"
    )
    profile = profiles / "work.yml"
    text = 'models:\n  p1:\n    model: "x"\n'
    profile.write_text(text, encoding="utf-8")

    with pytest.raises(RegistryWriteError) as exc:
        rw.remove_model(
            profile, "p1", force=True, is_profile=True, stack_base=base
        )

    message = str(exc.value)
    assert message.startswith('slots.default: алиас "p1" не найден')
    assert f"поправьте slots.default в основном конфиге: {base}" in message
    assert str(profile) not in message
    assert profile.read_text(encoding="utf-8") == text


def test_a_validation_refusal_carries_no_operation_names(stack):
    """Секция «Модели» на этом классе стендов показывала «remove_model strong:
    slots.max: …». Отказ валидации написан для человека — как и осознанный
    отказ операции, он начинается с собственного текста, а не со служебного
    имени; подпись операцией остаётся у МЕХАНИЧЕСКОГО сбоя (см.
    test_registry_write.py::test_a_deliberate_refusal_reaches_the_user_without_
    the_operation_name — сторож на ту ветку). Стенд — запись сама ломает
    здоровый стек, как и у соседей выше."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\n'
        'models:\n  fast:\n    model: "qwen/qwen3-8b"\n'
        '  strong:\n    model: "m-strong"\n'
        'slots:\n  default: "fast"\n',
        encoding="utf-8",
    )
    (profiles / "work.yml").write_text(
        'slots:\n  max: "strong"\n', encoding="utf-8"
    )

    with pytest.raises(RegistryWriteError) as exc:
        rw.apply_registry_ops(base, [("remove_model", "strong", True)], "")

    message = str(exc.value)
    assert message.startswith("slots.max:")
    for op_name in rw._OPERATIONS:
        assert op_name not in message


def test_the_culprit_is_judged_on_the_stack_the_validator_saw_not_on_the_disk(
    stack,
):
    """Каскад снимает ссылку в правимом файле, а тёзка-ссылка профиля остаётся
    висеть. По ДИСКУ значение slots.default «не менялось» от базы к профилю, и
    виновником вышел бы основной конфиг — файл, из которого ссылка как раз
    уходит. Виновник считается по стеку, где цель стоит ещё не записанным
    текстом, — тому же, что судил валидатор."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  a:\n    model: "x"\n  b:\n    model: "y"\nslots:\n  default: "a"\n',
        encoding="utf-8",
    )
    profile = profiles / "work.yml"
    profile.write_text('slots:\n  default: "a"\n', encoding="utf-8")

    with pytest.raises(RegistryWriteError) as exc:
        rw.remove_model(base, "a", force=True)

    message = str(exc.value)
    assert f'поправьте slots.default в профиле "work": {profile}' in message
    assert str(base) not in message


def test_an_unreadable_target_is_a_write_error(tmp_path):
    """Цель операции не читается — это отказ ЗАПИСИ, а не сюрприз вызывающему.

    Чтение цели шло голым `read_text`: `OSError` уходил наружу мимо
    `RegistryWriteError`, а `UnicodeDecodeError` (подкласс `ValueError`, не
    `OSError`) — мимо и того перехвата `(RegistryWriteError, OSError)`, которым
    секция ловит отказы. Цена: экран падал крэшем вместо строки отказа. Цель
    достижима: файл НЕАКТИВНОГО профиля в загрузке не участвует, его кодировку
    никто не проверял, а секция в него пишет."""
    target = tmp_path / "profile.yml"
    target.write_text('models:\n  a:\n    model: "x"\n', encoding="utf-16")

    with pytest.raises(RegistryWriteError) as exc:
        rw.upsert_model(target, "b", {"model": "y"}, "", is_profile=True)
    assert str(target) in str(exc.value)


def test_the_default_slot_question_does_not_crash_on_an_unreadable_target(
    tmp_path,
):
    """Тот же отказ на пути СПРАВКИ. `needs_default_slot` секция спрашивает до
    записи и без перехвата — голая ошибка чтения оттуда бросать ей нельзя — экран
    упал бы раньше, чем человек увидел отказ. Справка best-effort: свой перехват
    `(RegistryWriteError, OSError)` отвечает «слот не спрашиваем», а внятную
    причину человек получает от самой записи. До правки сюда прилетал голый
    `UnicodeDecodeError` мимо этого перехвата."""
    target = tmp_path / "profile.yml"
    target.write_text('models:\n  a:\n    model: "x"\n', encoding="utf-16")

    assert (
        rw.needs_default_slot(
            target,
            [("upsert_model", "b", {"model": "y"})],
            "",
            is_profile=True,
        )
        is False
    )


def test_a_readable_target_still_writes(tmp_path):
    """Сторож: расширение перехвата не превращает нормальную запись в отказ."""
    target = tmp_path / "profile.yml"
    target.write_text('models:\n  a:\n    model: "x"\n', encoding="utf-8")

    rw.upsert_model(target, "b", {"model": "y"}, "", is_profile=True)
    assert "b:" in target.read_text(encoding="utf-8")


# --- Нестроковый алиас: отказ с подписью операции, а не трейсбек (L17) ---
#
# Достижимо мастером: YAML-ключ `4.1:` без кавычек парсится float'ом, повторный
# прогон мастера собирает `('write_slot', 'default', 4.1)`. L17 ловил СЛЕДСТВИЕ —
# `quote_scalar` падал `AttributeError` ('float' object has no attribute
# 'replace') МИМО кортежа механических сбоев `_apply_ops_to_text`, — но не саму
# ПРИЧИНУ: нестроковый алиас так и оставался нестроковым. Ревью MR #80 (#4):
# граница нормализации типа (`str(alias)`/`str(value)` в `_write_slot_text`/
# `_write_assign_text`/`_yaml_key`) убирает сам триггер — дальше это ОБЫЧНЫЙ
# домен-отказ «такого алиаса нет в реестре» (модель `gc`, не «4.1»/«True»), а
# не механический сбой с именем операции вместо причины.


@pytest.mark.parametrize("alias", [4.1, True], ids=["float", "bool"])
def test_a_non_string_slot_alias_is_a_write_error_not_a_traceback(
    tmp_path, alias
):
    text = 'models:\n  gc:\n    model: "x"\nslots:\n  default: "gc"\n'
    target = _target(tmp_path, text)

    with pytest.raises(
        RegistryWriteError, match=f'slots\\.default: алиас "{alias}" не найден'
    ):
        rw.write_slot(target, "default", alias, "")
    assert target.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("value", [4.1, True], ids=["float", "bool"])
def test_a_non_string_assign_value_is_a_write_error_not_a_traceback(
    tmp_path, value
):
    text = 'models:\n  gc:\n    model: "x"\nassign:\n  finish_gate: "gc"\n'
    target = _target(tmp_path, text)

    with pytest.raises(
        RegistryWriteError, match=f'assign\\.finish_gate: значение "{value}"'
    ):
        rw.write_assign(target, "finish_gate", value, "")
    assert target.read_text(encoding="utf-8") == text
