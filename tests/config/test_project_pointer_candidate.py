"""Подстановка кандидата(ов) на место любого из трёх слоёв.

`_write_profile_pointer` (`tui/screens/settings.py`) пишет `profile:` не только в
базу, но и в проектный `.gc/config.yml` (§12.3/В1) — а `_merge_layers` умел
подставлять ещё не записанный текст только вместо профиля (`profile_doc`/
`profile_doc_path`). Первое обобщение — `candidate_doc`/`candidate_path`, одна и
та же проверка путей на все три слоя (managed/project/профиль). Второе —
`candidates: dict[путь, документ]` (может быть несколько сразу): переименование
пишет указатель на НОВОЕ имя профиля ДО физического переименования файла, и без
второго кандидата (будущий документ профиля на будущем пути) `resolve_profile`
не находит файл вовсе — стек резолвится БЕЗ профиля, хотя после факта
переименования всё сойдётся. Профильная сторона (одиночный кандидат, поиск
файла-тёзки по ДВУМ корням) остаётся застрахована
`tests/config/test_stacked_validation.py` (не трогается).
"""
from pathlib import Path

import pytest

from config import _merge_layers, _raw_from_text, validate_config_text_with_sections
from config_modules import env_vars
from config_modules.env_vars import EnvVars
from mcp_runtime import paths as mcp_paths


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """База + каталог профилей рядом с ней + проектный каталог, изолированные от
    машины — тот же паттерн, что в test_stacked_validation.py."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path / "gc_home"))
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base = tmp_path / "config.yml"
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    project_config = project_dir / ".gc" / "config.yml"
    return base, profiles, project_config


# --- обобщение 1: candidates={путь: документ} на любом слое ---------------------


def test_merge_layers_substitutes_the_candidate_for_the_project_layer(stack):
    """Кандидат садится на место ПРОЕКТНОГО слоя по совпадению пути — не читая
    диск. На диске лежит РАЗЪЕЗЖАЮЩИЙСЯ указатель: единственный способ отличить
    «подставился кандидат» от «прочитан файл с диска» (или тот вообще пропал бы,
    молча провалившись в фоллбэк на однофайловую фазу)."""
    (stack[1] / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")
    project_config = stack[2]
    project_config.parent.mkdir(parents=True, exist_ok=True)
    project_config.write_text('profile: "нет-такого"\n', encoding="utf-8")

    layers = _merge_layers(
        {"models": {"a": {"model": "x"}, "b": {"model": "y"}}}, str(stack[0]),
        candidates={project_config: _raw_from_text('profile: "work"\n')},
        project_path=str(project_config))

    assert layers.active_name == "work"
    assert sorted(layers.config["models"]) == ["a", "b"]
    assert layers.config["slots"]["default"] == "a"


def test_merge_layers_substitutes_the_candidate_for_the_managed_layer(stack):
    """То же правило, тот же код пути — на managed-оверлее (§ довод владельца:
    одна проверка на все три слоя, а не подстановка, склеенная из двух разных)."""
    managed_path = mcp_paths.managed_config_path()
    managed_path.parent.mkdir(parents=True, exist_ok=True)
    managed_path.write_text('profile: "нет-такого"\n', encoding="utf-8")
    (stack[1] / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")

    layers = _merge_layers(
        {"models": {"a": {"model": "x"}, "b": {"model": "y"}}}, str(stack[0]),
        candidates={managed_path: _raw_from_text('profile: "work"\n')},
        project_path=None)

    assert layers.active_name == "work"
    assert layers.config["slots"]["default"] == "a"


def test_merge_layers_reads_the_project_file_from_disk_for_other_paths(stack):
    """Кандидат — не джокер: не совпал путь, слой читается с диска как обычно."""
    other = stack[2].parent / "other.yml"  # заведомо не путь проектного config.yml
    project_config = stack[2]
    project_config.parent.mkdir(parents=True, exist_ok=True)
    project_config.write_text('profile: "work"\n', encoding="utf-8")
    (stack[1] / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")

    layers = _merge_layers(
        {"models": {"a": {"model": "x"}}}, str(stack[0]),
        candidates={other: _raw_from_text('profile: "не-должен-примениться"\n')},
        project_path=str(project_config))

    assert layers.active_name == "work"     # указатель — реальный диск, не кандидат


# --- обобщение 2: НЕСКОЛЬКО кандидатов + резолв указателя на кандидат-профиль ---


def test_merge_layers_resolves_the_pointer_into_a_not_yet_existing_profile_candidate(stack):
    """Переименование: указатель пишется на новое имя ДО физического rename()
    файла профиля — `resolve_profile("job", roots)` на диске файла `job.yml` не
    находит (он появится только ПОСЛЕ). Кандидат на его будущем пути обязан
    закрыть этот разрыв, иначе стек резолвится вовсе без профиля."""
    base, profiles, _project_config = stack
    new_profile_path = profiles / "job.yml"
    assert not new_profile_path.exists()

    layers = _merge_layers(
        {"profile": "job", "models": {"a": {"model": "x"}, "b": {"model": "y"}}},
        str(base),
        candidates={new_profile_path: _raw_from_text('slots:\n  default: "a"\n')})

    assert layers.active_name == "job"
    assert layers.ref is not None and Path(layers.ref.path) == new_profile_path
    assert layers.config["slots"]["default"] == "a"


def test_multiple_candidates_apply_together(stack):
    """Ровно раскладка переименования, когда указатель живёт в ПРОЕКТНОМ слое:
    указатель на новое имя — один кандидат, сам будущий профиль — второй, оба
    сразу, ни один не существует на диске."""
    base, profiles, project_config = stack
    base.write_text('models:\n  a:\n    model: "x"\n  b:\n    model: "y"\n',
                    encoding="utf-8")
    new_profile_path = profiles / "job.yml"
    assert not project_config.exists() and not new_profile_path.exists()

    layers = _merge_layers(
        _raw_from_text(base.read_text(encoding="utf-8")), str(base),
        candidates={
            project_config: _raw_from_text('profile: "job"\n'),
            new_profile_path: _raw_from_text('slots:\n  default: "a"\n'),
        },
        project_path=str(project_config))

    assert layers.active_name == "job"
    assert layers.config["slots"]["default"] == "a"


def test_candidate_profile_fallback_respects_the_reserved_word_guard(stack):
    """Фолбэк на кандидат не обходит защиту `resolve_profile` от зарезервированных
    имён — та же граница, что и у обычного резолва с диска."""
    base, profiles, _project_config = stack

    with pytest.raises(ValueError, match="зарезервировано"):
        _merge_layers(
            {"profile": "light"}, str(base),
            candidates={profiles / "light.yml": {"slots": {"default": "a"}}})


def test_a_real_file_on_disk_still_wins_over_a_namesake_candidate(stack):
    """Инвариант механизма: кандидат — фолбэк для случая, когда диск НЕ находит
    указатель, а не джокер, обгоняющий реальный резолв. Настоящий файл-тёзка
    лежит в ВЫИГРЫВАЮЩЕМ (проектном) корне с содержимым А; кандидат — на ТО ЖЕ
    ИМЯ файла (`work.yml`, тот же `stem`, что и указатель), но в ПРОИГРЫВАЮЩЕМ
    (базовом) корне, с содержимым Б. `resolve_profile("work", roots)` находит
    указатель на диске (в проектном корне) и никогда не поднимает `ValueError`
    — `_candidate_profile_ref` живёт только в ветке `except ValueError` и сюда
    не вызывается вовсе. Реализация с ИНВЕРТИРОВАННЫМ приоритетом (кандидат
    проверяется раньше диска) подмешала бы Б вместо А — тест обязан упасть на
    такой перестановке (находка ревью: прежняя редакция теста использовала
    кандидата с ДРУГИМ именем файла, который `_candidate_profile_ref` не нашёл
    бы ни при каком порядке проверки, — и потому не отличала правильный
    порядок от перевёрнутого)."""
    base, profiles, _project_config = stack
    project_profiles = Path.cwd() / ".gc" / "profiles"
    project_profiles.mkdir(parents=True)
    (project_profiles / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")
    losing_candidate = profiles / "work.yml"   # базовый корень — проигрывает проектному
    assert not losing_candidate.exists()

    layers = _merge_layers(
        {"profile": "work", "models": {"a": {"model": "x"}, "b": {"model": "y"}}},
        str(base),
        candidates={losing_candidate: {"slots": {"default": "b"}}})

    assert Path(layers.ref.path) == project_profiles / "work.yml"
    assert layers.config["slots"]["default"] == "a"


# --- интеграция: validate_config_text_with_sections(target=) --------------------


def test_the_project_candidate_is_visible_through_validate_config_text_with_sections(stack):
    """Активация через проектный `.gc/config.yml`, которого ещё нет на диске:
    видит РЕАЛЬНЫЕ модели базы (кандидат не подменяет базу собой)."""
    base, profiles, project_config = stack
    base.write_text('models:\n  a:\n    model: "x"\n  b:\n    model: "y"\n',
                    encoding="utf-8")
    (profiles / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")
    assert not project_config.exists()

    validate_config_text_with_sections(
        'profile: "work"\n', base_path=base, target=project_config)


def test_clearing_the_project_pointer_is_refused_when_it_breaks_the_stack(stack):
    """Ревью: указатель — в проектном слое, слот — в профиле (легальная
    раскладка). Снятие указателя рвёт единственный слот (моделей ≥2 в базе) —
    фаза 2 обязана поймать это ДО записи."""
    base, profiles, project_config = stack
    base.write_text('models:\n  a:\n    model: "x"\n  b:\n    model: "y"\n',
                    encoding="utf-8")
    (profiles / "work.yml").write_text('slots:\n  default: "a"\n', encoding="utf-8")
    project_config.parent.mkdir(parents=True, exist_ok=True)
    project_config.write_text('profile: "work"\n', encoding="utf-8")

    with pytest.raises(ValueError, match=r"slots\.default.*≥2"):
        validate_config_text_with_sections(
            'profile: ""\n', base_path=base, target=project_config)
    # синтаксис-то валиден — фаза 1 одного файла эту раскладку пропустила бы не глядя
    validate_config_text_with_sections('profile: ""\n')


def test_an_unresolvable_pointer_written_into_the_project_layer_is_refused(stack):
    """Стек не собрался — ответ по тому, КТО его сломал. Здесь ломает сама запись:
    до неё проектного слоя нет и стек жив, после — указатель называет профиль,
    которого нет ни в одном корне, а нерезолвимый `profile:` на холодном старте —
    строгая ошибка (§3.1). Прежний безусловный откат к однофайловой фазе 2
    пропускал такую запись: файл писался, а следующий старт не поднимался."""
    base, _profiles, project_config = stack
    base.write_text('models:\n  a:\n    model: "x"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="нет-такого"):
        validate_config_text_with_sections(
            'profile: "нет-такого"\n', base_path=base, target=project_config)


def test_an_already_dangling_pointer_still_falls_back_to_the_single_file(stack):
    """Обратная сторона той же развилки: указатель уже висит на несуществующем
    профиле (стек сломан и БЕЗ этой записи) — правку проектного слоя не
    запрещаем, иначе чинить его из /settings стало бы нечем."""
    base, _profiles, project_config = stack
    base.write_text('models:\n  a:\n    model: "x"\n', encoding="utf-8")
    project_config.parent.mkdir(parents=True, exist_ok=True)
    project_config.write_text('profile: "нет-такого"\n', encoding="utf-8")

    validate_config_text_with_sections(
        'profile: "нет-такого"\nlevel: default\n',
        base_path=base, target=project_config)


# --- куда ложится сам указатель: `profile_pointer_write_target` -----------------
#
# Зеркало `resolve_profile_pointer` (кто указатель НАЗЫВАЕТ), и прямых тестов у него
# не было ни одного: разъехавшись с зеркалом, правило отправляет запись в файл,
# который следующий старт всё равно перекроет, — то есть выглядит как молчаливый
# no-op, а профиль остаётся активным.


def test_pointer_write_target_is_the_base_when_the_project_layer_is_silent(stack):
    """Проектного слоя нет вовсе — писать некуда, кроме базы запуска."""
    from config_modules.layers import profile_pointer_write_target
    base, _profiles, _project_config = stack

    assert profile_pointer_write_target(base) == Path(base)


def test_pointer_write_target_is_the_project_file_that_carries_the_pointer(stack):
    """Указатель задаёт проектный `.gc/config.yml` — он же и переписывается.
    Запись в базу тут не меняет ничего: мерж отдаёт победу оверлею, и следующий
    старт поднимает прежнее имя."""
    from config_modules.layers import profile_pointer_write_target
    base, _profiles, project_config = stack
    project_config.parent.mkdir(parents=True, exist_ok=True)
    project_config.write_text('profile: "work"\n', encoding="utf-8")

    assert profile_pointer_write_target(base) == project_config


def test_pointer_write_target_follows_an_empty_project_pointer_too(stack):
    """Решает НАЛИЧИЕ ключа, а не его значение: `profile: ""` в проектном слое —
    такая же победа оверлея по merge-контракту, и активация, записанная в базу,
    не подействовала бы."""
    from config_modules.layers import profile_pointer_write_target
    base, _profiles, project_config = stack
    project_config.parent.mkdir(parents=True, exist_ok=True)
    project_config.write_text('profile: ""\n', encoding="utf-8")

    assert profile_pointer_write_target(base) == project_config


def test_pointer_write_target_ignores_the_managed_layer(stack):
    """Managed-файл правило намеренно не рассматривает: указатель там — не
    пользовательская настройка, и писать в чужой файл ради победы над ним мы не
    станем. Раскладка редкая, а исход честен: запись отвергнет гейт стека."""
    from config_modules.layers import profile_pointer_write_target
    base, _profiles, _project_config = stack
    managed = mcp_paths.managed_config_path()
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_text('profile: "work"\n', encoding="utf-8")

    assert profile_pointer_write_target(base) == Path(base)


def test_pointer_write_target_trusts_the_document_it_was_given(stack):
    """`project_doc` — уже прочитанный документ слоя (кэш прохода секции
    «Профили»): диск тогда не перечитывается вовсе, иначе футер ходил бы в файл
    на каждый кадр."""
    from config_modules.layers import profile_pointer_write_target
    base, _profiles, project_config = stack
    project_config.parent.mkdir(parents=True, exist_ok=True)
    project_config.write_text('profile: "work"\n', encoding="utf-8")

    assert profile_pointer_write_target(base, project_doc={}) == Path(base)
