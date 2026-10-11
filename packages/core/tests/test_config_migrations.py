"""`config_version` и пошаговые миграции формата файла-слоя.

Настоящих миграций пока нет (текущая версия — 1), поэтому механизм проверяется
синтетической миграцией 1 → 2, зарегистрированной только в тесте.
"""

import pytest
import yaml
from gc_core.config import load_settings
from gc_core.config_modules import env_vars, migrations
from gc_core.config_modules.root import AppConfig


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        env_vars, "managed_config_path", lambda: tmp_path / "managed.yml"
    )
    monkeypatch.setattr(env_vars, "project_local_config_path", lambda: None)

    def write(name: str, body: dict):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(body), encoding="utf-8")
        return path

    return write


def _rename_x_cl_id(raw):
    """Синтетический шаг 1 → 2: формат v1 звал `agent.x_cl_id` ключом
    `agent.client_id`."""
    agent = raw.get("agent")
    if isinstance(agent, dict) and "client_id" in agent:
        agent["x_cl_id"] = agent.pop("client_id")
        return raw, ["agent.client_id переименован в agent.x_cl_id"]
    return raw, []


@pytest.fixture
def version_two(monkeypatch):
    monkeypatch.setattr(migrations, "CURRENT_CONFIG_VERSION", 2)
    monkeypatch.setattr(migrations, "MIGRATIONS", {1: _rename_x_cl_id})


def test_current_version_is_one_and_is_the_default():
    assert migrations.CURRENT_CONFIG_VERSION == 1
    assert AppConfig().config_version == 1


def test_template_version_tracks_current():
    """Шаблоны бутстрапа пишут текущую версию — поднял версию, поправь шаблоны."""
    from gc_core.config import _default_config_path
    from gc_testkit.paths import REPO

    for path in (_default_config_path(), REPO / "demo_config.yml"):
        with open(path, encoding="utf-8") as fd:
            raw = yaml.safe_load(fd)
        assert raw["config_version"] == migrations.CURRENT_CONFIG_VERSION, path


def test_file_without_version_is_v1():
    assert migrations.document_version({}) == 1
    raw = {"agent": {"max_steps": 3}}
    doc, facts = migrations.migrate_document(raw)
    assert doc is raw and facts == []  # текущая версия — без копии и фактов


@pytest.mark.parametrize("bad", [0, -1, "1", True, 1.5, None])
def test_invalid_version_rejected(bad):
    with pytest.raises(ValueError, match="config_version"):
        migrations.document_version({"config_version": bad}, source="c.yml")


def test_newer_version_rejected_with_clear_message():
    with pytest.raises(ValueError) as info:
        migrations.migrate_document({"config_version": 9}, source="/x/c.yml")
    message = str(info.value)
    assert message.startswith("/x/c.yml: config_version: 9")
    assert "более новой версией" in message


def test_newer_version_fails_load(stack):
    base = stack("config.yml", {"config_version": 2})
    with pytest.raises(ValueError, match="более новой версией"):
        load_settings(str(base))


def test_schema_rejects_newer_version_too():
    with pytest.raises(ValueError, match="более новой версией"):
        AppConfig.model_validate({"config_version": 2})


@pytest.mark.usefixtures("version_two")
def test_migration_applied_without_mutating_input():
    raw = {"agent": {"client_id": "7"}}
    doc, facts = migrations.migrate_document(raw, source="c.yml")
    assert raw == {"agent": {"client_id": "7"}}
    assert doc == {"agent": {"x_cl_id": "7"}, "config_version": 2}
    assert facts[0] == "c.yml: agent.client_id переименован в agent.x_cl_id"
    assert "файл на диске не изменён" in facts[-1]


@pytest.mark.usefixtures("version_two")
def test_migration_is_idempotent_after_stamp():
    doc, _ = migrations.migrate_document({"agent": {"client_id": "7"}})
    again, facts = migrations.migrate_document(doc)
    assert again is doc and facts == []


def test_missing_migration_step_is_an_error(monkeypatch):
    monkeypatch.setattr(migrations, "CURRENT_CONFIG_VERSION", 3)
    monkeypatch.setattr(migrations, "MIGRATIONS", {1: _rename_x_cl_id})
    with pytest.raises(ValueError, match="нет миграции формата 2 → 3"):
        migrations.migrate_document({})


@pytest.mark.usefixtures("version_two")
def test_load_settings_migrates_each_layer_and_reports(stack):
    base = stack("config.yml", {"agent": {"client_id": "base-id"}})
    stack(
        "profiles/work.yml",
        {"config_version": 1, "agent": {"client_id": "profile-id"}},
    )
    loaded = load_settings(str(base), profile_override="work")
    assert loaded.settings.agent.x_cl_id == "profile-id"
    assert loaded.settings.config_version == 2
    notices = "\n".join(loaded.runtime.notices)
    assert f"{base}: agent.client_id переименован" in notices
    assert "work.yml: agent.client_id переименован" in notices


@pytest.mark.usefixtures("version_two")
def test_layer_written_by_new_version_is_not_migrated(stack):
    base = stack(
        "config.yml", {"config_version": 2, "agent": {"x_cl_id": "9"}}
    )
    loaded = load_settings(str(base))
    assert loaded.settings.agent.x_cl_id == "9"
    assert not any("переименован" in n for n in loaded.runtime.notices)


def test_compat_still_applies_and_reports(stack):
    """Compat (переименования внутри v1) работает как раньше, рядом с версией."""
    base = stack(
        "config.yml",
        {"config_version": 1, "chat_completions": {"llm": {"max_tokens": 77}}},
    )
    loaded = load_settings(str(base))
    assert loaded.settings.chat_completions.auxiliary_llm.max_tokens == 77
    assert any(
        "chat_completions.llm переименован" in n
        for n in loaded.runtime.notices
    )
