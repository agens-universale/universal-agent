import yaml

import onboarding
import onboarding_config_writer
import onboarding_wizard as wz


class _FakeMcp:
    def __init__(self):
        self.installed = []
    def installable_names(self):
        return ["office", "science", "duckduckgo"]
    def install(self, name=None):
        self.installed.append(name)
        return 0


def _io(answers):
    it = iter(answers)
    return wz.WizardIO(reader=lambda p, secret: next(it), printer=lambda m: None)


def test_run_writes_fields_installs_selected_and_returns_configured(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        '  completions_url: "http://your-backend:PORT/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
        "agent:\n"
        '  model: "gigacode_agent_v1.6_rc"\n',
        encoding="utf-8",
    )
    # После записи load_config должен вернуть «настроено».
    monkeypatch.setattr(wz, "_reload_config", lambda: {
        "completions_url": "https://x/v1",
        "chat_completion_headers": {"Authorization": "Bearer sk-1"},
        "model": "gigacode_agent_v1.6_rc",
    })
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    mcp = _FakeMcp()
    # endpoint, auth-меню «1» (ввести токен), токен, MCP-меню «2» (выбрать), номера «1,3»
    io = _io(["https://x/v1", "1", "sk-1", "2", "1,3"])
    configured = wz.run(cfg, cfg_file, review_all=False, io=io, mcp=mcp)
    assert configured is True
    text = cfg_file.read_text(encoding="utf-8")
    assert '  completions_url: "https://x/v1"' in text
    assert '    Authorization: "Bearer sk-1"' in text
    assert mcp.installed == ["office", "duckduckgo"]


def test_run_returns_false_when_still_pending(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        '  completions_url: "http://your-backend:PORT/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
        "agent:\n"
        '  model: "gigacode_agent_v1.6_rc"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(wz, "_reload_config", lambda: {
        "completions_url": onboarding.URL_SENTINEL,   # так и не задан
        "chat_completion_headers": {"Authorization": "Bearer sk-1"},
        "model": "gigacode_agent_v1.6_rc",
    })
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    # endpoint пропущен (Enter), auth-меню «1» (ввести токен), токен, MCP пропустить «3»
    io = _io(["", "1", "sk-1", "3"])
    assert wz.run(cfg, cfg_file, review_all=False, io=io, mcp=_FakeMcp()) is False


def test_run_without_mcp_skips_mcp(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        '  completions_url: "http://your-backend:PORT/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
        "agent:\n  model: \"m\"\n", encoding="utf-8")
    monkeypatch.setattr(wz, "_reload_config", lambda: {
        "completions_url": "https://x/v1", "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"})
    it = iter(["", "1", "sk"])   # endpoint skip, auth "1" token, token
    io = wz.WizardIO(reader=lambda p, s: next(it), printer=lambda m: None)
    # mcp не передаём → шаг MCP не должен вызваться (нет исключения от отсутствующего mcp)
    assert wz.run({"completions_url": "http://your-backend:PORT/v1/chat/completions",
                   "chat_completion_headers": {"Authorization": "Bearer <ВПИШИТЕ_ТОКЕН>"}, "model": "m"},
                  cfg_file, review_all=False, io=io) is True


def test_run_writes_registry_entry_and_flat_connection(tmp_path, monkeypatch):
    """CLI-визард пишет ТО ЖЕ, что и TUI-мастер (ядро записи одно, onboarding.py):
    подключение — плоскими ключами, модель — записью реестра со слотом default;
    легаси `agent.model` не трогается, конфиг после прогона настроен."""
    import config as cfg_mod
    from bundled_assets import bundled_root
    from config_modules import env_vars

    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("model_picker.fetch_models", lambda *a, **k: None)   # без сети
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        (bundled_root() / "config.default.yml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(wz, "_reload_config", lambda: cfg_mod.load_config(str(cfg_file)))

    starting = cfg_mod.load_config(str(cfg_file))
    # адрес, auth-меню «1» (токен), токен, модель (списка нет → ручной ввод)
    io = _io(["https://x/v1/chat/completions", "1", "sk-1", "qwen/qwen3-coder"])
    assert wz.run(starting, cfg_file, review_all=False, io=io, mcp=None) is True

    raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert raw["backend"]["completions_url"] == "https://x/v1/chat/completions"
    assert raw["backend"]["chat_completion_headers"]["Authorization"] == "Bearer sk-1"
    assert raw["models"] == {"qwen3-coder": {"model": "qwen/qwen3-coder"}}
    assert raw["slots"]["default"] == "qwen3-coder"
    assert raw["agent"]["model"] == onboarding.MODEL_SENTINEL
    assert onboarding.is_unconfigured(cfg_mod.load_config(str(cfg_file))) is False


def test_run_with_registry_but_skipped_endpoint_stays_unconfigured(tmp_path, monkeypatch):
    """Модель записана в реестр, а адрес пропущен Enter'ом: визард обязан отчитаться
    «не настроено» и напечатать гайд. Иначе «✓ Готово» закрывало бы вопрос навсегда —
    следующий старт мастер не предложил бы, а запрос ушёл бы на плейсхолдерный хост."""
    import config as cfg_mod
    from bundled_assets import bundled_root
    from config_modules import env_vars

    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("model_picker.fetch_models", lambda *a, **k: None)
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        (bundled_root() / "config.default.yml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(wz, "_reload_config", lambda: cfg_mod.load_config(str(cfg_file)))

    printed = []
    it = iter(["", "3", "qwen3-coder"])   # адрес пропущен, auth-меню «3» (пропустить), модель
    io = wz.WizardIO(reader=lambda p, s: next(it), printer=printed.append)
    assert wz.run(cfg_mod.load_config(str(cfg_file)), cfg_file,
                  review_all=False, io=io, mcp=None) is False

    raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert raw["models"] == {"qwen3-coder": {"model": "qwen3-coder"}}    # модель записана
    assert raw["backend"]["completions_url"] == onboarding.URL_SENTINEL  # адрес так и не задан
    blob = "\n".join(printed)
    assert "настройка не завершена" in blob
    assert "backend.completions_url" in blob


def test_run_reports_reload_failure_instead_of_crashing_after_all_answers(
        tmp_path, monkeypatch):
    """Ревью нашло: `_reload_config()` в конце run() не защищён вовсе. Файл на
    диске синтаксически сломан НАСТОЛЬКО, что патч поверх него отклоняет сама
    `validate_config_text` (первый except уже это ловит честно — «конфиг не
    записан», файл не тронут) — а следом run() САМ зовёт `_reload_config()` и
    падает ВТОРЫМ, уже необработанным исключением. Человек к этому моменту
    ответил на все вопросы мастера.

    `_reload_config` НЕ замокан (ревью нашло: прежний тест мокал `run()`
    целиком лямбдой — реальная функция не выполнялась вовсе). Подменены только
    reader/printer у `WizardIO` и сеть в `_collect_model`, как это делают
    соседние тесты этого файла."""
    from config_modules import env_vars
    from config_modules.env_vars import EnvVars

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("this is: : : not [valid yaml }\n", encoding="utf-8")
    # `_reload_config()` зовёт load_config(None) — дефолтный путь, не cfg_file
    # напрямую; изолируем его на ТОТ ЖЕ файл (ровно как в `_run_init()`, где
    # config_path и путь `load_config(None)` — один и тот же файл).
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path))
    monkeypatch.setattr("model_picker.fetch_models", lambda *a, **k: None)

    answers = iter([
        "https://x/v1/chat/completions",  # адрес
        "1", "sk-1",                      # авторизация: токен
        "qwen/qwen3-coder",                # модель (список не пришёл — вручную)
        "", "",                            # уровни — пропустить
    ])
    printed = []
    io = wz.WizardIO(reader=lambda p, s: next(answers, ""), printer=printed.append)

    result = wz.run({}, cfg_file, review_all=True, io=io, mcp=None)

    assert result is False
    # Файл действительно НЕ тронут — патч был отклонён валидацией до записи.
    assert cfg_file.read_text(encoding="utf-8") == "this is: : : not [valid yaml }\n"
    output = "\n".join(printed)
    assert "конфиг не записан" in output          # первый, уже работавший except
    assert "ответили на все вопросы" in output    # новое сообщение о втором крахе
    assert output.count("невалидный YAML") >= 2   # причина названа дважды: коротко и в explain


def _phase_two_broken_stand(tmp_path, monkeypatch):
    """База, дефект которой видит только ФАЗА 2 (`slots.max` на несуществующий
    алиас): текст файла валиден, поэтому запись мастера проходит проверку и
    ложится, а перечитывание конфига падает — тот самый исход, где обещание
    нетронутого файла становится ложью. Синтаксически битый файл, на котором
    ветка проверялась прежде, до записи не доходит вовсе."""
    from config_modules import env_vars
    from config_modules.env_vars import EnvVars

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        "  api_mode: chat_completions\n"
        f'  completions_url: "{onboarding.URL_SENTINEL}"\n'
        "  chat_completion_headers:\n"
        f'    Authorization: "{onboarding.TOKEN_SENTINEL}"\n'
        "models:\n"
        "  fast:\n"
        '    model: "qwen/qwen3-8b"\n'
        "slots:\n"
        '  default: "fast"\n'
        '  max: "ghost"\n', encoding="utf-8")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path))
    monkeypatch.setattr("model_picker.fetch_models", lambda *a, **k: None)
    return cfg_file


def test_a_reload_failure_after_a_landed_write_does_not_promise_an_untouched_file(
        tmp_path, monkeypatch):
    """Ревью (M8б): человек ответил, запись ПРОШЛА (фаза 1 судит текст, а не
    собираемость стека), файл изменён — и мастер сообщал, что файл «остался в
    прежнем виде». Человек шёл искать свои ответы в нетронутом файле, где они уже
    лежат, и вводил их заново."""
    cfg_file = _phase_two_broken_stand(tmp_path, monkeypatch)
    loaded = onboarding.load_config_for_repair()
    assert loaded.stage == onboarding.REPAIR_STAGE_TEMPLATE

    printed: list = []
    it = iter(["https://new.example/v1/chat/completions", "3", "", ""])
    io = wz.WizardIO(reader=lambda p, secret: next(it, ""), printer=printed.append)
    configured = wz.run(loaded.config, cfg_file, review_all=True, io=io)

    assert configured is False
    text = cfg_file.read_text(encoding="utf-8")
    assert "https://new.example/v1/chat/completions" in text     # ответ в файле
    output = "\n".join(printed)
    assert "ответили на все вопросы" in output
    assert "прежнем виде" not in output and "не тронут" not in output
    assert "ваши ответы записаны" in output
    assert "slots.max" in output                                 # причина названа


def test_a_reload_failure_without_a_write_still_says_the_file_is_untouched(
        tmp_path, monkeypatch):
    """Второй исход того же места, и он обязан отличаться: человек прошёл мастер,
    ничего не заполнив, — писателя не звали, файл и правда прежний. Усреднить два
    исхода одним текстом значит соврать в одном из них (живой прогон стенда)."""
    cfg_file = _phase_two_broken_stand(tmp_path, monkeypatch)
    before = cfg_file.read_text(encoding="utf-8")
    loaded = onboarding.load_config_for_repair()

    printed: list = []
    it = iter(["", "3", "", ""])                    # все ответы пропущены
    io = wz.WizardIO(reader=lambda p, secret: next(it, ""), printer=printed.append)
    configured = wz.run(loaded.config, cfg_file, review_all=True, io=io)

    assert configured is False
    assert cfg_file.read_text(encoding="utf-8") == before
    output = "\n".join(printed)
    assert "прежнем виде" in output
    assert "ваши ответы записаны" not in output


def test_run_reports_validation_error_without_traceback(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        '  completions_url: "http://your-backend:PORT/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
        "agent:\n  model: \"m\"\n", encoding="utf-8")

    # write_values бросает ValueError → run() обрабатывает без трейсбека
    def failing_write_values(*args, **kwargs):
        raise ValueError("схема не валидна")

    monkeypatch.setattr(onboarding_config_writer, "write_values", failing_write_values)
    monkeypatch.setattr(wz, "_reload_config", lambda: {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": "Bearer real"},
        "model": "m",
    })

    printed = []
    # Pending — ТОЛЬКО endpoint (auth и модель заданы): reader отвечает один раз,
    # write_values бросает ValueError → run() обрабатывает без трейсбека.
    io = wz.WizardIO(reader=lambda p, s: "https://x/v1", printer=lambda m: printed.append(m))
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": "Bearer real"},
        "model": "m",
    }
    wz.run(cfg, cfg_file, review_all=False, io=io)

    # run() не бросает; printer получает сообщение об ошибке
    output = "\n".join(printed)
    assert "конфиг не записан" in output
    assert "схема не валидна" in output


def test_run_reports_an_oserror_without_traceback(tmp_path, monkeypatch):
    """Близнец теста выше для OSError (ревью L33): запись штатно кидает его
    (диск, права, win-лок открытого файла), а перехват держал только ValueError —
    человек отвечал на все вопросы и получал голый трейсбек вместо
    «конфиг не записан»."""
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        '  completions_url: "http://your-backend:PORT/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
        "agent:\n  model: \"m\"\n", encoding="utf-8")

    def failing_write_values(*args, **kwargs):
        raise OSError("[WinError 5] отказано в доступе")

    monkeypatch.setattr(onboarding_config_writer, "write_values", failing_write_values)
    monkeypatch.setattr(wz, "_reload_config", lambda: {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": "Bearer real"},
        "model": "m",
    })

    printed = []
    io = wz.WizardIO(reader=lambda p, s: "https://x/v1", printer=lambda m: printed.append(m))
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": "Bearer real"},
        "model": "m",
    }
    wz.run(cfg, cfg_file, review_all=False, io=io)

    output = "\n".join(printed)
    assert "конфиг не записан" in output
    assert "[WinError 5] отказано в доступе" in output


def test_run_does_not_claim_an_untouched_config_after_the_registry_landed(
        tmp_path, monkeypatch):
    """Брат теста выше, второй полюс: там плоская запись — единственная, и «конфиг
    не записан» правда; здесь ей предшествует запись реестра, которая УЖЕ на диске.

    Записей две, и реестр идёт первым (политика в `onboarding.write_wizard_values`).
    Отказ второй оставляет модели в файле — сказать «конфиг не записан» значит
    отправить человека искать нетронутый файл, а там уже новый реестр."""
    import config as cfg_mod
    from bundled_assets import bundled_root
    from config_modules import env_vars

    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("model_picker.fetch_models", lambda *a, **k: None)
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        (bundled_root() / "config.default.yml").read_text(encoding="utf-8"),
        encoding="utf-8")
    monkeypatch.setattr(wz, "_reload_config", lambda: cfg_mod.load_config(str(cfg_file)))

    def failing_write_values(*args, **kwargs):
        raise ValueError("схема не валидна")

    monkeypatch.setattr(onboarding_config_writer, "write_values", failing_write_values)

    printed = []
    it = iter(["https://x/v1/chat/completions", "1", "sk-1", "qwen/qwen3-coder"])
    io = wz.WizardIO(reader=lambda p, s: next(it), printer=printed.append)
    wz.run(cfg_mod.load_config(str(cfg_file)), cfg_file, review_all=False, io=io, mcp=None)

    raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert raw["models"] == {"qwen3-coder": {"model": "qwen/qwen3-coder"}}
    output = "\n".join(printed)
    assert "схема не валидна" in output
    assert "конфиг не записан" not in output
    assert "модел" in output          # сказано, что записи моделей уже в файле


def test_summary_reports_all_three_levels_from_the_fresh_config():
    """Итог читается из конфига, а не из введённого: человеку важно состояние
    файла, а не история его ответов."""
    cfg = {"models": {"fast": {"model": "qwen/qwen3-8b"},
                      "brain": {"model": "qwen/qwen3-235b"}},
           "slots": {"default": "brain", "light": "fast"}}
    lines = wz._summary_lines(cfg)
    assert lines == [
        "    модель       qwen/qwen3-235b  (имя записи «brain»)",
        "    light        qwen/qwen3-8b  (имя записи «fast»)",
        "    max          не задан",
    ]


def test_summary_of_an_empty_registry_says_nothing_is_set():
    assert wz._summary_lines({}) == [
        "    модель       не задан",
        "    light        не задан",
        "    max          не задан",
    ]


def test_summary_shows_the_resolved_model_when_the_default_slot_is_unset():
    """Реестр из одной записи резолвится без явного слота — сводка обязана
    показать работающую модель, а не «не задан».

    Это сторож фоллбэка `assigned_level_model` → `effective_model`: без него
    строка `default` врёт про конфиг, который прекрасно работает. Источник
    здесь не назвать — ни `light`, ни `max` слота не имеют вовсе, обход
    матрицы `LEVEL_FALLBACK` не находит ничего, и выдумывать нельзя."""
    cfg = {"models": {"only": {"model": "vendor/only"}}, "slots": {}}
    lines = wz._summary_lines(cfg)
    assert lines[0] == "    модель       vendor/only  (default не задан)"


def test_summary_names_the_source_when_default_falls_back_to_a_level():
    """Без `slots.default` строка «модель» раньше показывала модель уровня
    `max` и отличалась от назначенной только отсутствием скобки — подсказка
    была слишком тихой, владелец её не заметил сразу и переспросил."""
    cfg = {"models": {"fast": {"model": "qwen/qwen3-8b"}},
           "slots": {"max": "fast"}}
    lines = wz._summary_lines(cfg)
    assert lines[0] == "    модель       qwen/qwen3-8b  (default не задан — работает max)"


def test_summary_fallback_model_is_the_one_of_the_level_it_names():
    """Без `slots.default` модель строки «модель» бралась `effective_model` —
    резолвом АКТИВНОГО уровня конфига, а подпись — обходом матрицы от default.
    При `level: light` с заданными обоими уровнями строка показывала модель light
    с подписью «работает max». Модель берётся из слота названного уровня."""
    cfg = {"models": {"fast": {"model": "qwen/qwen3-8b"},
                      "brain": {"model": "qwen/qwen3-235b"}},
           "slots": {"light": "fast", "max": "brain"}, "level": "light"}
    lines = wz._summary_lines(cfg)
    assert lines[0] == "    модель       qwen/qwen3-235b  (default не задан — работает max)"


# --- ступень «без профиля»: плоский мастер предупреждает и деактивирует -------

_STAND_BASE = (
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


def _broken_profile_stand(tmp_path, monkeypatch, *, slot="strong"):
    """Раскладка живого смока на НАСТОЯЩИХ файлах: рабочие адрес/токен/реестр в
    базе, слот профиля — на алиас, которого нет нигде. Изолированный ~/.gc и
    чистый cwd, иначе в стек лезут слои машины, где гоняется тест."""
    from config_modules import env_vars
    from config_modules.env_vars import EnvVars

    home = tmp_path / "gc"
    (home / "profiles").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=home))
    monkeypatch.chdir(project)
    (home / "config.yml").write_text(_STAND_BASE, encoding="utf-8")
    (home / "profiles" / "work.yml").write_text(
        f'slots:\n  default: "{slot}"\n', encoding="utf-8")
    monkeypatch.setattr("model_picker.fetch_models", lambda *a, **k: None)
    return home


_ANSWERS = ["https://openrouter.ai/api/v1/chat/completions",   # адрес
            "1", "sk-user-token",                              # авторизация: токен
            "claude-fable-5",                                  # модель
            "", ""]                                            # уровни — пропустить


def test_run_on_the_without_profile_stage_warns_and_deactivates(tmp_path, monkeypatch):
    """Живой тупик стенда у плоской чинилки: без снятия указателя запись отвергается
    («алиас "strong" не найден»), потому что гейт судит стек СЛЕДУЮЩЕГО старта.
    Финал снимает указатель — починка ложится в файл, а файл профиля остаётся на
    диске. Предупреждение печатается до записи, тем же текстом, что и в TUI."""
    import onboarding_fields as _of
    home = _broken_profile_stand(tmp_path, monkeypatch)

    loaded = onboarding.load_config_for_repair()
    assert loaded.stage == onboarding.REPAIR_STAGE_WITHOUT_PROFILE

    printed: list = []
    it = iter(_ANSWERS)
    io = wz.WizardIO(reader=lambda p, secret: next(it, ""), printer=printed.append)
    configured = wz.run(loaded.config, home / "config.yml", review_all=True, io=io,
                        deactivate_profile=True)

    assert any(_of.PROFILE_DEACTIVATION_WARNING in line for line in printed)
    raw = yaml.safe_load((home / "config.yml").read_text(encoding="utf-8"))
    assert raw["profile"] == ""                                 # указатель снят
    assert (home / "profiles" / "work.yml").is_file()           # …а файл профиля цел
    assert raw["slots"]["default"] == "claude-fable-5"          # настройки записаны
    assert configured is True                                   # конфиг перечитан и настроен


def test_run_without_the_flag_leaves_the_pointer_and_says_nothing(tmp_path, monkeypatch):
    """Отрицательный сторож: здоровый профиль — ни предупреждения, ни снятия."""
    home = _broken_profile_stand(tmp_path, monkeypatch, slot="fast")

    loaded = onboarding.load_config_for_repair()
    assert loaded.stage == onboarding.REPAIR_STAGE_FULL

    printed: list = []
    it = iter(_ANSWERS)
    io = wz.WizardIO(reader=lambda p, secret: next(it, ""), printer=printed.append)
    wz.run(loaded.config, home / "config.yml", review_all=True, io=io,
           deactivate_profile=False)

    assert not any("деактив" in line.lower() for line in printed)
    raw = yaml.safe_load((home / "config.yml").read_text(encoding="utf-8"))
    assert raw["profile"] == "work"


# --- отказ записи — не «✓ Готово» и не успех --------------------------------------

_REFUSAL = "схема не валидна"


def _refusing_write_values(*args, **kwargs):
    """Отказ плоской записи тем же исключением, что кидает настоящая
    `write_values` (валидация не прошла — файл не тронут)."""
    raise ValueError(_REFUSAL)


def test_a_refused_write_is_not_reported_as_done(tmp_path, monkeypatch):
    """Ревью: все ветки отказа записи проваливались дальше по коду. Отказ
    оставляет файл читаемым и настроенным, проверка «конфиг не настроен» ничего
    не находит — и мастер печатал «✓ Готово», сводку и отдавал успех при честном
    «[ошибка] конфиг не записан» строкой выше. `gc-cli --init-config && …` шёл
    дальше, считая модель сохранённой (рулинг владельца у `cli._run_init`: код
    возврата обязан отражать исход). Стенд — здоровый и настроенный: именно на
    таком отказ и прятался за галочкой."""
    home = _broken_profile_stand(tmp_path, monkeypatch, slot="fast")
    monkeypatch.setattr(onboarding_config_writer, "write_values", _refusing_write_values)
    loaded = onboarding.load_config_for_repair()
    assert loaded.stage == onboarding.REPAIR_STAGE_FULL

    printed: list = []
    # новый адрес · авторизация — пропустить · модель — оставить · уровни — пропустить:
    # в записи одно плоское поле, реестр не трогается — отказывает первая же запись
    it = iter(["https://new.example/v1/chat/completions", "3", "", ""])
    io = wz.WizardIO(reader=lambda p, secret: next(it, ""), printer=printed.append)
    configured = wz.run(loaded.config, home / "config.yml", review_all=True, io=io)

    assert configured is False
    output = "\n".join(printed)
    assert f"конфиг не записан: {_REFUSAL}" in output
    assert "✓ Готово" not in output
    assert (home / "config.yml").read_text(encoding="utf-8") == _STAND_BASE   # файл не тронут


def test_a_refused_flat_write_after_the_registry_landed_is_not_done_either(
        tmp_path, monkeypatch):
    """Второй полюс: реестр уже лёг первой транзакцией, отказала плоская запись.
    Конфиг настроен (модели новые, подключение прежнее) — и мастер точно так же
    рапортовал «✓ Готово». Итог честный: что легло, что нет, — и исход «не записано».
    Профиль здесь не снимался — и в итоге не упоминается."""
    home = _broken_profile_stand(tmp_path, monkeypatch, slot="fast")
    monkeypatch.setattr(onboarding_config_writer, "write_values", _refusing_write_values)
    loaded = onboarding.load_config_for_repair()

    printed: list = []
    it = iter(["https://new.example/v1/chat/completions", "3", "claude-fable-5", "", ""])
    io = wz.WizardIO(reader=lambda p, secret: next(it, ""), printer=printed.append)
    configured = wz.run(loaded.config, home / "config.yml", review_all=True, io=io)

    assert configured is False
    raw = yaml.safe_load((home / "config.yml").read_text(encoding="utf-8"))
    assert raw["slots"]["default"] == "claude-fable-5"                         # реестр лёг
    assert raw["backend"]["completions_url"] == "https://openrouter.ai/api/v1/chat/completions"
    output = "\n".join(printed)
    assert _REFUSAL in output
    assert "записи моделей" in output
    assert "деактивирован" not in output
    assert "конфиг не записан" not in output
    assert "✓ Готово" not in output


def test_fields_left_unwritten_are_not_done(tmp_path, monkeypatch):
    """Третья ветка: плоская запись не нашла якорей и вернула их список («всё или
    ничего» — её правило). Ошибка называла поля — и следом шло «✓ Готово»."""
    home = _broken_profile_stand(tmp_path, monkeypatch, slot="fast")
    monkeypatch.setattr(onboarding_config_writer, "write_values",
                        lambda path, values, **kw: list(values))   # ни один якорь не нашёлся
    loaded = onboarding.load_config_for_repair()

    printed: list = []
    it = iter(["https://new.example/v1/chat/completions", "3", "", ""])
    io = wz.WizardIO(reader=lambda p, secret: next(it, ""), printer=printed.append)
    configured = wz.run(loaded.config, home / "config.yml", review_all=True, io=io)

    assert configured is False
    output = "\n".join(printed)
    assert "не записаны поля: backend.completions_url" in output
    assert "✓ Готово" not in output


def test_on_the_without_profile_stage_a_refused_write_names_the_deactivation(
        tmp_path, monkeypatch):
    """Вторая половина находки: на ступени без профиля указатель снимается ДО
    транзакции реестра и в откате не участвует. Отказ дальше по цепочке оставлял
    человека с «конфиг не записан» — он уверен, что профиль активен, а следующий
    старт идёт без профиля. Итог обязан назвать снятие. Указатель снимается
    настоящей записью; отказывает только плоская."""
    home = _broken_profile_stand(tmp_path, monkeypatch)
    real_write_values = onboarding_config_writer.write_values

    def refusing_flat_write(path, values, **kwargs):
        if "profile" in values:                      # снятие указателя — настоящее
            return real_write_values(path, values, **kwargs)
        raise ValueError(_REFUSAL)

    monkeypatch.setattr(onboarding_config_writer, "write_values", refusing_flat_write)
    loaded = onboarding.load_config_for_repair()
    assert loaded.stage == onboarding.REPAIR_STAGE_WITHOUT_PROFILE

    printed: list = []
    it = iter(_ANSWERS)
    io = wz.WizardIO(reader=lambda p, secret: next(it, ""), printer=printed.append)
    configured = wz.run(loaded.config, home / "config.yml", review_all=True, io=io,
                        deactivate_profile=True)

    assert configured is False
    raw = yaml.safe_load((home / "config.yml").read_text(encoding="utf-8"))
    assert raw["profile"] == ""                                 # указатель снят — это уже случилось
    assert raw["slots"]["default"] == "claude-fable-5"          # и реестр лёг
    output = "\n".join(printed)
    assert "профиль деактивирован" in output
    assert "записи моделей" in output
    assert "конфиг не записан" not in output
    assert "✓ Готово" not in output


def test_the_outcome_names_exactly_what_the_writer_says_landed(tmp_path, monkeypatch):
    """Контракт: `write_wizard_values` отдаёт в `PartialWriteError.written` имена уже
    состоявшихся записей — и мастер печатает ровно их, не выводя состав из порядка
    записи. Здесь лёг только указатель профиля (отказала транзакция реестра): по
    догадке «реестр ушёл первым» мастер назвал бы и его. Писатель подменён целиком —
    предмет теста в том, что печатает мастер по ответу писателя, а не в том, когда
    писатель этот ответ даёт (это проверяет `test_config_repair_ladder`); стенд
    здоровый, чтобы перечитывание не примешало свой отказ."""
    home = _broken_profile_stand(tmp_path, monkeypatch, slot="fast")

    def refusing_after_the_pointer(config_path, values, *, deactivate_profile=False):
        raise onboarding.PartialWriteError("алиас «strong» не найден",
                                           written=("profile_pointer",))

    monkeypatch.setattr(onboarding, "write_wizard_values", refusing_after_the_pointer)
    loaded = onboarding.load_config_for_repair()

    printed: list = []
    it = iter(_ANSWERS)
    io = wz.WizardIO(reader=lambda p, secret: next(it, ""), printer=printed.append)
    configured = wz.run(loaded.config, home / "config.yml", review_all=True, io=io,
                        deactivate_profile=True)

    assert configured is False
    output = "\n".join(printed)
    assert "профиль деактивирован" in output
    assert "записи моделей" not in output       # реестр не лёг — и не назван
    assert "конфиг не записан" not in output
    assert "✓ Готово" not in output


# --- прерывание: Ctrl-C — не «✓ Готово» ------------------------------------

def test_an_interrupted_wizard_is_not_reported_as_done(tmp_path, monkeypatch):
    """Человек прервал настройку (Ctrl-C / EOF) — а мастер шёл дальше: перечитывал
    конфиг и на настроенном печатал «✓ Готово», сводку трёх уровней и отдавал успех.
    `gc-cli --init-config && …` продолжал так, будто человек всё подтвердил, а
    прерывание он выбрал ровно затем, чтобы этого не случилось.

    Стенд здоровый и настроенный: именно на таком рапорт и появлялся."""
    home = _broken_profile_stand(tmp_path, monkeypatch, slot="fast")
    before = (home / "config.yml").read_text(encoding="utf-8")

    def interrupting_reader(prompt, secret):
        raise KeyboardInterrupt

    printed: list = []
    io = wz.WizardIO(reader=interrupting_reader, printer=printed.append)
    configured = wz.run(onboarding.load_config_for_repair().config, home / "config.yml",
                        review_all=True, io=io, mcp=_FakeMcp())

    assert configured is False
    assert (home / "config.yml").read_text(encoding="utf-8") == before
    output = "\n".join(printed)
    assert "Прервано" in output
    assert "✓ Готово" not in output
    assert "qwen/qwen3-8b" not in output          # и сводки о записанном тоже нет
