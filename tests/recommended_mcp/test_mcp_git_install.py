from pathlib import Path

from recommended_mcp import install as inst


def test_repo_subdir():
    assert inst._repo_subdir(Path("../external/office-mcp")) == "external/office-mcp"
    assert inst._repo_subdir(Path("../external/science-mcp")) == "external/science-mcp"


def test_install_source_local_preferred(tmp_path, monkeypatch):
    src = tmp_path / "external" / "office-mcp"
    src.mkdir(parents=True)
    (src / "pyproject.toml").write_text("[project]\nname='office-mcp'\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(inst, "active_pip_python", lambda: "PY")
    monkeypatch.setattr(inst.subprocess, "run", lambda argv, **kw: calls.append(argv))
    inst._install_source("office", Path("external/office-mcp"), tmp_path,
                         editable=False, repo_url="https://x/repo")
    # локальный путь есть → ставим из него, НЕ из git
    assert calls and str(src) in calls[0] and not any("git+" in a for a in calls[0])


def test_install_source_git_fallback(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(inst, "pip_argv", lambda *a: ["PY", "-m", "pip", *a])
    monkeypatch.setattr(inst.subprocess, "run", lambda argv, **kw: calls.append(argv))
    # локального пути нет → git-установка
    inst._install_source("office", Path("../external/office-mcp"), tmp_path / "nope",
                         editable=False, repo_url="https://x/repo", repo_ref="main")
    joined = " ".join(calls[0])
    assert "git+https://x/repo@main#subdirectory=external/office-mcp" in joined


def test_install_source_no_local_no_repo_errors(tmp_path):
    import pytest
    with pytest.raises(inst.InstallError):
        inst._install_source("office", Path("../external/office-mcp"), tmp_path / "nope",
                             editable=False, repo_url=None)
