"""Pre-write валидация секций судит СТЕК, который соберёт следующий старт.

Реестр кросс-слоен по устройству (слот профиля вправе ссылаться на модель базы,
`docs/specs/2026-08-13-profile-effective-view-design.md` §1), поэтому вердикт по
одному файлу врёт в обе стороны: пропускает запись, после которой merged не
загрузится, и отвергает рабочую раскладку «модели в базе, уровни в профиле».
"""
from pathlib import Path

import pytest

from config import (
    validate_config_text_with_sections,
    validate_profile_text_with_sections,
)
from config_modules import env_vars
from config_modules.env_vars import EnvVars
from config_modules.profiles import validate_profile_text


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """База + каталог профилей рядом с ней, изолированные от машины (тот же
    паттерн, что в tests/config/test_profile_loader.py): иначе managed-оверлей и
    проектный `.gc/config.yml` разработчика подмешались бы в стек."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path / "gc_home"))
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base = tmp_path / "config.yml"
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    return base, profiles


def test_models_in_base_slots_in_profile_pass_on_the_stack(stack):
    """Раскладка «те же модели, другая раскладка уровней»: сам по себе config.yml
    нерезолвим (2 модели, ни одного слота), стек — валиден."""
    base, profiles = stack
    text = 'profile: "work"\nmodels:\n  a:\n    model: "x"\n  b:\n    model: "y"\n'
    base.write_text(text, encoding="utf-8")
    (profiles / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="slots.default"):
        validate_config_text_with_sections(text)
    validate_config_text_with_sections(text, base_path=base)


def test_dropping_a_model_the_active_profile_points_at_is_refused(stack):
    """Удаление записи базы, на которую смотрит слот активного профиля: по одному
    файлу это «чисто», а следующий старт падал бы на фазе 2 загрузки."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  gc:\n    model: "x"\n', encoding="utf-8"
    )
    (profiles / "work.yml").write_text('slots:\n  default: "gc"\n', encoding="utf-8")
    after_removal = 'profile: "work"\nmodels: {}\n'

    validate_config_text_with_sections(after_removal)
    with pytest.raises(ValueError, match=r'slots\.default.*gc'):
        validate_config_text_with_sections(after_removal, base_path=base)


def test_unresolvable_pointer_falls_back_to_the_single_file(stack):
    """Стек не собрался (профиля по указателю нет) — фаза 2 откатывается к
    однофайловой, а не роняет запись: хуже, чем было, не становится."""
    base, _profiles = stack
    text = 'profile: "нет-такого"\nmodels:\n  a:\n    model: "x"\n'
    base.write_text(text, encoding="utf-8")

    validate_config_text_with_sections(text, base_path=base)


def test_phase_one_stays_on_the_file_itself(stack):
    """Схему по-прежнему судят по правимому файлу: сообщение о чужом слое человеку
    в форме бесполезно."""
    base, _profiles = stack
    base.write_text('models:\n  a:\n    model: "x"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="схему"):
        validate_config_text_with_sections("models:\n  a:\n    model: 5\n",
                                           base_path=base)


def test_unsetting_a_slot_in_the_active_profile_is_refused(stack):
    """Активный профиль — часть стека следующего старта: снятое в нём значение
    оставляет merged нерезолвимым так же, как правка базы."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  a:\n    model: "x"\n  b:\n    model: "y"\n',
        encoding="utf-8",
    )
    target = profiles / "work.yml"
    target.write_text('slots:\n  default: "a"\n', encoding="utf-8")

    validate_profile_text("slots: {}\n")
    with pytest.raises(ValueError, match="slots.default"):
        validate_profile_text_with_sections(
            "slots: {}\n", target=target, base_path=base)


def test_an_inactive_profile_is_not_blocked_by_the_stack(stack):
    """Файл, которого следующий старт не грузит, остаётся на фазе 1: блокировать
    правку забытого профиля вреднее, чем предупредить при активации."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  a:\n    model: "x"\n  b:\n    model: "y"\n',
        encoding="utf-8",
    )
    (profiles / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")
    other = profiles / "other.yml"
    other.write_text('slots: {}\n', encoding="utf-8")

    validate_profile_text_with_sections("slots: {}\n", target=other, base_path=base)


def test_the_profile_text_stands_in_its_own_place_in_the_stack(stack):
    """Ещё не записанный текст профиля подставляется на место СВОЕГО файла — а не
    вместо чужого тёзки из другого корня."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  a:\n    model: "x"\n', encoding="utf-8"
    )
    target = profiles / "work.yml"
    target.write_text("slots: {}\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r'slots\.default.*ghost'):
        validate_profile_text_with_sections(
            'slots:\n  default: "ghost"\n', target=target, base_path=base)
    validate_profile_text_with_sections(
        'slots:\n  default: "a"\n', target=target, base_path=base)


def test_the_merged_stack_reads_the_profile_from_disk_for_other_targets(stack):
    """Проверка `profile_doc_path`: подставляем текст ДРУГОГО файла — стек обязан
    читать активный профиль с диска, а не принимать чужой документ за него."""
    base, profiles = stack
    base.write_text(
        'profile: "work"\nmodels:\n  a:\n    model: "x"\n', encoding="utf-8"
    )
    (profiles / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")
    other = profiles / "other.yml"
    other.write_text("slots: {}\n", encoding="utf-8")

    # текст «сломал бы» стек, будь он подставлен вместо work.yml, — но он для other.yml
    validate_profile_text_with_sections(
        'slots:\n  default: "ghost"\n', target=other, base_path=base)


def test_base_path_accepts_a_string_path(stack):
    """`_commit` передаёт цель как есть — строка обязана работать наравне с Path."""
    base, profiles = stack
    text = 'profile: "work"\nmodels:\n  a:\n    model: "x"\n  b:\n    model: "y"\n'
    base.write_text(text, encoding="utf-8")
    (profiles / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")

    validate_config_text_with_sections(text, base_path=str(base))
    validate_profile_text_with_sections(
        'slots:\n  default: "a"\n',
        target=str(profiles / "work.yml"),
        base_path=str(base),
    )


# --- Запрос «каким станет реестр» — намеренно не тот же вопрос, что у гейта ---


def test_prospective_registry_counts_the_profile_even_when_it_is_not_active(stack):
    """Гейт по неактивному профилю не бьёт, а спросить «какая модель по умолчанию»
    в нём полезно: собрать нерезолвимое состояние человек может сейчас, а
    активировать позже. Вопрос советует, гейт запрещает — полномочия разные."""
    from config import prospective_registry

    base, profiles = stack
    base.write_text('profile: "work"\nmodels:\n  a:\n    model: "x"\n', encoding="utf-8")
    (profiles / "work.yml").write_text("slots: {}\n", encoding="utf-8")
    other = profiles / "other.yml"
    other.write_text("slots: {}\n", encoding="utf-8")

    models, slots = prospective_registry(
        'models:\n  b:\n    model: "y"\n', target=other, base_path=base, is_profile=True)
    assert sorted(models) == ["a", "b"]
    assert slots.default is None


def test_prospective_registry_of_the_base_sees_the_active_profile(stack):
    from config import prospective_registry

    base, profiles = stack
    base.write_text('profile: "work"\n', encoding="utf-8")
    (profiles / "work.yml").write_text('models:\n  p1:\n    model: "x"\n', encoding="utf-8")

    models, _slots = prospective_registry(
        'profile: "work"\nmodels:\n  gc:\n    model: "y"\n', base_path=base)
    assert sorted(models) == ["gc", "p1"]


def test_prospective_registry_falls_back_to_the_single_file(stack):
    """Стек не собрался — вопрос лучше задать по своему файлу, чем не задать вовсе."""
    from config import prospective_registry

    base, _profiles = stack
    text = 'profile: "нет-такого"\nmodels:\n  a:\n    model: "x"\n  b:\n    model: "y"\n'
    base.write_text(text, encoding="utf-8")

    models, _slots = prospective_registry(text, base_path=base)
    assert sorted(models) == ["a", "b"]


def test_prospective_registry_returns_none_on_unparsable_text(stack):
    from config import prospective_registry

    base, _profiles = stack
    base.write_text("models: {}\n", encoding="utf-8")
    assert prospective_registry("models: [\n", base_path=base) is None
