"""Бенч-профиль не обязан хранить секрет открытым: `${ENV:…}` разворачивается.

Файловый путь ядра (`load_config`) маркеры разворачивает, а бенч-путь собирает
`config_dict` в памяти и раньше отдавал их как есть — литерал `${ENV:GC_API_KEY}`
уехал бы в HTTP-заголовок, и провайдер ответил бы 401.

Почему это не «мелкое удобство»: `agent_kwargs.gc_cli_config` попадает в
`config.json` трайла на диск (проверено на результатах прогона), а переданный
через `--ak` ключ виден ещё и в `ps`. Единственный способ прогнать бенч на
живом стенде, не раскладывая ключ по диску и списку процессов, — держать его
в окружении раннера.
"""
import importlib.util
import sys

from gc_testkit.paths import HARBOR


def _config_module():
    """Модуль сборки конфига. Он намеренно свободен от импортов harbor."""
    sys.path.insert(0, str(HARBOR))
    spec = importlib.util.spec_from_file_location(
        "adapters_gc_cli_config_for_tests", HARBOR / "adapters" / "gc_cli_config.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build(tmp_path, overrides):
    mod = _config_module()
    return mod.build_gc_cli_config(
        completions_url="http://test-backend/v1/chat/completions",
        model="test-model",
        prompts_dir=tmp_path / "prompts",
        skills_dir=tmp_path / "skills",
        config_dir=str(tmp_path),
        overrides=overrides,
    )


def test_env_marker_in_a_header_is_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("GC_TEST_STAND_KEY", "секрет-из-окружения")

    config = _build(tmp_path, {
        "chat_completion_headers": {"X-Api-Key": "${ENV:GC_TEST_STAND_KEY}"},
    })

    assert config["chat_completion_headers"]["X-Api-Key"] == "секрет-из-окружения"


def test_marker_never_leaks_as_a_literal(tmp_path, monkeypatch):
    """Литерал в заголовке — это 401 на живом стенде и час поисков в конфиге."""
    monkeypatch.setenv("GC_TEST_STAND_KEY", "k")

    config = _build(tmp_path, {
        "chat_completion_headers": {"X-Api-Key": "${ENV:GC_TEST_STAND_KEY}"},
        "proxy": "${ENV:GC_TEST_STAND_KEY}",
    })

    assert "${ENV:" not in repr(config)


def test_file_marker_reads_the_secret_from_disk(tmp_path):
    """`${FILE:…}` — вторая форма того же контракта (ключ в файле с chmod 600)."""
    secret = tmp_path / "key.txt"
    secret.write_text("ключ-из-файла\n", encoding="utf-8")

    config = _build(tmp_path, {
        "chat_completion_headers": {"X-Api-Key": f"${{FILE:{secret}}}"},
    })

    assert config["chat_completion_headers"]["X-Api-Key"] == "ключ-из-файла"
