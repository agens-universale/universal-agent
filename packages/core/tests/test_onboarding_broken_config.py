"""`onboarding.explain_broken_config` — человекочитаемое объяснение вместо
трейсбека, когда `load_config_ex()` не смог собрать конфиг.

Исключения берутся НАСТОЯЩИЕ (реальный `load_config_ex` на реальном сломанном
файле), а не сконструированные вручную — иначе тест проверял бы придуманный
текст ошибки, а не то, что реально прилетает из загрузчика.
"""
import os

import pytest

_BACKEND = (
    'backend:\n'
    '  api_mode: chat_completions\n'
    '  completions_url: "http://127.0.0.1:8014/v1/chat/completions"\n'
)


def _load_and_catch(base_path: str) -> ValueError:
    from gc_core.config import load_config_ex

    try:
        load_config_ex(base_path)
    except ValueError as exc:
        return exc
    pytest.fail("ожидался ValueError от load_config_ex")


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    """cwd изолирован в подпапке tmp_path — profile_roots() не должен видеть
    ни реальный `<repo>/.gc/profiles`, ни профили соседних тестов; проектный
    слой (`<cwd>/.gc/config.yml`) тем самым тоже пуст. Managed-слой — на tmp
    (владелец пути — `mcp_runtime.paths`, у него его спрашивают и загрузчик, и
    карта участников): иначе в стек лезет настоящий `~/.gc/config.local.yml`
    машины, и «сломан только профиль» перестаёт быть правдой.

    База тестов файла ОБЪЯВЛЯЕТСЯ дефолтным пользовательским конфигом (M9):
    советы-команды короткой формы (`--reset-config`, `--profile ""` без
    `--config`) законны только для него — на чужом пути они целятся не в тот
    файл, и объяснение обязано нести `--config` сессии."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr("gc_core.config_modules.env_vars.managed_config_path",
                        lambda: tmp_path / "config.local.yml")
    monkeypatch.setattr("gc_core.config.user_config_path", lambda: tmp_path / "base.yml")
    return cwd


# --- Сценарий A: сломанный ключ в АКТИВНОМ профиле --------------------------


def test_broken_slot_in_active_profile_names_key_and_profile_file(isolated_cwd, tmp_path):
    """slots.default в профиле ссылается на несуществующий алиас — объяснение
    называет ключ и ПОЛНЫЙ путь файла профиля (не «в файле выше»), помечает
    профиль активным в карте участников и советует запустить без профиля."""
    from gc_core.onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text(
        _BACKEND + "models:\n  flash: {}\n" "profile: work\n", encoding="utf-8",
    )
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    profile = profiles_dir / "work.yml"
    profile.write_text("slots:\n  default: strong\n", encoding="utf-8")

    exc = _load_and_catch(str(base))
    text = explain_broken_config(str(base), exc, program="gc-ua")

    assert text.startswith(f"[gc] Конфиг не загружается: {base}")
    assert 'slots.default: алиас "strong" не найден в merged-реестре models' in text
    # Карта участников: база + активный профиль, путь профиля указан целиком.
    assert str(base) in text
    assert f"{profile}   (активен)" in text
    # Совет чинить называет И ключ, И полный путь файла профиля в ОДНОЙ строке.
    assert f"поправить slots.default в:  {profile}" in text
    # Запуск без профиля — своим бинарём (program), не жёстко "gc-cli".
    assert text.rstrip().endswith(
        'запустить без профиля, ничего не потеряв:  gc-ua --profile ""')
    # Порядок советов: починка → без профиля.
    assert text.index("поправить slots.default") < text.index('--profile ""')
    # Откат (`gc-cli --reset-config`) подменяет БАЗУ — сломан профиль, база ни при
    # чём, и откат ей не поможет, а модели/MCP/голос потеряет (ревью).
    assert "--reset-config" not in text


def test_relative_config_path_shown_as_absolute(isolated_cwd, tmp_path, monkeypatch):
    """Ревью: `--config .\\config.yml` — заголовок и карта участников печатали
    базу ОТНОСИТЕЛЬНЫМ путём, как его набрали в командной строке, — рядом с уже
    абсолютным путём профиля (`_overlay_paths` резолвит его сама). Относительный
    путь бессмыслен вне момента запуска (другой cwd, копипаста в переписку) —
    все пути в объяснении обязаны быть абсолютными."""
    from gc_core.onboarding import explain_broken_config

    base_abs = isolated_cwd / "config.yml"
    base_abs.write_text(
        _BACKEND + 'models:\n  gpt:\n    url: "http://host"\n' 'profile: "work"\n',
        encoding="utf-8",
    )
    profiles_dir = isolated_cwd / "profiles"
    profiles_dir.mkdir()
    profile_abs = profiles_dir / "work.yml"
    profile_abs.write_text("slots:\n  default: gpt\n", encoding="utf-8")

    relative = os.path.join(".", "config.yml")
    exc = _load_and_catch(relative)
    assert "не задан токен" in str(exc)   # предпосылка: ошибка про адрес/токен, не про схему
    text = explain_broken_config(relative, exc, program="gc-ua")

    assert relative not in text
    assert f"[gc] Конфиг не загружается: {base_abs}" in text
    assert f"    база      {base_abs}" in text
    assert f"{profile_abs}   (активен)" in text
    assert f"поправить файл:  {base_abs}" in text


# --- Сценарий A2: профиль задан флагом, а не указателем ----------------------


def test_profile_override_drives_the_map_and_the_advice(isolated_cwd, tmp_path):
    """Ревью (H1): сессия шла с `--profile broken`, а объяснение резолвило
    профиль указателем из файла — его там нет. Карта «Участвуют» и совет
    строятся по профилю, переданному сессией: файл профиля назван, помечен
    активным ОТ ФЛАГА, совет чинить — в нём, «без профиля» предлагается, а
    откат базы — нет (она ни при чём)."""
    from gc_core.config import load_config_ex
    from gc_core.onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text(_BACKEND + "models:\n  flash: {}\n", encoding="utf-8")
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    profile = profiles_dir / "broken.yml"
    profile.write_text("slots:\n  default: strong\n", encoding="utf-8")

    try:
        load_config_ex(str(base), profile_override="broken")
    except ValueError as exc:
        text = explain_broken_config(str(base), exc, program="gc-ua", profile_override="broken")
    else:
        pytest.fail("ожидался ValueError от load_config_ex")

    assert f"{profile}   (активен: --profile)" in text
    assert f"поправить slots.default в:  {profile}" in text
    assert f"в:  {base}" not in text
    assert 'запустить без профиля, ничего не потеряв:  gc-ua --profile ""' in text
    assert "--reset-config" not in text


def test_profile_override_that_names_no_file_blames_the_flag(isolated_cwd, tmp_path):
    """Ревью (H1): опечатка в `--profile` — советовали «поправить файл: <база>»
    и откат, которые ни при чём. Имя пришло из флага, и файла с ним нет —
    сказать это прямо; чинить — имя во флаге либо запуск без профиля."""
    from gc_core.config import load_config_ex
    from gc_core.onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text(_BACKEND + "models:\n  flash: {}\n", encoding="utf-8")

    try:
        load_config_ex(str(base), profile_override="wrok")
    except ValueError as exc:
        text = explain_broken_config(str(base), exc, program="gc-ua", profile_override="wrok")
    else:
        pytest.fail("ожидался ValueError от load_config_ex")

    assert 'профиль "wrok" не найден' in text
    assert 'профиль "wrok" задан флагом --profile' in text
    assert "поправить" not in text
    assert 'запустить без профиля, ничего не потеряв:  gc-ua --profile ""' in text
    assert "--reset-config" not in text
    assert "(активен" not in text


def test_schema_error_on_the_pointer_is_not_a_missing_profile(isolated_cwd, tmp_path):
    """Ревью: `profile: 123` в базе (ошибка схемы — не строка) при живом и
    исправном `work.yml` и запуске `--profile work`. Виноват ключ базы, а совет
    объявлял несуществующим профиль, который на месте, и не называл ни одного
    файла: ветка про флаг срабатывала по одному лишь факту флага, потому что
    `key_origins` под override выбрасывает ключ `profile` и виновников не
    остаётся."""
    from gc_core.config import load_config_ex
    from gc_core.onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text(_BACKEND + "models:\n  flash: {}\n" "profile: 123\n", encoding="utf-8")
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    profile = profiles_dir / "work.yml"
    profile.write_text("slots:\n  default: flash\n", encoding="utf-8")

    try:
        load_config_ex(str(base), profile_override="work")
    except ValueError as exc:
        text = explain_broken_config(str(base), exc, program="gc-ua", profile_override="work")
    else:
        pytest.fail("ожидался ValueError от load_config_ex")

    assert f"поправить файл:  {base}" in text
    assert "такого профиля нет" not in text
    assert f"{profile}   (активен: --profile)" in text


def test_schema_error_explanation_does_not_leak_the_token(isolated_cwd, tmp_path):
    """Ревью MR #80 (L28, класс): объяснение сломанного конфига строило текст из
    `str(ValidationError)` — pydantic-дамп несёт `input_value=<repr записи>`, то
    есть токен из лежащего на диске конфига. Координата и причина обязаны
    остаться (по ним же ищутся виновники), сам секрет — нет."""
    from gc_core.config import load_config_ex
    from gc_core.onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text(
        _BACKEND
        + "models:\n"
        "  flash:\n"
        '    model: "google/gemini-2.5-flash"\n'
        '    url: "http://own/v1/chat/completions"\n'
        '    token: "sk-MY-OWN-SECRET"\n'
        "    backend:\n"
        "      chat_completion_headers:\n"
        '        Authorization: "Bearer ANOTHER"\n'
        "slots:\n"
        '  default: "flash"\n',
        encoding="utf-8")

    try:
        load_config_ex(str(base))
    except ValueError as exc:
        text = explain_broken_config(str(base), exc, program="gc-ua")
    else:
        pytest.fail("ожидался ValueError от load_config_ex")

    assert "sk-MY-OWN-SECRET" not in text
    assert "input_value" not in text
    assert "models.flash" in text


def test_empty_profile_override_means_no_profile_at_all(isolated_cwd, tmp_path):
    """`--profile ""` — профильный слой снят: указатель в файле инертен, и совет
    «запустить без профиля» бессмыслен (человек уже так запустил); карта без
    профиля, откат уместен — сломана сама база."""
    from gc_core.config import load_config_ex
    from gc_core.onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text(_BACKEND + "models:\n  flash: {}\n" "slots:\n  max: ghost\n" "profile: work\n",
                    encoding="utf-8")
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "work.yml").write_text("slots:\n  default: flash\n", encoding="utf-8")

    try:
        load_config_ex(str(base), profile_override="")
    except ValueError as exc:
        text = explain_broken_config(str(base), exc, program="gc-ua", profile_override="")
    else:
        pytest.fail("ожидался ValueError от load_config_ex")

    assert f"поправить slots.max в:  {base}" in text
    assert "(активен" not in text
    assert '--profile ""' not in text
    assert "gc-cli --reset-config" in text


# --- Сценарий B: битый YAML базы --------------------------------------------


def test_invalid_base_yaml_falls_back_to_naming_the_file(tmp_path, monkeypatch):
    """Ошибка не про ключ (битый YAML) — сопоставлять нечего, совет становится
    «поправить файл: <путь>», путь берётся из текста ошибки (`_layer_document`
    называет файл первым). «Без профиля» не предлагается — профиля нет нигде.
    База — дефолтный пользовательский файл (M9): короткая форма
    `gc-cli --reset-config` законна только для него."""
    from gc_core.onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text("this is: : : not [valid yaml }\n", encoding="utf-8")
    monkeypatch.setattr("gc_core.config.user_config_path", lambda: base)

    exc = _load_and_catch(str(base))
    text = explain_broken_config(str(base), exc)

    assert "невалидный YAML" in text
    assert f"поправить файл:  {base}" in text
    assert '--profile ""' not in text
    assert "gc-cli --reset-config" in text


def test_undecodable_base_is_explained_not_traced(tmp_path, monkeypatch):
    """Файл НА МЕСТЕ, но не декодируется (сохранён в UTF-16, «блокнотом») — это
    тот же «существует, но не читается», что и права/каталог/сетевой диск, только
    класс исключения другой (`UnicodeDecodeError` — подкласс `ValueError`, мимо
    `except OSError`). Точки входа его ловят, а вот САМО объяснение падало
    трейсбеком, читая тот же файл ещё раз: сперва резолвом указателя, потом
    картой происхождения ключей. База — дефолтный пользовательский файл (M9)."""
    from gc_core.onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text("backend:\n  api_mode: chat_completions\n", encoding="utf-16")
    monkeypatch.setattr("gc_core.config.user_config_path", lambda: base)

    exc = _load_and_catch(str(base))
    text = explain_broken_config(str(base), exc)

    assert f"поправить файл:  {base}" in text
    assert "gc-cli --reset-config" in text


def test_undecodable_project_overlay_names_the_overlay_not_the_base(isolated_cwd, tmp_path):
    """Не декодируется ПРОЕКТНЫЙ слой — виноват он, а совет называл базу и
    предлагал откат: откат подменил бы базу (потеряв модели, MCP и голос), а
    нечитаемый оверлей остался бы на месте."""
    from gc_core.onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text(_BACKEND + "models:\n  flash: {}\n", encoding="utf-8")
    project = isolated_cwd / ".gc" / "config.yml"
    project.parent.mkdir(parents=True)
    project.write_text("agent:\n  model: x\n", encoding="utf-16")

    exc = _load_and_catch(str(base))
    text = explain_broken_config(str(base), exc)

    assert f"поправить файл:  {project}" in text
    assert "gc-cli --reset-config" not in text


# --- Сценарий C: профиль не найден по указателю -----------------------------


def test_profile_not_found_suggests_running_without_profile(isolated_cwd, tmp_path):
    """Профиль по указателю не резолвится — совет называет ключ указателя и файл,
    где он лежит (здесь — база), и «без профиля»: пустой `--profile ""` — самый
    прямой фикс именно для этой поломки, а указатель у неё есть (просто не
    резолвится). Откат предлагается: указатель в базе, и откат его снимет."""
    from gc_core.onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text(_BACKEND + "profile: work\n", encoding="utf-8")

    exc = _load_and_catch(str(base))
    text = explain_broken_config(str(base), exc, program="gc-ua")

    assert 'профиль "work" не найден' in text
    assert f"поправить profile в:  {base}" in text
    assert 'запустить без профиля, ничего не потеряв:  gc-ua --profile ""' in text
    assert "gc-cli --reset-config" in text
    # Ненайденный профиль не резолвится в файл — карта участников не может
    # пометить несуществующий путь «активным» (маркер есть только у РЕЗОЛВНУВШЕГОСЯ
    # профиля, см. config_origin._overlay_paths).
    assert "(активен)" not in text


def test_dangling_pointer_in_project_overlay_names_the_overlay_not_the_base(
        isolated_cwd, tmp_path):
    """Ревью (H2б): указатель на удалённый профиль лежит в проектном
    `.gc/config.yml` — объяснение называло базу («поправить файл: <база>») и
    советовало `--reset-config`, который проектный слой не трогает. Виновник —
    файл с указателем; откат не предлагается: база ни при чём."""
    from gc_core.onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text(_BACKEND + "models:\n  flash: {}\n", encoding="utf-8")
    (isolated_cwd / ".gc").mkdir()
    project = isolated_cwd / ".gc" / "config.yml"
    project.write_text('profile: "gone"\n', encoding="utf-8")

    exc = _load_and_catch(str(base))
    text = explain_broken_config(str(base), exc, program="gc-ua")

    assert 'профиль "gone" не найден' in text
    assert f"поправить profile в:  {project}" in text
    assert f"поправить файл:  {base}" not in text
    assert 'запустить без профиля, ничего не потеряв:  gc-ua --profile ""' in text
    assert "--reset-config" not in text


# --- Сценарий C2: ошибка схемы в оверлее ------------------------------------


def test_schema_error_in_project_overlay_names_the_overlay(isolated_cwd, tmp_path):
    """Ревью (H2а): ошибка схемы (pydantic) в проектном оверлее — координата из
    текста не читалась, объяснение советовало «поправить файл: <база>» и
    `--reset-config`, который оверлей не трогает. Координата у pydantic есть
    (`loc`) — по ней виновник тот файл, что задал ключ."""
    from gc_core.onboarding import explain_broken_config
    from pydantic import ValidationError

    base = tmp_path / "base.yml"
    base.write_text(_BACKEND + "models:\n  flash: {}\n", encoding="utf-8")
    (isolated_cwd / ".gc").mkdir()
    project = isolated_cwd / ".gc" / "config.yml"
    project.write_text("agent:\n  max_steps: many\n", encoding="utf-8")

    exc = _load_and_catch(str(base))
    # контракт загрузки: отказ схемы завёрнут (дамп с input_value не летает),
    # сырой ValidationError — в __cause__, по нему explain берёт координаты
    assert not isinstance(exc, ValidationError)
    assert isinstance(exc.__cause__, ValidationError)
    text = explain_broken_config(str(base), exc, program="gc-ua")

    assert "конфиг не проходит схему:" in text
    assert f"поправить agent.max_steps в:  {project}" in text
    assert f"поправить файл:  {base}" not in text
    assert "--reset-config" not in text


def test_schema_error_inside_a_list_in_the_overlay_names_the_overlay(isolated_cwd, tmp_path):
    """Ревью: координата pydantic заходит В СПИСОК (`endpoint_presets.0.name`), и
    склеенная как есть она не резолвится ни в одном документе — все слои выглядят
    пустыми, виноватой назначается база, а совет становится
    `gc-cli --reset-config`. Откат снёс бы модели, MCP и голос, оставив сломанный
    оверлей на месте, — то есть человека ведут ломать целое ради чужой поломки.

    Координата обрезается до первого нестрокового элемента: `endpoint_presets`
    резолвится, и слой находится."""
    from gc_core.onboarding import explain_broken_config
    from pydantic import ValidationError

    base = tmp_path / "base.yml"
    base.write_text(_BACKEND + "models:\n  flash: {}\n", encoding="utf-8")
    (isolated_cwd / ".gc").mkdir()
    project = isolated_cwd / ".gc" / "config.yml"
    project.write_text("endpoint_presets:\n  - name: 1\n    url: u\n", encoding="utf-8")

    exc = _load_and_catch(str(base))
    # контракт загрузки: отказ схемы завёрнут (дамп с input_value не летает),
    # сырой ValidationError — в __cause__, по нему explain берёт координаты
    assert not isinstance(exc, ValidationError)
    assert isinstance(exc.__cause__, ValidationError)
    text = explain_broken_config(str(base), exc, program="gc-ua")

    assert f"поправить endpoint_presets в:  {project}" in text
    assert str(base) not in text.split("Что сделать:")[1]
    assert "--reset-config" not in text


def test_schema_error_in_the_base_still_offers_the_reset(tmp_path, monkeypatch):
    """Отрицательный сторож к правилу отката: тот же дефект схемы в самой базе —
    ключ на базе, откат уместен."""
    from gc_core.onboarding import explain_broken_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("gc_core.config_modules.env_vars.managed_config_path",
                        lambda: tmp_path / "config.local.yml")
    base = tmp_path / "base.yml"
    base.write_text(_BACKEND + "agent:\n  max_steps: many\n", encoding="utf-8")
    monkeypatch.setattr("gc_core.config.user_config_path", lambda: base)

    exc = _load_and_catch(str(base))
    text = explain_broken_config(str(base), exc)

    assert f"поправить agent.max_steps в:  {base}" in text
    assert "gc-cli --reset-config" in text


# --- M9: советы-команды несут `--config` сессии ------------------------------


def test_advice_carries_the_config_flag_of_the_session(isolated_cwd, tmp_path, monkeypatch):
    """Ревью (M9): сессия шла с `--config <путь>`, а советы печатались
    фиксированными строками — `gc-cli --reset-config` сбросил бы ЧУЖОЙ исправный
    `~/.gc/config.yml`, оставив сломанный на месте, а `--profile ""` запустил бы
    не тот конфиг. Оба совета обязаны нести `--config` сессии; путь — в кавычках
    (пробелы в путях)."""
    from gc_core.onboarding import explain_broken_config

    monkeypatch.setattr("gc_core.config.user_config_path",
                        lambda: tmp_path / "home-config.yml")
    based = tmp_path / "with space"
    based.mkdir()
    base = based / "cfg.yml"
    base.write_text(
        _BACKEND + "models:\n  flash: {}\n" "slots:\n  max: ghost\n" "profile: work\n",
        encoding="utf-8",
    )
    (based / "profiles").mkdir()
    (based / "profiles" / "work.yml").write_text("slots:\n  default: flash\n",
                                                 encoding="utf-8")

    exc = _load_and_catch(str(base))
    text = explain_broken_config(str(base), exc, program="gc-ua")

    assert (f'запустить без профиля, ничего не потеряв:  '
            f'gc-ua --config "{base}" --profile ""') in text
    assert (f'откатить конфиг к шаблону (старый уйдёт в бэкап):  '
            f'gc-cli --config "{base}" --reset-config') in text
    # Короткие формы без --config целятся в другой файл — их быть не должно.
    assert 'gc-ua --profile ""' not in text.replace(f'gc-ua --config "{base}"', "")
    assert "gc-cli --reset-config" not in text.replace(f'gc-cli --config "{base}"', "")


def test_reset_is_not_offered_for_the_bundled_template(tmp_path, monkeypatch):
    """Ревью (M9): `--reset-config` на пакетном шаблоне отказывает («его не
    правим») — советовать сброс для этого пути значит слать человека в тупик."""
    from gc_core.onboarding import explain_broken_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("gc_core.config_modules.env_vars.managed_config_path",
                        lambda: tmp_path / "config.local.yml")
    base = tmp_path / "config.default.yml"
    base.write_text(_BACKEND + "agent:\n  max_steps: many\n", encoding="utf-8")

    exc = _load_and_catch(str(base))
    monkeypatch.setattr("gc_core.config._default_config_path", lambda: str(base))
    monkeypatch.setattr("gc_core.config.user_config_path", lambda: tmp_path / "elsewhere.yml")
    text = explain_broken_config(str(base), exc)

    assert f"поправить agent.max_steps в:  {base}" in text
    assert "--reset-config" not in text


# --- Сценарий D: два разных ключа в РАЗНЫХ файлах ---------------------------


def test_two_broken_keys_in_different_files_get_one_line_each(isolated_cwd, tmp_path):
    """slots.max сломан в БАЗЕ, slots.default — в ПРОФИЛЕ: две строки «поправить»,
    каждая — свой файл. Ни одна не должна поймать оба ключа в одну кучу, и
    «models» не должен ложно приплестись (сегмент про slots.max не начинается со
    слова models, хотя и заканчивается им — общая фраза «merged-реестре models»
    есть в ОБОИХ сообщениях)."""
    from gc_core.onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text(
        _BACKEND + "models:\n  flash: {}\n" "slots:\n  max: ghost2\n" "profile: work\n",
        encoding="utf-8",
    )
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    profile = profiles_dir / "work.yml"
    profile.write_text("slots:\n  default: ghost1\n", encoding="utf-8")

    exc = _load_and_catch(str(base))
    text = explain_broken_config(str(base), exc)

    assert f"поправить slots.default в:  {profile}" in text
    assert f"поправить slots.max в:  {base}" in text
    assert "models в:" not in text  # секция models сама по себе не сломана
    # Порядок — как в закрытом списке подозреваемых (slots.default раньше slots.max).
    assert text.index("slots.default") < text.index("slots.max")
    # Один из виновников — база: откат уместен и идёт последним.
    assert text.rstrip().endswith(
        "откатить конфиг к шаблону (старый уйдёт в бэкап):  gc-cli --reset-config")
    assert text.index('--profile ""') < text.index("--reset-config")


def test_assign_keys_get_their_own_fix_line(isolated_cwd, tmp_path):
    """Ревью (H2в, решение владельца): ключи привязок не участвовали — смешанная
    ошибка «slots.max в базе + assign.* в профиле» теряла строку про привязки.
    Координата — полная (`assign.subagents.subagent_worker`), файл — тот, где
    привязка лежит."""
    from gc_core.onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text(
        _BACKEND + "models:\n  flash: {}\n" "slots:\n  max: ghost\n" "profile: work\n",
        encoding="utf-8",
    )
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    profile = profiles_dir / "work.yml"
    profile.write_text(
        "assign:\n  summary: nothere\n  subagents:\n    subagent_worker: nope\n",
        encoding="utf-8",
    )

    exc = _load_and_catch(str(base))
    text = explain_broken_config(str(base), exc)

    assert f"поправить slots.max в:  {base}" in text
    assert f"поправить assign.summary, assign.subagents.subagent_worker в:  {profile}" in text


# --- Сценарий E: два ключа в ОДНОМ файле — одна строка ----------------------


def test_two_broken_keys_in_same_file_share_one_line(isolated_cwd, tmp_path):
    """slots.default И slots.light сломаны в одном профиле — список «поправить»
    растёт по числу ФАЙЛОВ, а не ключей: одна строка с обоими именами."""
    from gc_core.onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text(
        _BACKEND + "models:\n  flash: {}\n" "profile: work\n", encoding="utf-8",
    )
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    profile = profiles_dir / "work.yml"
    profile.write_text("slots:\n  default: ghost1\n  light: ghost3\n", encoding="utf-8")

    exc = _load_and_catch(str(base))
    text = explain_broken_config(str(base), exc)

    assert f"поправить slots.default, slots.light в:  {profile}" in text
    assert text.count("поправить ") == 1


# --- pydantic ValidationError → читаемый текст ------------------------------


def test_pydantic_validation_error_gets_readable_prefix(tmp_path):
    """`pydantic.ValidationError` — подкласс ValueError, но текст многострочный и
    служебный («1 validation error for AppConfig\\n...»). В объяснении он должен
    получить тот же читаемый вид, что уже есть на путях записи
    (layers.py::_app_config_from_raw): «конфиг не проходит схему: …»."""
    from gc_core.onboarding import explain_broken_config
    from pydantic import ValidationError

    base = tmp_path / "base.yml"
    base.write_text(
        _BACKEND + "this_field_does_not_exist_in_schema: 123\n", encoding="utf-8",
    )

    exc = _load_and_catch(str(base))
    assert not isinstance(exc, ValidationError)
    assert isinstance(exc.__cause__, ValidationError)
    text = explain_broken_config(str(base), exc)

    assert "конфиг не проходит схему:" in text
    assert "validation error for AppConfig" not in text   # дампа больше нет
    assert "input_value" not in text
