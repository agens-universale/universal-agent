"""Слой записи секций моделей — `config_modules/registry_write.py` (волна B, Task 1).

Операции реестра (`upsert_model`/`remove_model`/`write_slot`/`write_assign`) и их
группа `apply_registry_ops` — единственный вход записи для UI: правки ложатся на
текст цели, валидируется РЕЗУЛЬТАТ (config.yml — фаза 1 схемы + фаза 2 ссылочной
целостности; профиль — фаза 1 профильного текст-валидатора) и только потом идёт
одна атомарная запись. Невалидно — `RegistryWriteError`, файл байт-в-байт не тронут.

Плюс профильная сторона слоя записи: `PROFILE_SKELETON` (бланк нового файла) и
`validate_profile_text` (pre-write валидатор текста профиля).
"""
from pathlib import Path

import pytest
import yaml

import onboarding_config_writer as w
from config import _default_config_path
from config_modules.profiles import (
    PROFILE_SKELETON,
    load_profile_document,
    validate_profile_text,
)
from config_modules.registry_write import (
    RegistryWriteError,
    apply_registry_ops,
    remove_model,
    upsert_model,
    write_assign,
    write_slot,
)


_BASE = (
    "# основной конфиг\n"
    'profile: ""\n'
    "level: default\n"
    "backend:\n"
    '  completions_url: "http://real/v1/chat/completions"\n'
)

_REGISTRY = (
    'profile: ""\n'
    "level: default\n"
    "models:\n"
    "  flash:\n"
    '    model: "google/gemini-2.5-flash"\n'
    '    url: "https://openrouter.ai/api/v1/chat/completions"\n'
    '    token: "sk-secret"\n'
    "    pricing:\n"
    "      input: 0.3\n"
    "      output: 1.2\n"
    "    backend:\n"
    "      request_timeout_sec: 120\n"
    "  glm:\n"
    '    model: "zhipu/glm"\n'
    "slots:\n"
    '  default: "flash"\n'
    "backend:\n"
    '  completions_url: "http://real/v1/chat/completions"\n'
)


@pytest.fixture
def template() -> str:
    return Path(_default_config_path()).read_text(encoding="utf-8")


def _cfg(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(body, encoding="utf-8")
    return path


def _models(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["models"]


# --- upsert_model -------------------------------------------------------------


def test_upsert_model_creates_section_and_entry(tmp_path, template):
    path = _cfg(tmp_path, _BASE)

    upsert_model(path, "flash", {"model": "google/gemini-2.5-flash"}, template)

    assert _models(path) == {"flash": {"model": "google/gemini-2.5-flash"}}
    assert 'completions_url: "http://real/v1/chat/completions"' in path.read_text(encoding="utf-8")


def test_upsert_model_merge_preserves_pricing_and_backend(tmp_path, template):
    """Критичная находка: замена записи целиком стирала `pricing`/`backend`."""
    path = _cfg(tmp_path, _REGISTRY)

    upsert_model(path, "flash", {"url": "https://other/v1"}, template)

    entry = _models(path)["flash"]
    assert entry["url"] == "https://other/v1"          # ключ со значением — установлен
    assert entry["pricing"] == {"input": 0.3, "output": 1.2}   # отсутствующий — как был
    assert entry["backend"] == {"request_timeout_sec": 120}
    assert entry["model"] == "google/gemini-2.5-flash"
    assert entry["token"] == "sk-secret"


def test_upsert_model_merge_removes_key_passed_as_none(tmp_path, template):
    path = _cfg(tmp_path, _REGISTRY)

    upsert_model(path, "flash", {"model": None}, template)

    entry = _models(path)["flash"]
    assert "model" not in entry                        # ключ со значением None — удалён
    assert entry["url"] == "https://openrouter.ai/api/v1/chat/completions"   # сосед как был


# --- M3 (ревью MR #80): пересборка блока не теряет комментарии и не рвёт якоря --


_COMMENTED_REGISTRY = (
    'profile: ""\n'
    "level: default\n"
    "models:\n"
    "  # прод-эндпоинт, не трогать без согласования\n"
    "  flash:\n"
    '    model: "old-model"  # выбран на бенче, спорно\n'
    '    url: "https://prod/v1"  # инлайновый на url\n'
    '    token: "sk-secret"\n'
    "slots:\n"
    '  default: "flash"\n'
    "backend:\n"
    '  completions_url: "http://real/v1/chat/completions"\n'
)


def test_upsert_model_carries_comments_of_untouched_lines(tmp_path, template):
    """Правка ОДНОГО поля пересобирает блок записи через reparse+reserialize —
    прикреплённый комментарий записи и трейлинг-комментарии НЕТРОНУТЫХ соседних
    строк молча терялись: результат схемно валиден, валидация потерю не ловит,
    бэкапа нет. Трейлинг самой ПРАВИМОЙ строки не переносится намеренно: он
    описывал старое значение, приписать его новому — соврать (терялся и в BASE)."""
    path = _cfg(tmp_path, _COMMENTED_REGISTRY)

    upsert_model(path, "flash", {"model": "new-model"}, template)

    text = path.read_text(encoding="utf-8")
    assert "# прод-эндпоинт, не трогать без согласования" in text
    assert "# инлайновый на url" in text            # сосед не тронут — комментарий цел
    assert "# выбран на бенче" not in text          # правимая строка — комментарий ушёл
    assert _models(path)["flash"] == {
        "model": "new-model", "url": "https://prod/v1", "token": "sk-secret"}


_ANCHORED_REGISTRY = (
    'profile: ""\n'
    "level: default\n"
    "models:\n"
    "  proto: &proto\n"
    '    model: "base-model"\n'
    "  flash:\n"
    "    <<: *proto\n"
    '    url: "https://prod/v1"\n'
    '    token: "sk-x"\n'
    "slots:\n"
    '  default: "flash"\n'
    "backend:\n"
    '  completions_url: "http://real/v1/chat/completions"\n'
)


def test_upsert_model_refuses_an_entry_that_uses_yaml_anchors(tmp_path, template):
    """reparse+reserialize разворачивает `<<: *proto` в обычные ключи — связь с
    якорем рвётся МОЛЧА: текст валиден, запись немедленная, отмены нет. Молча
    инлайнить — порча, предупреждать постфактум — поздно; единственный честный
    исход — отказ ДО записи, файл байт-в-байт цел."""
    path = _cfg(tmp_path, _ANCHORED_REGISTRY)
    before = path.read_bytes()

    with pytest.raises(RegistryWriteError, match="якор"):
        upsert_model(path, "flash", {"model": "new"}, template)

    assert path.read_bytes() == before


def test_upsert_model_refuses_to_silently_drop_an_anchor_declaration(tmp_path, template):
    """Якорь без единой ссылки НА СЕГОДНЯ: пересборка молча роняла `&addr` —
    результат валиден, потерю не ловил никто. Тот же отказ, что и у alias/merge."""
    body = (
        'profile: ""\n'
        "level: default\n"
        "models:\n"
        "  flash:\n"
        '    url: &addr "https://prod/v1"\n'
        '    token: "sk-x"\n'
        '    model: "old"\n'
        "slots:\n"
        '  default: "flash"\n'
    )
    path = _cfg(tmp_path, body)
    before = path.read_bytes()

    with pytest.raises(RegistryWriteError, match="якор"):
        upsert_model(path, "flash", {"model": "new"}, template)

    assert path.read_bytes() == before


def test_editing_the_anchor_owner_is_still_refused(tmp_path, template):
    """Сторож: правка записи-ВЛАДЕЛЬЦА якоря отбивалась и раньше (пересборка
    роняет `&proto`, и `*proto` соседа перестаёт разбираться — отказ валидации);
    отказ обязан остаться отказом, а файл — целым."""
    path = _cfg(tmp_path, _ANCHORED_REGISTRY)
    before = path.read_bytes()

    with pytest.raises(RegistryWriteError):
        upsert_model(path, "proto", {"model": "new"}, template)

    assert path.read_bytes() == before


def test_a_quoted_star_value_is_not_mistaken_for_an_alias(tmp_path, template):
    """Сторож от текстового пере-детекта: `"*wild*"` в кавычках — строка, не
    alias. Вопрос «запись использует якоря?» задаётся парсеру (потоку событий),
    а не байтам строки — легальная запись правится свободно."""
    body = (
        "models:\n"
        "  flash:\n"
        '    model: "*wild*"\n'
        '    url: "https://x/v1"\n'
        '    token: "sk-x"\n'
        "slots:\n"
        '  default: "flash"\n'
    )
    path = _cfg(tmp_path, body)

    upsert_model(path, "flash", {"model": "new"}, template)

    assert _models(path)["flash"]["model"] == "new"


def test_write_slot_keeps_the_attached_comment_of_the_slot_line(tmp_path, template):
    """Тот же класс у однострочных записей: прикреплённый комментарий слота
    переживает смену значения (трейлинг правимой строки — нет, как и у записи
    реестра)."""
    body = (
        'models:\n  a:\n    model: "x"\n  b:\n    model: "y"\n'
        "slots:\n"
        "  # осознанный выбор: см. решение от 2026-08\n"
        '  default: "a"\n'
    )
    path = _cfg(tmp_path, body)

    write_slot(path, "default", "b", template)

    text = path.read_text(encoding="utf-8")
    assert "# осознанный выбор: см. решение от 2026-08" in text
    assert yaml.safe_load(text)["slots"] == {"default": "b"}


def test_write_assign_keeps_the_attached_comment_of_the_binding(tmp_path, template):
    body = (
        'models:\n  a:\n    model: "x"\n'
        "assign:\n"
        "  # гейт слабой моделью не судим\n"
        '  finish_gate: "a"\n'
    )
    path = _cfg(tmp_path, body)

    write_assign(path, "finish_gate", "light", template)

    text = path.read_text(encoding="utf-8")
    assert "# гейт слабой моделью не судим" in text
    assert yaml.safe_load(text)["assign"] == {"finish_gate": "light"}


def test_upsert_model_leaving_half_a_pair_is_rejected(tmp_path, template):
    """Слой записи валидирует РЕЗУЛЬТАТ, значит правило пары (§2) действует и на нём:
    снять `token` у записи, которая держит свой адрес, — это оставить половину, после
    которой общий ключ ушёл бы на её хост. Файл не тронут ни на байт, как на любом
    другом отказе валидации."""
    path = _cfg(tmp_path, _REGISTRY)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteError):
        upsert_model(path, "flash", {"token": None}, template)

    assert path.read_text(encoding="utf-8") == before


def test_upsert_model_keeps_neighbour_entry(tmp_path, template):
    path = _cfg(tmp_path, _REGISTRY)

    upsert_model(path, "flash", {"model": "google/gemini-3"}, template)

    assert _models(path)["glm"] == {"model": "zhipu/glm"}


def test_upsert_model_is_idempotent(tmp_path, template):
    path = _cfg(tmp_path, _BASE)

    upsert_model(path, "flash", {"model": "google/gemini-2.5-flash"}, template)
    once = path.read_text(encoding="utf-8")
    upsert_model(path, "flash", {"model": "google/gemini-2.5-flash"}, template)

    assert path.read_text(encoding="utf-8") == once


def test_upsert_model_invalid_result_leaves_file_untouched(tmp_path, template):
    """Зарезервированный алиас не проходит схему — файл не тронут, включая якорь
    секции (он тоже часть невыполненной записи)."""
    path = _cfg(tmp_path, _BASE)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteError):
        upsert_model(path, "light", {"model": "m"}, template)

    assert path.read_text(encoding="utf-8") == before


def test_upsert_model_schema_conflict_does_not_leak_the_token(tmp_path, template):
    """Ревью MR #80 (L28): запись токена поверх записи, у которой уже задан
    `backend.chat_completion_headers.Authorization`, валится правилом пары
    `ModelEntry` — а pydantic-дамп `ValidationError` несёт `input_value=<repr
    записи>`, то есть только что набранный токен целиком. Отказ обязан назвать
    координату (`models.<alias>`) и причину, но не сам секрет."""
    body = (
        'profile: ""\n'
        "level: default\n"
        "models:\n"
        "  flash:\n"
        '    model: "google/gemini-2.5-flash"\n'
        "    backend:\n"
        "      chat_completion_headers:\n"
        '        Authorization: "Bearer OLD-VALUE"\n'
        "slots:\n"
        '  default: "flash"\n'
    )
    path = _cfg(tmp_path, body)

    with pytest.raises(RegistryWriteError) as excinfo:
        upsert_model(path, "flash", {"token": "sk-MY-OWN-SECRET"}, template)

    message = str(excinfo.value)
    assert "sk-MY-OWN-SECRET" not in message
    assert "input_value" not in message
    assert "Value error" not in message   # служебная обёртка pydantic — не причина
    assert "models.flash" in message
    assert "token и backend.chat_completion_headers.Authorization заданы одновременно" in message


def test_profile_upsert_schema_conflict_does_not_leak_the_token(tmp_path):
    """Близнец теста выше для ФАЙЛА ПРОФИЛЯ: неактивный профиль судится фазой 1
    (`profiles._validate_profile_document`), и там `AppConfig.model_validate`
    бросал СЫРОЙ pydantic-`ValidationError` — тот же `input_value` с только что
    набранным токеном уезжал в нотис секции «Модели» этим, вторым путём. Класс
    один: любой `str(ValidationError)`, доходящий до человека."""
    path = tmp_path / "work.yml"
    path.write_text(
        "models:\n"
        "  flash:\n"
        '    model: "google/gemini-2.5-flash"\n'
        "    backend:\n"
        "      chat_completion_headers:\n"
        '        Authorization: "Bearer OLD-VALUE"\n',
        encoding="utf-8")

    with pytest.raises(RegistryWriteError) as excinfo:
        upsert_model(path, "flash", {"token": "sk-MY-OWN-SECRET"}, "",
                     is_profile=True)

    message = str(excinfo.value)
    assert "sk-MY-OWN-SECRET" not in message
    assert "input_value" not in message
    assert "models.flash" in message


def test_upsert_model_into_file_without_trailing_newline(tmp_path):
    """Файл после ручной правки часто остаётся без завершающего перевода строки, а
    реестр — последней секцией: вторая запись не должна упираться в этот шов."""
    path = tmp_path / "work.yml"
    path.write_text('models:\n  glm:\n    model: "zhipu/glm"', encoding="utf-8")   # без \n

    upsert_model(path, "flash", {"model": "google/flash"}, "", is_profile=True)

    assert load_profile_document(path)["models"] == {
        "glm": {"model": "zhipu/glm"}, "flash": {"model": "google/flash"}}


def test_upsert_model_alias_with_dot_replaces_not_duplicates(tmp_path, template):
    path = _cfg(tmp_path, _BASE)

    upsert_model(path, "opus-4.8", {"model": "anthropic/claude-opus-4.8"}, template)
    upsert_model(path, "opus-4.8", {"url": "https://other/v1", "token": "sk-own"}, template)

    models = _models(path)
    assert list(models) == ["opus-4.8"]
    assert models["opus-4.8"] == {
        "model": "anthropic/claude-opus-4.8", "url": "https://other/v1", "token": "sk-own"}


def test_upsert_model_quotes_yaml_scalar_alias(tmp_path, template):
    """Алиас, который YAML разобрал бы НЕ строкой (`123`, `true`, `null`, `~`), уходит
    в кавычках: без них ключ реестра становится числом/булевым/None, запись валится
    сырым текстом pydantic — и это худший момент для отказа, плоское подключение к
    этому месту уже записано. Провайдерские id вида `123` встречаются у локальных
    раздач моделей, вход достижим через `default_alias_for`."""
    for alias in ("123", "true", "null", "~", "0755"):
        path = _cfg(tmp_path, _BASE)

        upsert_model(path, alias, {"model": alias}, template)

        assert _models(path) == {alias: {"model": alias}}, alias
        # снятие идёт тем же ключом — поиск и запись обязаны видеть одну форму
        remove_model(path, alias)
        assert _models(path) in ({}, None), alias


# --- remove_model -------------------------------------------------------------


def test_remove_model_with_references_reports_without_writing(tmp_path):
    path = _cfg(tmp_path, _REGISTRY)
    before = path.read_text(encoding="utf-8")

    report = remove_model(path, "flash")

    assert report.removed is False
    assert report.references == ["slots.default"]
    assert path.read_text(encoding="utf-8") == before


def test_remove_model_force_cleans_references(tmp_path):
    path = _cfg(tmp_path, _REGISTRY)

    report = remove_model(path, "flash", force=True)

    assert report.removed is True
    assert report.references == ["slots.default"]
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["models"] == {"glm": {"model": "zhipu/glm"}}
    assert raw["slots"] in ({}, None) or raw["slots"].get("default") is None


def test_remove_model_without_references_writes(tmp_path):
    path = _cfg(tmp_path, _REGISTRY)

    report = remove_model(path, "glm")

    assert report.removed is True
    assert report.references == []
    assert list(_models(path)) == ["flash"]


def test_remove_model_reports_assign_references(tmp_path):
    body = _REGISTRY + "assign:\n  summary: \"glm\"\n  subagents:\n    subagent_critic: \"glm\"\n"
    path = _cfg(tmp_path, body)

    report = remove_model(path, "glm")

    assert report.removed is False
    assert report.references == ["assign.summary", "assign.subagents.subagent_critic"]


def test_remove_model_missing_alias_is_noop(tmp_path):
    path = _cfg(tmp_path, _REGISTRY)
    before = path.read_text(encoding="utf-8")

    report = remove_model(path, "нет-такого")

    assert report.removed is False and report.references == []
    assert path.read_text(encoding="utf-8") == before


# --- write_slot ---------------------------------------------------------------


def test_write_slot_sets_and_clears(tmp_path, template):
    path = _cfg(tmp_path, _REGISTRY)

    write_slot(path, "light", "glm", template)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["slots"]["light"] == "glm"

    write_slot(path, "light", None, template)
    slots = yaml.safe_load(path.read_text(encoding="utf-8"))["slots"]
    assert "light" not in slots and slots["default"] == "flash"


def test_write_slot_creates_section_when_absent(tmp_path, template):
    path = _cfg(tmp_path, _BASE)
    upsert_model(path, "flash", {"model": "m"}, template)

    write_slot(path, "default", "flash", template)

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["slots"]["default"] == "flash"


def test_write_slot_dangling_alias_leaves_file_untouched(tmp_path, template):
    """Фаза 2 (ссылочная целостность) — иначе на диск ушёл бы config.yml,
    который следующий запуск не загрузит."""
    path = _cfg(tmp_path, _REGISTRY)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteError):
        write_slot(path, "light", "нет-такого", template)

    assert path.read_text(encoding="utf-8") == before


def test_write_slot_normalizes_a_non_string_alias_from_a_bare_numeric_yaml_key(
        tmp_path, template):
    """[ревью #4] Алиас `4.1` в YAML (без кавычек) разбирается `yaml.safe_load`
    ФЛОУТОМ, не строкой — картина, с которой видит его любой код, читающий
    реестр из документа (пикер значений слота, например). Аргумент `alias`
    сюда доезжает тем же флоутом — баг нормализации типа, а не механический
    сбой: `write_slot` обязан привести его к строке на границе, а не падать
    `AttributeError` внутри `quote_scalar` (`'float' object has no attribute
    'replace'`), проглоченным общим перехватом `_apply_ops_to_text`."""
    path = _cfg(tmp_path, _BASE.replace(
        'level: default\n', 'level: default\nmodels:\n  "4.1":\n    model: "x"\n'))

    write_slot(path, "default", 4.1, template)

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["slots"]["default"] == "4.1"


def test_write_slot_clearing_last_slot_of_multi_model_registry_is_rejected(tmp_path, template):
    path = _cfg(tmp_path, _REGISTRY)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteError):
        write_slot(path, "default", None, template)

    assert path.read_text(encoding="utf-8") == before


# --- write_assign -------------------------------------------------------------


def test_write_assign_scalar_consumer(tmp_path, template):
    path = _cfg(tmp_path, _REGISTRY)

    write_assign(path, "finish_gate", "light", template)

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["assign"]["finish_gate"] == "light"


def test_write_assign_nested_subagent(tmp_path, template):
    path = _cfg(tmp_path, _REGISTRY)

    write_assign(path, "subagents.subagent_critic", "flash", template)

    assign = yaml.safe_load(path.read_text(encoding="utf-8"))["assign"]
    assert assign["subagents"] == {"subagent_critic": "flash"}


def test_write_assign_wildcard_lands_under_subagents(tmp_path, template):
    path = _cfg(tmp_path, _REGISTRY)

    write_assign(path, "*", "light", template)

    assign = yaml.safe_load(path.read_text(encoding="utf-8"))["assign"]
    assert assign["subagents"] == {"*": "light"}
    assert '"*"' in path.read_text(encoding="utf-8")   # ключ закавычен — иначе не YAML


def test_write_assign_clears_value(tmp_path, template):
    path = _cfg(tmp_path, _REGISTRY)
    write_assign(path, "subagents.subagent_critic", "flash", template)
    write_assign(path, "*", "light", template)

    write_assign(path, "subagents.subagent_critic", None, template)

    assign = yaml.safe_load(path.read_text(encoding="utf-8"))["assign"]
    assert assign["subagents"] == {"*": "light"}


def test_write_assign_is_idempotent(tmp_path, template):
    path = _cfg(tmp_path, _REGISTRY)

    write_assign(path, "summary", "flash", template)
    once = path.read_text(encoding="utf-8")
    write_assign(path, "summary", "flash", template)

    assert path.read_text(encoding="utf-8") == once


def test_write_assign_unknown_subagent_leaves_file_untouched(tmp_path, template):
    path = _cfg(tmp_path, _REGISTRY)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteError):
        write_assign(path, "subagents.нет-такого", "flash", template)

    assert path.read_text(encoding="utf-8") == before


# --- транзакция: одна валидация и одна запись на группу операций --------------

_SINGLE_MODEL = (
    'profile: ""\n'
    "level: default\n"
    "models:\n"
    "  flash:\n"
    '    model: "google/gemini-2.5-flash"\n'
    "backend:\n"
    '  completions_url: "http://real/v1/chat/completions"\n'
)


def test_second_model_alone_is_rejected(tmp_path, template):
    """Реестр из одной модели без слотов валиден, из двух — нет (нужен слот).
    Поодиночке вторую модель добавить невозможно: это и есть повод к транзакции."""
    path = _cfg(tmp_path, _SINGLE_MODEL)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteError) as exc:
        upsert_model(path, "opus", {"model": "anthropic/opus"}, template)

    assert "slots" in str(exc.value)                   # сообщение называет причину
    assert path.read_text(encoding="utf-8") == before


def test_transaction_adds_second_model_with_slot(tmp_path, template):
    path = _cfg(tmp_path, _SINGLE_MODEL)

    apply_registry_ops(path, [
        ("upsert_model", "opus", {"model": "anthropic/opus"}),
        ("write_slot", "default", "flash"),
    ], template)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert sorted(raw["models"]) == ["flash", "opus"]
    assert raw["slots"]["default"] == "flash"


def test_transaction_failure_leaves_file_untouched(tmp_path, template):
    """Падает ВТОРАЯ из трёх операций — не пишется ничего, включая успевшую первую."""
    path = _cfg(tmp_path, _REGISTRY)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteError) as exc:
        apply_registry_ops(path, [
            ("upsert_model", "opus", {"model": "anthropic/opus"}),
            ("write_slot", "light", "нет-такого"),
            ("write_assign", "summary", "flash"),
        ], template)

    assert "нет-такого" in str(exc.value)
    assert path.read_text(encoding="utf-8") == before


def test_transaction_applies_ops_in_order(tmp_path, template):
    path = _cfg(tmp_path, _REGISTRY)

    apply_registry_ops(path, [
        ("write_slot", "light", "flash"),
        ("write_slot", "light", "glm"),
    ], template)

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["slots"]["light"] == "glm"


def test_transaction_second_upsert_merges_onto_first(tmp_path, template):
    """Вторая операция видит результат первой (текст в памяти), а не диск."""
    path = _cfg(tmp_path, _SINGLE_MODEL)

    apply_registry_ops(path, [
        ("upsert_model", "opus", {"model": "anthropic/opus"}),
        ("upsert_model", "opus", {"url": "https://other/v1", "token": "sk-own"}),
        ("write_slot", "default", "flash"),
    ], template)

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["models"]["opus"] == {
        "model": "anthropic/opus", "url": "https://other/v1", "token": "sk-own"}


def test_transaction_can_remove_model_together_with_its_references(tmp_path, template):
    path = _cfg(tmp_path, _REGISTRY)

    apply_registry_ops(path, [
        ("write_slot", "default", "glm"),
        ("remove_model", "flash"),
    ], template)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert list(raw["models"]) == ["glm"]
    assert raw["slots"]["default"] == "glm"


def test_transaction_remove_model_with_live_references_names_them(tmp_path, template):
    """В транзакции у `remove_model` нет канала отчёта — ссылки становятся ошибкой."""
    path = _cfg(tmp_path, _REGISTRY)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteError) as exc:
        apply_registry_ops(path, [("remove_model", "flash")], template)

    assert "slots.default" in str(exc.value)
    assert path.read_text(encoding="utf-8") == before


def test_transaction_unknown_operation_is_named(tmp_path, template):
    path = _cfg(tmp_path, _REGISTRY)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteError, match="write_everything"):
        apply_registry_ops(path, [("write_everything", "flash")], template)

    assert path.read_text(encoding="utf-8") == before


# --- снятие записи: фаза «текст не изменился — отказ» --------------------------
#
# Писатель адресует запись ПОСТРОЧНО, а «есть ли она» читает по разобранному
# документу: на рукописной инлайновой форме эти два взгляда расходятся, и
# снятие, ничего не снявшее, обязано отказать, а не отчитаться успехом.


_PROFILE_WITH_REFS = (
    "models:\n"
    "  opus:\n"
    '    model: "anthropic/claude-opus-4"\n'
    "slots:\n"
    '  default: "opus"\n'
    "assign:\n"
    "  subagents:\n"
    '    subagent_critic: "opus"\n'
)


def _profile(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "work.yml"
    path.write_text(body, encoding="utf-8")
    return path


# Рукописная flow-форма секции С ЗАПИСЯМИ: в документе запись есть, а СТРОКИ под
# неё нет — писатель правит файл построчно, и снять её текстом нечем.
_PROFILE_FLOW_ENTRIES = ('models: {flash: {model: "my/own"}}\n'
                         "slots:\n"
                         '  default: "flash"\n')


def test_remove_model_refuses_the_same_way_instead_of_stripping_references(tmp_path):
    """`remove_model` над flow-формой врал: запись оставалась на месте, а ссылки на
    неё снимались каскадом — отчёт `removed=True` о файле, в котором удалять было
    нечем. Отказ приходит ДО ссылок: запись снимается первой, и транзакция
    обрывается на ней — файл байт-в-байт."""
    path = _profile(tmp_path, _PROFILE_FLOW_ENTRIES)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteError, match="flash"):
        remove_model(path, "flash", force=True, is_profile=True)

    assert path.read_text(encoding="utf-8") == before


# Зеркальная конфигурация: запись БЛОЧНАЯ (снимается штатно), а секция ссылок —
# инлайновая. Каскад «снимал» ссылку, которой не касался, и оставлял её висеть на
# удалённом алиасе — у профиля это никто не ловит (фаза 2 к нему не применяется).
_PROFILE_INLINE_SLOTS = ("models:\n"
                         "  flash:\n"
                         '    model: "m"\n'
                         'slots: {default: "flash"}\n')
_PROFILE_INLINE_ASSIGN = ("models:\n"
                          "  flash:\n"
                          '    model: "m"\n'
                          "assign:\n"
                          '  subagents: {subagent_critic: "flash"}\n')


@pytest.mark.parametrize("body, reference", [
    (_PROFILE_INLINE_SLOTS, "slots.default"),
    (_PROFILE_INLINE_ASSIGN, "assign.subagents.subagent_critic"),
])
def test_remove_model_refuses_when_a_reference_cannot_be_stripped(tmp_path, body,
                                                                  reference):
    """Каскад обязан снять КАЖДУЮ ссылку, которую пообещал, — иначе он оставляет
    файл ровно в том состоянии, ради недопущения которого и заведён.

    Зеркало флоу-дефекта записи: ссылка живёт инлайновым мэппингом, строки под неё
    нет, и `remove_mapping_entry` молча возвращает текст — запись снималась, ссылка
    повисала на несуществующем алиасе, отчёт говорил «удалено». У config.yml такой
    итог отвергла бы фаза 2, у профиля не ловит никто (он валидируется частичным).
    Обещание докстринга «молча оставить нерезолвимый файл нельзя» должно быть
    правдой."""
    path = _profile(tmp_path, body)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteError, match=reference.rsplit(".", 1)[-1]):
        remove_model(path, "flash", force=True, is_profile=True)

    assert path.read_text(encoding="utf-8") == before


def test_a_deliberate_refusal_reaches_the_user_without_the_operation_name(tmp_path):
    """Отказ операции написан ДЛЯ ЧЕЛОВЕКА и доезжает до notice дословно.

    Служебное имя операции впереди («remove_model flash: строки записи «flash»…»)
    — служебный лог в пользовательском тексте: имя ему ничего не говорит, алиас
    повторяется дважды, строка распухает. Подпись операцией остаётся у
    МЕХАНИЧЕСКОГО сбоя — там фразы для человека нет вовсе, и «что сломалось»
    несёт только она.

    Бэктики в тексте отказа тоже недопустимы: TUI печатает notice как есть, и
    разметка markdown доезжает до человека буквально."""
    path = _profile(tmp_path, 'models: {flash: {model: "m"}}\n')

    with pytest.raises(RegistryWriteError) as exc:
        apply_registry_ops(path, [("remove_model", "flash", True)], "",
                           is_profile=True)

    assert "remove_model" not in str(exc.value)
    assert str(exc.value).startswith("строки записи «flash»")
    assert "`" not in str(exc.value)

    with pytest.raises(RegistryWriteError, match="remove_model"):
        apply_registry_ops(path, [("remove_model", "flash", {"opt": True})], "",
                           is_profile=True)


def test_removal_of_a_block_form_entry_is_untouched_by_that_check(tmp_path):
    """Проверка «текст не изменился» стоит на снятии САМОЙ записи и обычную
    блочную форму не трогает: каскад ссылок работает как работал, а отсутствующий
    алиас остаётся идемпотентным no-op (снимать там нечего — и отказывать не в
    чем)."""
    path = _cfg(tmp_path, _REGISTRY)

    report = remove_model(path, "flash", force=True)

    assert report.removed is True and report.references == ["slots.default"]
    assert list(_models(path)) == ["glm"]
    before = path.read_text(encoding="utf-8")
    assert remove_model(path, "нет-такого").removed is False
    assert path.read_text(encoding="utf-8") == before


def test_option_dict_in_the_force_slot_is_refused_not_silently_obeyed(tmp_path):
    """Аргументы операций доезжают ПОЗИЦИОННО, и «словарь опций» третьим элементом
    кортежа ложится в позицию `force`: truthy-словарь молча включал каскадное
    удаление ссылок — ровно обратное тому, чего от «опций» ждут (сверка
    исполнением). Форма отвергается ошибкой, называющей операцию; файл
    байт-в-байт, ссылки на месте."""
    path = _profile(tmp_path, _PROFILE_WITH_REFS)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteError, match="remove_model"):
        apply_registry_ops(path, [("remove_model", "opus", {"keep_references": True})],
                           "", is_profile=True)

    assert path.read_text(encoding="utf-8") == before


# --- цель-профиль: фаза 1, без ссылочной целостности --------------------------


def test_profile_target_slot_may_point_to_alias_from_base(tmp_path):
    """Профиль частичен: слот вправе ссылаться на алиас нижнего слоя, которого в
    самом файле профиля нет — фаза 2 к нему не применяется."""
    path = tmp_path / "work.yml"
    w.create_profile_file(path, PROFILE_SKELETON)

    write_slot(path, "default", "из-базы", "", is_profile=True)

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["slots"]["default"] == "из-базы"


def test_profile_target_rejects_invalid_entry(tmp_path):
    path = tmp_path / "work.yml"
    w.create_profile_file(path, PROFILE_SKELETON)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RegistryWriteError):
        upsert_model(path, "max", {"model": "m"}, "", is_profile=True)

    assert path.read_text(encoding="utf-8") == before


# --- PROFILE_SKELETON / validate_profile_text ---------------------------------


def test_profile_skeleton_is_blank_without_literal_token(tmp_path):
    path = tmp_path / "new_profile.yml"
    w.create_profile_file(path, PROFILE_SKELETON)

    assert "sk-" not in PROFILE_SKELETON            # бланк, не учебный пример с токеном
    # Ни одного КЛЮЧА (ревизия владельца): пустые `models: {}`/`slots: {}` делали
    # свежий профиль «переопределяющим» секции, которых он не трогал.
    assert load_profile_document(path) == {}
    assert PROFILE_SKELETON.lstrip().startswith("#")


def test_profile_skeleton_accepts_first_entry(tmp_path):
    path = tmp_path / "new_profile.yml"
    w.create_profile_file(path, PROFILE_SKELETON)

    upsert_model(path, "flash", {"model": "google/gemini-2.5-flash"}, "", is_profile=True)

    assert load_profile_document(path)["models"] == {"flash": {"model": "google/gemini-2.5-flash"}}


def test_validate_profile_text_rejects_profile_key_and_bad_types():
    with pytest.raises(ValueError, match="запрещ"):
        validate_profile_text("profile: other\nmodels: {}\n")
    with pytest.raises(ValueError):
        validate_profile_text("models: not-a-mapping\n")
    with pytest.raises(ValueError):
        # пара §2 задана целиком — отказ остаётся именно про зарезервированный алиас
        validate_profile_text("models:\n  light:\n    url: http://x\n    token: sk\n")
    validate_profile_text(
        "models:\n  flash:\n    url: http://x\n    token: sk\nslots:\n  default: flash\n")


def test_validate_profile_text_rejects_broken_yaml():
    with pytest.raises(ValueError):
        validate_profile_text("models: [незакрытый\n  список")
