"""Стражи `demo_profile.yml` (docs/specs/2026-07-23-profiles-design.md §13):

- документ валиден профильной схемой — через реальный лоадер (`load_profile_document`),
  не копию его логики;
- список субагентов в `assign.subagents` демо == канонический реестр кода
  (`tools/subagents/registry.py::SUBAGENT_CLASSES`) == статический frozenset,
  которым фаза-2 валидатор (`config_modules/model_sections_validation.py`)
  проверяет ключи `assign.subagents` без импорта реестра (направление
  зависимостей — открытый пункт Task 2). Новый субагент, не заведённый во всех
  трёх местах, роняет этот тест.
- комментарии не расходятся со схемой: минимум — все ключи демо парсятся
  схемой (покрыто первым пунктом), плюс token-примеры демонстрируют обе
  семантики своего токена (значение vs `token: ""`).
"""

import pytest
import yaml
from gc_core.config_modules.model_sections_validation import _SUBAGENT_NAMES
from gc_core.config_modules.profiles import load_profile_document
from gc_core.tools.subagents.registry import SUBAGENT_CLASSES
from gc_testkit.paths import REPO

DEMO_PROFILE_PATH = REPO / "demo_profile.yml"


@pytest.fixture
def demo_profile_raw(monkeypatch) -> dict:
    # Файл использует env-интерполяцию для token — подставляем фиктивные значения,
    # чтобы load_profile_document не варнил на отсутствующие переменные окружения.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-placeholder")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-placeholder")
    return load_profile_document(DEMO_PROFILE_PATH)


def test_demo_profile_validates_against_profile_schema(demo_profile_raw):
    # load_profile_document уже бросает ValueError на невалидном документе
    # (фаза 1 AppConfig + запрет ключа profile) — если фикстура отдала raw, схема пройдена.
    assert isinstance(demo_profile_raw, dict)
    assert "profile" not in demo_profile_raw


def test_demo_profile_subagents_match_registry_and_validator_frozenset(demo_profile_raw):
    demo_subagents = {name for name in demo_profile_raw["assign"]["subagents"] if name != "*"}
    registry_names = {cls.agent_name for cls in SUBAGENT_CLASSES}

    assert demo_subagents == registry_names, (
        "demo_profile.yml assign.subagents разошёлся с реестром субагентов кода "
        f"(tools/subagents/registry.py): демо={sorted(demo_subagents)}, "
        f"реестр={sorted(registry_names)}"
    )
    assert demo_subagents == _SUBAGENT_NAMES, (
        "demo_profile.yml assign.subagents разошёлся со статическим frozenset "
        "валидатора (config_modules/model_sections_validation.py::_SUBAGENT_NAMES): "
        f"демо={sorted(demo_subagents)}, валидатор={sorted(_SUBAGENT_NAMES)}"
    )
    assert registry_names == _SUBAGENT_NAMES, (
        "Реестр субагентов кода разошёлся со статическим frozenset валидатора "
        "(config_modules/model_sections_validation.py::_SUBAGENT_NAMES) — новый "
        f"субагент не заведён в валидаторе: реестр={sorted(registry_names)}, "
        f"валидатор={sorted(_SUBAGENT_NAMES)}"
    )
    assert demo_profile_raw["assign"]["subagents"]["*"] in ("light", "default", "max")


def test_demo_profile_wildcard_and_pin_present(demo_profile_raw):
    subagents = demo_profile_raw["assign"]["subagents"]
    assert "*" in subagents
    # хотя бы один субагент — жёсткий пин алиасом (не уровнем), демонстрируя
    # механику "точный id сильнее wildcard"
    aliases = set(demo_profile_raw["models"])
    pinned = [name for name, value in subagents.items() if value in aliases]
    assert pinned, "ожидался хотя бы один субагент, пришпиленный алиасом (не уровнем)"


def test_demo_profile_covers_all_service_consumers(demo_profile_raw):
    assign = demo_profile_raw["assign"]
    for consumer in (
        "finish_gate",
        "text_answer_judge",
        "command_judge",
        "summary",
        "intent_scope",
        "memory_compaction",
        "auto_judge",
    ):
        assert consumer in assign, f"demo_profile.yml не привязывает потребителя {consumer}"


def test_demo_profile_models_demonstrate_both_token_semantics(demo_profile_raw):
    """Обе семантики СВОЕГО токена: значение (свой ключ провайдера) и `token: ""`
    («сервер не требует авторизации»). Третьей — «ключа token нет» — у записи со
    своим адресом больше не существует: адрес и токен задаются парой (§2), и
    справочник обязан показывать именно ту форму, которую примет валидатор."""
    models = demo_profile_raw["models"]
    assert any(entry.get("token") for entry in models.values()), "нет примера модели со своим token"
    assert any(entry.get("token") == "" for entry in models.values()), (
        'нет примера модели с token: "" (адрес без авторизации)'
    )


def test_demo_profile_has_full_and_minimal_registry_forms(demo_profile_raw):
    """Полная форма (model+url+token+pricing+backend) и минимальная. Минимальная
    после правила пары (§2) — это ровно `url` + `token`, а не «только url»."""
    models = demo_profile_raw["models"]
    full_form_keys = {"model", "url", "token", "pricing", "backend"}
    assert any(full_form_keys <= set(entry) for entry in models.values()), (
        "нет примера полной формы записи модели (model/url/token/pricing/backend)"
    )
    assert any(set(entry) == {"url", "token"} for entry in models.values()), (
        "нет примера минимальной формы записи модели (url + token)"
    )


def test_demo_profile_has_slots_and_overlay_example(demo_profile_raw):
    assert set(demo_profile_raw["slots"]) >= {"light", "default", "max"}
    # Оверлей-пример: гварды + суммаризация поверх обычных секций
    assert "security" in demo_profile_raw
    security = demo_profile_raw["security"]
    for guard in ("command_moderation", "write_guard", "read_guard", "url_policy"):
        assert guard in security
    assert "summary" in demo_profile_raw


def test_demo_profile_yaml_has_no_stray_top_level_keys():
    """Сырой YAML не содержит опечаток верхнеуровневых ключей — прогон через
    yaml.safe_load напрямую (без env-интерполяции), сверка с ожидаемым составом."""
    raw = yaml.safe_load(DEMO_PROFILE_PATH.read_text(encoding="utf-8"))
    assert set(raw) == {"models", "slots", "assign", "security", "summary"}
