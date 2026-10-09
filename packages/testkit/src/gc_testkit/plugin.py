"""Pytest-плагин тест-кита — общий для наборов всех пакетов ``gc-*``.

Подключается автоматически через entry point ``pytest11``, поэтому отдельный
``conftest.py`` в каждом пакете не нужен: набор любого пакета получает и
заглушку ``tools``, и сбрасывающие состояние autouse-фикстуры.

Тот же ``pytest11`` подключает плагин и в ЧУЖИХ прогонах — в любом окружении,
куда попало колесо ``gc-testkit``, и при ЛЮБОМ rootdir. Всё, что плагин делает,
осмысленно только для наших наборов, поэтому включается он по гейту
``_suite_root_is_ours`` ниже: корень СОБИРАЕМОГО набора (``config.rootpath``)
обязан быть корнем нашей рабочей копии или каталогом одного из её
``packages/<имя>``. Вне рабочей копии плагин ПОЛНОСТЬЮ ИНЕРТЕН. Раньше
инертности не было вовсе: жадный ``REPO`` в ``gc_testkit.paths`` бросал
``RuntimeError`` прямо на импорте и обрывал чужой pytest ДО сбора тестов, а
autouse-фикстуры уронили бы каждый чужой тест на
``ModuleNotFoundError: gc_core``.

Гейт спрашивает про собираемый НАБОР, а не про место самого плагина. Прежний
``_IN_WORKING_COPY = repo_root() is not None`` проверял, лежит ли внутри
рабочей копии сам плагин, и ничего не знал о том, чей набор сейчас гоняют. До
разбиения на пакеты этот вопрос и не стоял: бутстрап жил в корневом
``conftest.py``, а conftest подхватывается только для тестов НИЖЕ своего
каталога — у вендоренных серверов (``external/vendor/*-mcp``) свои
``pyproject.toml``, то есть свой rootdir, и корневой conftest до них не
доставал. Отсечка была бесплатной, от устройства pytest.

Гейт ниже держится на том же признаке, поэтому у вендора обязана быть своя
секция ``[tool.pytest.ini_options]``: без неё pytest поднимает rootdir до корня
монорепозитория, и отличить чужой прогон от нашего уже нечем. У ``ddgs-mcp``
секции не было, и на нём утечка пережила первую починку гейта; теперь это
стережёт ``tests/test_testkit_rootdir_gate.py``. Точка входа
``pytest11`` грузится НЕЗАВИСИМО от rootdir, и отсечка пропала: в прогоне
вендоренного сервера на ``sys.path`` оказывались все ``packages/*/src``, а без
``mcp`` в окружении — ещё и заглушки ``trafilatura``/``readability``, у которых
``extract()`` всегда None, а ``Document.summary()`` — пустая строка. Чужой
набор от этого зеленеет: извлекать перестаёт что бы то ни было. Наблюдалось
запуском из ``external/vendor/office-mcp``.

Pytest: keep a consistent real ``tools`` package when MCP is installed.

Some tests replace ``sys.modules['tools']`` with stubs (summary_triggers, inject_tool_fields,
processing_responses stubs). Without cleanup, later tests see ``ModuleNotFoundError`` for
``tools.connect_mcp`` / ``tools.skills``.

When MCP is **not** installed, a minimal stub is registered before the conftests are imported (see
``pytest_load_initial_conftests``) so intent tests can import ``states`` without running
``tools/__init__.py``.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

# Исходники разъехались по пакетам — раскладку знает gc_testkit.paths.
# Импорты абсолютные, как во всех остальных пакетах: относительные оставались
# только здесь.
from gc_testkit.paths import (
    _declares_gc_core,
    module_dir,
    package_dir,
    repo_root,
    repo_root_from,
    use_repo_root,
)
from gc_testkit.sources import ensure_sources_on_path

# Константы вместо повторяющихся литералов (Sonar S1192).
_MOD_TOOLS = "gc_core.tools"
_MOD_TOOLS_MCP = "gc_core.tools.connect_mcp"
_MOD_TOOLS_SKILLS = "gc_core.tools.skills"


#: Собираем ли мы СВОЙ набор. Ставится в ``pytest_load_initial_conftests``:
#: раньше хука rootdir неизвестен, а без него гейт не отличить от прежнего
#: «плагин лежит в рабочей копии» — см. шапку модуля.
_OUR_SUITE = False


def _suite_root(rootpath) -> Path | None:
    """Корень рабочей копии, которой ПРИНАДЛЕЖИТ собираемый набор, иначе None.

    Наши прогоны дают ровно два вида rootdir: корень репозитория (запуск из
    корня) и ``packages/<имя>`` — у каждого пакета свой
    ``[tool.pytest.ini_options]``, поэтому корнем pytest объявляет его каталог.
    Отсюда и форма проверки. Смотреть «rootpath где-то внутри рабочей копии»
    нельзя: вендоренные серверы лежат в ``external/vendor/`` НАШЕГО дерева, и
    такая проверка пропустила бы ровно тот случай, ради которого гейт и заведён.

    Корень берётся от САМОГО rootpath, а не от места плагина. Прежняя редакция
    сверяла rootpath с ``repo_root()``, то есть с чекаутом, ОТКУДА УСТАНОВЛЕН
    тест-кит. Пока чекаут один, разницы нет; при двух (git worktree, второй
    клон, каталог ревью) venv чекаута A, запущенный в чекауте B, давал
    несовпадение — и гейт объявлял наш же набор чужим. Предупреждения при этом
    не было: его печатает только ветка ``repo_root() is None``, а тут корень
    прекрасно находился, просто не тот. Бутстрап и все autouse-фикстуры молча
    выключались, набор B шёл по исходникам A через ``.pth`` и был зелёным.
    """
    rootpath = Path(rootpath).resolve()
    if _declares_gc_core(rootpath):
        return rootpath
    if rootpath.parent.name == "packages" and _declares_gc_core(
        rootpath.parent.parent
    ):
        return rootpath.parent.parent
    return None


_MINIMAL_PRELOADED = False


def _mcp_runtime_available() -> bool:
    try:
        importlib.import_module("mcp.client.session")
        return True
    except ImportError:
        return False


def _load_file_module(name: str, rel: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    # Раскладку спрашиваем ЗДЕСЬ, а не на импорте модуля: вне рабочей копии
    # package_dir() честно бросает, а импорт плагина обязан быть безобидным.
    path = package_dir("gc_core") / rel
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_optional_web_parsers() -> None:
    if "trafilatura" not in sys.modules:
        mod = types.ModuleType("trafilatura")
        mod.extract = lambda *args, **kwargs: None
        sys.modules["trafilatura"] = mod

    if "readability" not in sys.modules:
        mod = types.ModuleType("readability")

        class _Document:
            def __init__(self, *_args, **_kwargs):
                pass

            def summary(self, html_partial=True):
                return ""

        mod.Document = _Document
        sys.modules["readability"] = mod


#: Файлы, которые заглушка грузит из дерева gc_core «как есть».
#:
#: Гейт рабочей копии проверяет ОДИН файл-маркер (``packages/core/pyproject.toml``
#: со строкой ``name = "gc-core"``), а заглушке нужно целое дерево. На неполном
#: чекауте (sparse, worktree с частичной выкладкой, bisect на коммит до
#: появления этих путей) маркер на месте, а файлов нет — и голый
#: ``FileNotFoundError`` прилетал из pluggy ДО сбора тестов, без единого
#: упоминания gc_testkit в тексте ошибки. Проверено запуском на синтетическом
#: дереве. Поэтому гейт расширен проверкой самих загружаемых файлов: жёстко
#: падать плагину нельзя (прогон может не иметь к заглушке никакого отношения),
#: а молчать нельзя тем более — отказ обязан назвать себя.
_SIMPLE_TOOL_BASE_SRC = "tools/simple_tool_base.py"
_SUBMIT_SOLUTION_SRC = "tools/turn/submit_solution.py"
_PRELOAD_SOURCES = (_SIMPLE_TOOL_BASE_SRC, _SUBMIT_SOLUTION_SRC)


def _missing_preload_sources() -> list[str]:
    """Какие из ``_PRELOAD_SOURCES`` в рабочей копии не выложены."""
    core = package_dir("gc_core")
    return [rel for rel in _PRELOAD_SOURCES if not (core / rel).is_file()]


def preload_minimal_tools_package() -> None:
    """Minimal ``tools`` tree for intent tests when MCP is not installed.

    На неполном дереве отказывается вслух и НЕ прерывает прогон — см.
    ``_PRELOAD_SOURCES``.
    """
    global _MINIMAL_PRELOADED
    if _MINIMAL_PRELOADED:
        return

    missing = _missing_preload_sources()
    if missing:
        print(
            "gc_testkit: заглушка пакета tools не собрана — в "
            f"{package_dir('gc_core')} не выложены: {', '.join(missing)}. "
            "Прогон продолжаю; наборы, которым нужен tools без установленного "
            "mcp, покраснеют на импорте.",
            file=sys.stderr,
        )
        return

    _MINIMAL_PRELOADED = True

    # То же, что в _load_file_module: раскладка спрашивается в момент вызова.
    tools_dir = module_dir("tools")

    _stub_optional_web_parsers()
    for name in list(sys.modules):
        if name == _MOD_TOOLS or name.startswith("gc_core.tools."):
            del sys.modules[name]

    tools_pkg = types.ModuleType(_MOD_TOOLS)
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules[_MOD_TOOLS] = tools_pkg

    session_pkg = types.ModuleType("gc_core.tools.session")
    session_pkg.__path__ = [str(tools_dir / "session")]
    sys.modules["gc_core.tools.session"] = session_pkg
    tools_pkg.session = session_pkg

    stored_pkg = types.ModuleType("gc_core.tools.session.stored_outputs")
    stored_pkg.__path__ = [str(tools_dir / "session" / "stored_outputs")]
    sys.modules["gc_core.tools.session.stored_outputs"] = stored_pkg
    session_pkg.stored_outputs = stored_pkg
    # Хранилище выводов (`gc_core.execution.output_store`) — обычный модуль
    # вне `gc_core.tools`, заглушке грузить его незачем. Обёртку вывода
    # заменяем тождественной, как раньше на её старом пути в tools.
    wrapping_mod = types.ModuleType("gc_core.execution.output_wrapping")
    wrapping_mod.maybe_store_and_wrap_tool_output = lambda tool_res, **_kw: (
        tool_res
    )
    sys.modules["gc_core.execution.output_wrapping"] = wrapping_mod

    skills_pkg = types.ModuleType(_MOD_TOOLS_SKILLS)
    skills_pkg.__path__ = [str(tools_dir / "skills")]
    sys.modules[_MOD_TOOLS_SKILLS] = skills_pkg

    _load_file_module("gc_core.tools.simple_tool_base", _SIMPLE_TOOL_BASE_SRC)
    _load_file_module(
        "gc_core.tools.turn.submit_solution",
        _SUBMIT_SOLUTION_SRC,
    )

    connect_mcp_pkg = types.ModuleType(_MOD_TOOLS_MCP)
    connect_mcp_pkg.__path__ = [str(tools_dir / "connect_mcp")]
    sys.modules[_MOD_TOOLS_MCP] = connect_mcp_pkg


def _real_tools_loaded() -> bool:
    """Загружен ли НАСТОЯЩИЙ пакет `gc_core.tools`, а не заглушка.

    Признак — файл модуля: заглушки собираются как `types.ModuleType` и файла
    не имеют, даже когда повторяют имена пакета (`abuild_tools_data` ставит,
    например, заглушка finish-gate). Прежний признак — `build_tools_data` —
    снят вместе с синхронной сборкой, и ответ стал «не загружен» ВСЕГДА:
    фикстура перезагружала весь пакет после каждого теста.
    """
    tools = sys.modules.get(_MOD_TOOLS)
    if tools is None:
        return False
    return (
        getattr(tools, "__file__", None) is not None
        and callable(getattr(tools, "abuild_tools_data", None))
        and _MOD_TOOLS_MCP in sys.modules
        and _MOD_TOOLS_SKILLS in sys.modules
        and isinstance(
            getattr(sys.modules.get(_MOD_TOOLS_SKILLS), "__path__", None), list
        )
        and len(sys.modules[_MOD_TOOLS_SKILLS].__path__) > 0
    )


def ensure_real_tools_package() -> None:
    """Reload full ``tools`` from ``src/tools`` (requires MCP and project deps)."""
    if _real_tools_loaded():
        return
    for name in list(sys.modules):
        if name == _MOD_TOOLS or name.startswith("gc_core.tools."):
            del sys.modules[name]
    importlib.import_module(_MOD_TOOLS)


def _isolate_gc_home() -> None:
    """GC_HOME набора — пустой каталог сессии, а не ``~/.gc`` запускающего.

    Изоляция слоёв в ``_isolated_config_layers`` закрывает managed и проектный
    слой, но базовый ``config.yml`` и данные читались из домашнего каталога: у
    того, кто пользуется агентом, набор краснел на личных ключах. Ставим до
    импорта conftest'ов — ``env_vars.env`` читает окружение один раз, на импорте.
    Точечные ``GC_*_DIR`` сильнее GC_HOME, поэтому снимаются. Подпроцессы тестов
    наследуют то же окружение.
    """
    import atexit
    import os
    import shutil
    import tempfile

    home = tempfile.mkdtemp(prefix="gc-test-home-")
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    for name in (
        "GC_STATE_DIR",
        "GC_CONFIG_DIR",
        "GC_CACHE_DIR",
        "GC_DATA_DIR",
    ):
        os.environ.pop(name, None)
    os.environ["GC_HOME"] = home


def pytest_load_initial_conftests(early_config) -> None:
    """Бутстрап нашего набора: ``sys.path`` и, без mcp, заглушка ``tools``.

    Хук выбран не произвольно. ``rootpath`` появляется только в хуках — на
    импорте модуля rootdir ещё не определён; и это последний момент ПЕРЕД
    импортом conftest'ов, а они импортируют ``gc_testkit.paths`` и ``gc_core``,
    так что к их загрузке ``sys.path`` обязан быть готов. В
    ``pytest_configure`` было бы уже поздно: порядок «наш хук → conftest'ы →
    configure» проверен запуском.

    Обе строчки раньше стояли на импорте модуля. Набор может гоняться из корня
    репозитория или из каталога одного пакета; во втором случае каталоги
    исходников соседних пакетов на ``sys.path`` не попадут сами.
    """
    global _OUR_SUITE
    suite_root = _suite_root(early_config.rootpath)
    if suite_root is None:
        if repo_root() is None:
            # Отказ обязан называть себя. Молчание тут стоит дорого: плагин
            # приезжает через pytest11 и в не-editable установке (колесо без
            # рабочей копии) бутстрап не отработает, а набор умрёт
            # `ModuleNotFoundError: gc_core` — из текста которого имя gc-testkit
            # не читается вовсе, и человек ищет причину не там. По образцу
            # `_missing_preload_sources` ниже.
            print(
                f"[warn] gc_testkit: рабочая копия не найдена вверх от "
                f"{Path(__file__).resolve().parent} — бутстрап sys.path "
                f"пропущен. Наши наборы гоняются из чекаута; из установленного "
                f"колеса они не собираются.",
                file=sys.stderr,
            )
        return

    # Расхождение — не ошибка (запуск набора чужим venv законен), но и не то,
    # о чём стоит молчать: дальше корнем всего тест-кита работает СОБИРАЕМЫЙ
    # чекаут, и колёса в test_packaging_wheel соберутся тоже из него.
    installed_root = repo_root_from(Path(__file__))
    if installed_root is not None and installed_root != suite_root:
        print(
            f"[warn] gc_testkit: набор из {suite_root}, тест-кит установлен из "
            f"{installed_root} — беру исходники набора.",
            file=sys.stderr,
        )
    use_repo_root(suite_root)

    _OUR_SUITE = True
    _isolate_gc_home()
    ensure_sources_on_path()
    if not _mcp_runtime_available():
        preload_minimal_tools_package()


@pytest.fixture(autouse=True)
def _restore_real_tools_package_after_test():
    yield
    if _OUR_SUITE and _mcp_runtime_available():
        ensure_real_tools_package()


@pytest.fixture(autouse=True)
def _clear_runtime_notices_between_tests():
    # В чужом прогоне gc_core может быть не установлен вовсе: autouse-фикстура
    # обязана промолчать, а не уронить каждый чужой тест на ModuleNotFoundError.
    if not _OUR_SUITE:
        yield
        return
    from gc_core.runtime_notices import clear_runtime_notices

    clear_runtime_notices(listeners=True)
    yield
    clear_runtime_notices(listeners=True)


@pytest.fixture(autouse=True)
def _isolated_config_layers(tmp_path, monkeypatch):
    """Managed- и проектный слои конфига видны тесту, только если он сам их и положил.

    Приехало из develop (`tests/conftest.py`), где у набора был общий корневой
    conftest. У нас его нет — autouse-фикстуры живут здесь, в плагине, поэтому
    фикстура переносится сюда вместе с гейтом `_OUR_SUITE`: в ЧУЖОМ прогоне она
    обязана промолчать, а не тянуть gc_core.

    Оба слоя адресуются от МАШИНЫ: проектный — от cwd (`<cwd>/.gc/config.yml`),
    managed — от каталога конфигов (`~/.gc/config.local.yml`). Тест, который ни
    того ни другого не переносил, мержит `.gc` репозитория и `~/.gc`
    разработчика: набор зелёный только на чистой машине, а у того, кто агентом
    пользуется, краснеет. Читать — полбеды: финал мастера уходил ПИСАТЬ в
    проектный конфиг разработчика, снимая там указатель профиля.

    Правило одно на оба слоя и не знает ни одного теста поимённо: адрес слоя
    снимается ДО теста, и слой отдаётся, только если тест этот адрес сдвинул сам —
    `monkeypatch.chdir` для проектного, свой `gc_home`/`gc_config_dir` (или прямая
    подмена `managed_config_path`) для managed. Managed-слой теста живёт в
    `tmp_path/config.local.yml`: кому слой нужен НЕПУСТЫМ, достаточно записать
    этот файл.

    Подменяются ВЛАДЕЛЬЦЫ обеих функций. У нас владелец обеих — один и тот же
    модуль `gc_core.config_modules.env_vars`: `managed_config_path` переехала
    туда из `mcp_runtime.paths` вместе с разделением на пакеты (путь принадлежит
    конфигу, и держать его в MCP-подсистеме значило заставлять config.py
    статически от неё зависеть). На develop это два разных модуля — отсюда
    расхождение с оригиналом фикстуры.
    """
    if not _OUR_SUITE:
        yield
        return

    from gc_core.config_modules import env_vars

    inherited_project = env_vars.project_gc_dir().resolve()
    inherited_managed = env_vars.managed_config_path().resolve()
    real_project = env_vars.project_local_config_path
    real_managed = env_vars.managed_config_path
    sandbox_managed = tmp_path / "config.local.yml"

    def project_layer():
        if env_vars.project_gc_dir().resolve() == inherited_project:
            return None
        return real_project()

    def managed_layer():
        path = real_managed()
        return sandbox_managed if path.resolve() == inherited_managed else path

    monkeypatch.setattr(env_vars, "project_local_config_path", project_layer)
    monkeypatch.setattr(env_vars, "managed_config_path", managed_layer)
    yield
