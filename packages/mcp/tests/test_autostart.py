"""Тесты автозапуска MCP-daemon'ов по конфигу агента."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest
from recommended_mcp.autostart import (
    ensure_daemons_for_agent,
    wait_daemon_url_ready,
)
from recommended_mcp.runtime.manifest_resolve import (
    resolve_agent_manifest_path,
)


def _write_manifest(tmp_path: Path, content: str) -> Path:
    manifest = tmp_path / "recommended_mcp" / "config.local.yml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(dedent(content), encoding="utf-8")
    return manifest


def test_resolve_manifest_from_gc_config(tmp_path: Path, monkeypatch) -> None:
    gc_config = tmp_path / "gc-config"
    manifest = gc_config / "recommended_mcp" / "config.local.yml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("mcps: []\n", encoding="utf-8")
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(
        env_vars, "env", env_vars.EnvVars(gc_config_dir=gc_config)
    )
    path = resolve_agent_manifest_path({})
    assert path == manifest.resolve()


def test_ensure_starts_only_configured_http_mcp(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path,
        """
        mcps:
          - name: science
            path: ./science
            install: {type: source, source: ../external/science-mcp}
            run:
              type: command
              command: science-mcp
              args: ["--transport", "http"]
          - name: duckduckgo_search
            path: ./duckduckgo_search
            install: {type: pypi, package: ddgs-mcp==0.1.0}
        """,
    )
    config_dict = {
        "mcp-servers": {
            "science": {
                "transport": "stream_http",
                "url": "http://127.0.0.1:8765/mcp",
            },
            "duckduckgo_search": {"transport": "stdio", "command": "ddgs-mcp"},
        },
    }
    with patch(
        "recommended_mcp.runtime.manifest_resolve.resolve_agent_manifest_path",
        return_value=manifest,
    ):
        with patch(
            "recommended_mcp.autostart.run_entry", return_value=True
        ) as run_entry:
            with patch(
                "recommended_mcp.autostart.wait_daemon_url_ready",
                return_value=True,
            ) as wait_ready:
                ensure_daemons_for_agent(config_dict)
    run_entry.assert_called_once()
    assert run_entry.call_args.args[0].name == "science"
    wait_ready.assert_called_once_with("http://127.0.0.1:8765/mcp")


def test_ensure_skips_ready_wait_when_daemon_already_running(
    tmp_path: Path,
) -> None:
    manifest = _write_manifest(
        tmp_path,
        """
        mcps:
          - name: science
            path: ./science
            install: {type: source, source: ../external/science-mcp}
            run:
              type: command
              command: science-mcp
              args: ["--transport", "http"]
        """,
    )
    config_dict = {
        "mcp-servers": {
            "science": {
                "transport": "stream_http",
                "url": "http://127.0.0.1:8765/mcp",
            },
        },
    }
    with patch(
        "recommended_mcp.runtime.manifest_resolve.resolve_agent_manifest_path",
        return_value=manifest,
    ):
        with patch("recommended_mcp.autostart.run_entry", return_value=False):
            with patch(
                "recommended_mcp.autostart.wait_daemon_url_ready"
            ) as wait_ready:
                ensure_daemons_for_agent(config_dict)
    wait_ready.assert_not_called()


def test_wait_daemon_url_ready_accepts_open_port() -> None:
    import socket
    from threading import Thread

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    host, port = sock.getsockname()

    def _serve() -> None:
        conn, _ = sock.accept()
        conn.close()

    thread = Thread(target=_serve, daemon=True)
    thread.start()
    try:
        assert (
            wait_daemon_url_ready(f"http://{host}:{port}/mcp", timeout=2.0)
            is True
        )
    finally:
        sock.close()


def test_wired_but_not_installed_daemon_does_not_kill_the_frontend(
    tmp_path: Path,
) -> None:
    """Подключённый, но не установленный рецепт — предупреждение, а не падение.

    Что случилось 01.09.2026. Рецепт `science` остался подключённым в
    config.local.yml, а его venv был снесён. `gc-ua` поднимает daemon'ы по
    умолчанию (`set_defaults(auto_mcp=True)` в gc_tui), Popen получил
    FileNotFoundError, `RunSupervisor` завернул его в SupervisorError — и тот
    вылетел наружу, убив ВЕСЬ TUI ещё до первого экрана. В трейсбеке не было ни
    имени рецепта, ни того, что с этим делать.

    Проводка и установка — две независимые операции: сервер деинсталлируют,
    сносят venv рецептов, переносят конфиг на другую машину. Состояние
    «подключён, но не установлен» штатное, и фронтенд обязан его пережить.

    Подмена, которую ловит тест: убрать `try/except SupervisorError` вокруг
    `run_entry` — SupervisorError снова полетит наружу.
    """
    manifest = _write_manifest(
        tmp_path,
        """
        mcps:
          - name: science
            path: ./science
            install: {type: source, source: science-mcp}
            run:
              type: command
              command: definitely-not-installed-mcp
              args: ["--transport", "http"]
          - name: other
            path: ./other
            install: {type: source, source: other-mcp}
            run:
              type: command
              command: also-not-installed-mcp
              args: ["--transport", "http"]
        """,
    )
    config = {
        "mcp-servers": {
            "science": {
                "transport": "stream_http",
                "url": "http://127.0.0.1:8765/mcp",
            },
            "other": {
                "transport": "stream_http",
                "url": "http://127.0.0.1:8766/mcp",
            },
        }
    }

    supervisor = ensure_daemons_for_agent(config, manifest_path=manifest)

    # Ни одного процесса не поднялось, но вызов вернулся штатно.
    assert supervisor is not None


def test_broken_daemon_does_not_block_its_healthy_neighbour(
    tmp_path: Path,
) -> None:
    """Один непоставленный рецепт не мешает подняться остальным.

    Раньше это следовало из падения: до соседа очередь просто не доходила.
    Проверяется отдельно от предыдущего теста, потому что `continue` легко
    заменить на `break` или на ранний `return`, и оба варианта оставят первый
    тест зелёным.
    """
    manifest = _write_manifest(
        tmp_path,
        """
        mcps:
          - name: broken
            path: ./broken
            install: {type: source, source: broken-mcp}
            run:
              type: command
              command: definitely-not-installed-mcp
              args: ["--transport", "http"]
          - name: healthy
            path: ./healthy
            install: {type: source, source: healthy-mcp}
            run:
              type: command
              command: healthy-mcp
              args: ["--transport", "http"]
        """,
    )
    config = {
        "mcp-servers": {
            "broken": {
                "transport": "stream_http",
                "url": "http://127.0.0.1:8765/mcp",
            },
            "healthy": {
                "transport": "stream_http",
                "url": "http://127.0.0.1:8766/mcp",
            },
        }
    }

    seen: list[str] = []

    def _fake_run_entry(entry, root, supervisor):
        seen.append(entry.name)
        if entry.name == "broken":
            from recommended_mcp.process_supervisor import SupervisorError

            raise SupervisorError(
                "mcp 'broken': failed to start: no such file"
            )
        return False

    with patch("recommended_mcp.autostart.run_entry", _fake_run_entry):
        ensure_daemons_for_agent(config, manifest_path=manifest)

    assert seen == ["broken", "healthy"], (
        f"после провала первого рецепта очередь до соседа не дошла: {seen}"
    )


def test_bridge_never_prevents_the_frontend_from_starting(
    monkeypatch, capsys
) -> None:
    """Любой сбой автозапуска — предупреждение, фронтенд стартует.

    Второй рубеж поверх поштучной защиты рецептов: ловит то, что случилось ДО
    цикла или вокруг него — битый манифест, недоступный каталог рецептов, сбой
    резолва пути. Обещание «фронтенд стартует всегда» должно быть свойством
    одной функции, а не дисциплиной каждого вызывающего: их двое (gc_cli, gc_tui),
    и в TUI автозапуск включён по умолчанию.

    Подмена: убрать try/except в ensure_mcp_daemons — исключение снова полетит
    в main() и убьёт процесс до первого экрана.
    """
    from recommended_mcp.runtime import daemon_autostart

    def _boom(_config):
        raise RuntimeError("манифест не читается")

    monkeypatch.setattr(daemon_autostart, "_supervisor", None, raising=False)
    monkeypatch.setattr(
        "recommended_mcp.autostart.ensure_daemons_for_agent", _boom
    )

    daemon_autostart.ensure_mcp_daemons({})  # не должно бросить

    err = capsys.readouterr().err
    assert "автозапуск MCP пропущен" in err
    assert "манифест не читается" in err


def test_bridge_does_not_mask_a_broken_recipes_package(monkeypatch) -> None:
    """ImportError ИЗ рецептов обязан лететь как есть.

    Это поломка пакета, а не рабочее состояние: опечатка в модуле, забытая
    зависимость. Выдать её за «просто не завелось» значит спрятать баг, который
    иначе виден сразу. Граница проходит по месту: импорт стоит ВНЕ try, вызов —
    внутри.

    Подмена: втянуть строку импорта внутрь try — тест краснеет.
    """
    import builtins

    from recommended_mcp.runtime import daemon_autostart

    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == "recommended_mcp.autostart":
            raise ImportError("No module named 'recommended_mcp.autostart'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    try:
        daemon_autostart.ensure_mcp_daemons({})
    except ImportError:
        pass
    else:
        raise AssertionError(
            "ImportError из рецептов проглочен — поломка пакета скрыта"
        )


def _fail_one_recipe(tmp_path: Path):
    """Манифест с одним рецептом `science`."""
    return _write_manifest(
        tmp_path,
        """
        mcps:
          - name: science
            path: ./science
            install: {type: source, source: science-mcp}
            run:
              type: command
              command: definitely-not-installed-mcp
              args: ["--transport", "http"]
        """,
    )


def _raise_supervisor_error(entry, root, supervisor):
    """`run_entry`, всегда падающий SupervisorError.

    Подменяем, а не полагаемся на несуществующую команду: на машине разработчика
    настоящий `science` может быть ЗАПУЩЕН, и тогда рецепт уходит в ветку
    `[skip] already running`, ветка предупреждения не исполняется, а тест зеленеет
    впустую. Поймано на живой машине.
    """
    from recommended_mcp.process_supervisor import SupervisorError

    raise SupervisorError("mcp 'science': failed to start: no such file")


_SCIENCE_CONFIG = {
    "mcp-servers": {
        "science": {
            "transport": "stream_http",
            "url": "http://127.0.0.1:8765/mcp",
        }
    }
}


def test_disable_hint_comes_from_the_frontend_not_from_this_module(
    tmp_path, capsys
):
    """Как выключить автозапуск — знает команда, а не общий модуль.

    Модуль зовут оба фронтенда: `gc_tui/__init__.py` и `gc_cli/cli.py`. Флаг
    `--no-auto-mcp` есть только у gc-ua; у gc-cli автозапуск opt-in через `-a`,
    и такого флага там нет вовсе — argparse отдал бы его в `apply_cli_overrides`
    как перекрытие неизвестного ключа конфига, а автозапуск остался бы включён.
    То есть подсказка советовала бы половине пользователей действие, которое не
    работает и о котором они не узнают.
    """
    manifest = _fail_one_recipe(tmp_path)

    with patch("recommended_mcp.autostart.run_entry", _raise_supervisor_error):
        ensure_daemons_for_agent(
            _SCIENCE_CONFIG,
            manifest_path=manifest,
            disable_hint="отключить автозапуск: запустите без `-a`",
        )

    err = capsys.readouterr().err
    assert "запустите без `-a`" in err
    assert "--no-auto-mcp" not in err, (
        "имя флага TUI не должно течь в подсказку gc-cli"
    )


def test_autostart_warning_reaches_the_notices_channel(tmp_path, monkeypatch):
    """Предупреждение обязано дожить до `/notices`, а не только до stderr.

    `gc-ua` зовёт автозапуск непосредственно перед `app.run()`, а тот
    переключает терминал в alternate screen: подсказка «`gc-mcp install
    science`» мелькала долю секунды и уезжала под экран TUI. В `/notices`
    оставалось только следствие — «нет соединения с http://127.0.0.1:…» — без
    единого слова о причине и лекарстве.

    Тем самым терялся смысл правки, ради которой предупреждение и появилось:
    фронтенд переставал падать, но взамен человек не получал ничего.
    """
    from gc_core import runtime_notices

    monkeypatch.setattr(runtime_notices, "_notices", [], raising=False)
    manifest = _fail_one_recipe(tmp_path)

    with patch("recommended_mcp.autostart.run_entry", _raise_supervisor_error):
        ensure_daemons_for_agent(
            _SCIENCE_CONFIG, manifest_path=manifest, disable_hint="проба"
        )

    codes = [n.code for n in runtime_notices._notices]
    assert "mcp_autostart_recipe" in codes, codes
    notice = next(
        n for n in runtime_notices._notices if n.code == "mcp_autostart_recipe"
    )
    assert notice.level == "warning"
    assert "gc-mcp install science" in notice.message
    # Имя рецепта помечено для подсветки — иначе карточка читается как сплошной
    # текст, а важно в ней именно оно.
    assert "science" in notice.highlights


def test_no_frontend_flag_is_hardcoded_in_the_shared_module():
    """Сторож против возврата: имя флага одного фронтенда в общем коде.

    Проверяется исходник, а не прогон: подсказку легко вернуть строкой в любую
    из веток предупреждений, и ни один функциональный тест этого не заметит —
    текст остаётся правдоподобным.

    Смотрим на строковые литералы через AST, а не на текст файла: докстринги и
    комментарии флаг УПОМИНАТЬ обязаны — там объясняется, почему его нельзя
    зашивать. Запрещено ровно одно: чтобы имя доехало до пользователя.
    """
    import ast

    from gc_testkit.paths import REPO

    for rel in ("autostart.py", "runtime/daemon_autostart.py"):
        path = REPO / "packages" / "mcp" / "src" / "recommended_mcp" / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))

        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(
                node,
                (
                    ast.Module,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                ),
            ):
                doc = node.body[0] if node.body else None
                if (
                    isinstance(doc, ast.Expr)
                    and isinstance(doc.value, ast.Constant)
                    and isinstance(doc.value.value, str)
                ):
                    docstrings.add(id(doc.value))

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                assert "--no-auto-mcp" not in node.value, (
                    f"{rel}:{node.lineno}: имя флага gc-ua зашито в общий модуль "
                    "— у gc-cli его нет, подсказка увела бы половину пользователей"
                )


@pytest.mark.parametrize(
    "rel",
    ["packages/cli/src/gc_cli/cli.py", "packages/tui/src/gc_tui/__init__.py"],
    ids=["gc-cli", "gc-ua"],
)
def test_both_frontends_pass_their_own_disable_hint(rel):
    """Оба вызывающих обязаны СКАЗАТЬ, как у них выключается автозапуск.

    Вторая половина того же контракта: общий модуль имя флага не знает, значит
    его обязан передать фронтенд. Убери `disable_hint=` из вызова — и подсказка
    тихо станет нейтральной заглушкой, а ни один функциональный тест этого не
    заметит: текст останется осмысленным, просто бесполезным.

    Читается ИСХОДНИК, а не импортируется модуль: набор gc-mcp не должен
    зависеть от пакетов профилей (`tests/test_test_ownership.py`).
    """
    import ast

    from gc_testkit.paths import REPO

    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"), rel)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None))
        == "ensure_mcp_daemons"
    ]
    assert calls, f"{rel}: вызов ensure_mcp_daemons не найден — тест устарел"
    for call in calls:
        assert any(kw.arg == "disable_hint" for kw in call.keywords), (
            f"{rel}:{call.lineno}: ensure_mcp_daemons зовётся без disable_hint — "
            "пользователю достанется нейтральная заглушка вместо способа "
            "выключить автозапуск в ЭТОЙ команде"
        )
