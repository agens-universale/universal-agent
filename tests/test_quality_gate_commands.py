"""Контракты запуска гейта: реальные пути, выбор хуков и ошибки анализа."""

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from gc_testkit.paths import REPO


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repository(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_lint_expands_literal_directories_without_splitting_filenames(repository):
    folder = repository / "src [literal]"
    folder.mkdir()
    tracked = folder / "tracked file.py"
    tracked.write_text("value = 1\n")
    subprocess.run(["git", "add", "."], check=True)
    new = folder / "new file.py"
    new.write_text("value = 2\n")
    (repository / ".gitignore").write_text("*.ignored\n")
    (folder / "secret.ignored").write_text("ignore me")
    checker = load_script("lint_paths")
    expected = sorted([str(tracked.relative_to(repository)), str(new.relative_to(repository))])
    assert checker.expand_paths([folder.name, str(tracked)]) == expected
    assert checker.expand_paths(["."]) == sorted([*expected, ".gitignore"])


@pytest.mark.parametrize("path", ["missing", "empty", "../outside"])
def test_lint_rejects_missing_empty_and_external_paths(repository, path):
    (repository / "empty").mkdir()
    with pytest.raises(ValueError):
        load_script("lint_paths").expand_paths([path])


def test_lint_propagates_runner_failure_and_passes_real_files(repository):
    (repository / "src").mkdir()
    (repository / "src" / "bad file.py").write_text("if flag:\n    pass\n")
    spy = repository / "runner.py"
    spy.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(17)\n")
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts/lint_paths.py"), sys.executable, str(spy), "--", "src"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 17
    assert json.loads(result.stdout) == ["--hook-stage", "manual", "--files", "src/bad file.py"]


@pytest.mark.skipif(os.name == "nt", reason="lint.sh requires a POSIX shell")
def test_lint_shell_wires_directory_expansion(repository, monkeypatch):
    import shutil

    scripts = repository / "scripts"
    scripts.mkdir()
    for name in ("lint.sh", "lint_paths.py"):
        shutil.copy(REPO / "scripts" / name, scripts / name)
    (repository / "src").mkdir()
    (repository / "src" / "a file.py").write_text("value = 1\n")
    bin_dir = repository / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!" + sys.executable + "\nimport os, sys, json\n"
        "args = sys.argv[4:]\n"
        'if args[0] == "python":\n    os.execv(sys.executable, [sys.executable, *args[1:]])\n'
        "print(json.dumps(args))\nsys.exit(19)\n"
    )
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(["bash", str(scripts / "lint.sh"), "src"], text=True, capture_output=True)
    assert result.returncode == 19, result.stderr
    assert json.loads(result.stdout) == ["pre-commit", "run", "--hook-stage", "manual", "--files", "src/a file.py"]


@pytest.mark.parametrize("stage", ["pre-commit", "pre-push", "manual"])
def test_lock_only_change_cannot_skip_consistency_hook(repository, stage):
    config = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())
    hook = next(h for h in config["repos"][0]["hooks"] if h["id"] == "uv-lock-check")
    # Сохраняем реальные фильтры и стадии. Анализатор заменяем отказом:
    # тест должен увидеть этот отказ даже при глобально исключённом uv.lock.
    hook["entry"] = shlex.join([sys.executable, "-c", "raise SystemExit(42)"])
    config["repos"][0]["hooks"] = [hook]
    (repository / ".pre-commit-config.yaml").write_text(yaml.safe_dump(config))
    (repository / "uv.lock").write_text("invalid lock for this test\n")
    result = subprocess.run(
        [sys.executable, "-m", "pre_commit", "run", "uv-lock-check", "--hook-stage", stage, "--files", "uv.lock"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "exit code: 42" in result.stdout
    assert "Skipped" not in result.stdout


@pytest.mark.parametrize("content", [b"def broken(:\n", b"\xff\xfe\xff"])
def test_dead_code_input_errors_fail_the_command(repository, monkeypatch, capsys, content):
    checker = load_script("dead_code_check")
    (repository / "broken.py").write_bytes(content)
    monkeypatch.setattr(checker, "ROOT", repository)
    monkeypatch.setattr(checker, "PROD_PATHS", ["broken.py"])
    monkeypatch.setattr(checker, "USAGE_PATHS", [])
    monkeypatch.setattr(checker, "WHITELIST", "missing.py")
    assert checker.main() == 2
    output = capsys.readouterr()
    assert "could not analyze" in output.err
    assert "0 finding(s)" not in output.out
