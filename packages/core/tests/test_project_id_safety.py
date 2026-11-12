"""id проекта из конфига или SDK не выводит запись за каталог состояния:
он попадает в путь бэкапа памяти и в ключи хранилищ в базе."""

from gc_core.agent_store import ProjectKey
from gc_testkit.settings import make_settings


def test_project_id_cannot_be_a_path():
    """id проекта попадает в путь бэкапа памяти и в ключи базы: разделители
    и ``..`` из конфига (в том числе из ``.gc/config.yml`` репозитория) не
    выводят запись за каталог состояния."""
    import pytest
    from pydantic import ValidationError

    for bad in ("../x", "a/b", "a\\b", "..", ".", "", " p"):
        with pytest.raises(ValidationError):
            make_settings({"agent.project_id": bad})
    assert make_settings({"agent.project_id": "billing-2.v1_x"})


def test_memory_backup_path_refuses_ids_that_leave_the_state_dir(tmp_path):
    import pytest
    from gc_core.agent_store.file_store import memory_backup_path

    for bad in ("../../etc", "a/b", "a\\b", ".."):
        with pytest.raises(ValueError):
            memory_backup_path(ProjectKey(bad, str(tmp_path)))
