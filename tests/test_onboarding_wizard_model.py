import onboarding
import onboarding_wizard as wz
from model_picker import ModelInfo


def _io(answers):
    it = iter(answers)
    out = []
    return wz.WizardIO(reader=lambda p, secret: next(it), printer=out.append), out


def test_model_pick_from_list():
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"},
           "model": onboarding.MODEL_SENTINEL}
    fake = lambda url, auth, **k: [ModelInfo("Qwen", tool_capable=True), ModelInfo("CodeChat", tool_capable=False)]
    io, _ = _io(["1"])   # выбрать пункт 1 (Qwen)
    values = {}
    wz._collect_model(io, cfg, values, fetch=fake)
    assert values["model"] == "Qwen"


def test_model_manual_fallback_when_no_list():
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"},
           "model": onboarding.MODEL_SENTINEL}
    fake = lambda url, auth, **k: None   # список не получен
    io, _ = _io(["vllm/Qwen3-Coder-Next-262k"])   # ручной ввод
    values = {}
    wz._collect_model(io, cfg, values, fetch=fake)
    assert values["model"] == "vllm/Qwen3-Coder-Next-262k"


def test_model_warning_shown_only_with_non_tool():
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"}, "model": onboarding.MODEL_SENTINEL}
    io, out = _io(["1"])
    wz._collect_model(io, cfg, {}, fetch=lambda *a, **k: [ModelInfo("Q", tool_capable=True)])
    assert not any("⚠" in m for m in out)   # все tool-capable → без ⚠


def test_model_manual_fallback_when_empty_list():
    """fetch_models -> [] (пустой список) → ручной ввод, не пустое меню."""
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"},
           "model": onboarding.MODEL_SENTINEL}
    fake = lambda url, auth, **k: []   # пустой список
    io, _ = _io(["vllm/SomeModel"])
    values = {}
    wz._collect_model(io, cfg, values, fetch=fake)
    assert values["model"] == "vllm/SomeModel"


def test_choice_is_confirmed_after_a_pick_and_silent_on_skip():
    """Промах на соседнюю строку (`claude-sonnet-5` / `claude-sonnet-5:batch`)
    раньше всплывал только в финальной сводке — после успешной записи, когда
    его уже не отменить не заметив. Подтверждение печатается сразу."""
    cfg = {"completions_url": "https://h/v1/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"}}
    io, out = _io(["1"])
    wz._collect_model(io, cfg, {}, fetch=lambda *a, **k: _models3())
    assert "  выбрано для default: gigacode" in out
    io2, out2 = _io([""])
    wz._collect_model(io2, cfg, {}, fetch=lambda *a, **k: _models3())
    assert not any(m.startswith("  выбрано для") for m in out2)


def test_choice_is_confirmed_after_manual_entry_when_no_list():
    """Тот же риск, что и со списком, только на другой ветке: без списка
    человек вписывает id вручную, и опечатка там всплыла бы только в
    финальной сводке — после записи в файл — если бы не эта строка."""
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"},
           "model": onboarding.MODEL_SENTINEL}
    io, out = _io(["vllm/Qwen3-Coder-Next-262k"])
    wz._collect_model(io, cfg, {}, fetch=lambda *a, **k: None)
    assert "  выбрано для default: vllm/Qwen3-Coder-Next-262k" in out


def test_network_is_reached_only_after_the_header_is_printed():
    """На недоступном эндпоинте мастер раньше уходил в сеть ДО заголовка и
    молчал десятки секунд, выглядя зависшим (подтверждено живым замером).
    `fetch` дописывает свою метку в тот же список вывода, что и `printer`, —
    так виден точный порядок событий."""
    out = []
    io = wz.WizardIO(reader=lambda p, s: "", printer=out.append)
    cfg = {"completions_url": "https://h/v1/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"}}

    def fake_fetch(url, auth, **k):
        out.append("<fetch>")
        return None

    wz._collect_model(io, cfg, {}, fetch=fake_fetch)
    assert out.index("  Модель") < out.index("<fetch>")
    assert out.index(f"  {wz._of.MODEL_FETCHING}") < out.index("<fetch>")


def test_model_warning_shown_when_non_tool_present():
    """⚠-подсказка появляется, если хотя бы одна модель tool_capable is False."""
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"}, "model": onboarding.MODEL_SENTINEL}
    io, out = _io(["1"])
    wz._collect_model(io, cfg, {}, fetch=lambda *a, **k: [
        ModelInfo("Q", tool_capable=True), ModelInfo("C", tool_capable=False)
    ])
    assert any("⚠ отмеченные модели" in m for m in out)


def test_model_section_header_and_marker_consistent_when_no_list():
    # единый плоский стиль: заголовок «  Модель» даже без списка, приглашение «  > », без labeled-текста
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"}, "model": onboarding.MODEL_SENTINEL}
    prompts, out = [], []
    io = wz.WizardIO(reader=lambda p, s: prompts.append(p) or "", printer=out.append)
    wz._collect_model(io, cfg, {}, fetch=lambda *a, **k: None)   # список не получен
    assert any(l.strip() == "Модель" for l in out)       # заголовок секции есть всегда
    assert "  > " in prompts                             # приглашение в общем стиле
    assert not any("Имя модели" in p for p in prompts)   # старое labeled-приглашение убрано


def _many(n: int) -> list:
    """n моделей с предсказуемыми id: m001 … mNNN."""
    return [ModelInfo(f"vendor/m{i:03d}", tool_capable=True) for i in range(1, n + 1)]


def _models3() -> list:
    """Три модели с говорящими именами — для тестов, где важен не размер списка,
    а поведение выбора. В файле такого хелпера ещё нет."""
    return [ModelInfo("gigacode", tool_capable=True),
            ModelInfo("glm-5.2-fp8", tool_capable=True),
            ModelInfo("openai/gpt-5.6-terra", tool_capable=True)]


def test_short_list_is_shown_whole_without_a_counter():
    models = _many(20)
    shown, note = wz._view_initial(models)
    assert shown == models
    assert note == ""


def test_long_list_is_cut_to_fifteen_with_a_counter():
    models = _many(397)
    shown, note = wz._view_initial(models)
    assert len(shown) == 15
    assert note == "показаны 15 из 397 · * — показать все"


def test_empty_input_means_keep():
    assert wz._interpret("", _many(3), _many(3)) == ("keep", "")


def test_star_expands_the_whole_list():
    assert wz._interpret("*", _many(30), _many(15))[0] == "all"


def test_a_number_picks_the_shown_row():
    models = _many(30)
    shown = models[:15]
    assert wz._interpret("2", models, shown) == ("pick", "vendor/m002")


def test_a_number_out_of_range_is_treated_as_a_query():
    """«99» строки не выбирает — и молча ничего не делать нельзя: это запрос,
    который просто ничего не найдёт."""
    models = _many(30)
    assert wz._interpret("99", models, models[:15])[0] == "ask"


def test_exact_id_is_picked_without_a_number():
    models = _many(30)
    assert wz._interpret("VENDOR/M007", models, models[:15]) == ("pick", "vendor/m007")


def test_a_query_narrows_the_view():
    models = _many(30) + [ModelInfo("qwen/qwen3-8b", tool_capable=True)]
    kind, (rows, note) = wz._interpret("qwen", models, models[:15])
    assert kind == "view"
    assert [m.id for m in rows] == ["qwen/qwen3-8b"]
    assert note == "найдено 1 из 31 · * — весь каталог"


def test_too_many_matches_are_cut_and_said_so():
    models = _many(40)
    kind, (rows, note) = wz._interpret("vendor", models, models[:15])
    assert kind == "view"
    assert len(rows) == 15
    assert note == "показаны 15 из 40 совпадений · уточните запрос · * — весь каталог"


def test_star_is_promised_in_both_result_counters():
    """`*` работает всегда, но раньше был назван только в счётчике начального
    показа — после запроса человек не знал, что клавиша всё ещё доступна, и
    что она разворачивает ВЕСЬ каталог, а не только совпадения."""
    models = _many(30) + [ModelInfo("qwen/qwen3-8b", tool_capable=True)]
    _, (_, note_found) = wz._interpret("qwen", models, models[:15])
    assert note_found.endswith(" · * — весь каталог")
    _, (_, note_cut) = wz._interpret("vendor", _many(40), _many(40)[:15])
    assert note_cut.endswith(" · * — весь каталог")


def test_a_query_without_matches_asks_to_take_it_as_is():
    assert wz._interpret("vllm/Own", _many(30), _many(15)) == ("ask", "vllm/Own")


def test_a_superscript_digit_is_a_query_not_a_crash():
    """Ревью: проверка «это номер» была шире, чем умеет разобрать `int()` —
    надстрочная `²` проходит `isdigit()`, а `int("²")` падает: мастер ронял
    трейсбек, и все ответы до этого места терялись. Такой ввод — обычный запрос,
    который ничего не найдёт."""
    assert wz._interpret("²", _many(3), _many(3)) == ("ask", "²")


def test_current_line_names_the_registry_entry():
    cfg = {"models": {"fast": {"model": "qwen/qwen3-8b"}},
           "slots": {"default": "fast"}}
    assert wz._current_line(cfg, "default", "qwen/qwen3-8b") == (
        "сейчас: qwen/qwen3-8b  (имя записи «fast») · Enter — оставить")


def test_current_line_without_a_slot_has_no_entry_name():
    assert wz._current_line({}, "default", "vllm/Own") == (
        "сейчас: vllm/Own · Enter — оставить")


def test_current_line_of_an_empty_level():
    assert wz._current_line({}, "light", "") == "сейчас: не задана · Enter — оставить"


def test_current_line_omits_the_entry_name_when_it_equals_the_id():
    """Запись с именем, совпадающим с id модели (типовой конфиг `models: {gigacode:
    {}}`), не должна повторять то же слово в скобках."""
    cfg = {"models": {"gigacode": {}}, "slots": {"light": "gigacode"}}
    assert wz._current_line(cfg, "light", "gigacode") == (
        "сейчас: gigacode · Enter — оставить")


def test_current_model_is_marked_in_the_list():
    io, out = _io(["1"])
    wz._choose_model(io, _models3(), current="glm-5.2-fp8")
    assert any("glm-5.2-fp8" in m and "← сейчас" in m for m in out)


def test_warning_note_is_printed_only_for_shown_models():
    """Пояснение к ⚠ — про показанные строки. Помеченная модель есть в каталоге,
    но в показанные пятнадцать не попала: объяснять пометку, которой человек не
    видит, значит говорить про чужой экран."""
    models = _many(20) + [ModelInfo("vendor/no-tools", tool_capable=False)]
    io, out = _io([""])
    wz._choose_model(io, models, current="")
    assert not any("⚠" in m for m in out)


def test_enter_keeps_the_current_model():
    io, _ = _io([""])
    assert wz._choose_model(io, _models3(), current="gigacode") is None


def test_blank_line_separates_each_list_print():
    """Экран не должен слипаться: пустая строка стоит перед КАЖДЫМ показом
    списка (и первым, и повторным после запроса) и перед приглашением после
    блока — а не между списком и его собственным счётчиком: счётчик подпись
    к списку, отрывать её от того, что она описывает, нельзя."""
    models = _models3() + [ModelInfo("qwen/qwen3-8b", tool_capable=True)]
    io, out = _io(["qwen", "1"])
    wz._choose_model(io, models, current="")

    first_row = out.index("  1) gigacode")           # первая строка первого списка
    last_row = out.index("  4) qwen/qwen3-8b")        # его последняя строка (счётчика нет)
    second_row = out.index("  1) qwen/qwen3-8b")      # первая строка списка после запроса
    counter = out.index("  найдено 1 из 4 · * — весь каталог")   # счётчик второго показа

    assert first_row > 0 and out[first_row - 1] == ""            # воздух ПЕРЕД списком
    assert last_row + 1 < len(out) and out[last_row + 1] == ""   # воздух ПОСЛЕ блока без счётчика
    assert second_row > 0 and out[second_row - 1] == ""          # воздух перед повторным списком
    assert counter + 1 < len(out) and out[counter + 1] == ""     # воздух ПОСЛЕ счётчика, не перед ним


def test_query_then_number_picks_from_the_matches():
    models = _many(30) + [ModelInfo("qwen/qwen3-8b", tool_capable=True),
                          ModelInfo("qwen/qwen3-235b", tool_capable=True)]
    io, _ = _io(["qwen", "2"])
    assert wz._choose_model(io, models, current="") == "qwen/qwen3-235b"


def test_exact_id_pick_of_a_non_tool_model_still_warns():
    """Ревью (M13): точный ввод id выбирает модель ДО всякой печати — на длинном
    каталоге 41-я модель с ⚠ в показанные 15 не попадала, и человек забирал
    модель без инструментов, ни разу не увидев предупреждения. Печатаем строку
    модели с ⚠ и пояснение в момент выбора."""
    models = _many(40) + [ModelInfo("vendor/legacy-no-tools", tool_capable=False)]
    io, out = _io(["vendor/legacy-no-tools"])
    assert wz._choose_model(io, models, current="") == "vendor/legacy-no-tools"
    assert any("vendor/legacy-no-tools ⚠" in m for m in out)
    assert any(wz._of.MODEL_WARN in m for m in out)


def test_exact_id_pick_of_a_tool_capable_model_stays_silent():
    """Отрицательный сторож: точный выбор модели С инструментами предупреждение
    не печатает — иначе ⚠ обесценивается."""
    models = _many(40)
    io, out = _io(["vendor/m031"])
    assert wz._choose_model(io, models, current="") == "vendor/m031"
    assert not any("⚠" in m for m in out)


def test_number_pick_of_a_shown_non_tool_model_does_not_double_warn():
    """Ревью #15: `_print_models` уже дорисовал ⚠ к строке и напечатал
    предупреждение под КОРОТКИМ каталогом (весь целиком в `shown`) — выбор ТОЙ ЖЕ
    строки номером не должен печатать его ещё раз. `_warn_if_tool_incapable`
    нацелен на путь точного id, который печать каталога обходит вовсе (см.
    `test_exact_id_pick_of_a_non_tool_model_still_warns`) — строка из `shown`
    предупреждение уже получила."""
    models = [ModelInfo("vendor/no-tools", tool_capable=False)] + _many(2)
    io, out = _io(["1"])
    assert wz._choose_model(io, models, current="") == "vendor/no-tools"
    assert out.count(f"  ⚠ {wz._of.MODEL_WARN}") == 1


def test_no_matches_taken_as_id_when_confirmed():
    io, out = _io(["vllm/Own", "y"])
    assert wz._choose_model(io, _models3(), current="") == "vllm/Own"
    assert any("таких у провайдера нет." in m for m in out)
    assert any("взять «vllm/Own» как есть?" in m for m in out)


def test_no_matches_declined_is_treated_as_a_new_query():
    """Отказ не должен стоить ещё одного ввода: ответ, который не «да» и не
    отказ («n»/«н»/«нет»), и не пусто, — это уже следующий запрос."""
    models = _models3() + [ModelInfo("qwen/qwen3-8b", tool_capable=True)]
    io, _ = _io(["vllm/Own", "qwen", "1"])
    assert wz._choose_model(io, models, current="") == "qwen/qwen3-8b"


def test_no_matches_declined_by_n_returns_to_the_list():
    """`n` — явный отказ, а против канона `[y/N]` (`cli.py:232-241`) поиск по
    одной букве «н» в каталоге бессмысленен: отказ возвращает к прежнему
    списку, а не превращается в запрос."""
    io, _ = _io(["vllm/Own", "n", "1"])
    assert wz._choose_model(io, _models3(), current="") == "gigacode"


def test_almost_no_word_is_still_a_query_not_a_decline():
    """Слово, похожее на отказ («но»), не входит в набор `n`/«н»/«нет»
    буквально — и не должно совпасть с ним по подстроке: это новый
    (безрезультатный) запрос, мастер снова спрашивает «взять как есть»."""
    io, out = _io(["vllm/Own", "но", "y"])
    assert wz._choose_model(io, _models3(), current="") == "но"
    assert out.count("  таких у провайдера нет.") == 2


def test_no_matches_prompt_names_three_outcomes():
    """Приглашение называет ВСЕ три исхода. Раньше про `n` там не было ни
    слова — и это самый вероятный промах живого прогона."""
    prompts = []
    seq = iter(["vllm/Own", "n", ""])
    io = wz.WizardIO(reader=lambda p, s: (prompts.append(p), next(seq))[1],
                     printer=lambda m: None)
    wz._choose_model(io, _models3(), current="")
    assert "  > (y — да · n — нет · другой запрос · Enter — пропустить шаг): " in prompts


def test_no_matches_enter_keeps_the_current_model():
    io, _ = _io(["vllm/Own", ""])
    assert wz._choose_model(io, _models3(), current="gigacode") is None


def test_star_prints_the_whole_list():
    io, out = _io(["*", "397"])
    models = _many(397)
    assert wz._choose_model(io, models, current="") == "vendor/m397"
    assert any("vendor/m397" in m for m in out)


def test_model_step_names_its_level():
    cfg = {"completions_url": "https://h/v1/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"}}
    io, out = _io([""])
    wz._collect_model(io, cfg, {}, fetch=lambda *a, **k: _models3())
    assert any("уровень default" in m for m in out)


# --- «сейчас» шага — про слот default, а не про активный уровень ---------------

def _two_level_registry() -> dict:
    return {"completions_url": "https://h/v1/chat/completions",
            "chat_completion_headers": {"Authorization": "Bearer x"},
            "models": {"fast": {"model": "qwen/qwen3-8b"},
                       "brain": {"model": "qwen/qwen3-235b"}},
            "slots": {"default": "fast", "max": "brain"}}


def _qwen2() -> list:
    return [ModelInfo("qwen/qwen3-8b", tool_capable=True),
            ModelInfo("qwen/qwen3-235b", tool_capable=True)]


def test_the_model_step_shows_the_default_slot_not_the_active_level():
    """Ревью: «сейчас» бралось `effective_model` — резолвом АКТИВНОГО уровня
    конфига. При `level: max` шаг про слот default показывал модель уровня max с
    именем записи слота default, галочка «← сейчас» вставала не на ту строку, а
    сводка после Enter называла другую модель. Шаг задаёт слот default — и
    «сейчас» говорит про него."""
    cfg = {**_two_level_registry(), "level": "max"}
    io, out = _io([""])
    wz._collect_model(io, cfg, {}, fetch=lambda *a, **k: _qwen2())
    assert "  сейчас: qwen/qwen3-8b  (имя записи «fast») · Enter — оставить" in out
    assert "  1) qwen/qwen3-8b  ← сейчас" in out
    assert not any("qwen3-235b" in m and "← сейчас" in m for m in out)


def test_the_model_step_keeps_showing_the_legacy_model_on_a_flat_config():
    """Сторож фоллбэка: без реестра слотов нет вовсе, единственная модель конфига —
    легаси `agent.model`, и действует она на любом уровне (`level` её не сдвигает).
    Не показать её значило бы отнять «сейчас» у всех конфигов до волны B."""
    cfg = {"completions_url": "https://h/v1/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"},
           "model": "gigacode_agent_v1.6_rc", "level": "max"}
    io, out = _io([""])
    wz._collect_model(io, cfg, {}, fetch=lambda *a, **k: None)
    assert "  сейчас: gigacode_agent_v1.6_rc · Enter — оставить" in out


def test_an_unset_default_slot_shows_the_fallback_model_with_its_source():
    """Ревью (M12): слота default нет, а max задан — строки «сейчас» не было
    вовсе, и человек шёл по шагу вслепую. Показываем модель, которой default
    работает СЕЙЧАС по фоллбэку, С ПОДПИСЬЮ об источнике (та же, что в сводке):
    «слот задан» строка не утверждает, а «← сейчас» встаёт на неё — Enter
    насквозь ничего не пишет. Обход матрицы фиксирован от default, от активного
    `level` ответ не зависит."""
    cfg = {**_two_level_registry(), "slots": {"max": "brain"}, "level": "light"}
    io, out = _io([""])
    values = {}
    wz._collect_model(io, cfg, values, fetch=lambda *a, **k: _qwen2())
    assert ("  сейчас: qwen/qwen3-235b  (default не задан — работает max)"
            " · Enter — оставить") in out
    assert any("qwen3-235b" in m and "← сейчас" in m for m in out)
    assert values == {}                                   # проход насквозь не пишет


def test_a_single_entry_registry_without_slots_is_still_shown_as_current():
    """Живой стенд чинилки: слот default жил в отброшенном профиле, в базе — одна
    запись реестра и ни одного слота. Резолв тут от уровня не зависит (единственная
    модель реестра работает на любом), и «сейчас» обязана её назвать — как и до
    правки про активный уровень; без имени записи в скобках: слот не задан."""
    cfg = {**_two_level_registry(), "models": {"fast": {"model": "qwen/qwen3-8b"}},
           "slots": {}, "level": "max"}
    io, out = _io([""])
    wz._collect_model(io, cfg, {}, fetch=lambda *a, **k: _qwen2())
    assert "  сейчас: qwen/qwen3-8b · Enter — оставить" in out
    assert "  1) qwen/qwen3-8b  ← сейчас" in out
