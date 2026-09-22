"""Вывод фоновой команды не сохраняется — это решение, а не недосмотр.

Долгоживущий процесс переживает агента, пока у него нет ТРУБЫ к нам: пропал
читатель — потомок получает EPIPE или встаёт на полном буфере, и сервис, поднятый
агентом, погибал вместе с ним. Лог в файле этой связи не создавал, зато упирался
в гарды с обеих сторон:

* `log_path` называла модель, а открывал файл тул — тул становился примитивом
  записи, и путь приходилось гнать через гард записи с вопросами и откатами;
* свой лог по умолчанию лежал в `.gc/tmp`, а весь `.gc` закрыт на чтение: модель
  не могла прочитать то, что ей велели читать (проверено 18.09.2026 — отказ и у
  `fs_read_file`, и у `cat` через этот же тул).

Поэтому вывод фона уходит в нулевое устройство: пути нет — гардам нечего
проверять. Здесь закреплено, что тул больше ничего не пишет на диск и прямо
говорит модели, что вывода не будет.
"""

import json
import sys

from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_testkit import run_coro
from gc_testkit.helpers.tool_ctx import local_ctx, with_local_ctx

_CMD = f'"{sys.executable}" -c "print(1)"'


def _project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    return project


def _files_under(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_background_launch_writes_nothing_and_says_output_is_not_captured(tmp_path):
    """Фоновый запуск не заводит лог и не отсылает модель его читать.

    Ломается, если вернуть лог по умолчанию: в проекте появится `.gc/tmp/…`, а в
    ответе — путь, который модель всё равно не прочитает.
    """
    project = _project(tmp_path)
    tool = with_local_ctx(ExecuteCommand, config_dict={"launch_root": str(project)})

    res = json.loads(tool(command=_CMD, run_in_background=True, cwd=str(project)))

    assert res["ok"] is True, res
    assert res["background"]["pid"] > 0, res
    assert res["background"]["output"] == "not captured", res["background"]
    assert "log_path" not in res["background"], res["background"]
    assert _files_under(project) == [], "фоновый запуск записал что-то в проект"


def test_a_log_path_from_the_model_is_dropped_and_nothing_is_written(tmp_path):
    """Модель по старой памяти просит лог: поле снято, файла нет, модели сказано.

    Проверяется путь вызова целиком, как его видит модель: схема → снятие лишних
    полей → тул. Ломается, если `log_path` вернуть в схему: поле снова доедет до
    тула, и тот начнёт писать туда, куда укажет модель.
    """
    # Внутри теста, а не на уровне модуля: модули `tests/tools/` импортируются
    # под заглушкой пакета `gc_core.tools` (см. conftest), и `aapply_tool` в ней нет.
    from gc_core.tools import aapply_tool

    project = _project(tmp_path)
    cfg = {"launch_root": str(project)}
    wanted = project / "server.log"
    tools = {
        "exec_command": {
            "tool_fabric": ExecuteCommand,
            "spec": ExecuteCommand.tool_description(cfg),
            "injected_fields": [],
        }
    }

    raw = run_coro(
        aapply_tool(
            "exec_command",
            {"command": _CMD, "run_in_background": True, "log_path": str(wanted)},
            tools_data_=tools,
            ctx=local_ctx(cfg),
        )
    )

    res = json.loads(raw)
    assert res["ok"] is True, res
    assert res["background"]["pid"] > 0, "команду не запустили"
    assert "log_path" in res.get("dropped_arguments", ""), f"модели не сказали, что поле снято: {res}"
    assert not wanted.exists(), "тул записал вывод туда, куда указала модель"
