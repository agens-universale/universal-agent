"""Серверная поставка не выдаёт модели ни одного встроенного артефактного тула.

Тулы записи файлов и запуска команд описывает один набор имён на весь репозиторий
(`ARTIFACT_TOOL_NAMES`). Серверный демо-конфиг запрещает встроенные из них через
`agent_overrides` → `agent.stop_tools`; страж смотрит на фактическую сборку встроенного
реестра с этими запретами и падает, если набор расширится, а список запретов
— нет. Граница стража названа явно: тул скиллов `skills_run_script` во встроенный
реестр не входит и `stop_tools` не снимается, MCP-тулы по имени не
классифицируются.
"""
import yaml

from gc_core.constants.tool_names import ARTIFACT_TOOL_NAMES, SKILLS_RUN_SCRIPT_NAME
from gc_core.tools.registry import all_builtin_tool_names, get_builtin_tools_data
from gc_testkit.paths import REPO
from gc_testkit.settings import make_settings


def _server_stop_tools() -> list:
    raw = yaml.safe_load((REPO / "demo_config_server.yml").read_text(encoding="utf-8")) or {}
    return raw["agent_overrides"]["agent.stop_tools"]


def test_server_demo_stop_tools_hide_every_artifact_tool():
    served = set(get_builtin_tools_data(make_settings({
        "agent.stop_tools": _server_stop_tools(),
        "backend.api_mode": "chat_completions",
    })))
    assert served & ARTIFACT_TOOL_NAMES == set()


def test_server_demo_stop_tools_keep_serving_non_artifact_tools():
    """Обратная сторона: запрет не выкосил вообще всё — реестр сервера не пуст."""
    served = set(get_builtin_tools_data(make_settings({
        "agent.stop_tools": _server_stop_tools(),
        "backend.api_mode": "chat_completions",
    })))
    assert "turn_finish" in served and "session_think" in served


def test_skills_run_script_is_outside_the_builtin_registry_this_guard_sees():
    """Непокрытое — явно: `skills_run_script` пишет и исполняет (он в наборе
    записи и запуска), но это тул скиллов, а не встроенный, — `stop_tools` его не снимает, и
    сервер его выдаёт. Когда глобальный `stop_tools` научится снимать тулы
    скиллов, страж выше расширяется на полную сборку, а этот тест уходит."""
    assert SKILLS_RUN_SCRIPT_NAME in ARTIFACT_TOOL_NAMES
    assert SKILLS_RUN_SCRIPT_NAME not in all_builtin_tool_names(make_settings({"backend.api_mode": "chat_completions"}))
