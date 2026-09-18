"""Фоновый запуск обязан работать ЧЕРЕЗ обёртку TUI, а не только на бэкенде.

Ровно здесь фоновый режим и сломался в первой редакции: метод жил только на
`LocalFsBackend`, а TUI при сборке контекста всегда оборачивает бэкенд в
`TrackingFsBackend` (`attach_to_context` в `app.py`, безусловно). Вызов падал
`AttributeError`, то есть в единственном месте, где фоновый режим и нужен —
там, где человек поднимает `npm run dev`, — он не работал вовсе.

Почему этого не поймал прежний набор: тесты фонового режима звали
`LocalFsBackend` напрямую либо строили тулу через `with_local_ctx`, где бэкенд
тоже голый. Проверялся путь, который написан, а не тот, по которому ходит
пользователь. Структурный сторож паритета (`test_tracking.py`) ловит пропажу
метода, но не отвечает на вопрос «а работает ли оно целиком».
"""
import json
import shlex
import sys
import time

from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_tui.diff_tui.tracking import TrackingFsBackend

from gc_testkit.helpers.tool_ctx import with_local_ctx

#: Вывод фона уходит в нулевое устройство, поэтому жизнь потомка видна только по
#: его СОБСТВЕННОМУ файлу (путь — первым аргументом).
_CHILD = (
    "import sys, time\n"
    "with open(sys.argv[1], 'a', encoding='utf-8') as ticks:\n"
    "    for i in range(6):\n"
    "        print('tick', i, flush=True)\n"
    "        print('tick', i, file=ticks, flush=True)\n"
    "        time.sleep(0.1)\n"
)


def _command(ticks) -> str:
    return (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(_CHILD)} "
        f"{shlex.quote(str(ticks))}"
    )


def test_background_works_through_the_tui_wrapper(tmp_path):
    """Сквозной путь: тула → обёртка TUI → бэкенд. Процесс запущен и работает."""
    project = tmp_path / "proj"
    project.mkdir()
    ticks = tmp_path / "ticks.log"
    tool = with_local_ctx(ExecuteCommand, config_dict={"launch_root": str(project)})
    ctx = tool._ctx
    ctx.os_backend = TrackingFsBackend(ctx.os_backend, ctx)

    res = json.loads(tool(command=_command(ticks), run_in_background=True))

    assert res["ok"] is True, res
    assert res["background"]["pid"] > 0, res
    assert res["background"]["output"] == "not captured", res["background"]

    deadline = time.monotonic() + 15
    text = ""
    while time.monotonic() < deadline:
        text = ticks.read_text(encoding="utf-8") if ticks.exists() else ""
        if text.count("tick") >= 6:
            break
        time.sleep(0.1)
    assert text.count("tick") == 6, f"через обёртку потомок не отработал: {text!r}"


def test_wrapper_reports_the_inner_capability(tmp_path):
    """Обёртка не решает сама, умеет ли она фон, — она повторяет внутренний."""

    class _NoBackground:
        supports_background = False
        is_sandbox = False
        default_exec_timeout = None

    tool = with_local_ctx(ExecuteCommand)
    ctx = tool._ctx
    assert TrackingFsBackend(ctx.os_backend, ctx).supports_background is True
    assert TrackingFsBackend(_NoBackground(), ctx).supports_background is False


def test_backend_without_background_refuses_honestly(tmp_path):
    """Бэкенд без фона отвечает ОТКАЗОМ ТУЛА, а не падением атрибута.

    Так ведёт себя песочница Harbor: параметр в схеме есть, возможности нет.
    Модель должна получить внятную причину и уйти в передний план, а не
    сообщение про отсутствующий атрибут.
    """
    class _NoBackground:
        supports_background = False
        is_sandbox = True
        default_exec_timeout = None

        def default_cwd(self):
            return str(tmp_path)

        async def is_dir(self, path) -> bool:
            # Разбор аргументов идёт ДО отказа и проверяет каталог: заглушке
            # нужна эта форма, иначе тест падал бы на ней, а не на проверяемом.
            return True

    tool = with_local_ctx(ExecuteCommand)
    tool._ctx.os_backend = _NoBackground()

    res = json.loads(tool(command="echo hi", run_in_background=True))

    assert res["ok"] is False, res
    assert res["error"]["type"] == "background_not_supported", res["error"]
    assert "background" not in res, "ничего не должно было запуститься"
