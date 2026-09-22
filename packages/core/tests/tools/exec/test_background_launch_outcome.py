"""Что фоновый запуск говорит модели, когда команда не прожила и мгновения.

Ответ фонового запуска — единственное, что модель знает о команде: вывода нет.
Пока он говорил `ok: true` и давал pid даже мёртвого процесса, опечатка в
команде выглядела запущенным сервисом, и модель шла проверять его порт.
"""
import json

from gc_core.backends import local as local_backend
from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_testkit.helpers.tool_ctx import with_local_ctx


def _run_in_background(tmp_path, monkeypatch, command: str) -> dict:
    # Запас на медленную машину: `exit N` выходит за миллисекунды, но под полным
    # набором тестов старт процесса бывает долгим.
    monkeypatch.setattr(local_backend, "_BACKGROUND_EARLY_EXIT_SEC", 1.0)
    tool = with_local_ctx(ExecuteCommand, config_dict={"launch_root": str(tmp_path)})
    return json.loads(tool(command=command, run_in_background=True, cwd=str(tmp_path)))


def test_a_command_that_dies_at_once_is_a_failure(tmp_path, monkeypatch):
    res = _run_in_background(tmp_path, monkeypatch, "exit 3")

    assert res["ok"] is False, res
    assert res["exit_code"] == 3, res
    assert res["error"]["type"] == "background_exited", res["error"]
    assert "background" not in res, "pid мёртвого процесса выдан как запущенный"


def test_a_command_that_exits_cleanly_at_once_is_not_called_running(tmp_path, monkeypatch):
    """Код 0 сразу — не ошибка (так ведут себя демоны, уходящие в фон сами), но и
    не «работает»: модели сказано, что после неё ничего может не остаться."""
    res = _run_in_background(tmp_path, monkeypatch, "exit 0")

    assert res["ok"] is True, res
    assert res["exit_code"] == 0, res
    assert "note" in res["background"], res["background"]


def test_the_schema_printed_in_the_prompt_matches_the_real_one():
    """Схема тула, напечатанная в промпт, перечисляет те же поля, что настоящая.

    Печатная копия отстала: `run_in_background` в неё так и не попал, хотя абзац
    про фон правили рядом.
    """
    text = ExecuteCommand.prompt_description()
    line = next(ln for ln in text.splitlines() if ln.startswith("Input schema: "))
    printed = json.loads(line[len("Input schema: "):])
    real = ExecuteCommand.tool_description()["parameters"]

    assert sorted(printed["properties"]) == sorted(real["properties"])
