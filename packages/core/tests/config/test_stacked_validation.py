"""Pre-write валидация секций судит СТЕК, который соберёт следующий старт.

Реестр кросс-слоен по устройству (слот профиля вправе ссылаться на модель базы,
`docs/specs/2026-08-13-profile-effective-view-design.md` §1), поэтому вердикт по
одному файлу врёт в обе стороны: пропускает запись, после которой merged не
загрузится, и отвергает рабочую раскладку «модели в базе, уровни в профиле».
"""
from pathlib import Path

import pytest
from gc_core.config import (
    validate_config_text_with_sections,
    validate_profile_text_with_sections,
)
from gc_core.config_modules import env_vars
from gc_core.config_modules import env_vars as mcp_paths
from gc_core.config_modules.env_vars import EnvVars
from gc_core.config_modules.profiles import validate_profile_text


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

    validate_config_text_with_sections(text, base_path=base,
        lenient_prebroken=True)


def _write_utf16(path: Path, text: str) -> Path:
    """Файл, сохранённый «блокнотом» в UTF-16: читается ТОЛЬКО как «не удалось
    прочитать» (`UnicodeDecodeError` — подкласс ValueError), синтаксис при этом
    безупречен. Живая раскладка находки, а не выдуманная."""
    path.write_bytes(text.encode("utf-16"))
    return path


def test_a_pointer_to_an_unreadable_profile_is_refused(stack):
    """ГЛАВНОЕ: запись, которая САМА заводит в стек нечитаемый файл, обязана быть
    отклонена. Раньше `_stacked_app_config` отвечала на неё тем же `None`, что и
    на «цель в стек не входит», потребители читали его как «проверять нечего» —
    указатель уезжал на диск, и следующий старт не поднимался вовсе."""
    base, profiles = stack
    body = 'models:\n  a:\n    model: "x"\nslots:\n  default: "a"\n'
    base.write_text('profile: ""\n' + body, encoding="utf-8")   # стек ЖИВ до записи
    _write_utf16(profiles / "broken-enc.yml", 'slots:\n  default: "a"\n')

    with pytest.raises(ValueError, match=r"broken-enc\.yml.*не удалось прочитать"):
        validate_config_text_with_sections('profile: "broken-enc"\n' + body,
                                           base_path=base)


def test_editing_the_base_under_an_already_unreadable_profile_is_allowed(stack):
    """Обратная цена той же ошибки: стек СЛОМАН И БЕЗ этой записи (нечитаемый
    профиль уже активен) — правку базы не запрещаем, иначе человека запирает
    внутри: чинить нечитаемый слой из /settings стало бы нечем."""
    base, profiles = stack
    base.write_text(
        'profile: "broken-enc"\nmodels:\n  a:\n    model: "x"\n', encoding="utf-8")
    _write_utf16(profiles / "broken-enc.yml", 'slots:\n  default: "a"\n')

    validate_config_text_with_sections(
        'profile: "broken-enc"\nmodels:\n  a:\n    model: "y"\n', base_path=base,
        lenient_prebroken=True)


def test_editing_the_base_under_a_foreign_dangling_reference_is_allowed(stack):
    """Зеркало теста выше для ССЫЛОЧНОЙ поломки чужого слоя (ревью MR #80, L19):
    проектный `.gc/config.yml` держит `assign.finish_gate: "ghost"` — стек не
    проходит вопрос гейта и БЕЗ этой записи, но СОБИРАЕТСЯ (merge+схема чисты),
    и поблажка «сломан и без нас» не срабатывала: `_stack_assembles` не задавала
    вопрос ссылочной целостности, который задаёт гейт. Итог — человек заперт:
    ЛЮБАЯ запись в свой файл, не чинящая чужую ссылку, отклонялась."""
    base, _profiles = stack
    base.write_text('models:\n  a:\n    model: "x"\n', encoding="utf-8")
    gc_dir = Path.cwd() / ".gc"
    gc_dir.mkdir()
    (gc_dir / "config.yml").write_text(
        'assign:\n  finish_gate: "ghost"\n', encoding="utf-8")

    validate_config_text_with_sections(
        'models:\n  a:\n    model: "y"\n', base_path=base,
        lenient_prebroken=True)


def test_editing_the_active_profile_under_a_foreign_dangling_reference_is_allowed(stack):
    """Тот же откат у второго входа гейта: правится активный профиль, а вопрос
    гейта заваливает висячая ссылка проектного слоя — профиль остаётся на фазе 1."""
    base, profiles = stack
    base.write_text('profile: "work"\nmodels:\n  a:\n    model: "x"\n',
                    encoding="utf-8")
    (profiles / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")
    gc_dir = Path.cwd() / ".gc"
    gc_dir.mkdir()
    (gc_dir / "config.yml").write_text(
        'assign:\n  finish_gate: "ghost"\n', encoding="utf-8")

    validate_profile_text_with_sections(
        'slots:\n  default: "a"\n', target=profiles / "work.yml", base_path=base,
        lenient_prebroken=True)


def test_a_pointer_write_is_judged_strictly_even_on_a_prebroken_stack(stack):
    """Граница поблажки: её просит ЗАПИСЬ содержимого стоящего слоя
    (`registry_write._commit`), а не вход валидации. Запись УКАЗАТЕЛЯ меняет сам
    СОСТАВ стека — активирует, создаёт или переносит поломку, — и поблажка
    подменяла причину отказа чужой: аварийный откат на несуществующий профиль
    отклонялся текстом «моделей ≥2» вместо «профиль не найден», активация
    профиля со слотом-призраком создавала указатель МОЛЧА, а витрина хаба
    рисовала ✓ на стеке, сломанном профилем (регресс волны фиксов MR #80)."""
    base, _profiles = stack
    # сломан и без записи: 2 модели, ни одного слота
    base.write_text('models:\n  a:\n    model: "x"\n  b:\n    model: "y"\n',
                    encoding="utf-8")

    with pytest.raises(ValueError, match="нет-такого"):
        validate_config_text_with_sections(
            'profile: "нет-такого"\nmodels:\n  a:\n    model: "x"\n'
            '  b:\n    model: "y"\n',
            base_path=base)


def test_the_fallback_under_a_foreign_break_still_judges_the_file_itself(stack):
    """Откат — не амнистия (тот же принцип, что и у
    test_a_broken_layer_does_not_disable_phase_one): при чужой висячей ссылке
    свой файл судится ОДНОФАЙЛОВОЙ фазой 2, и его собственная нерезолвимость
    (2 модели без единого слота) — по-прежнему отказ."""
    base, _profiles = stack
    base.write_text('models:\n  a:\n    model: "x"\n', encoding="utf-8")
    gc_dir = Path.cwd() / ".gc"
    gc_dir.mkdir()
    (gc_dir / "config.yml").write_text(
        'assign:\n  finish_gate: "ghost"\n', encoding="utf-8")

    with pytest.raises(ValueError, match=r"slots\.default"):
        validate_config_text_with_sections(
            'models:\n  a:\n    model: "x"\n  b:\n    model: "y"\n', base_path=base,
        lenient_prebroken=True)


def test_dropping_the_pointer_to_an_unreadable_profile_is_allowed(stack):
    """Выход из ловушки: снятие указателя стек как раз ЧИНИТ — отказать здесь
    значило бы отобрать единственную кнопку, которой человек выбирается."""
    base, profiles = stack
    base.write_text(
        'profile: "broken-enc"\nmodels:\n  a:\n    model: "x"\n', encoding="utf-8")
    _write_utf16(profiles / "broken-enc.yml", 'slots:\n  default: "a"\n')

    validate_config_text_with_sections(
        'profile: ""\nmodels:\n  a:\n    model: "x"\n', base_path=base)


def test_writing_into_an_unreadable_active_profile_is_not_blocked(stack):
    """Второй вход гейта: правится САМ нечитаемый профиль — его текст в стеке
    подставляется кандидатом, читать файл незачем, отказывать не за что."""
    base, profiles = stack
    base.write_text(
        'profile: "broken-enc"\nmodels:\n  a:\n    model: "x"\n', encoding="utf-8")
    target = _write_utf16(profiles / "broken-enc.yml", 'slots:\n  default: "a"\n')

    validate_profile_text_with_sections(
        'slots:\n  default: "a"\n', target=target, base_path=base)


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
    from gc_core.config import prospective_registry

    base, profiles = stack
    base.write_text('profile: "work"\nmodels:\n  a:\n    model: "x"\n', encoding="utf-8")
    (profiles / "work.yml").write_text("slots: {}\n", encoding="utf-8")

    models, slots = prospective_registry(
        'models:\n  b:\n    model: "y"\n', base_path=base, is_profile=True)
    assert sorted(models) == ["a", "b"]
    assert slots.default is None


def test_prospective_registry_of_the_base_sees_the_active_profile(stack):
    from gc_core.config import prospective_registry

    base, profiles = stack
    base.write_text('profile: "work"\n', encoding="utf-8")
    (profiles / "work.yml").write_text('models:\n  p1:\n    model: "x"\n', encoding="utf-8")

    models, _slots = prospective_registry(
        'profile: "work"\nmodels:\n  gc:\n    model: "y"\n', base_path=base)
    assert sorted(models) == ["gc", "p1"]


def test_prospective_registry_falls_back_to_the_single_file(stack):
    """Стек не собрался — вопрос лучше задать по своему файлу, чем не задать вовсе."""
    from gc_core.config import prospective_registry

    base, _profiles = stack
    text = 'profile: "нет-такого"\nmodels:\n  a:\n    model: "x"\n  b:\n    model: "y"\n'
    base.write_text(text, encoding="utf-8")

    models, _slots = prospective_registry(text, base_path=base)
    assert sorted(models) == ["a", "b"]


def test_prospective_registry_returns_none_on_unparsable_text(stack):
    from gc_core.config import prospective_registry

    base, _profiles = stack
    base.write_text("models: {}\n", encoding="utf-8")
    assert prospective_registry("models: [\n", base_path=base) is None


# --- Негодный ЧУЖОЙ файл стека: «стек не собрался», а не крэш ---

# Две формы, которыми чужой файл выводит стек из строя: не разбирается вовсе и
# разбирается не в словарь. Читаются слои голым `yaml.safe_load`, поэтому первая
# приходила `yaml.YAMLError`, вторая — `AttributeError` на `.get`, и ОБЕ шли мимо
# отката `except (OSError, ValueError)`.
BROKEN = [
    pytest.param("models:\n  b: {\n", id="unparsable"),
    pytest.param("- a\n- b\n", id="not-a-mapping"),
]


def _break_layer(layer: str, profiles: Path, content: str) -> None:
    """Положить негодный документ в указанный слой стека."""
    if layer == "managed":
        path = mcp_paths.managed_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    elif layer == "project":
        gc_dir = Path.cwd() / ".gc"
        gc_dir.mkdir(parents=True, exist_ok=True)
        (gc_dir / "config.yml").write_text(content, encoding="utf-8")
    else:
        (profiles / "work.yml").write_text(content, encoding="utf-8")


def _stack_with_profile(base: Path, profiles: Path) -> str:
    """Стек, резолвимый И целиком, И одной базой: тесты ниже про КЛАСС ошибки, а не
    про содержание вердикта. Возьми базу, нерезолвимую в одиночку (2 модели без
    слотов), — откат к одному файлу дал бы законный отказ, и отличить его от крэша
    стало бы нечем."""
    text = 'profile: "work"\nmodels:\n  a:\n    model: "x"\n'
    base.write_text(text, encoding="utf-8")
    (profiles / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")
    return text


@pytest.mark.parametrize("layer", ["managed", "project", "profile"])
@pytest.mark.parametrize("content", BROKEN)
def test_a_broken_layer_leaves_the_gate_on_the_file_itself(stack, layer, content):
    """Негодный ЧУЖОЙ файл стека — тот же случай, что ненайденный профиль: фаза 2
    отключается, вердикт даёт свой файл.

    Не-модельные секции правятся руками, так что сломанный слой — рабочий
    сценарий. Утечка мимо отката роняла /settings прямо на рендере меню
    (`_models_glyph` → `_models_verdict` ловит только ValueError), а запись
    реестра — вместо нотиса."""
    base, profiles = stack
    text = _stack_with_profile(base, profiles)
    _break_layer(layer, profiles, content)

    validate_config_text_with_sections(text, base_path=base,
        lenient_prebroken=True)


@pytest.mark.parametrize("content", BROKEN)
def test_the_strict_verdict_on_a_broken_foreign_layer_stays_a_value_error(stack, content):
    """Строгий режим не воскрешает старый крэш: негодный чужой файл стека — отказ
    КЛАССОМ ValueError (`StackAssemblyError`), который витрина и точки записи уже
    ловят, а не YAMLError/AttributeError мимо перехватов."""
    base, profiles = stack
    text = _stack_with_profile(base, profiles)
    _break_layer("project", profiles, content)

    with pytest.raises(ValueError):
        validate_config_text_with_sections(text, base_path=base)


@pytest.mark.parametrize("content", BROKEN)
def test_a_broken_layer_does_not_disable_phase_one(stack, content):
    """Откат к одному файлу — не амнистия: схему своего файла судят по-прежнему.
    Сторож от «починки» глушением всего подряд."""
    base, profiles = stack
    _stack_with_profile(base, profiles)
    _break_layer("project", profiles, content)

    with pytest.raises(ValueError, match="схему"):
        validate_config_text_with_sections('models:\n  a:\n    model: 5\n',
                                           base_path=base,
        lenient_prebroken=True)


@pytest.mark.parametrize("layer", ["managed", "project"])
@pytest.mark.parametrize("content", BROKEN)
def test_a_broken_layer_leaves_the_profile_gate_on_the_file_itself(stack, layer, content):
    """Тот же откат у второго входа гейта — записи в файл профиля."""
    base, profiles = stack
    _stack_with_profile(base, profiles)
    _break_layer(layer, profiles, content)

    validate_profile_text_with_sections(
        'slots:\n  default: "a"\n', target=profiles / "work.yml", base_path=base,
        lenient_prebroken=True)


@pytest.mark.parametrize("layer", ["managed", "project", "profile"])
@pytest.mark.parametrize("content", BROKEN)
def test_a_broken_layer_leaves_prospective_registry_on_the_file_itself(stack, layer, content):
    """Вопрос «каким станет реестр» на битом слое отвечает по своему файлу, а не
    падает: у него та же ловушка `except (OSError, ValueError)`."""
    from gc_core.config import prospective_registry

    base, profiles = stack
    text = _stack_with_profile(base, profiles)
    _break_layer(layer, profiles, content)

    models, _slots = prospective_registry(text, base_path=base)
    assert sorted(models) == ["a"]
