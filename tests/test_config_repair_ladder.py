"""Лестница загрузки «чинилок» (`--init-config`): на какой ступени приземляемся
и что при этом уцелело у человека.

Одна лестница на обе поверхности (gc-cli и gc-ua): полная загрузка → загрузка
БЕЗ ПРОФИЛЯ → пакетный шаблон. Проверяется на НАСТОЯЩИХ файлах (load_config_ex
не мокается) — раскладка взята со стенда живого смока: рабочие адрес, токен и
реестр в базе, нерезолвимый слот в профиле.
"""
import pytest

from config_modules import env_vars
from config_modules.env_vars import EnvVars

# Раскладка стенда смока: всё рабочее, кроме профиля.
_BASE_OK = (
    'profile: "work"\n'
    "backend:\n"
    "  api_mode: chat_completions\n"
    '  completions_url: "https://openrouter.ai/api/v1/chat/completions"\n'
    "  chat_completion_headers:\n"
    '    Authorization: "Bearer sk-user-token"\n'
    "models:\n"
    "  fast:\n"
    '    model: "qwen/qwen3-8b"\n'
)
_USER_URL = "https://openrouter.ai/api/v1/chat/completions"
_USER_TOKEN = "Bearer sk-user-token"


@pytest.fixture
def stand(tmp_path, monkeypatch):
    """Изолированный ~/.gc + чистый cwd: иначе в мерж лезут managed и проектный
    слои машины, где тест гоняется, и «сломан только профиль» перестаёт быть
    правдой."""
    home = tmp_path / "gc"
    (home / "profiles").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=home))
    monkeypatch.chdir(project)
    return home


def test_healthy_config_lands_on_full_stage_without_message(stand):
    """Здоровый конфиг — ступень 1: ни деградации, ни сообщения человеку."""
    (stand / "config.yml").write_text(_BASE_OK, encoding="utf-8")
    (stand / "profiles" / "work.yml").write_text('slots:\n  default: "fast"\n', encoding="utf-8")

    import onboarding
    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_FULL
    assert loaded.message is None
    assert loaded.config["profile"] == "work"          # профиль остался в силе
    assert loaded.meta.active_name == "work"


def test_broken_profile_only_drops_profile_and_keeps_user_connection(stand):
    """Сломан ТОЛЬКО профиль (слот на несуществующий алиас — раскладка смока):
    снимаем профиль, но НЕ данные человека.

    Уронить этот случай на пакетный шаблон значило бы увести мастер на
    `your-backend:PORT` при полностью рабочем токене — ровно то, что видел
    человек на стенде."""
    (stand / "config.yml").write_text(_BASE_OK, encoding="utf-8")
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8")

    import onboarding
    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_WITHOUT_PROFILE
    cfg = loaded.config
    assert cfg["completions_url"] == _USER_URL
    assert cfg["completions_url"] != onboarding.URL_SENTINEL
    assert cfg["chat_completion_headers"]["Authorization"] == _USER_TOKEN
    assert list(cfg["models"]) == ["fast"]
    assert cfg["profile"] == ""                        # профиль — единственное, что снято
    # Человеку сказано, что именно отброшено, а не просто «конфиг не читается».
    assert "work" in loaded.message


def test_broken_base_falls_back_to_template_usable_by_runtime(stand):
    """Сломана база — ступень 3, и её результат пригоден рантайму: пустой `{}`
    здесь ронял `resolve_threads_dir` сразу следом (ловилось как Critical)."""
    (stand / "config.yml").write_text("this is: : : not [valid yaml }\n", encoding="utf-8")

    import onboarding
    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_TEMPLATE
    assert loaded.config["completions_url"] == onboarding.URL_SENTINEL

    from thread_store import resolve_threads_dir
    assert resolve_threads_dir(loaded.config)
    assert loaded.message


def test_broken_base_and_broken_profile_still_lands_on_template(stand):
    """Ступень 2 не годится (база не читается сама по себе) — падаем на шаблон,
    а не отдаём наверх исключение."""
    (stand / "config.yml").write_text("this is: : : not [valid yaml }\n", encoding="utf-8")
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8")

    import onboarding
    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_TEMPLATE
    assert loaded.config["completions_url"] == onboarding.URL_SENTINEL
