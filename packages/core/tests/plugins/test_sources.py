"""Git-источники плагинов: клон при сборке снимка, обновление веток (§8.2)."""

import os
import subprocess

from gc_core.config_modules.plugins import PluginSource
from gc_core.plugins import catalog, sources
from gc_testkit.settings import make_settings

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _run(cwd, *cmd):
    done = subprocess.run(
        ["git", *cmd],
        cwd=cwd,
        check=True,
        env=_ENV,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _repo(tmp_path):
    """Репозиторий с двумя коммитами; возвращает (путь, первый sha)."""
    repo = tmp_path / "repo"
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "gitplug", "version": "1.0.0"}', encoding="utf-8"
    )
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "one")
    first = _run(repo, "rev-parse", "HEAD")
    (repo / "marker.txt").write_text("two", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "two")
    return repo, first


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(sources, "_resolved", {})
    monkeypatch.setattr(sources, "_warned", set())


def _loaded(src):
    return make_settings(
        {"plugins.sources": [src], "plugins.use_installed": False}
    )


def _head(clone):
    return _run(clone, "rev-parse", "HEAD")


def _clone_of(src):
    return sources.cache_dir(sources.normalize_url(src.git), src.ref)


def test_update_keeps_commit_pin(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo, first = _repo(tmp_path)
    src = PluginSource(git=str(repo), ref=first)
    assert sources.resolve_source(src) is not None
    clone = _clone_of(src)

    assert sources.is_pinned(clone)
    assert sources.update_cached() == []
    assert _head(clone) == first


def test_update_keeps_tag_pin(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo, first = _repo(tmp_path)
    _run(repo, "tag", "v1", first)
    src = PluginSource(git=str(repo), ref="v1")
    assert sources.resolve_source(src) is not None

    assert sources.update_cached() == []
    assert _head(_clone_of(src)) == first


def test_update_moves_branch(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo, first = _repo(tmp_path)
    _run(repo, "checkout", "-q", "-b", "dev", first)
    src = PluginSource(git=str(repo), ref="dev")
    assert sources.resolve_source(src) is not None
    clone = _clone_of(src)
    assert not sources.is_pinned(clone)
    (repo / "x.txt").write_text("x", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "three")

    assert sources.update_cached() == [(clone.name, True)]
    assert _head(clone) == _run(repo, "rev-parse", "HEAD")


def test_snapshot_clones_source_when_clone_true(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo, _ = _repo(tmp_path)
    loaded = _loaded({"git": str(repo)})

    snap = catalog.build_plugin_snapshot(loaded, clone=True)

    assert [p.name for p in snap.plugins] == ["gitplug"]
    assert snap.notices == ()


def test_snapshot_without_clone_reports_missing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo, _ = _repo(tmp_path)

    snap = catalog.build_plugin_snapshot(
        _loaded({"git": str(repo)}), clone=False
    )

    assert snap.plugins == ()
    assert any("репозиторий не получен" in n for n in snap.notices)


def test_missing_subdir_notice_names_subfolder(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo, _ = _repo(tmp_path)

    snap = catalog.build_plugin_snapshot(
        _loaded({"git": str(repo), "subdir": "nope"}), clone=True
    )

    assert any("подкаталог" in n for n in snap.notices)
    assert not any("репозиторий не получен" in n for n in snap.notices)


def test_clone_failure_is_notice(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    snap = catalog.build_plugin_snapshot(
        _loaded({"git": str(tmp_path / "nope")}), clone=True
    )

    assert snap.plugins == ()
    assert len(snap.notices) == 1


def test_unusable_source_is_notice_only(tmp_path, monkeypatch, caplog):
    """Причина — в уведомлении; лог его не дублирует в stderr CLI."""
    _isolate(tmp_path, monkeypatch)
    repo, _ = _repo(tmp_path)
    caplog.set_level("WARNING", logger=sources.__name__)
    loaded = make_settings(
        {
            "plugins.sources": [
                {"git": str(tmp_path / "nope")},
                {"git": str(repo), "subdir": "nope"},
            ],
            "plugins.use_installed": False,
        }
    )

    snap = catalog.build_plugin_snapshot(loaded, clone=True)

    assert len(snap.notices) == 2
    assert caplog.records == []


def test_clone_is_memoised_per_process(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    repo, _ = _repo(tmp_path)
    loaded = _loaded({"git": str(repo)})
    catalog.build_plugin_snapshot(loaded, clone=True)
    calls = []
    monkeypatch.setattr(sources, "_clone", lambda *a: calls.append(a))

    catalog.build_plugin_snapshot(loaded, clone=True)

    assert calls == []


def test_clone_url_never_read_as_git_option(tmp_path, monkeypatch):
    """Адрес из конфига идёт после ``--``: ``-…`` не станет опцией git."""
    calls = []

    def _git(args, _cwd=None):
        calls.append(args)
        if args[0] == "clone":
            os.makedirs(args[-1])

    monkeypatch.setattr(sources, "_git", _git)
    url = "--upload-pack=touch x"
    sources._clone(url, None, tmp_path / "a")
    sources._clone(url, "abc1234", tmp_path / "b")

    clone, remote = calls[0], calls[2]
    assert clone[-3:] == ["--", url, str(tmp_path / "a.tmp")]
    assert remote == ["remote", "add", "origin", "--", url]
