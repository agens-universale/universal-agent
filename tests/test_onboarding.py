"""Онбординг первого запуска: детект ненастроенного конфига + текст-гайд + запись."""
import pytest
import yaml

import config
import onboarding
from bundled_assets import bundled_root
from config_modules import env_vars
from model_router import ModelRouter


def _default_yaml() -> dict:
    return yaml.safe_load(
        (bundled_root() / "config.default.yml").read_text(encoding="utf-8")
    )


def _isolated_default(tmp_path, monkeypatch) -> dict:
    """config.default.yml реальным загрузчиком, изолировав машинный и project-local overlay."""
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.setattr(config, "_project_local_config_path", lambda: None)
    return config.load_config(str(bundled_root() / "config.default.yml"))


# --- базовый детект -------------------------------------------------------

def test_default_config_is_unconfigured(tmp_path, monkeypatch):
    cfg = _isolated_default(tmp_path, monkeypatch)
    assert onboarding.is_unconfigured(cfg) is True


def test_configured_when_url_token_model_set():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer real-token"},
        "model": "gigacode_agent_v1.6_rc",
    }
    assert onboarding.is_unconfigured(cfg) is False


def test_unconfigured_if_token_still_placeholder():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    assert onboarding.is_unconfigured(cfg) is True


# --- auth: токен ИЛИ agent_auth ------------------------------------------

def test_agent_auth_satisfies_auth_without_token():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "model": "gigacode_agent_v1.6_rc",
        "agent_auth": {"auth_endpoint": "https://auth.local/token"},
    }
    assert onboarding.is_unconfigured(cfg) is False


def test_agent_auth_set_drops_token_from_guide():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "model": "gigacode_agent_v1.6_rc",
        "agent_auth": {"auth_endpoint": "https://auth.local/token"},
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
    }
    text = onboarding.setup_guide("/home/u/.gc/config.yml", cfg)
    assert "Authorization" not in text
    assert "настроен" in text


def test_no_token_and_no_complete_agent_auth_is_configured():
    """Нет токен-заголовка и нет полноценного agent_auth, но и нет шаблонного
    плейсхолдера → конфиг настроен. Локальный прокси / эндпоинт без auth сам ставит
    авторизацию; гейт ловит только незаполненный сентинел, а не намеренное отсутствие.
    `agent_auth: {}` — то, во что config.py:277 разворачивает отсутствие секции."""
    cfg = {
        "completions_url": "http://127.0.0.1:8014/v1/chat/completions",
        "model": "MiniMaxAI/MiniMax-M2.7",
        "agent_auth": {},
    }
    assert onboarding.is_unconfigured(cfg) is False


# --- реестр закрывает ТОЛЬКО требование модели ---------------------------

def test_registry_closes_model_requirement():
    """Спека §11: непустой merged-реестр — рабочая модель, легаси-плейсхолдер
    `agent.model` при нём не блокирует (мастер его и не пишет с волны B)."""
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer real-token"},
        "model": onboarding.MODEL_SENTINEL,
        "models": {"flash": {"model": "google/gemini-2.5-flash"}},
    }
    assert onboarding.is_unconfigured(cfg) is False


def test_registry_does_not_close_endpoint_requirement():
    """Реестр НЕ закрывает адрес: записи мастера наследуют url/токен от плоского
    `backend` (решение 10), поэтому плейсхолдерный endpoint при живом реестре —
    это «нечем работать», а не «настроено». Иначе мастер отчитывался бы «✓ Готово»,
    а следующий старт больше не предложил бы настройку."""
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": "Bearer real-token"},
        "model": onboarding.MODEL_SENTINEL,
        "models": {"flash": {"model": "google/gemini-2.5-flash"}},
    }
    assert onboarding.is_unconfigured(cfg) is True


def test_registry_does_not_close_auth_requirement():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": onboarding.MODEL_SENTINEL,
        "models": {"flash": {"model": "google/gemini-2.5-flash"}},
    }
    assert onboarding.is_unconfigured(cfg) is True


def test_flat_model_without_endpoint_is_unconfigured():
    """Тот же страж в плоском мире — симметрия детекта (§11)."""
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": "Bearer real-token"},
        "model": "qwen3-coder",
    }
    assert onboarding.is_unconfigured(cfg) is True


def test_self_sufficient_registry_closes_endpoint_and_auth_in_chat_completions():
    """Конфиг, который пишет наша же `ModelEntryForm` (решение 7): у записей СВОИ
    адрес и токен, плоский backend остался шаблонным. В `chat_completions` он
    полностью рабочий (Router отдаёт url и заголовок из записи) — предлагать мастер
    при каждом старте было бы ложной тревогой."""
    cfg = {
        "backend_api_mode": "chat_completions",
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": onboarding.MODEL_SENTINEL,
        "models": {"flash": {"model": "g", "url": "https://x/v1",
                             "authorization_header": "Bearer sk"}},
    }
    assert onboarding.is_unconfigured(cfg) is False


def test_self_sufficient_registry_does_not_close_endpoint_in_responses():
    """Тот же конфиг в `responses`: per-model `url`/`token` там мертвы (§4), запрос
    ушёл бы на плоский плейсхолдер — «не настроено»."""
    cfg = {
        "backend_api_mode": "responses",
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": onboarding.MODEL_SENTINEL,
        "models": {"flash": {"model": "g", "url": "https://x/v1",
                             "authorization_header": "Bearer sk"}},
    }
    assert onboarding.is_unconfigured(cfg) is True


def test_registry_with_one_entry_without_url_does_not_close_endpoint():
    """Проверка по ВСЕМ записям, а не по резолвнутой: намеренно грубее — засчитываем
    «настроено», только когда самодостаточны все, тогда ошибиться в опасную сторону
    (объявить готовым конфиг, который уйдёт на `your-backend:PORT`) невозможно."""
    cfg = {
        "backend_api_mode": "chat_completions",
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": onboarding.MODEL_SENTINEL,
        "models": {
            "flash": {"model": "g", "url": "https://x/v1", "authorization_header": "Bearer sk"},
            "naked": {"model": "n"},          # наследует всё от плоского backend
        },
    }
    assert onboarding.is_unconfigured(cfg) is True


def test_registry_with_own_url_but_no_token_does_not_close_auth():
    """Адрес записей закрыт, авторизация — нет: плоский заголовок остался шаблонным,
    а записи своего не несут."""
    cfg = {
        "backend_api_mode": "chat_completions",
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": onboarding.MODEL_SENTINEL,
        "models": {"flash": {"model": "g", "url": "https://x/v1"}},
    }
    assert onboarding.is_unconfigured(cfg) is True


def test_registry_of_local_servers_without_auth_closes_auth():
    """`token: ""` («сервер не требует авторизации») — ЗАДАННЫЙ токен: адрес и токен
    записи задаются парой, и локальная раздача без ключа пишется именно так. Её
    `authorization_header` пустой, поэтому проверка по truthiness объявила бы
    полностью настроенный локальный сетап незаполненным шаблоном и гнала бы человека
    в мастер на каждом старте."""
    cfg = {
        "backend_api_mode": "chat_completions",
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": onboarding.MODEL_SENTINEL,
        "models": {"glm-local": {"model": "g", "url": "http://127.0.0.1:8014/v1",
                                 "authorization_header": ""}},
    }
    assert onboarding.is_unconfigured(cfg) is False


def _loaded_with_registry(tmp_path, monkeypatch, extra: str, replace=()) -> dict:
    """config.yml из пакетного шаблона + `extra` в хвосте, прочитанный НАСТОЯЩИМ
    загрузчиком (слои изолированы).

    Детект судит СНИМОК записи (`config._serialize_model_entry`), а не сырой YAML:
    формы «своего подключения» разложены там по разным ключам (`url` /
    `backend_overrides.completions_url`), и синтетический словарь того, что реально
    приезжает в детект, не доказывает."""
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.setattr(config, "_project_local_config_path", lambda: None)
    monkeypatch.chdir(tmp_path)
    text = (bundled_root() / "config.default.yml").read_text(encoding="utf-8")
    for old, new in replace:
        text = text.replace(old, new)
    path = tmp_path / "config.yml"
    path.write_text(text + extra, encoding="utf-8")
    return config.load_config(str(path))


def test_registry_entry_in_backend_form_closes_endpoint_and_auth(tmp_path, monkeypatch):
    """Вторая документированная форма своего подключения (docs/configuration.md,
    «Профили и уровни моделей»): `backend.completions_url` +
    `backend.chat_completion_headers.Authorization` — равноправная с `url`/`token`.
    Router её маршрутизирует, значит конфиг рабочий; судить о «своём подключении»
    по одним плоским ключам записи значит гнать в мастер на КАЖДОМ старте человека,
    заполнившего реестр по документации."""
    cfg = _loaded_with_registry(tmp_path, monkeypatch, """
models:
  flash:
    model: g
    backend:
      completions_url: https://x/v1/chat/completions
      chat_completion_headers:
        Authorization: Bearer sk-real
slots:
  default: flash
""")
    assert onboarding.is_unconfigured(cfg) is False
    resolved = ModelRouter(cfg, level_provider=lambda: "default").resolve("main")
    assert resolved.url == "https://x/v1/chat/completions"


def test_registry_entry_with_empty_url_does_not_close_endpoint(tmp_path, monkeypatch):
    """`url: ""` — «своего адреса нет, пара наследуется» (`connection_parts`), а не
    «адрес задан пустой строкой»: так его резолвит рантайм. При плейсхолдерном
    плоском адресе запрос уйдёт на `your-backend:PORT` — детект обязан прочитать
    запись так же, иначе объявляет готовым конфиг, которому работать нечем."""
    cfg = _loaded_with_registry(tmp_path, monkeypatch, """
models:
  flash:
    model: g
    url: ""
slots:
  default: flash
""", replace=((f'Authorization: "{onboarding.TOKEN_SENTINEL}"',
               'Authorization: "Bearer sk-real"'),))
    assert onboarding.is_unconfigured(cfg) is True
    resolved = ModelRouter(cfg, level_provider=lambda: "default").resolve("main")
    assert resolved.url == onboarding.URL_SENTINEL


def test_setup_guide_on_registry_config_does_not_demand_model():
    """Рабочий реестровый конфиг: гайд не требует `agent.model` (иначе `--init-config`
    без TTY и стоп-гайд `gc-ua` печатали бы «не заполнено» настроенному человеку)."""
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer real-token"},
        "model": onboarding.MODEL_SENTINEL,
        "models": {"flash": {"model": "google/gemini-2.5-flash"}},
    }
    text = onboarding.setup_guide("/p", cfg)
    assert "agent.model" not in text
    assert "настроен" in text


def test_setup_guide_preset_note_is_about_flat_key_only():
    """Ремарка «модель по умолчанию уже задана» — про ПЛОСКИЙ ключ: при реестре с
    плейсхолдером она печатала бы «agent.model = <ВПИШИТЕ_МОДЕЛЬ>»."""
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": onboarding.MODEL_SENTINEL,
        "models": {"flash": {"model": "google/gemini-2.5-flash"}},
    }
    text = onboarding.setup_guide("/p", cfg)
    assert onboarding.MODEL_SENTINEL not in text
    assert "Модель по умолчанию уже задана" not in text


# --- adaptive model ------------------------------------------------------

def test_model_sentinel_is_pending():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer real-token"},
        "model": onboarding.MODEL_SENTINEL,
    }
    text = onboarding.setup_guide("/p", cfg)
    assert "agent.model" in text


def test_filled_model_not_in_guide():
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    text = onboarding.setup_guide("/p", cfg)
    assert "имя модели" not in text   # модель не в списке полей к заполнению


# --- ремарка про preset-модель -------------------------------------------

def test_setup_guide_notes_preset_model_when_pending():
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    text = onboarding.setup_guide("/p", cfg)
    assert "Модель по умолчанию уже задана" in text
    assert "gigacode_agent_v1.6_rc" in text


def test_setup_guide_no_preset_model_note_when_model_pending():
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": onboarding.MODEL_SENTINEL,
    }
    text = onboarding.setup_guide("/p", cfg)
    assert "Модель по умолчанию уже задана" not in text
    assert "имя модели" in text   # модель в списке pending


# --- позитивное сообщение, когда всё покрыто -----------------------------

def test_setup_guide_positive_when_all_covered():
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer real-token"},
        "model": "gigacode_agent_v1.6_rc",
    }
    text = onboarding.setup_guide("/home/u/.gc/config.yml", cfg)
    assert "настроен" in text
    assert "можно запускать" in text
    assert "•" not in text


# --- страж сентинелов vs config.default.yml ------------------------------

def test_sentinels_match_default_yaml():
    raw = _default_yaml()
    assert raw["backend"]["completions_url"] == onboarding.URL_SENTINEL
    assert (
        raw["backend"]["chat_completion_headers"]["Authorization"]
        == onboarding.TOKEN_SENTINEL
    )
    assert raw["agent"]["model"] == onboarding.MODEL_SENTINEL


def test_setup_guide_lists_pending_fields_and_path(tmp_path, monkeypatch):
    """Путь в подсказке — абсолютный: относительный (или платформенно-чужой,
    как POSIX-стиль на Windows) бессмыслен вне момента запуска."""
    cfg = _isolated_default(tmp_path, monkeypatch)
    config_path = str(tmp_path / ".gc" / "config.yml")
    text = onboarding.setup_guide(config_path, cfg)
    assert config_path in text
    assert "backend.completions_url" in text
    assert "Authorization" in text
    assert "имя модели" in text   # модель тоже не задана в дефолте → выбирается в мастере


def test_setup_guide_shows_a_relative_path_as_absolute(tmp_path, monkeypatch):
    """Тот же вопрос, что и у `explain_broken_config` (соседняя функция, тот же
    способ печати пути) — `--config .\\config.yml` не должен всплывать
    относительным: он бессмыслен вне момента запуска этой команды."""
    import os
    cfg = _isolated_default(tmp_path, monkeypatch)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    config_abs = cwd / "config.yml"
    relative = os.path.join(".", "config.yml")

    text = onboarding.setup_guide(relative, cfg)

    assert relative not in text
    assert str(config_abs) in text


def test_setup_guide_offers_init_config_helper_with_program():
    """pending-подсказка называет команду помощника с именем бинаря (program)."""
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    assert "gc-cli --init-config" in onboarding.setup_guide("/p", cfg, program="gc-cli")
    assert "gc-ua --init-config" in onboarding.setup_guide("/p", cfg, program="gc-ua")


def test_setup_guide_marks_config_created_when_just_created():
    """just_created=True → явно сообщает о создании файла; False → «Конфигурация:»."""
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    created = onboarding.setup_guide("/p", cfg, just_created=True)
    assert "Создан файл конфигурации" in created
    existing = onboarding.setup_guide("/p", cfg, just_created=False)
    assert "Создан файл конфигурации" not in existing
    assert "Конфигурация:" in existing


def test_setup_guide_mentions_gc_mcp():
    """pending-подсказка упоминает gc-mcp для установки MCP-серверов."""
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    assert "gc-mcp install" in onboarding.setup_guide("/p", cfg)


# --- алиас записи реестра из провайдерского id ----------------------------

def test_default_alias_for_last_path_segment():
    assert onboarding.default_alias_for("qwen/qwen-2.5-flash") == "qwen-2.5-flash"


def test_default_alias_for_short_name_as_is():
    assert onboarding.default_alias_for("glm-4-plus") == "glm-4-plus"


def test_default_alias_for_strips_options_suffix():
    assert onboarding.default_alias_for("qwen/qwen-2.5-flash:free") == "qwen-2.5-flash"


def test_default_alias_for_reserved_word_gets_suffix():
    """Провайдерский id, совпавший с именем уровня, — единственный источник
    невалидного YAML на ровном месте (§4: алиас не может называться light/default/
    max/auto), поэтому имя разводится суффиксом, а не отдаётся валидатору."""
    assert onboarding.default_alias_for("light") == "light-model"
    assert onboarding.default_alias_for("max") == "max-model"


def test_default_alias_for_empty_id():
    assert onboarding.default_alias_for("") == "model"


# --- запись собранного мастером (волна B, спека §11) ----------------------

def _wizard_config(tmp_path, monkeypatch):
    """Настоящий config.yml из пакетного шаблона + изоляция слоёв загрузки.
    Мастер пишет в НАСТОЯЩИЙ файл (запись реестра валидирует результат целиком) —
    синтетический огрызок YAML этот путь не прошёл бы."""
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.setattr(config, "_project_local_config_path", lambda: None)
    monkeypatch.chdir(tmp_path)   # проектный корень профилей — не реальный репозиторий
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        (bundled_root() / "config.default.yml").read_text(encoding="utf-8"), encoding="utf-8")
    return cfg_path


def test_write_wizard_values_writes_connection_flat_and_model_to_registry(tmp_path, monkeypatch):
    """Решение 10: подключение — плоскими ключами (база наследования + источник
    пробы), модель — записью реестра со слотом default. Запись сравнивается
    ЦЕЛИКОМ: per-model url/token мастер не пишет (§4 — в responses они мертвы)."""
    cfg_path = _wizard_config(tmp_path, monkeypatch)
    onboarding.write_wizard_values(cfg_path, {
        "completions_url": "https://x/v1/chat/completions",
        "Authorization": "Bearer sk-1",
        "model": "qwen/qwen3-coder",
    })
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["backend"]["completions_url"] == "https://x/v1/chat/completions"
    assert raw["backend"]["chat_completion_headers"]["Authorization"] == "Bearer sk-1"
    assert raw["models"] == {"qwen3-coder": {"model": "qwen/qwen3-coder"}}
    assert raw["slots"]["default"] == "qwen3-coder"


def test_write_wizard_values_does_not_touch_agent_model(tmp_path, monkeypatch):
    """Легаси-ключ мастер больше не пишет — остаётся плейсхолдером шаблона."""
    cfg_path = _wizard_config(tmp_path, monkeypatch)
    onboarding.write_wizard_values(cfg_path, {
        "completions_url": "https://x/v1/chat/completions",
        "model": "qwen3-coder",
    })
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["agent"]["model"] == onboarding.MODEL_SENTINEL


def test_write_wizard_values_writes_proxy_as_before(tmp_path, monkeypatch):
    cfg_path = _wizard_config(tmp_path, monkeypatch)
    onboarding.write_wizard_values(cfg_path, {"proxy": "socks5h://proxy.example:1080"})
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["backend"]["proxy"] == "socks5h://proxy.example:1080"


def test_write_wizard_values_without_model_leaves_registry_absent(tmp_path, monkeypatch):
    """Модель мастер не собрал — секции реестра не заводятся вовсе (пустой якорь
    `models: {}` был бы записью «реестр включён», которой пользователь не просил)."""
    cfg_path = _wizard_config(tmp_path, monkeypatch)
    onboarding.write_wizard_values(cfg_path, {"completions_url": "https://x/v1"})
    text = cfg_path.read_text(encoding="utf-8")
    assert yaml.safe_load(text).get("models") is None
    assert "\nmodels:" not in text


def test_write_wizard_values_rerun_keeps_pricing_of_same_entry(tmp_path, monkeypatch):
    """Повторный прогон мастера — upsert той же записи по merge-контракту: тариф,
    дописанный редактором секции «Модели», переживает второй проход."""
    from pathlib import Path

    from config import _default_config_path
    from config_modules.registry_write import upsert_model

    cfg_path = _wizard_config(tmp_path, monkeypatch)
    onboarding.write_wizard_values(cfg_path, {
        "completions_url": "https://x/v1/chat/completions", "model": "qwen3-coder"})
    template = Path(_default_config_path()).read_text(encoding="utf-8")
    upsert_model(cfg_path, "qwen3-coder", {"pricing": {"input": 12, "output": 48}}, template)

    onboarding.write_wizard_values(cfg_path, {
        "Authorization": "Bearer sk-2", "model": "qwen3-coder"})
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["models"]["qwen3-coder"]["model"] == "qwen3-coder"
    assert raw["models"]["qwen3-coder"]["pricing"] == {"input": 12, "output": 48}
    assert raw["slots"]["default"] == "qwen3-coder"


def test_write_wizard_values_does_not_hijack_a_taken_alias_with_its_own_pair(
        tmp_path, monkeypatch):
    """Имя алиаса, уже занятое ДРУГОЙ моделью, брать нельзя: `upsert_model` мержит
    в существующую запись («ключа нет — сохранить как был»), поэтому её собственная
    пара пережила бы прогон и увела бы слот на СТАРЫЙ хост со СТАРЫМ токеном — при
    том что человек только что ввёл новые. Занятыми считаются и алиасы из файла, а
    не только запланированные этим прогоном."""
    from pathlib import Path

    from config import _default_config_path
    from config_modules.registry_write import apply_registry_ops

    cfg_path = _wizard_config(tmp_path, monkeypatch)
    template = Path(_default_config_path()).read_text(encoding="utf-8")
    apply_registry_ops(cfg_path, [
        ("upsert_model", "qwen-plus", {"model": "vendor-a/qwen-plus",
                                       "url": "https://a/v1/chat/completions",
                                       "token": "A"}),
        ("write_slot", "default", "qwen-plus"),
    ], template)

    onboarding.write_wizard_values(cfg_path, {
        "completions_url": "https://b/v1/chat/completions",
        "Authorization": "Bearer B",
        "model": "vendor-b/qwen-plus",
    })
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["models"]["qwen-plus"] == {"model": "vendor-a/qwen-plus",
                                          "url": "https://a/v1/chat/completions",
                                          "token": "A"}
    # Новая запись — без своей пары: работает через только что записанное общее
    # подключение, ради которого мастер и запускали.
    assert raw["models"]["qwen-plus-2"] == {"model": "vendor-b/qwen-plus"}
    assert raw["slots"]["default"] == "qwen-plus-2"


def test_write_wizard_values_leaves_no_token_when_the_registry_write_fails(
        tmp_path, monkeypatch):
    """Отказ записи — «конфиг не записан» и для СЕКРЕТА тоже: реестр пишется первым,
    поэтому его провал не оставляет на диске плоский токен от несостоявшегося
    прогона (мастер при этом честно говорит, что ничего не записал)."""
    import config_modules.registry_write as rw

    cfg_path = _wizard_config(tmp_path, monkeypatch)

    def _refuse(*_args, **_kwargs):
        raise rw.RegistryWriteError("запись реестра отказала")

    monkeypatch.setattr(rw, "apply_registry_ops", _refuse)
    with pytest.raises(ValueError):
        onboarding.write_wizard_values(cfg_path, {
            "completions_url": "https://x/v1/chat/completions",
            "Authorization": "Bearer sk-secret",
            "model": "qwen/qwen3-coder",
        })
    text = cfg_path.read_text(encoding="utf-8")
    assert "sk-secret" not in text
    assert onboarding.TOKEN_SENTINEL in text


def test_write_wizard_values_survives_a_missing_template(tmp_path, monkeypatch):
    """Пакетный шаблон недоступен — мастер не падает трейсбеком: вызывающие ловят
    только `ValueError`, а якорь секции писатель синтезирует сам, и пустой шаблон
    ему не мешает."""
    cfg_path = _wizard_config(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "_default_config_path",
                        lambda: str(tmp_path / "no-such-template.yml"))
    onboarding.write_wizard_values(cfg_path, {
        "completions_url": "https://x/v1/chat/completions",
        "Authorization": "Bearer sk-1",
        "model": "qwen/qwen3-coder",
    })
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["models"] == {"qwen3-coder": {"model": "qwen/qwen3-coder"}}
    assert raw["slots"]["default"] == "qwen3-coder"
    assert raw["backend"]["completions_url"] == "https://x/v1/chat/completions"


def test_write_wizard_values_result_resolves_in_both_api_modes(tmp_path, monkeypatch):
    """Страж решения 10: конфиг после мастера рабочий в ОБОИХ режимах. В `responses`
    от записи действует только провайдерский id (§4) — он и приходит из реестра, а
    адрес/токен берутся из плоского подключения; в `chat_completions` тот же резолв
    отдаёт полный комплект. Проверяется Router'ом — единственным владельцем вопроса
    «какой моделью идёт вызов» (§6/§7)."""
    import onboarding_config_writer as ocw

    cfg_path = _wizard_config(tmp_path, monkeypatch)
    onboarding.write_wizard_values(cfg_path, {
        "completions_url": "https://x/v1/chat/completions",
        "Authorization": "Bearer sk-1",
        "model": "qwen/qwen3-coder",
    })
    for api_mode in ("chat_completions", "responses"):
        text = ocw.set_scalar_at_path(
            cfg_path.read_text(encoding="utf-8"), "backend.api_mode", api_mode)
        cfg_path.write_text(text, encoding="utf-8")
        cfg = config.load_config(str(cfg_path))
        assert onboarding.is_unconfigured(cfg) is False
        resolved = ModelRouter(cfg, level_provider=lambda: "default").resolve("main")
        assert resolved.model == "qwen/qwen3-coder"
        assert resolved.url == "https://x/v1/chat/completions"   # унаследован от backend
        assert resolved.authorization is None                    # наследуется заголовок


# --- шаг «light/max»: его доля в записи (волна B, Task 8) ---

def test_write_wizard_values_never_touches_the_profile_pointer(tmp_path):
    """Мастер профилями не управляет (спека §2): указатель в файле остаётся
    таким, каким был, при любом проходе."""
    from pathlib import Path
    import onboarding
    from config import _default_config_path
    cfg = tmp_path / "config.yml"
    cfg.write_text(Path(_default_config_path()).read_text(encoding="utf-8"),
                   encoding="utf-8")
    import yaml
    text = cfg.read_text(encoding="utf-8").replace('profile: ""', 'profile: work')
    cfg.write_text(text, encoding="utf-8")
    onboarding.write_wizard_values(str(cfg), {
        "completions_url": "http://h/v1/chat/completions",
        "Authorization": "Bearer t",
        onboarding.MODEL_SLOT_KEYS["default"]: "m",
    })
    assert (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("profile") == "work"


def test_write_wizard_values_writes_level_models_with_slots(tmp_path, monkeypatch):
    """Шаг (в): заполненные уровни — записи реестра + свои слоты, ОДНОЙ транзакцией
    с моделью по умолчанию (по отдельности вторая запись без слота нерезолвима, §4).
    Per-model url/token не пишутся и здесь — наследуются от плоского подключения."""
    cfg_path = _wizard_config(tmp_path, monkeypatch)
    onboarding.write_wizard_values(cfg_path, {
        "completions_url": "https://x/v1/chat/completions",
        "model": "qwen/qwen3-coder",
        "model_light": "qwen/qwen3-flash",
        "model_max": "qwen/qwen3-max",
    })
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["models"] == {
        "qwen3-coder": {"model": "qwen/qwen3-coder"},
        "qwen3-flash": {"model": "qwen/qwen3-flash"},
        "qwen3-max": {"model": "qwen/qwen3-max"},
    }
    assert raw["slots"] == {"default": "qwen3-coder", "light": "qwen3-flash",
                            "max": "qwen3-max"}


def test_write_wizard_values_skipped_levels_write_nothing(tmp_path, monkeypatch):
    """Шаг (в) пропущен — ни записи, ни слота: пустой слот увёл бы уровень на
    несуществующий алиас, а лишняя запись появилась бы в реестре без спроса."""
    cfg_path = _wizard_config(tmp_path, monkeypatch)
    onboarding.write_wizard_values(cfg_path, {
        "completions_url": "https://x/v1/chat/completions", "model": "m"})
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["models"] == {"m": {"model": "m"}}
    assert raw["slots"] == {"default": "m"}


def test_write_wizard_values_same_model_on_two_levels_writes_one_entry(tmp_path, monkeypatch):
    """Один и тот же id на двух уровнях — одна запись и два слота на неё: близнецы
    в реестре только запутывали бы."""
    cfg_path = _wizard_config(tmp_path, monkeypatch)
    onboarding.write_wizard_values(cfg_path, {
        "completions_url": "https://x/v1/chat/completions",
        "model": "glm-4-plus", "model_light": "glm-4-plus"})
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["models"] == {"glm-4-plus": {"model": "glm-4-plus"}}
    assert raw["slots"] == {"default": "glm-4-plus", "light": "glm-4-plus"}


def test_write_wizard_values_alias_collision_keeps_both_models(tmp_path, monkeypatch):
    """Два РАЗНЫХ id с одинаковым хвостом дают одно имя алиаса: без разведения
    вторая запись затёрла бы первую, и оба слота увели бы уровни на одну модель —
    выбор человека потерялся бы молча."""
    cfg_path = _wizard_config(tmp_path, monkeypatch)
    onboarding.write_wizard_values(cfg_path, {
        "completions_url": "https://x/v1/chat/completions",
        "model": "vendor-a/qwen-plus", "model_max": "vendor-b/qwen-plus"})
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["models"] == {
        "qwen-plus": {"model": "vendor-a/qwen-plus"},
        "qwen-plus-2": {"model": "vendor-b/qwen-plus"},
    }
    assert raw["slots"] == {"default": "qwen-plus", "max": "qwen-plus-2"}


def test_write_wizard_values_reuses_the_alias_of_an_already_planned_model(tmp_path, monkeypatch):
    """Разведённое имя — тоже имя ЭТОЙ модели: тот же id на третьем уровне обязан
    получить уже выданный алиас, а не третью запись-близнеца (находка ревью Task 8:
    поиск шёл только по первому расчёту `default_alias_for`)."""
    cfg_path = _wizard_config(tmp_path, monkeypatch)
    onboarding.write_wizard_values(cfg_path, {
        "completions_url": "https://x/v1/chat/completions",
        "model": "vendor-a/qwen-plus",
        "model_light": "vendor-b/qwen-plus",
        "model_max": "vendor-b/qwen-plus"})
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["models"] == {
        "qwen-plus": {"model": "vendor-a/qwen-plus"},
        "qwen-plus-2": {"model": "vendor-b/qwen-plus"},
    }
    assert raw["slots"] == {"default": "qwen-plus", "light": "qwen-plus-2",
                            "max": "qwen-plus-2"}


def test_write_wizard_values_reuses_an_existing_alias_named_by_a_human(tmp_path, monkeypatch):
    """Повторный прогон мастера поверх готового реестра НЕ плодит близнецов.

    Имя за той же моделью переиспользуется — так обещает докстринг, — но поиск шёл
    только среди запланированного этой записью. Записи, названные человеком
    (`fast`, `brain`), мастер не узнавал: обещание держалось, лишь когда имя
    совпадало с рассчитанным `default_alias_for`.

    Цена ошибки высокая: чтобы поменять протухший токен, достаточно запустить
    мастер и пройти шаги Enter'ом — шаг «Модель» открывается с уже отмеченной
    действующей моделью. Реестр вырастал вдвое, слоты переезжали на новые записи
    без своей пары «адрес и токен», а прежние записи со своим адресом оставались
    осиротевшими: уровень, ходивший на локальный сервер, начинал уходить на
    облачный endpoint с id локальной модели."""
    from config_modules.registry_write import apply_registry_ops

    cfg_path = _wizard_config(tmp_path, monkeypatch)
    apply_registry_ops(cfg_path, [
        ("upsert_model", "fast", {"model": "qwen/qwen3-8b"}),
        ("upsert_model", "brain", {"model": "qwen/qwen3-235b"}),
        ("write_slot", "default", "brain"),
    ], "")

    onboarding.write_wizard_values(cfg_path, {
        "completions_url": "https://x/v1/chat/completions",
        "model": "qwen/qwen3-235b", "model_light": "qwen/qwen3-8b"})

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert raw["models"] == {
        "fast": {"model": "qwen/qwen3-8b"},
        "brain": {"model": "qwen/qwen3-235b"},
    }
    assert raw["slots"] == {"default": "brain", "light": "fast"}


def test_effective_model_reads_registry_not_the_flat_key():
    """Волна B пишет реестр, а плоский ключ остаётся значением ШАБЛОНА: на
    настроенном конфиге по нему мастер видел бы чужую модель."""
    cfg = {"models": {"fast": {"model": "qwen/qwen3-8b"}},
           "slots": {"default": "fast"}, "model": "MiniMax-M2.7"}
    assert onboarding.effective_model(cfg) == "qwen/qwen3-8b"


def test_effective_model_resolves_single_entry_without_a_slot():
    """Реестр из одной записи резолвится без явного слота — так же, как в рантайме."""
    cfg = {"models": {"fast": {"model": "qwen/qwen3-8b"}}, "slots": {},
           "model": "MiniMax-M2.7"}
    assert onboarding.effective_model(cfg) == "qwen/qwen3-8b"


def test_effective_model_falls_back_to_the_legacy_key():
    cfg = {"models": {}, "slots": {}, "model": "vllm/Qwen3-Coder"}
    assert onboarding.effective_model(cfg) == "vllm/Qwen3-Coder"


def test_effective_model_survives_a_slot_pointing_nowhere():
    """Резолв бросает ValueError на битом слоте — мастер обязан открываться на
    любом конфиге, поэтому падение переходит в фоллбэк, а не наружу."""
    cfg = {"models": {"fast": {"model": "qwen/qwen3-8b"}},
           "slots": {"default": "нет-такой"}, "model": "vllm/Qwen3-Coder"}
    assert onboarding.effective_model(cfg) == "vllm/Qwen3-Coder"


def test_effective_model_is_empty_on_the_template_sentinel():
    cfg = {"models": {}, "slots": {}, "model": onboarding.MODEL_SENTINEL}
    assert onboarding.effective_model(cfg) == ""


def test_assigned_level_model_reports_only_explicit_slots():
    """Незаданный `max` работает моделью `default`, но «назначен» он не был:
    написать против него её имя значило бы закрепить выдуманное."""
    cfg = {"models": {"fast": {"model": "qwen/qwen3-8b"},
                      "brain": {"model": "qwen/qwen3-235b"}},
           "slots": {"default": "brain", "light": "fast"}}
    assert onboarding.assigned_level_model(cfg, "light") == "qwen/qwen3-8b"
    assert onboarding.assigned_level_model(cfg, "max") == ""


def test_assigned_level_model_falls_back_to_the_alias_itself():
    """Запись без ключа `model` — законна: id равен имени записи (спека §4)."""
    cfg = {"models": {"gigacode": {}}, "slots": {"light": "gigacode"}}
    assert onboarding.assigned_level_model(cfg, "light") == "gigacode"


def test_assigned_level_model_survives_a_broken_slot():
    cfg = {"models": {}, "slots": {"light": "нет-такой"}}
    assert onboarding.assigned_level_model(cfg, "light") == "нет-такой"
