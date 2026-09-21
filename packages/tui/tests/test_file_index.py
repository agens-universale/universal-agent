"""Юнит-тесты file_index: парс @-сегмента, listing, fuzzy, резолв, футер."""
from gc_tui.services.file_index import parse_at_segment


def test_at_at_start():
    assert parse_at_segment("@age", 4) == (0, "age")

def test_at_after_space_midphrase():
    text = "почини @age"
    assert parse_at_segment(text, len(text)) == (7, "age")

def test_at_inside_word_not_triggered():
    assert parse_at_segment("foo@bar", 7) is None

def test_space_in_segment_breaks():
    text = "@agents now"
    assert parse_at_segment(text, len(text)) is None

def test_cursor_before_at():
    assert parse_at_segment("x @age", 1) is None

def test_path_with_slash_is_segment():
    assert parse_at_segment("@agents/core", 12) == (0, "agents/core")

def test_trailing_slash_kept():
    assert parse_at_segment("@agents/", 8) == (0, "agents/")


from gc_tui.services.file_index import list_dir


def test_list_dir_dirs_before_files_with_slash(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "zeta").mkdir()
    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text("x", encoding="utf-8")
    out = list_dir(str(tmp_path), "")
    assert out == ["agents/", "zeta/", ".env", "alpha.py"]

def test_list_dir_subdir(tmp_path):
    (tmp_path / "agents" / "core").mkdir(parents=True)
    (tmp_path / "agents" / "a.py").write_text("x", encoding="utf-8")
    out = list_dir(str(tmp_path), "agents")
    assert out == ["agents/core/", "agents/a.py"]

def test_list_dir_hides_git(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    assert list_dir(str(tmp_path), "") == ["a.py"]


import subprocess

from gc_tui.services.file_index import fuzzy_search, rank


def test_rank_prefix_of_basename_first():
    paths = ["src/agent_runner.py", "agents/agent.py", "x/zagent.py"]
    out = rank("agent", paths)
    assert out[0] in ("agents/agent.py",)


def test_fuzzy_filters_by_segment_prefix_not_char_subsequence(tmp_path):
    """Короткий ввод — префикс/подстрока сегмента, не разрозненные буквы по path."""
    (tmp_path / "demo_config.yml").write_text("x", encoding="utf-8")
    (tmp_path / "readme.md").write_text("x", encoding="utf-8")
    (tmp_path / "config.default.yml").write_text("x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    # char-subsequence «dem» ложно цеплял бы readme/config; префикс — нет.
    out = fuzzy_search(str(tmp_path), "dem")
    assert out == ["demo_config.yml"]
    # префикс сегмента: «sr» → src/, «mai» → main.py
    assert "src/" in fuzzy_search(str(tmp_path), "sr")
    assert "src/main.py" in fuzzy_search(str(tmp_path), "mai")
    # навигация с '/'
    assert "src/main.py" in fuzzy_search(str(tmp_path), "src/ma")


def test_fuzzy_substring_inside_segment(tmp_path):
    """Поиск внутри сегмента: query как непрерывная подстрока имени, не только префикс."""
    (tmp_path / "demo_config.yml").write_text("x", encoding="utf-8")
    (tmp_path / "config.default.yml").write_text("x", encoding="utf-8")
    (tmp_path / "readme.md").write_text("x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "tui").mkdir()
    (tmp_path / "src" / "tui" / "file_index.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "tui" / "main_screen.py").write_text("x", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_file_index.py").write_text("x", encoding="utf-8")
    (tmp_path / "docs_md").mkdir()
    (tmp_path / "docs_md" / "strict-optional-validation.md").write_text("x", encoding="utf-8")

    # «config» внутри demo_config.yml и как префикс config.default.yml
    out = fuzzy_search(str(tmp_path), "config")
    assert "demo_config.yml" in out
    assert "config.default.yml" in out
    assert "readme.md" not in out

    # середина имени файла
    assert "src/tui/file_index.py" in fuzzy_search(str(tmp_path), "index")
    assert "tests/test_file_index.py" in fuzzy_search(str(tmp_path), "file_index")
    assert "src/tui/main_screen.py" in fuzzy_search(str(tmp_path), "screen")

    # середина имени папки / сегмента пути
    assert "src/tui/" in fuzzy_search(str(tmp_path), "tui")
    assert any(p.startswith("docs_md/") for p in fuzzy_search(str(tmp_path), "optional"))

    # дефис/подстрока в длинном имени
    assert "docs_md/strict-optional-validation.md" in fuzzy_search(
        str(tmp_path), "optional-validation"
    )
    assert "docs_md/strict-optional-validation.md" in fuzzy_search(str(tmp_path), "strict")

    # регистр не важен
    assert "demo_config.yml" in fuzzy_search(str(tmp_path), "CONFIG")
    assert "src/tui/file_index.py" in fuzzy_search(str(tmp_path), "File_Index")

    # char-subsequence по-прежнему не матчит: «fidx» ≠ непрерывная подстрока
    assert fuzzy_search(str(tmp_path), "fidx") == []
    # «rdm» не цепляет readme через разрозненные буквы
    assert fuzzy_search(str(tmp_path), "rdm") == []


def test_fuzzy_slash_navigation_and_nested(tmp_path):
    """Query с '/' — startswith или подстрока всего path; вложенные пути."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "tui").mkdir()
    (tmp_path / "src" / "tui" / "screens").mkdir()
    (tmp_path / "src" / "tui" / "screens" / "main.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "tui" / "services").mkdir()
    (tmp_path / "src" / "tui" / "services" / "file_index.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "tools").mkdir()
    (tmp_path / "src" / "tools" / "schema_optional.py").write_text("x", encoding="utf-8")
    (tmp_path / "tests" / "tui").mkdir(parents=True)
    (tmp_path / "tests" / "tui" / "test_file_index.py").write_text("x", encoding="utf-8")

    # префикс пути
    out = fuzzy_search(str(tmp_path), "src/tui/")
    assert "src/tui/screens/" in out
    assert "src/tui/services/" in out
    assert "src/tui/screens/main.py" in out
    assert "src/tools/schema_optional.py" not in out

    # частичный сегмент после '/'
    assert "src/tui/screens/main.py" in fuzzy_search(str(tmp_path), "src/tui/sc")
    assert "src/tui/services/file_index.py" in fuzzy_search(str(tmp_path), "src/tui/se")

    # подстрока path не с корня: «tui/serv»
    assert "src/tui/services/file_index.py" in fuzzy_search(str(tmp_path), "tui/serv")
    assert "src/tui/screens/main.py" not in fuzzy_search(str(tmp_path), "tui/serv")

    # другой корень с тем же хвостом не путается с src/tui при узком префиксе
    only_tests = fuzzy_search(str(tmp_path), "tests/tui/")
    assert "tests/tui/test_file_index.py" in only_tests
    assert all(p.startswith("tests/") for p in only_tests)


def test_fuzzy_rank_prefix_basename_over_deeper_hit(tmp_path):
    """Префикс basename выше глубокого совпадения; папки с '/' тоже ранжируются."""
    (tmp_path / "agent.py").write_text("x", encoding="utf-8")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "core").mkdir()
    (tmp_path / "agents" / "core" / "runner.py").write_text("x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "my_agent_utils.py").write_text("x", encoding="utf-8")

    out = fuzzy_search(str(tmp_path), "agent")
    assert out[0] in ("agent.py", "agents/")
    assert "agent.py" in out
    assert "agents/" in out
    assert "src/my_agent_utils.py" in out
    # basename-префикс agent.py раньше mid-name my_agent_utils
    assert out.index("agent.py") < out.index("src/my_agent_utils.py")


def test_fuzzy_non_git_fallback_walk(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "junk.py").write_text("x", encoding="utf-8")
    out = fuzzy_search(str(tmp_path), "agent")
    assert "agents/agent.py" in out
    assert all(".venv" not in p for p in out)
    # поиск внутри имени вложенного файла
    assert "agents/agent.py" in fuzzy_search(str(tmp_path), "gent")

def test_fuzzy_git_respects_gitignore(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    (tmp_path / "secret.log").write_text("x", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    out = fuzzy_search(str(tmp_path), "")
    assert "agents/" in out
    out2 = fuzzy_search(str(tmp_path), "log")
    assert all(not p.endswith("secret.log") for p in out2)


import os

from gc_tui.services.file_index import extract_at_mentions, resolve_attachment, build_attachment_footer


def test_extract_mentions():
    text = "сравни @a.py и @sub/b.py, но не foo@bar"
    assert extract_at_mentions(text) == ["a.py", "sub/b.py"]

def test_resolve_file_and_dir(tmp_path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    f = resolve_attachment(str(tmp_path), "a.py")
    d = resolve_attachment(str(tmp_path), "sub/")
    assert f[0] == "a.py" and f[2] == "файл" and os.path.isabs(f[1])
    assert d[0] == "sub/" and d[2] == "папка" and os.path.isabs(d[1])
    assert resolve_attachment(str(tmp_path), "nope.py") is None

def test_build_footer():
    body = build_attachment_footer([
        ("a.py", r"D:\proj\a.py", "файл"),
        ("sub/", r"D:\proj\sub", "папка"),
    ])
    assert "@a.py" in body and r"D:\proj\a.py" in body and "(файл)" in body
    assert "@sub/" in body and "(папка)" in body
    assert body.splitlines()[0].startswith("Пользователь прикрепил через «@»")

def test_build_footer_empty():
    assert build_attachment_footer([]) == ""


def test_fuzzy_finds_untracked_files(tmp_path, monkeypatch):
    """Untracked (но не gitignored) файлы находятся fuzzy-ем, как и через drill-in.

    Раньше _all_paths брал только `git ls-files` (tracked) → «.claude/» листился
    drill-in'ом с диска, а «.claude/s» давал «нет совпадений» — новые/untracked
    файлы не находились набором ни в @-пикере, ни в /view."""
    # герметичность: глобальный ignore юзера — это XDG-дефолт
    # ($XDG_CONFIG_HOME/git/ignore), уводим его в пустую папку
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    (tmp_path / "tracked.py").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
    out = fuzzy_search(str(tmp_path), ".claude/s")
    assert ".claude/settings.local.json" in out
    # и папка находится без хвостового '/'
    assert any(p.startswith(".claude") for p in fuzzy_search(str(tmp_path), ".cla"))


def test_fuzzy_slash_query_lists_ignored_dir_like_drill_in(tmp_path):
    """Drill-инвариант: gitignored-файл виден по «dir/» (list_dir читает диск),
    значит и «dir/<буква>» обязан его находить — раньше пропадал на первой букве.
    Глобальный поиск БЕЗ '/' ignore уважает (мусор не лезет в выдачу)."""
    (tmp_path / "tracked.py").write_text("x", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "run.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("logs/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    assert "logs/run.txt" in fuzzy_search(str(tmp_path), "logs/r")
    assert all("run.txt" not in p for p in fuzzy_search(str(tmp_path), "run"))


def test_fuzzy_git_dir_respects_gitignore(tmp_path):
    """В git-репо .gitignore'нутая ПАПКА не протекает в fuzzy (деривация из ls-files)."""
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "run.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("logs/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    out = fuzzy_search(str(tmp_path), "log")
    # папка logs/ проигнорена git'ом → не появляется
    assert all("logs" not in p for p in out)
    # tracked-папка agents/ доступна
    out2 = fuzzy_search(str(tmp_path), "agent")
    assert "agents/" in out2


def test_absolute_and_drive_queries_are_not_listed(tmp_path):
    """Абсолютные/дисковые пути — вне корня проекта: пикер их не подсказывает
    (спека: внешние пути набираются руками). Windows-ловушка: join(root, "D:")
    даёт drive-relative путь = «текущая папка диска D» — листался бы корень
    проекта под видом D:/…"""
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    assert list_dir(str(tmp_path), "D:") == []
    assert list_dir(str(tmp_path), "C:/Users") == []
    assert fuzzy_search(str(tmp_path), "D:/") == []
    assert fuzzy_search(str(tmp_path), "C:/Us") == []
    assert fuzzy_search(str(tmp_path), "/etc/pas") == []


def test_path_index_builds_once_then_filters_in_memory(tmp_path, monkeypatch):
    """Индекс собирается один раз: вторая буква не запускает второй обход диска."""
    from gc_tui.services import file_index
    from gc_tui.services.file_index import PathIndex
    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    (tmp_path / "beta.py").write_text("x", encoding="utf-8")
    calls = []
    real = file_index._all_paths

    def counting(root, max_paths, **kw):
        calls.append(root)
        return real(root, max_paths, **kw)

    monkeypatch.setattr(file_index, "_all_paths", counting)
    idx = PathIndex(str(tmp_path))
    assert idx.search("alp") == ["alpha.py"]
    assert idx.search("bet") == ["beta.py"]
    assert calls == [str(tmp_path)]


def test_concurrent_searches_build_index_once(tmp_path, monkeypatch):
    """Поиск зовут из фоновых потоков по одному на нажатие: пока первый собирает
    список, второй ждёт его, а не запускает свой обход."""
    import threading
    import time
    from gc_tui.services import file_index
    from gc_tui.services.file_index import PathIndex
    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    calls = []
    real = file_index._all_paths

    def slow(root, max_paths, **kw):
        calls.append(root)
        time.sleep(0.2)
        return real(root, max_paths, **kw)

    monkeypatch.setattr(file_index, "_all_paths", slow)
    idx = PathIndex(str(tmp_path))
    results = []
    threads = [threading.Thread(target=lambda: results.append(idx.search("alp")))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [["alpha.py"], ["alpha.py"]]
    assert calls == [str(tmp_path)]


def test_walk_cap_keeps_shallow_levels_of_all_dirs(tmp_path):
    """Предохранитель срезает глубину: неглубокий файл соседней папки попадает
    в индекс раньше глубоких хвостов большой (os.walk шёл вглубь — zzz/target.py
    до потолка не доживал)."""
    from gc_tui.services.file_index import PathIndex
    big = tmp_path / "aaa_big" / "deep"
    big.mkdir(parents=True)
    for i in range(50):
        (big / f"f{i}.txt").write_text("x", encoding="utf-8")
    (tmp_path / "zzz").mkdir()
    (tmp_path / "zzz" / "target.py").write_text("x", encoding="utf-8")
    idx = PathIndex(str(tmp_path), max_paths=10)
    assert idx.search("target") == ["zzz/target.py"]
    assert idx.truncated is True


def test_index_not_truncated_when_project_fits(tmp_path):
    """Ровно в размер потолка — не «обрезано»: подсказка не должна врать."""
    from gc_tui.services.file_index import PathIndex
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    idx = PathIndex(str(tmp_path), max_paths=1)
    assert idx.search("a") == ["a.py"]
    assert idx.truncated is False


def test_git_listing_cap_keeps_shallow_files(tmp_path, monkeypatch):
    """git-ветка: при превышении потолка остаются самые неглубокие файлы."""
    from gc_tui.services.file_index import PathIndex
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    deep = tmp_path / "aaa" / "b" / "c"
    deep.mkdir(parents=True)
    for i in range(10):
        (deep / f"f{i}.txt").write_text("x", encoding="utf-8")
    (tmp_path / "top.py").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    idx = PathIndex(str(tmp_path), max_paths=3)
    assert idx.search("top") == ["top.py"]
    assert idx.truncated is True


def test_git_timeout_falls_back_to_walk_and_is_reported(tmp_path, monkeypatch):
    """git ls-files не уложился в таймаут (огромный репозиторий): индекс собран
    обходом, а факт таймаута виден вызывающему — он больше не будет звать git."""
    from gc_tui.services import file_index
    from gc_tui.services.file_index import PathIndex
    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")

    def timed_out(root):
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(file_index, "_git_ls_files", timed_out)
    idx = PathIndex(str(tmp_path))
    assert idx.search("alp") == ["alpha.py"]
    assert idx.git_timed_out is True


def test_use_git_false_never_calls_git(tmp_path, monkeypatch):
    from gc_tui.services import file_index
    from gc_tui.services.file_index import PathIndex
    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    calls = []

    def git_called(root):
        calls.append(root)
        raise AssertionError("git called")

    monkeypatch.setattr(file_index, "_git_ls_files", git_called)
    idx = PathIndex(str(tmp_path), use_git=False)
    assert idx.search("alp") == ["alpha.py"]
    assert calls == []
    assert idx.git_timed_out is False


def test_git_index_finds_non_ascii_names(tmp_path, monkeypatch):
    """git ls-files квотит не-ASCII имена ("\\320\\276…"): раньше в индекс попадал
    мусор вида «"/320/276/…» и русские имена не находились вовсе."""
    from gc_tui.services.file_index import PathIndex
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    (tmp_path / "доки").mkdir()
    (tmp_path / "доки" / "план.md").write_text("x", encoding="utf-8")
    (tmp_path / "отчёт.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    idx = PathIndex(str(tmp_path))
    assert idx.search("отч") == ["отчёт.md"]
    assert idx.search("план") == ["доки/план.md"]
    assert "доки/" in idx.search("док")
    assert idx.search("320") == []
    assert idx.git_timed_out is False


def test_git_index_survives_unquoted_utf8_output(tmp_path, monkeypatch):
    """core.quotePath=false: git пишет имена сырым UTF-8. С text=True вывод
    декодировался локальной кодировкой (cp1251 на Windows) — падение или кракозябры."""
    from gc_tui.services.file_index import PathIndex
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    (tmp_path / "отчёт.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "core.quotePath", "false"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    assert PathIndex(str(tmp_path)).search("отч") == ["отчёт.md"]


def test_walk_keeps_rest_of_dir_when_one_entry_errors(tmp_path, monkeypatch):
    """Ошибка на одной записи не должна выкидывать из индекса остаток папки."""
    from gc_tui.services import file_index
    (tmp_path / "aaa.py").write_text("x", encoding="utf-8")
    (tmp_path / "bad").mkdir()
    (tmp_path / "zzz.py").write_text("x", encoding="utf-8")
    real_scandir = os.scandir

    class _Entry:
        def __init__(self, entry):
            self._entry = entry
            self.name = entry.name

        def is_dir(self):
            return self._entry.is_dir()

        def is_symlink(self):
            if self.name == "bad":
                raise OSError("boom")
            return self._entry.is_symlink()

    class _Scan:
        def __init__(self, path):
            self._it = real_scandir(path)

        def __enter__(self):
            self._it.__enter__()
            return (_Entry(e) for e in self._it)

        def __exit__(self, *exc):
            return self._it.__exit__(*exc)

    monkeypatch.setattr(file_index.os, "scandir", _Scan)
    paths, truncated = file_index._walk_paths(str(tmp_path))
    assert sorted(paths) == ["aaa.py", "bad/", "zzz.py"]
    assert truncated is False


def test_rank_with_limit_equals_sorted_prefix():
    paths = ["b/agent.py", "agents/", "a/x/agent_old.py", "Agent.md", "zz/reagent.py",
             "agents/agent.py", "docs/agent/", "AGENT.txt", "docs/Agent.md", "docs/agent.md"]
    assert rank("agent", paths, limit=3) == rank("agent", paths)[:3]
    assert rank("agent", paths, limit=50) == rank("agent", paths)


def test_names_existing_dir(tmp_path):
    from gc_tui.services.file_index import names_existing_dir
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "agent.py").write_text("x", encoding="utf-8")
    root = str(tmp_path)
    assert names_existing_dir(root, "agents/ag") is True
    assert names_existing_dir(root, "agents/") is True
    assert names_existing_dir(root, "nodir/ag") is False
    assert names_existing_dir(root, "agents/agent.py/x") is False   # файл, не папка
    assert names_existing_dir(root, "agent") is False
    assert names_existing_dir(root, "/etc/pas") is False
    assert names_existing_dir(root, "C:/Us") is False


import pytest


def test_run_bounded_kills_whole_process_tree_on_timeout(tmp_path):
    """Команда не уложилась в таймаут, а её ДОЧЕРНИЙ процесс держит stdout (так
    ведёт себя git из PATH на Windows — обёртка над настоящим git): вызов обязан
    вернуться около таймаута, а не ждать закрытия пайпа, и потомка не оставить."""
    import sys
    import time
    from gc_tui.services import file_index
    beat = tmp_path / "beat.txt"
    # Потомок и родитель самоограничены (~20 с): если гашение дерева сломают,
    # тест честно упадёт на времени, а не повесит прогон и не оставит сирот.
    child = ("import time, sys\n"
             "for _ in range(200):\n"
             "    open(sys.argv[1], 'a').write('x')\n"
             "    time.sleep(0.1)\n")
    parent = ("import subprocess, sys, time\n"
              f"subprocess.Popen([sys.executable, '-c', {child!r}, sys.argv[1]])\n"
              "time.sleep(20)\n")
    started = time.perf_counter()
    with pytest.raises(subprocess.TimeoutExpired):
        file_index._run_bounded([sys.executable, "-c", parent, str(beat)], str(tmp_path), 3.0)
    elapsed = time.perf_counter() - started
    assert elapsed < 10
    assert beat.exists()                          # потомок успел родиться — есть кого гасить
    time.sleep(0.5)
    size = beat.stat().st_size
    time.sleep(0.7)
    assert beat.stat().st_size == size            # потомок мёртв
