"""app_version: версия пакета + источник установки из direct_url.json (PEP 610)."""

from gc_core import app_version

GIT_PINNED = '{"url": "https://github.com/pypa/sampleproject", "vcs_info": {"vcs": "git", "commit_id": "621e497", "requested_revision": "main"}}'
GIT_DEFAULT = (
    '{"url": "https://gitverse.ru/gigacode-ai/universal-agent", "vcs_info": {"vcs": "git", "commit_id": "a1b2c3d"}}'
)
EDITABLE = '{"url": "file:///D:/dev/sber/background-agent", "dir_info": {"editable": true}}'


def test_install_source_git_pinned():
    src = app_version.install_source(_read=lambda: GIT_PINNED)
    assert src == app_version.InstallSource(
        url="https://github.com/pypa/sampleproject", ref="main", commit="621e497", editable=False
    )


def test_install_source_git_default_branch_has_no_ref():
    src = app_version.install_source(_read=lambda: GIT_DEFAULT)
    assert src.ref is None
    assert src.commit == "a1b2c3d"
    assert not src.editable


def test_install_source_editable():
    src = app_version.install_source(_read=lambda: EDITABLE)
    assert src.editable is True
    assert src.commit is None


def test_install_source_absent_and_broken():
    assert app_version.install_source(_read=lambda: None) is None
    assert app_version.install_source(_read=lambda: "не json{") is None


def test_agent_version_is_nonempty_string():
    # editable-установка этого репо даёт версию из pyproject; фолбэк — "dev"
    assert isinstance(app_version.agent_version(), str)
    assert app_version.agent_version()


# --- подсказки установки ------------------------------------------------------
#
# Переехали 27.08.2026 из packages/core/tests/test_missing_profile_guards.py: тот
# файл сторожил шимы `tui_entry`/`cli_entry`/`serve_entry`, а их в ядре больше нет —
# после разведения имён команд «команда есть, а пакета нет» в здоровой установке не
# случается. Сами подсказки остались: `install_hint` зовёт gc_tui, когда не находит
# textual. Дыра, ради которой проверки заведены, — расхождение текста подсказки с
# реальным подкаталогом манифеста: пользователя отправляли в несуществующий
# `packaging/`.


def test_install_hint_uses_real_subdirs():
    """Подсказка собирается из PROFILE_SUBDIRS, а не из строки рядом с текстом."""
    for dist, subdir in app_version.PROFILE_SUBDIRS.items():
        hint = app_version.install_hint(dist)
        assert f"#subdirectory={subdir}" in hint
        assert hint.startswith("uv tool install")
        assert "\n" not in hint


def test_no_stale_packaging_dir_in_hints():
    """Каталога packaging/ не существует — ни одна подсказка не должна его называть."""
    for dist in app_version.PROFILE_SUBDIRS:
        assert "packaging/" not in app_version.install_hint(dist)


def test_install_hint_survives_every_profile():
    """Естественная форма вызова — `install_hint(installed_dist())`. Пока
    подкаталог брался как `PROFILE_SUBDIRS[dist]`, она падала KeyError на самой
    обычной, ПОЛНОЙ установке: `installed_dist()` возвращает `gc-ua`, а меты в
    словаре нет и быть не должно — её манифест лежит в корне."""
    for dist in app_version.PROFILE_DISTS:
        hint = app_version.install_hint(dist)
        assert hint.startswith("uv tool install")
        assert f'"{dist} @ git+' in hint
        assert "\n" not in hint
    assert "#subdirectory" not in app_version.install_hint("gc-ua"), (
        "мета лежит в корне — подкаталог ей не нужен и вреден"
    )
    assert "#subdirectory=packages/mcp" in app_version.install_hint("gc-mcp")


def test_profile_order_probes_mcp_before_core():
    """Порядок PROFILE_DISTS — не алфавит, а «полнота». Ядро тянут ВСЕ
    дистрибутивы, поэтому оно последнее. gc-mcp обязан стоять перед ним: он тоже
    зависит от ядра, и на голой установке менеджера перебор, наткнувшись сперва
    на gc-core, назвал бы её ядром — а `--update` переустановил бы не тот
    дистрибутив. Профили-приложения стоят выше gc-mcp: они его тянут, и находить
    надо их."""
    order = app_version.PROFILE_DISTS
    assert order[0] == "gc-ua", "мета полнее всех — она первая"
    assert order[-1] == "gc-core", "ядро транзитивно у всех — оно последнее"
    assert order.index("gc-mcp") < order.index("gc-core")
    for app_dist in ("gc-tui", "gc-server", "gc-cli"):
        assert order.index(app_dist) < order.index("gc-mcp"), app_dist

    # Второй несущий порядок, появившийся вместе с зависимостью gc-server → gc-cli:
    # в окружении серверной установки теперь лежат ОБА дистрибутива, и перебор
    # обязан наткнуться на gc-server первым. Иначе `--update` на сервере опознает
    # установку как CLI и переустановит узкий профиль вместо полного — молча, и
    # набор от этого не покраснеет, потому что раньше здесь закреплялись только
    # пары с gc-mcp и gc-core.
    assert order.index("gc-server") < order.index("gc-cli"), (
        "gc-server тянет gc-cli, поэтому обязан проверяться раньше него"
    )


def test_update_keeps_mcp_executables_for_leaf_profiles():
    """Команда обновления повторяет `--with-executables-from gc-mcp`.

    `uv tool install` не дописывает к существующей установке, а ПЕРЕПИСЫВАЕТ её
    по командной строке: чего в строке нет — того после обновления не будет.
    Листовые профили ставятся с этим флагом (readme.md, docs/packaging.md), и
    без него первое же `--update` молча уносило `gc-mcp` и `gc-mcp-install` из
    PATH. `uv` при этом рапортует «Installed 1 executable» и считает обновление
    удачным; в TUI ничего не ломается, потому что `/mcp` импортирует модуль
    напрямую, — замечает только тот, кто наберёт `gc-mcp` в шелле.

    До разбиения на профили этого быть не могло: единственный дистрибутив
    объявлял команды MCP сам, и любая переустановка их возвращала.

    Подмена, которую ловит тест: убрать `_executables_args` из
    `build_install_command`.
    """
    from gc_core.updater.runner import build_install_command
    from gc_core.updater.source import UpdateSource

    def _cmd(dist):
        src = UpdateSource(
            url="https://example.invalid/repo",
            ref=None,
            commit=None,
            dist=dist,
            subdirectory=app_version.PROFILE_SUBDIRS.get(dist),
        )
        return build_install_command(src, [])

    for dist in ("gc-cli", "gc-tui", "gc-server"):
        cmd = _cmd(dist)
        assert "--with-executables-from" in cmd, f"{dist}: обновление потеряет gc-mcp/gc-mcp-install из PATH"
        assert cmd[cmd.index("--with-executables-from") + 1] == "gc-mcp", dist

    # Отрицательный контроль: у этих трёх флаг не нужен и был бы ложью. Мета
    # объявляет все шесть команд сама, gc-mcp — их владелец, ядро не объявляет
    # ничего и как инструмент не ставится. Без этой половины тест прошёл бы и
    # на реализации «дописывать флаг всем подряд».
    for dist in ("gc-ua", "gc-mcp", "gc-core"):
        assert "--with-executables-from" not in _cmd(dist), dist


def test_install_hint_keeps_mcp_executables_for_leaf_profiles():
    """Та же оговорка в подсказке, которую агент печатает человеку.

    `gc_tui/__init__.py` показывает её при «профиль не установлен». Без флага
    человек получит рабочий профиль без `gc-mcp` в PATH — и следом совет
    онбординга ставить менеджер отдельным инструментом.
    """
    for dist in ("gc-cli", "gc-tui", "gc-server"):
        assert "--with-executables-from gc-mcp" in app_version.install_hint(dist), dist
    for dist in ("gc-ua", "gc-mcp", "gc-core"):
        assert "--with-executables-from" not in app_version.install_hint(dist), dist
