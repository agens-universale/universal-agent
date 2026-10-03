"""``skills_run_script`` запускает скрипт нативно, без потока из пула.

Этот тул — единственный, где файловая работа смешана с долгой. Резолв пути
скрипта на диске занимает микросекунды, а сам скрипт живёт до таймаута из
конфига, и по умолчанию это минуты.

Поэтому он не попал в набор «инлайн» (`test_file_tools_run_inline.py`): у
запуска процесса, в отличие от чтения файла, нативная асинхронная форма ЕСТЬ —
``asyncio.create_subprocess_exec`` внутри ``LocalFsBackend.aexec_argv``
(``backends/local.py``). Раньше тул звал синхронный ``exec_argv``, и его
уводил в поток диспетчер; теперь ``acall`` ждёт ``aexec_argv`` напрямую.

Синхронный ``exec_argv`` — сам по себе шим: ``run_coro_blocking(self.aexec_argv(…))``
(``backends/local.py:220-230``). То есть подмена не меняет реализацию, а
убирает с пути поток и лишний event loop.
"""

import json
import threading

import pytest
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars
from gc_core.tools.skills.registry import get_skills_registry
from gc_core.tools.skills.skills_tools import RunSkillScriptTool
from gc_testkit.helpers.tool_ctx import local_ctx
from gc_testkit.settings import make_settings

#: Сколько спит скрипт-двойник. Заметно больше шага пульса ниже, чтобы
#: заморозка loop не могла спрятаться между тиками.
SCRIPT_SLEEP = 0.6
TICK = 0.02


@pytest.fixture
def skill_with_script(monkeypatch, tmp_path):
    """Реестр из одной папки с единственным скиллом и скриптом в нём."""
    root = tmp_path / "skills"
    skill_dir = root / "sleeper"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        '---\nid: sleeper\nname: sleeper\ndescription: "спит"\n---\n# тело\n',
        encoding="utf-8",
    )
    (skill_dir / "scripts" / "nap.py").write_text(
        f"import time\ntime.sleep({SCRIPT_SLEEP})\nprint('проснулся')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    return get_skills_registry(make_settings({"skills_data_path": str(root)}))


def _tool(registry):
    return RunSkillScriptTool(registry, ctx=local_ctx())


async def test_acall_runs_the_script_and_returns_its_output(skill_with_script):
    """Базовое: нативный вход делает ту же работу, что и синхронный."""
    out = json.loads(
        await _tool(skill_with_script).acall(
            skill_id="sleeper", script_name="nap.py"
        )
    )

    assert out["ok"] is True, out
    assert out["script_ok"] is True, out
    assert "проснулся" in out["result"]["stdout"]["content"]
    assert out["result"]["exit_code"] == 0


async def test_loop_stays_responsive_while_the_script_runs(skill_with_script):
    """Главная проверка: пока скрипт спит, loop продолжает тикать.

    Именно это отличает нативный запуск от синхронного вызова на loop. Поток
    из пула дал бы тот же результат — поэтому рядом стоит проверка на потоки.
    """
    from gc_testkit import LoopPulse, quiet_gc_for_loop_timing

    # Сборщик глушится на окне: порог абсолютный, а пауза поколения 2 на куче
    # полного набора (180–220 мс) — большая часть бюджета, и тест судил бы
    # её, а не запуск скрипта.
    #
    # Пульс прогретый и общий. Собственный ставился в очередь прямо перед
    # `await acall` и не видел ничего, что тул делает до первой уступки: 0,4 с
    # сна в начале `acall` тест пропускал — проверено подменой.
    with quiet_gc_for_loop_timing():
        async with LoopPulse(step=TICK) as pulse:
            await _tool(skill_with_script).acall(
                skill_id="sleeper", script_name="nap.py"
            )

    assert len(pulse.gaps) > 10, (
        f"пульс почти не тикал: {len(pulse.gaps)} тиков"
    )
    assert pulse.worst() < SCRIPT_SLEEP / 2, (
        f"loop залипал на {pulse.worst():.3f}с, пока шёл скрипт — запуск не асинхронный ({pulse.report()})"
    )


async def test_no_helper_thread_is_spawned(skill_with_script):
    """Ни одного потока на вызов: запуск идёт через asyncio.subprocess.

    Без этой проверки предыдущая осталась бы зелёной и на ``to_thread``:
    поток тоже не держит loop. Она и отделяет «нативно» от «в пуле».
    """
    tool = _tool(skill_with_script)
    # Прогреваем: первый вызов может поднять ленивые синглтоны реестра.
    await tool.acall(skill_id="sleeper", script_name="nap.py")

    before = threading.active_count()
    await tool.acall(skill_id="sleeper", script_name="nap.py")
    assert threading.active_count() == before, (
        "на вызов появился поток — значит запуск уехал в пул, а не в asyncio.create_subprocess_exec"
    )


def test_no_sync_entry(skill_with_script):
    """Синхронного входа у тула нет: форма одна — ``acall``."""
    tool = _tool(skill_with_script)

    assert not callable(tool), "у тула скиллов снова появился синхронный вход"


async def test_bad_json_args_return_an_error_not_a_broken_await(
    skill_with_script,
):
    """Отказ разбора аргументов приходит значением, а не ломает ``await``.

    ``acall`` обёрнут ``parse_json_object_argument``, и на ошибочной ветке тот
    отдаёт готовый словарь. Пока у декоратора не было корутинной ветки, этот
    вызов падал с ``TypeError: object dict can't be used in 'await'``.
    """
    out = json.loads(
        await _tool(skill_with_script).acall(
            skill_id="sleeper",
            script_name="nap.py",
            args="не json",
        )
    )

    assert out["ok"] is False, out
    assert out["error"]["code"] == "INVALID_ARGUMENT", out
    assert out["error"]["field"] == "args", out
