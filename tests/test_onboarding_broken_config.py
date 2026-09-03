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
    from config import load_config_ex

    try:
        load_config_ex(base_path)
    except ValueError as exc:
        return exc
    pytest.fail("ожидался ValueError от load_config_ex")


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    """cwd изолирован в подпапке tmp_path — profile_roots() не должен видеть
    ни реальный `<repo>/.gc/profiles`, ни профили соседних тестов."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


# --- Сценарий A: сломанный ключ в АКТИВНОМ профиле --------------------------


def test_broken_slot_in_active_profile_names_key_and_profile_file(isolated_cwd, tmp_path):
    """slots.default в профиле ссылается на несуществующий алиас — объяснение
    называет ключ и ПОЛНЫЙ путь файла профиля (не «в файле выше»), помечает
    профиль активным в карте участников и советует запустить без профиля."""
    from onboarding import explain_broken_config

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
    assert 'запустить без профиля, ничего не потеряв:  gc-ua --profile ""' in text
    # Откат к шаблону — всегда последним и всегда через gc-cli (--reset-config
    # есть только у него).
    assert text.rstrip().endswith(
        "откатить конфиг к шаблону (старый уйдёт в бэкап):  gc-cli --reset-config"
    )
    # Порядок советов: починка → без профиля → откат.
    assert (
        text.index("поправить slots.default")
        < text.index('--profile ""')
        < text.index("--reset-config")
    )


def test_relative_config_path_shown_as_absolute(isolated_cwd, tmp_path, monkeypatch):
    """Ревью: `--config .\\config.yml` — заголовок и карта участников печатали
    базу ОТНОСИТЕЛЬНЫМ путём, как его набрали в командной строке, — рядом с уже
    абсолютным путём профиля (`_overlay_paths` резолвит его сама). Относительный
    путь бессмыслен вне момента запуска (другой cwd, копипаста в переписку) —
    все пути в объяснении обязаны быть абсолютными."""
    from onboarding import explain_broken_config

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


# --- Сценарий B: битый YAML базы --------------------------------------------


def test_invalid_base_yaml_falls_back_to_naming_the_file(tmp_path):
    """Ошибка не про ключ (битый YAML) — сопоставлять нечего, совет становится
    «поправить файл: <путь>», путь берётся из текста ошибки (`_layer_document`
    называет файл первым). «Без профиля» не предлагается — профиля нет нигде."""
    from onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text("this is: : : not [valid yaml }\n", encoding="utf-8")

    exc = _load_and_catch(str(base))
    text = explain_broken_config(str(base), exc)

    assert "невалидный YAML" in text
    assert f"поправить файл:  {base}" in text
    assert '--profile ""' not in text
    assert "gc-cli --reset-config" in text


# --- Сценарий C: профиль не найден по указателю -----------------------------


def test_profile_not_found_suggests_running_without_profile(isolated_cwd, tmp_path):
    """Профиль по указателю не резолвится — ключей не найти (текст на русском,
    суспект-лист латиницей), совет откатывается к «поправить файл», НО «без
    профиля» всё равно предлагается: пустой `--profile ""` — самый прямой фикс
    именно для этой поломки, и указатель у неё есть (просто не резолвится)."""
    from onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text(_BACKEND + "profile: work\n", encoding="utf-8")

    exc = _load_and_catch(str(base))
    text = explain_broken_config(str(base), exc, program="gc-ua")

    assert 'профиль "work" не найден' in text
    assert f"поправить файл:  {base}" in text
    assert 'запустить без профиля, ничего не потеряв:  gc-ua --profile ""' in text
    # Ненайденный профиль не резолвится в файл — карта участников не может
    # пометить несуществующий путь «активным» (маркер есть только у РЕЗОЛВНУВШЕГОСЯ
    # профиля, см. config_origin._overlay_paths).
    assert "(активен)" not in text


# --- Сценарий D: два разных ключа в РАЗНЫХ файлах ---------------------------


def test_two_broken_keys_in_different_files_get_one_line_each(isolated_cwd, tmp_path):
    """slots.max сломан в БАЗЕ, slots.default — в ПРОФИЛЕ: две строки «поправить»,
    каждая — свой файл. Ни одна не должна поймать оба ключа в одну кучу, и
    «models» не должен ложно приплестись (сегмент про slots.max не начинается со
    слова models, хотя и заканчивается им — общая фраза «merged-реестре models»
    есть в ОБОИХ сообщениях)."""
    from onboarding import explain_broken_config

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


# --- Сценарий E: два ключа в ОДНОМ файле — одна строка ----------------------


def test_two_broken_keys_in_same_file_share_one_line(isolated_cwd, tmp_path):
    """slots.default И slots.light сломаны в одном профиле — список «поправить»
    растёт по числу ФАЙЛОВ, а не ключей: одна строка с обоими именами."""
    from onboarding import explain_broken_config

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
    from pydantic import ValidationError
    from onboarding import explain_broken_config

    base = tmp_path / "base.yml"
    base.write_text(
        _BACKEND + "this_field_does_not_exist_in_schema: 123\n", encoding="utf-8",
    )

    exc = _load_and_catch(str(base))
    assert isinstance(exc, ValidationError)
    text = explain_broken_config(str(base), exc)

    assert "конфиг не проходит схему:" in text
    assert "validation error for AppConfig" in text
