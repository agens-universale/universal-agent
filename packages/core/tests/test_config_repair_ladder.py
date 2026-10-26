"""Лестница загрузки «чинилок» (`--init-config`): на какой ступени приземляемся
и что при этом уцелело у человека.

Одна лестница на обе поверхности (gc-cli и gc-ua): полная загрузка → загрузка
БЕЗ ПРОФИЛЯ → пакетный шаблон. Проверяется на НАСТОЯЩИХ файлах (load_settings
не мокается) — раскладка взята со стенда живого смока: рабочие адрес, токен и
реестр в базе, нерезолвимый слот в профиле.
"""

import pytest
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars

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
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "fast"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_FULL
    assert loaded.message is None
    assert loaded.config.runtime.active_profile == "work"  # профиль в силе
    assert loaded.meta.active_name == "work"


def test_broken_profile_only_drops_profile_and_keeps_user_connection(stand):
    """Сломан ТОЛЬКО профиль (слот на несуществующий алиас — раскладка смока):
    снимаем профиль, но НЕ данные человека.

    Уронить этот случай на пакетный шаблон значило бы увести мастер на
    `your-backend:PORT` при полностью рабочем токене — ровно то, что видел
    человек на стенде."""
    (stand / "config.yml").write_text(_BASE_OK, encoding="utf-8")
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_WITHOUT_PROFILE
    cfg = loaded.config
    backend = cfg.settings.backend
    assert backend.completions_url == _USER_URL
    assert backend.completions_url != onboarding.URL_SENTINEL
    assert backend.chat_completion_headers["Authorization"] == _USER_TOKEN
    assert list(cfg.settings.models) == ["fast"]
    assert (
        cfg.runtime.active_profile == ""
    )  # профиль — единственное, что снято
    # Человеку сказано, что именно отброшено, а не просто «конфиг не читается».
    assert "work" in loaded.message


def test_without_profile_stage_names_the_dropped_profile_and_offers_deactivation(
    stand,
):
    """Ступень «без профиля» отдаёт признак для финала мастера: указатель лежит
    в базе, снять его мастер может — и имя снятого профиля, чтобы поверхности
    называли его, не разбирая текст сообщения."""
    (stand / "config.yml").write_text(_BASE_OK, encoding="utf-8")
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_WITHOUT_PROFILE
    assert loaded.profile == "work"
    assert loaded.deactivate_profile is True


def test_pointer_in_managed_layer_is_named_and_no_deactivation_is_promised(
    stand,
):
    """Ревью (D3): указатель `profile:` лежит в managed-файле — правило адреса
    записи managed не рассматривает, и финал мастера писал бы пустой якорь в
    базу, а запись всё равно отклонялась бы: managed поднимает тот же сломанный
    профиль. Лестница обязана не обещать деактивацию: признак — «снять нельзя»,
    сообщение называет файл с указателем и что снять его нужно там."""
    (stand / "config.yml").write_text(
        _BASE_OK.replace('profile: "work"\n', ""), encoding="utf-8"
    )
    managed = stand / "config.local.yml"
    managed.write_text('profile: "work"\n', encoding="utf-8")
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_WITHOUT_PROFILE
    assert (
        loaded.config.settings.backend.completions_url == _USER_URL
    )  # ступень та же: данные целы
    assert loaded.profile == "work"
    assert loaded.deactivate_profile is False
    assert str(managed) in loaded.message
    assert "мастер его не снимет" in loaded.message
    assert "снять нужно там" in loaded.message


def test_pointer_named_by_two_layers_is_deactivated_where_the_write_lands(
    stand,
):
    """Ревью: признак деактивации спрашивал, ЧЕЙ слой назвал профиль, — а вопрос
    стоит иначе: сотрёт ли указатель запись в тот файл, куда мастер пишет.

    Ответы расходятся, когда указатель называют два слоя ОДИНАКОВО: managed
    задаёт значение первым, проектный повторяет его — и слоем-изменившим числится
    managed, хотя при чтении побеждает проектный. Туда же (правило адреса записи)
    ложится и снятие, то есть указатель стирается. Мастер отказывался снимать то,
    что снял бы, и оставлял человека в тупике, ради которого лестница заведена.

    Доказательство — не признаком, а концом: с этим признаком мастер дописывает
    починку, и следующий старт настоящим загрузчиком поднимается БЕЗ профиля."""
    (stand / "config.yml").write_text(
        _BASE_OK.replace('profile: "work"\n', ""), encoding="utf-8"
    )
    (stand / "config.local.yml").write_text(
        'profile: "work"\n', encoding="utf-8"
    )
    project_gc = stand.parent / "project" / ".gc"
    project_gc.mkdir()
    (project_gc / "config.yml").write_text(
        'profile: "work"\n', encoding="utf-8"
    )
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_WITHOUT_PROFILE
    assert loaded.profile == "work"
    assert loaded.deactivate_profile is True
    assert "мастер его не снимет" not in loaded.message

    onboarding.write_wizard_values(
        stand / "config.yml",
        dict(_WIZARD_ANSWERS),
        deactivate_profile=loaded.deactivate_profile,
    )

    assert _load_yaml(project_gc / "config.yml").get("profile") == ""
    from gc_core.config import load_settings

    meta = load_settings(None).profile_meta
    assert meta.active_name in (None, "")


def test_a_pointer_repeated_by_managed_over_the_base_is_not_promised(stand):
    """Обратная раскладка того же вопроса: указатель назвала база, а managed
    повторил его. Адрес записи — база, но снятие там managed перекроет, и
    указатель переживёт запись: обещать деактивацию нельзя.

    Признак по виду слоя отвечал здесь True — база задала значение первой, а
    managed его «не менял», — и мастер клал в базу пустой якорь, после которого
    гейт всё равно отвергал собственную починку."""
    (stand / "config.yml").write_text(_BASE_OK, encoding="utf-8")
    managed = stand / "config.local.yml"
    managed.write_text('profile: "work"\n', encoding="utf-8")
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_WITHOUT_PROFILE
    assert loaded.profile == "work"
    assert loaded.deactivate_profile is False
    assert str(managed) in loaded.message


def test_guide_keeps_the_registry_note_when_the_profile_is_not_deactivated(
    stand,
):
    """Ревью: гайд без TTY (`gc-cli --init-config`) выводил «профиль будет снят» из
    СТУПЕНИ лестницы, а экран мастера — из признака деактивации. На managed-стадии
    они расходятся: указатель мастеру не снять, профиль переживёт запись и
    продолжит перекрывать реестр, — а ремарка «действует реестр моделей из другого
    слоя» на этой поверхности глохла. Человек выбирал модель, мастер писал её в
    базу, работал слот профиля, и никто об этом не предупреждал; экран мастера на
    том же конфиге предупреждал. Признак — один, тот же, что у экрана."""
    (stand / "config.yml").write_text(
        _BASE_OK.replace('profile: "work"\n', ""), encoding="utf-8"
    )
    (stand / "config.local.yml").write_text(
        'profile: "work"\n', encoding="utf-8"
    )
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()
    assert loaded.deactivate_profile is False  # предпосылка расхождения

    guide = onboarding.setup_guide(
        str(stand / "config.yml"),
        loaded.config,
        program="gc-cli",
        repair=loaded,
    )
    assert "действует реестр моделей из другого слоя" in guide


def test_guide_drops_the_registry_note_when_the_profile_will_be_deactivated(
    stand,
):
    """Обратный сторож: указатель в базе, финал его снимет — перекрыть выбор
    профиль уже не сможет, и ремарка о чужом реестре противоречила бы соседней
    строке о деактивации (живой смок)."""
    (stand / "config.yml").write_text(_BASE_OK, encoding="utf-8")
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()
    assert loaded.deactivate_profile is True  # предпосылка

    guide = onboarding.setup_guide(
        str(stand / "config.yml"),
        loaded.config,
        program="gc-cli",
        repair=loaded,
    )
    assert "действует реестр моделей из другого слоя" not in guide


def test_healthy_config_drops_no_profile(stand):
    """Ступень `full`: снятого профиля нет и снимать нечего — оба поля пусты,
    хотя профиль активен."""
    (stand / "config.yml").write_text(_BASE_OK, encoding="utf-8")
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "fast"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_FULL
    assert loaded.profile == ""
    assert loaded.deactivate_profile is False


def test_template_stage_message_does_not_promise_a_rewrite(stand):
    """Ревью (M8а): ступень шаблона говорила «мастер перепишет ваш файл», а
    писатель только патчит текст по якорям: на неразбираемом файле человек
    отвечает на все вопросы и получает отказ записи, файл не тронут. Текст обязан
    говорить, что будет на самом деле: ответы ложатся точечно, целиком файл не
    переписывается, поломка мимо них останется — чинить руками или сбросить."""
    (stand / "config.yml").write_text(
        "this is: : : not [valid yaml }\n", encoding="utf-8"
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_TEMPLATE
    assert "перепишет" not in loaded.message
    assert "точечно" in loaded.message
    assert "не переписывается" in loaded.message
    assert "gc-cli --reset-config" in loaded.message
    assert loaded.profile == ""
    assert loaded.deactivate_profile is False


def test_broken_base_falls_back_to_template_usable_by_runtime(stand):
    """Сломана база — ступень 3, и её результат пригоден рантайму: пустой `{}`
    здесь ронял `resolve_threads_dir` сразу следом (ловилось как Critical)."""
    (stand / "config.yml").write_text(
        "this is: : : not [valid yaml }\n", encoding="utf-8"
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_TEMPLATE
    assert (
        loaded.config.settings.backend.completions_url
        == onboarding.URL_SENTINEL
    )

    from gc_core.thread_store import resolve_threads_dir

    assert resolve_threads_dir(loaded.config)
    assert loaded.message


def test_template_stage_still_says_so_when_the_overlays_cover_every_field(
    stand,
):
    """Шаблон несёт сентинелы всех обязательных полей — но закрывают их ВЕРХНИЕ
    слои, и «ступень шаблона, требований не осталось» вполне достижима: база не
    читается, а проектный `.gc/config.yml` задаёт адрес, токен и модель.

    Сторож ветки, а не новое поведение: её уже принимали за мёртвую. Без неё гайд
    печатает «Конфиг настроен — можно запускать» поверх нечитаемого файла
    человека, о котором не сказано ни слова."""
    (stand / "config.yml").write_text(
        "this is: : : not [valid yaml }\n", encoding="utf-8"
    )
    project_gc = stand.parent / "project" / ".gc"
    project_gc.mkdir()
    (project_gc / "config.yml").write_text(
        "backend:\n"
        "  api_mode: chat_completions\n"
        '  completions_url: "https://real/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer sk-real"\n'
        "agent:\n"
        '  model: "real-model"\n',
        encoding="utf-8",
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_TEMPLATE
    assert onboarding.pending_requirements(loaded.config) == []
    guide = onboarding.setup_guide(
        str(stand / "config.yml"), loaded.config, repair=loaded
    )
    assert "можно запускать" not in guide
    assert "показан пакетный шаблон" in guide
    assert "gc-cli --reset-config" in guide


def test_broken_base_and_broken_profile_still_lands_on_template(stand):
    """Ступень 2 не годится (база не читается сама по себе) — падаем на шаблон,
    а не отдаём наверх исключение."""
    (stand / "config.yml").write_text(
        "this is: : : not [valid yaml }\n", encoding="utf-8"
    )
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_TEMPLATE
    assert (
        loaded.config.settings.backend.completions_url
        == onboarding.URL_SENTINEL
    )


def test_template_stage_with_an_active_pointer_refuses_before_the_questions(
    stand,
):
    """Ревью (M10): база не собирается и без профиля (ступень шаблона), а
    указатель `profile:` жив — гейт записи судит стек СЛЕДУЮЩЕГО старта, и при
    сломанном профиле отвергнет всё, что мастер соберёт: человек отвечал на все
    вопросы (включая токен), а файл не менялся (md5 тот же, прогон ревьюера).
    «Просто снять указатель» финалом тоже не работает: снятие — отдельная
    запись с фазой 2, а без профиля база не собирается и так.

    Честный отказ ДО вопросов: назвать профиль и файлы, предложить ручную
    правку/сброс — и НЕ советовать `--profile ""` (без профиля база не грузится
    тоже)."""
    (stand / "config.yml").write_text(
        _BASE_OK + 'slots:\n  light: "nope-prof"\n', encoding="utf-8"
    )
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    with pytest.raises(onboarding.RepairRefused) as info:
        onboarding.load_config_for_repair()

    text = str(info.value)
    assert "не починит" in text
    assert "профиль" in text and '"work"' in text
    assert str(stand / "config.yml") in text  # файл починки назван
    assert "gc-cli --reset-config" in text
    assert '--profile ""' not in text  # флаг тут не помогает


def test_template_stage_refusal_names_the_file_of_the_pointer(stand):
    """Указатель может жить не в базе: снять его человеку нужно в СВОЁМ файле —
    отказ называет его (проектный `.gc/config.yml`)."""
    (stand / "config.yml").write_text(
        _BASE_OK.replace('profile: "work"\n', "")
        + 'slots:\n  light: "nope-prof"\n',
        encoding="utf-8",
    )
    project_gc = stand.parent / "project" / ".gc"
    project_gc.mkdir()
    (project_gc / "config.yml").write_text(
        'profile: "work"\n', encoding="utf-8"
    )
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    with pytest.raises(onboarding.RepairRefused) as info:
        onboarding.load_config_for_repair()

    assert str(project_gc / "config.yml") in str(info.value)


def test_template_stage_without_a_pointer_still_runs_the_wizard(stand):
    """Отрицательный сторож: та же поломка базы, указателя нет — отказывать не в
    чем, лестница отдаёт ступень шаблона, мастер работает по её контракту."""
    (stand / "config.yml").write_text(
        _BASE_OK.replace('profile: "work"\n', "")
        + 'slots:\n  light: "nope-prof"\n',
        encoding="utf-8",
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()

    assert loaded.stage == onboarding.REPAIR_STAGE_TEMPLATE


def test_broken_project_layer_hands_the_caller_the_first_error_not_a_template_diagnosis(
    stand,
):
    """Ревью (H3): все три ступени накладывают верхние слои, и третья (шаблон) не
    была обёрнута перехватом — на сломанном проектном `.gc/config.yml` лестница
    роняла вызывающего исключением ступени шаблона: диагнозом подставленного
    стека (здесь без `slots.max` базы), которого человек нигде больше не видит.

    Форма исхода — исключение, не четвёртая ступень с пустым конфигом: конфига
    нет, и молчаливый `None` в `RepairLoad.config` рано или поздно доехал бы до
    `resolve_threads_dir`. Отдаётся ПЕРВАЯ ошибка — та же, что на обычном
    старте: сломан слой, в который мастер не пишет, и объяснение обязано быть
    тем же, что печатает обычный старт."""
    (stand / "config.yml").write_text(
        _BASE_OK.replace('profile: "work"\n', "")
        + 'slots:\n  max: "ghost2"\n',
        encoding="utf-8",
    )
    project = stand.parent / "project"
    (project / ".gc").mkdir()
    (project / ".gc" / "config.yml").write_text(
        'slots:\n  default: "ghost"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    with pytest.raises(ValueError, match="slots.max") as info:
        onboarding.load_config_for_repair()

    from gc_core.config import load_settings

    with pytest.raises(ValueError) as first:
        load_settings(None)
    assert str(info.value) == str(first.value)


# --- финал мастера на ступени «без профиля» --------------------------------

_WIZARD_ANSWERS = {
    "completions_url": "https://openrouter.ai/api/v1/chat/completions",
    "Authorization": "Bearer sk-user-token",
    "model": "claude-fable-5",
}


def _load_yaml(path):
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _refusing(message):
    """Отказ записи тем же исключением, каким отказывают настоящие писатели."""

    def _refuse(*_args, **_kwargs):
        raise ValueError(message)

    return _refuse


def test_wizard_finish_on_without_profile_stage_deactivates_the_profile(stand):
    """Живой тупик стенда: лестница сняла профиль НА ЗАПУСК, мастер дошёл до
    конца — и запись отказана («slots.default: алиас "strong" не найден»), потому
    что гейт судит стек СЛЕДУЮЩЕГО старта, а там указатель снова поднимает
    сломанный профиль. Чинилка запускалась, спрашивала всё и не могла записать
    собственную починку.

    Финал обязан снять указатель — тогда починка ложится в файл, а следующий
    старт поднимается. Файл профиля при этом остаётся на диске: человек чинит
    его позже и включает обратно."""
    (stand / "config.yml").write_text(_BASE_OK, encoding="utf-8")
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()
    assert loaded.stage == onboarding.REPAIR_STAGE_WITHOUT_PROFILE

    onboarding.write_wizard_values(
        stand / "config.yml",
        dict(_WIZARD_ANSWERS),
        deactivate_profile=loaded.deactivate_profile,
    )

    raw = _load_yaml(stand / "config.yml")
    assert raw.get("profile") == ""  # указатель снят
    assert (stand / "profiles" / "work.yml").is_file()  # …а файл профиля цел
    assert _load_yaml(stand / "profiles" / "work.yml") == {
        "slots": {"default": "strong"}
    }
    # Настройки мастера действительно записаны, а не потеряны ради деактивации.
    assert raw["models"]["claude-fable-5"] == {"model": "claude-fable-5"}
    assert raw["slots"]["default"] == "claude-fable-5"

    # Главное: следующий старт поднимается. Настоящий загрузчик, не мок — тупик
    # был именно в том, что стек следующего старта не собирался.
    from gc_core.config import load_settings

    config = load_settings(None)
    meta = config.profile_meta
    assert meta.active_name in (None, "")
    assert onboarding.effective_model(config) == "claude-fable-5"
    assert config.settings.backend.completions_url == _USER_URL


def test_wizard_finish_on_a_healthy_config_leaves_the_active_profile_alone(
    stand,
):
    """Отрицательный сторож: конфиг здоров (ступень `full`), профиль активен и
    ничего не ломает — мастер к указателю не прикасается. Деактивация тут была бы
    молчаливым отъёмом чужой настройки."""
    (stand / "config.yml").write_text(_BASE_OK, encoding="utf-8")
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "fast"\n', encoding="utf-8"
    )

    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()
    assert loaded.stage == onboarding.REPAIR_STAGE_FULL

    onboarding.write_wizard_values(
        stand / "config.yml",
        dict(_WIZARD_ANSWERS),
        deactivate_profile=loaded.deactivate_profile,
    )

    assert _load_yaml(stand / "config.yml").get("profile") == "work"
    from gc_core.config import load_settings

    meta = load_settings(None).profile_meta
    assert meta.active_name == "work"


# --- что именно легло: контракт `PartialWriteError.written` ------------------
#
# Состав уже состоявшихся записей знает только писатель. Поверхности мастера
# выводили его из порядка записи, и такая догадка разъезжается с писателем на
# первой же правке порядка: человек читает про снятый профиль там, где его не
# снимали, — или ищет нетронутый файл, в котором уже лежит новый реестр.


def _broken_stand(stand):
    """Раскладка ступени «без профиля»: снятие указателя тут — настоящая запись."""
    (stand / "config.yml").write_text(_BASE_OK, encoding="utf-8")
    (stand / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )


def test_a_refused_registry_after_the_deactivation_names_the_pointer(
    stand, monkeypatch
):
    """Указатель снят, транзакция реестра отказала: обещать «конфиг не записан»
    нельзя — человек уверен, что профиль активен, а следующий старт идёт без него."""
    from gc_core.config_modules import registry_write as rw

    _broken_stand(stand)

    monkeypatch.setattr(rw, "apply_registry_ops", _refusing("реестр отказал"))
    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()
    with pytest.raises(onboarding.PartialWriteError) as info:
        onboarding.write_wizard_values(
            stand / "config.yml",
            dict(_WIZARD_ANSWERS),
            deactivate_profile=loaded.deactivate_profile,
        )

    assert info.value.written == ("profile_pointer",)
    assert str(info.value) == "реестр отказал"
    assert (
        _load_yaml(stand / "config.yml").get("profile") == ""
    )  # и правда снят
    assert (
        _load_yaml(stand / "config.yml").get("slots") is None
    )  # а реестр — нет


def test_a_refused_flat_write_after_the_deactivation_names_both(
    stand, monkeypatch
):
    """Легли обе первые записи, отказала плоская — названы обе, в порядке
    исполнения."""
    from gc_core import onboarding_config_writer as ocw

    _broken_stand(stand)
    real_write_values = ocw.write_values

    def refusing_flat_write(path, values, **kwargs):
        if "profile" in values:  # снятие указателя — настоящее
            return real_write_values(path, values, **kwargs)
        raise ValueError("схема не валидна")

    monkeypatch.setattr(ocw, "write_values", refusing_flat_write)
    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()
    with pytest.raises(onboarding.PartialWriteError) as info:
        onboarding.write_wizard_values(
            stand / "config.yml",
            dict(_WIZARD_ANSWERS),
            deactivate_profile=loaded.deactivate_profile,
        )

    assert info.value.written == ("profile_pointer", "registry")
    raw = _load_yaml(stand / "config.yml")
    assert (
        raw.get("profile") == ""
        and raw["slots"]["default"] == "claude-fable-5"
    )


def test_a_refused_pointer_write_landed_nothing(stand, monkeypatch):
    """Отказала САМА первая запись — не легло ничего, и форма ответа это говорит:
    голый `ValueError`. `PartialWriteError` с пустым списком был бы той же ложью,
    только вывернутой наизнанку."""
    from gc_core import onboarding_config_writer as ocw

    _broken_stand(stand)

    monkeypatch.setattr(
        ocw, "write_profile_pointer", _refusing("указатель не записан")
    )
    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()
    with pytest.raises(ValueError) as info:
        onboarding.write_wizard_values(
            stand / "config.yml",
            dict(_WIZARD_ANSWERS),
            deactivate_profile=loaded.deactivate_profile,
        )

    assert not isinstance(info.value, onboarding.PartialWriteError)
    assert (
        _load_yaml(stand / "config.yml").get("profile") == "work"
    )  # файл не тронут


# --- те же три исхода, но исключением файловой системы (ревью L33) -----------
#
# Все три записи штатно кидают OSError (диск, права, win-лок открытого файла), а
# перехваты писателя держали только ValueError: наружу уходил голый OSError —
# CLI печатал трейсбек, TUI падал, при этом на диске уже лежали `profile: ""` и
# реестр, и снятый указатель не назывался нигде.


def _os_refusing(message):
    def _refuse(*_args, **_kwargs):
        raise OSError(message)

    return _refuse


def test_an_oserror_of_the_registry_after_the_deactivation_names_the_pointer(
    stand, monkeypatch
):
    """Близнец ValueError-кейса выше: форма ответа не зависит от класса отказа —
    указатель уже снят, и об этом обязан сказать PartialWriteError, не трейсбек."""
    from gc_core.config_modules import registry_write as rw

    _broken_stand(stand)

    monkeypatch.setattr(
        rw, "apply_registry_ops", _os_refusing("диск недоступен")
    )
    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()
    with pytest.raises(onboarding.PartialWriteError) as info:
        onboarding.write_wizard_values(
            stand / "config.yml",
            dict(_WIZARD_ANSWERS),
            deactivate_profile=loaded.deactivate_profile,
        )

    assert info.value.written == ("profile_pointer",)
    assert str(info.value) == "диск недоступен"
    assert (
        _load_yaml(stand / "config.yml").get("profile") == ""
    )  # и правда снят


def test_an_oserror_of_the_flat_write_after_the_deactivation_names_both(
    stand, monkeypatch
):
    """Близнец: легли указатель и реестр, плоская запись упала OSError —
    названы обе состоявшиеся записи, в порядке исполнения."""
    from gc_core import onboarding_config_writer as ocw

    _broken_stand(stand)
    real_write_values = ocw.write_values

    def failing_flat_write(path, values, **kwargs):
        if "profile" in values:  # снятие указателя — настоящее
            return real_write_values(path, values, **kwargs)
        raise OSError("диск недоступен")

    monkeypatch.setattr(ocw, "write_values", failing_flat_write)
    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()
    with pytest.raises(onboarding.PartialWriteError) as info:
        onboarding.write_wizard_values(
            stand / "config.yml",
            dict(_WIZARD_ANSWERS),
            deactivate_profile=loaded.deactivate_profile,
        )

    assert info.value.written == ("profile_pointer", "registry")
    raw = _load_yaml(stand / "config.yml")
    assert (
        raw.get("profile") == ""
        and raw["slots"]["default"] == "claude-fable-5"
    )


def test_an_oserror_of_the_pointer_write_stays_an_oserror(stand, monkeypatch):
    """Отрицательный сторож: отказала САМАЯ первая запись — не легло ничего, и
    OSError уходит как есть (его ловят поверхности); PartialWriteError с пустым
    списком был бы ложью, вывернутой наизнанку."""
    from gc_core import onboarding_config_writer as ocw

    _broken_stand(stand)

    monkeypatch.setattr(ocw, "write_profile_pointer", _os_refusing("нет прав"))
    from gc_core import onboarding

    loaded = onboarding.load_config_for_repair()
    with pytest.raises(OSError) as info:
        onboarding.write_wizard_values(
            stand / "config.yml",
            dict(_WIZARD_ANSWERS),
            deactivate_profile=loaded.deactivate_profile,
        )

    assert not isinstance(info.value, onboarding.PartialWriteError)
    assert (
        _load_yaml(stand / "config.yml").get("profile") == "work"
    )  # файл не тронут
