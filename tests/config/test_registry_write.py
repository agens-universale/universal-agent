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

    upsert_model(path, "flash", {"token": None}, template)

    entry = _models(path)["flash"]
    assert "token" not in entry                        # ключ со значением None — удалён
    assert entry["url"] == "https://openrouter.ai/api/v1/chat/completions"


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
    upsert_model(path, "opus-4.8", {"url": "https://other/v1"}, template)

    models = _models(path)
    assert list(models) == ["opus-4.8"]
    assert models["opus-4.8"] == {
        "model": "anthropic/claude-opus-4.8", "url": "https://other/v1"}


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
        ("upsert_model", "opus", {"url": "https://other/v1"}),
        ("write_slot", "default", "flash"),
    ], template)

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["models"]["opus"] == {
        "model": "anthropic/opus", "url": "https://other/v1"}


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
    assert load_profile_document(path) == {"models": {}, "slots": {}}


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
        validate_profile_text("models:\n  light:\n    url: http://x\n")   # зарезервированный алиас
    validate_profile_text("models:\n  flash:\n    url: http://x\nslots:\n  default: flash\n")


def test_validate_profile_text_rejects_broken_yaml():
    with pytest.raises(ValueError):
        validate_profile_text("models: [незакрытый\n  список")
