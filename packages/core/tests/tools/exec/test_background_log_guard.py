"""Лог фоновой команды создаём МЫ — значит он проходит гард записи.

Параметр `log_path` называет модель, а открываем файл мы. Без проверки это была
бы дыра в периметре: вывод процесса можно было бы дописать в любой файл на
диске. Поэтому путь идёт через ту же `check_write_path_allowed`, что и обычная
запись, и через тот же вопрос пользователю при `mode=ask`.

Второе свойство здесь важнее первого: отказ обязан быть СКАЗАН модели. Молчаливый
откат на «вывод в никуда» — это авария из чужого опыта (LangChain #39953, где
таймаут молча пересоздавал сессию, теряя состояние): модель, не знающая, что
лога нет, пойдёт его читать и начнёт строить догадки на пустом месте.

При этом отказ в логе НЕ отменяет запуск: команда стартует и живёт дальше.
Выживание процесса важнее его вывода — иначе фоновый режим переставал бы
работать из-за настройки безопасности.
"""
import json
import sys

from gc_core.security import path_policy
from gc_core.tools.exec.execute_command import ExecuteCommand

from gc_testkit.helpers.tool_ctx import with_local_ctx

_CMD = f'"{sys.executable}" -c "print(1)"'


def _tool(tmp_path, **write_guard):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    cfg = {"launch_root": str(project)}
    if write_guard:
        cfg["write_guard"] = write_guard
    return with_local_ctx(ExecuteCommand, config_dict=cfg), project


def test_log_outside_the_project_is_refused_and_the_model_is_told(tmp_path):
    """`mode=deny`: лог вне периметра не пишем, но команду запускаем."""
    tool, _ = _tool(tmp_path, enabled=True, mode="deny")
    outside = tmp_path / "outside.log"

    res = json.loads(tool(command=_CMD, run_in_background=True, log_path=str(outside)))

    assert res["ok"] is True, res
    assert res["background"]["pid"] > 0, "команду не запустили из-за лога"
    assert res["background"]["log_path"] is None
    assert res["background"]["note"], "отказ не сообщён модели — она пойдёт читать лог"
    assert not outside.exists(), "файл вне периметра всё-таки создан"


def test_ask_mode_honours_a_refusal(tmp_path, monkeypatch):
    """`mode=ask`: спросили, ответили «нет» — лога нет, причина сказана."""
    asked = []

    def _fake(**kwargs):
        asked.append(kwargs["path"])
        return False

    monkeypatch.setattr(path_policy, "_ask_user_confirmation_for_write", _fake)
    tool, _ = _tool(tmp_path, enabled=True, mode="ask")
    outside = tmp_path / "outside.log"

    res = json.loads(tool(command=_CMD, run_in_background=True, log_path=str(outside)))

    assert asked == [str(outside)], f"пользователя не спросили: {asked}"
    assert res["background"]["log_path"] is None
    assert "declined" in res["background"]["note"]
    assert not outside.exists()


def test_ask_mode_honours_a_yes(tmp_path, monkeypatch):
    """`mode=ask`: разрешили — пишем туда, куда просили, и молчим про отказ."""
    monkeypatch.setattr(path_policy, "_ask_user_confirmation_for_write", lambda **_: True)
    tool, _ = _tool(tmp_path, enabled=True, mode="ask")
    outside = tmp_path / "outside.log"

    res = json.loads(tool(command=_CMD, run_in_background=True, log_path=str(outside)))

    assert res["background"]["log_path"] == str(outside)
    assert "note" not in res["background"], res["background"]
    assert outside.exists(), "разрешение получено, а файл не создан"


def test_disabled_guard_writes_where_asked(tmp_path):
    """Гард выключен — ведём себя как обычная запись: пишем без вопросов."""
    tool, _ = _tool(tmp_path)                       # write_guard отсутствует → OFF
    outside = tmp_path / "outside.log"

    res = json.loads(tool(command=_CMD, run_in_background=True, log_path=str(outside)))

    assert res["background"]["log_path"] == str(outside)
    assert outside.exists()


def test_default_log_lands_inside_the_project(tmp_path):
    """Без `log_path` лог уходит в `.gc/tmp/` ПРОЕКТА, а не рядом с cwd команды.

    Якорь — корень проекта: иначе команда с чужим `cwd` заводила бы лог там, где
    его не найти, да ещё и вне периметра записи.
    """
    tool, project = _tool(tmp_path, enabled=True, mode="deny")

    res = json.loads(tool(command=_CMD, run_in_background=True, cwd=str(tmp_path)))

    log_path = res["background"]["log_path"]
    assert log_path is not None, res["background"]
    assert log_path.startswith(str(project / ".gc" / "tmp")), log_path
