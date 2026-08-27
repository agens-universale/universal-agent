"""Сторожи слоя записи: отказ вместо тихого успеха и вместо крэша.

Два класса, которые модуль уже объявил дефектом для удаления записи
(`_drop_mapping_entry`: «текст не изменился — это отказ»), но не довёл до
остальных путей: снятие слота/привязки и секция, оказавшаяся не словарём.
"""
from pathlib import Path

import pytest
import yaml

from config_modules import registry_write as rw
from config_modules.registry_write import RegistryWriteError


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
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["slots"] == {"light": "gc"}


def test_unsetting_a_slot_written_as_a_line_still_works(tmp_path):
    text = 'models:\n  gc:\n    model: "x"\nslots:\n  default: "gc"\n'
    target = _target(tmp_path, text)

    rw.write_slot(target, "default", None, "")
    assert (yaml.safe_load(target.read_text(encoding="utf-8")).get("slots") or {}) == {}


def test_unsetting_an_assign_quoted_differently_still_finds_it(tmp_path):
    """Ключ wildcard написан одинарными кавычками, писатель пишет свои двойные —
    и всё же строка обязана найтись: ключи сравниваются как YAML-скаляры, а не
    побайтово. Иначе снятие «удавалось» бы на нетронутом файле."""
    text = (
        'models:\n  gc:\n    model: "x"\n'
        "assign:\n  subagents:\n    '*': gc\n"
    )
    target = _target(tmp_path, text)

    rw.write_assign(target, "*", None, "")
    assert (yaml.safe_load(target.read_text(encoding="utf-8"))["assign"]
            .get("subagents") or {}) == {}


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


def test_unsetting_a_nested_assign_written_as_a_line_still_works(tmp_path):
    text = (
        'models:\n  gc:\n    model: "x"\n'
        'assign:\n  subagents:\n    "*": "gc"\n'
    )
    target = _target(tmp_path, text)

    rw.write_assign(target, "*", None, "")
    assert (yaml.safe_load(target.read_text(encoding="utf-8"))["assign"]
            .get("subagents") or {}) == {}


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
    text = (
        'models:\n  gc:\n    model: "x"\n'
        "assign:\n  subagents: [x]\n"
    )
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
    from config_modules import env_vars
    from config_modules.env_vars import EnvVars

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path / "gc_home"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    return tmp_path / "config.yml", profiles


def test_removing_a_model_the_active_profile_points_at_is_refused(tmp_path, stack):
    """Секция «Модели» на основном файле не видит слотов профиля — но следующий
    старт видит: без стекового гейта запись уходила бы с зелёным «✓», а старт
    падал бы `ValueError` на фазе 2 загрузки."""
    base, profiles = stack
    text = 'profile: "work"\nmodels:\n  gc:\n    model: "x"\n'
    base.write_text(text, encoding="utf-8")
    (profiles / "work.yml").write_text('slots:\n  default: "gc"\n', encoding="utf-8")

    with pytest.raises(RegistryWriteError, match=r'gc'):
        rw.remove_model(base, "gc", force=True)
    assert base.read_text(encoding="utf-8") == text


def test_a_base_resolvable_only_through_the_profile_still_accepts_writes(tmp_path, stack):
    """Обратная сторона: сам по себе основной файл нерезолвим (2 модели, ни одного
    слота), а стек валиден — правка записи обязана проходить."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  a:\n    model: "x"\n  b:\n    model: "y"\n',
        encoding="utf-8")
    (profiles / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")

    rw.upsert_model(base, "a", {"model": "x2"}, "")
    assert yaml.safe_load(base.read_text(encoding="utf-8"))["models"]["a"]["model"] == "x2"


def test_an_active_profile_write_is_judged_on_the_stack(tmp_path, stack):
    """Профиль-цель со `stack_base`: снятие своего слота оставило бы merged
    нерезолвимым — отказ. Без `stack_base` (неактивный профиль) — фаза 1, запись
    проходит."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  a:\n    model: "x"\n  b:\n    model: "y"\n',
        encoding="utf-8")
    target = profiles / "work.yml"
    text = 'slots:\n  default: "a"\n'
    target.write_text(text, encoding="utf-8")

    with pytest.raises(RegistryWriteError, match="slots.default"):
        rw.write_slot(target, "default", None, "", is_profile=True, stack_base=base)
    assert target.read_text(encoding="utf-8") == text

    rw.write_slot(target, "default", None, "", is_profile=True)
    assert (yaml.safe_load(target.read_text(encoding="utf-8")).get("slots") or {}) == {}


# --- Справка «оставит ли транзакция реестр без слота» ---


def test_needs_default_slot_sees_the_active_profile_from_the_base(stack):
    """Сценарий, из-за которого всё затевалось: легаси-база без реестра + активный
    профиль с моделью. Первая запись в базу делает merged неоднозначным — вопрос
    обязан быть задан, иначе гейт отвергнет запись, а предложить слот будет негде."""
    base, profiles = stack
    base.write_text('profile: "work"\n', encoding="utf-8")
    (profiles / "work.yml").write_text('models:\n  p1:\n    model: "x"\n', encoding="utf-8")

    assert rw.needs_default_slot(base, [("upsert_model", "gc", {"model": "y"})], "") is True
    # и ответ на него разрешает запись одной транзакцией — тупика нет
    rw.apply_registry_ops(base, [("upsert_model", "gc", {"model": "y"}),
                                 ("write_slot", "default", "gc")], "")


def test_needs_default_slot_is_quiet_when_the_profile_sets_the_slot(stack):
    """Обратная сторона: слот задан в профиле — merged однозначен, лишнего вопроса
    быть не должно."""
    base, profiles = stack
    base.write_text('profile: "work"\nmodels:\n  a:\n    model: "x"\n', encoding="utf-8")
    (profiles / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")

    assert rw.needs_default_slot(base, [("upsert_model", "gc", {"model": "y"})], "") is False


def test_needs_default_slot_for_a_profile_counts_the_base_under_it(stack):
    base, profiles = stack
    base.write_text('profile: "work"\nmodels:\n  a:\n    model: "x"\n', encoding="utf-8")
    target = profiles / "work.yml"
    target.write_text("models: {}\n", encoding="utf-8")

    assert rw.needs_default_slot(
        target, [("upsert_model", "b", {"model": "y"})], "",
        is_profile=True, stack_base=base) is True


def test_needs_default_slot_does_not_raise_on_ops_that_cannot_apply(stack):
    """Справка не имеет права ронять форму: не наложилось — вопроса нет, а
    невозможность записи всё равно поймает гейт своим отказом."""
    base, _profiles = stack
    base.write_text('models: {a: {model: "x"}}\n', encoding="utf-8")   # инлайновый мэппинг

    assert rw.needs_default_slot(base, [("upsert_model", "b", {"model": "y"})], "") is False


# --- Ссылки из ДРУГИХ файлов стека ---


def test_broken_references_name_the_file_and_the_coordinate(stack):
    """Отказ удаления обязан назвать файл и координату: без этого человеку некуда
    идти — свой файл он видит, чужой нет."""
    base, profiles = stack
    base.write_text('profile: "work"\nmodels:\n  gc:\n    model: "x"\n', encoding="utf-8")
    (profiles / "work.yml").write_text('slots:\n  default: "gc"\n', encoding="utf-8")

    assert rw.references_broken_by_removal(base, "gc") == [("work.yml", "slots.default")]


def test_broken_references_ignore_the_targets_own_file(stack):
    """Свой файл сюда не попадает — его по-прежнему отдаёт `references_to`, и
    удалению своей ссылки каскадом ничего не мешает."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  gc:\n    model: "x"\nslots:\n  default: "gc"\n',
        encoding="utf-8")
    (profiles / "work.yml").write_text("slots: {}\n", encoding="utf-8")

    assert rw.references_broken_by_removal(base, "gc") == []


def test_broken_references_of_a_profile_see_the_base(stack):
    base, profiles = stack
    base.write_text('profile: "work"\nslots:\n  default: "p1"\n', encoding="utf-8")
    target = profiles / "work.yml"
    target.write_text('models:\n  p1:\n    model: "x"\n', encoding="utf-8")

    assert rw.references_broken_by_removal(target, "p1", stack_base=base) == [
        ("config.yml", "slots.default")]


def test_a_shadowed_alias_survives_its_own_removal_and_breaks_nothing(stack):
    """Точный инвариант — не «на алиас смотрит другой файл», а «после удаления алиас
    в стеке НЕ останется». Профиль перекрывает запись базы тем же именем: снять
    профильную можно, запись базы остаётся на её месте, и слот базы резолвится.
    Отказать здесь значило бы запретить возможное."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  gc:\n    model: "base-x"\nslots:\n  default: "gc"\n',
        encoding="utf-8")
    target = profiles / "work.yml"
    target.write_text('models:\n  gc:\n    model: "profile-y"\n', encoding="utf-8")

    assert rw.references_broken_by_removal(target, "gc", stack_base=base) == []
    rw.remove_model(target, "gc", force=True, is_profile=True, stack_base=base)


# --- Негодная секция в ЧУЖОМ файле стека: пропуск, а не крэш ---

# Зеркало `test_a_section_shaped_as_a_list_is_refused_not_crashed`: там правило
# объявлено для файла, в который пишем (`_document` сторожит форму секций), здесь —
# для остальных файлов стека, куда то же правило не дошло. Отказ тут был бы неверен:
# чужой файл мы не правим, и запрещать правку своего из-за соседской опечатки — то
# же самое, что блокировать правку профиля, которого никто не грузит. Негодная
# секция просто не содержит ссылок.


@pytest.mark.parametrize("section", ["slots", "assign"])
def test_a_broken_section_in_another_stack_file_yields_no_references(stack, section):
    """`slots: [x]` в профиле роняло `references_broken_by_removal` `AttributeError`
    мимо `(RegistryWriteError, OSError)`, который ловит секция, — то есть Del на
    строке модели убивал приложение. Достижимо и без чужих правок: указатель
    профиля берётся из БАЗЫ, так что сюда попадает и файл, который старт не
    грузил и схемой не проверял."""
    base, profiles = stack
    base.write_text('profile: "work"\nmodels:\n  gc:\n    model: "x"\n', encoding="utf-8")
    (profiles / "work.yml").write_text(f"{section}: [x]\n", encoding="utf-8")

    assert rw.references_broken_by_removal(base, "gc") == []


def test_broken_nested_subagents_in_another_stack_file_yield_no_references(stack):
    """Третья форма того же: `assign.subagents` не словарь — `.items()` на списке."""
    base, profiles = stack
    base.write_text('profile: "work"\nmodels:\n  gc:\n    model: "x"\n', encoding="utf-8")
    (profiles / "work.yml").write_text("assign:\n  subagents: [x]\n", encoding="utf-8")

    assert rw.references_broken_by_removal(base, "gc") == []


def test_a_broken_section_does_not_hide_the_sound_one_beside_it(stack):
    """Пропускается негодная СЕКЦИЯ, а не весь файл: иначе одна опечатка в slots
    прятала бы живую ссылку из assign — и удаление уходило бы с зелёным «✓»,
    оставив привязку висеть."""
    base, profiles = stack
    base.write_text('profile: "work"\nmodels:\n  gc:\n    model: "x"\n', encoding="utf-8")
    (profiles / "work.yml").write_text(
        'slots: [x]\nassign:\n  summarizer: "gc"\n', encoding="utf-8")

    assert rw.references_broken_by_removal(base, "gc") == [
        ("work.yml", "assign.summarizer")]
