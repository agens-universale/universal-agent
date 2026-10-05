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
    (tmp_path / "src" / "tui" / "file_index.py").write_text(
        "x", encoding="utf-8"
    )
    (tmp_path / "src" / "tui" / "main_screen.py").write_text(
        "x", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_file_index.py").write_text(
        "x", encoding="utf-8"
    )
    (tmp_path / "docs_md").mkdir()
    (tmp_path / "docs_md" / "strict-optional-validation.md").write_text(
        "x", encoding="utf-8"
    )

    # «config» внутри demo_config.yml и как префикс config.default.yml
    out = fuzzy_search(str(tmp_path), "config")
    assert "demo_config.yml" in out
    assert "config.default.yml" in out
    assert "readme.md" not in out

    # середина имени файла
    assert "src/tui/file_index.py" in fuzzy_search(str(tmp_path), "index")
    assert "tests/test_file_index.py" in fuzzy_search(
        str(tmp_path), "file_index"
    )
    assert "src/tui/main_screen.py" in fuzzy_search(str(tmp_path), "screen")

    # середина имени папки / сегмента пути
    assert "src/tui/" in fuzzy_search(str(tmp_path), "tui")
    assert any(
        p.startswith("docs_md/")
        for p in fuzzy_search(str(tmp_path), "optional")
    )

    # дефис/подстрока в длинном имени
    assert "docs_md/strict-optional-validation.md" in fuzzy_search(
        str(tmp_path), "optional-validation"
    )
    assert "docs_md/strict-optional-validation.md" in fuzzy_search(
        str(tmp_path), "strict"
    )

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
    (tmp_path / "src" / "tui" / "screens" / "main.py").write_text(
        "x", encoding="utf-8"
    )
    (tmp_path / "src" / "tui" / "services").mkdir()
    (tmp_path / "src" / "tui" / "services" / "file_index.py").write_text(
        "x", encoding="utf-8"
    )
    (tmp_path / "src" / "tools").mkdir()
    (tmp_path / "src" / "tools" / "schema_optional.py").write_text(
        "x", encoding="utf-8"
    )
    (tmp_path / "tests" / "tui").mkdir(parents=True)
    (tmp_path / "tests" / "tui" / "test_file_index.py").write_text(
        "x", encoding="utf-8"
    )

    # префикс пути
    out = fuzzy_search(str(tmp_path), "src/tui/")
    assert "src/tui/screens/" in out
    assert "src/tui/services/" in out
    assert "src/tui/screens/main.py" in out
    assert "src/tools/schema_optional.py" not in out

    # частичный сегмент после '/'
    assert "src/tui/screens/main.py" in fuzzy_search(
        str(tmp_path), "src/tui/sc"
    )
    assert "src/tui/services/file_index.py" in fuzzy_search(
        str(tmp_path), "src/tui/se"
    )

    # подстрока path не с корня: «tui/serv»
    assert "src/tui/services/file_index.py" in fuzzy_search(
        str(tmp_path), "tui/serv"
    )
    assert "src/tui/screens/main.py" not in fuzzy_search(
        str(tmp_path), "tui/serv"
    )

    # другой корень с тем же хвостом не путается с src/tui при узком префиксе
    only_tests = fuzzy_search(str(tmp_path), "tests/tui/")
    assert "tests/tui/test_file_index.py" in only_tests
    assert all(p.startswith("tests/") for p in only_tests)


def test_fuzzy_rank_prefix_basename_over_deeper_hit(tmp_path):
    """Префикс basename выше глубокого совпадения; папки с '/' тоже ранжируются."""
    (tmp_path / "agent.py").write_text("x", encoding="utf-8")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "core").mkdir()
    (tmp_path / "agents" / "core" / "runner.py").write_text(
        "x", encoding="utf-8"
    )
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

from gc_tui.services.file_index import (
    build_attachment_footer,
    extract_at_mentions,
    resolve_attachment,
)


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


def test_an_image_is_resolved_as_an_image_by_its_bytes(tmp_path):
    """Картинка из проекта через «@» — ссылка, как любой файл: агент смотрит её
    тулом по пути. Пометка «изображение» говорит модели, что файл — картинка;
    узнаём по первым байтам, как при вставке, а не по расширению."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 3)).save(buf, format="PNG")
    (tmp_path / "shot.dat").write_bytes(buf.getvalue())
    (tmp_path / "fake.png").write_text("не картинка", encoding="utf-8")

    shot = resolve_attachment(str(tmp_path), "shot.dat")
    fake = resolve_attachment(str(tmp_path), "fake.png")
    assert shot[2] == "изображение"
    assert fake[2] == "файл"
    body = build_attachment_footer([shot])
    assert f"@shot.dat → {shot[1]} (изображение)" in body


def test_build_footer():
    body = build_attachment_footer(
        [
            ("a.py", r"D:\proj\a.py", "файл"),
            ("sub/", r"D:\proj\sub", "папка"),
        ]
    )
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
    (tmp_path / ".claude" / "settings.local.json").write_text(
        "{}", encoding="utf-8"
    )
    out = fuzzy_search(str(tmp_path), ".claude/s")
    assert ".claude/settings.local.json" in out
    # и папка находится без хвостового '/'
    assert any(
        p.startswith(".claude") for p in fuzzy_search(str(tmp_path), ".cla")
    )


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


def test_rooted_query_is_outside_root_even_when_isabs_says_no(
    tmp_path, monkeypatch
):
    """Python 3.13 на Windows: isabs("/Users") стал False, а join(root, "/Users")
    по-прежнему даёт D:/Users — путь от корня диска листался бы как папка проекта.
    Тесты идут на 3.12, поэтому поведение 3.13 подменяем."""
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    monkeypatch.setattr(os.path, "isabs", lambda p: False)
    assert list_dir(str(tmp_path), "/Users") == []
    assert list_dir(str(tmp_path), "\\Users") == []
    assert list_dir(str(tmp_path), "/") == []
    assert fuzzy_search(str(tmp_path), "/Us") == []


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
    """Поиск зовут потоки сеансов пикера: поток закрытого сеанса может ещё собирать
    список, когда поток следующего уже ищет — тот ждёт на локе, а не запускает второй
    обход диска."""
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
    threads = [
        threading.Thread(target=lambda: results.append(idx.search("alp")))
        for _ in range(2)
    ]
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
    обходом, а корень запомнен как медленный — git для него больше не зовётся."""
    from gc_tui.services import file_index
    from gc_tui.services.file_index import PathIndex

    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")

    def timed_out(root, max_paths):
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(file_index, "_git_ls_files", timed_out)
    idx = PathIndex(str(tmp_path))
    assert idx.search("alp") == ["alpha.py"]
    assert str(tmp_path) in file_index._slow_git_roots


def test_git_timeout_is_remembered_by_the_module_for_a_while(
    tmp_path, monkeypatch
):
    """Память о медленном git живёт в самом модуле индекса: новый PathIndex того
    же корня git не зовёт, пока не вышел срок; после срока — пробует снова."""
    from gc_tui.services import file_index
    from gc_tui.services.file_index import PathIndex

    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    git_calls = []

    def timed_out(root, max_paths):
        git_calls.append(root)
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(file_index, "_git_ls_files", timed_out)
    assert PathIndex(str(tmp_path)).search("alp") == ["alpha.py"]
    assert PathIndex(str(tmp_path)).search("alp") == ["alpha.py"]
    assert len(git_calls) == 1
    monkeypatch.setattr(file_index, "_SLOW_GIT_RETRY_AFTER", 0.0)  # срок вышел
    assert PathIndex(str(tmp_path)).search("alp") == ["alpha.py"]
    assert len(git_calls) == 2


def test_use_git_false_never_calls_git(tmp_path, monkeypatch):
    from gc_tui.services import file_index
    from gc_tui.services.file_index import PathIndex

    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    calls = []

    def git_called(root, max_paths):
        calls.append(root)
        raise AssertionError("git called")

    monkeypatch.setattr(file_index, "_git_ls_files", git_called)
    idx = PathIndex(str(tmp_path), use_git=False)
    assert idx.search("alp") == ["alpha.py"]
    assert calls == []
    assert str(tmp_path) not in file_index._slow_git_roots


def test_git_index_finds_non_ascii_names(tmp_path, monkeypatch):
    """git ls-files квотит не-ASCII имена ("\\320\\276…"): раньше в индекс попадал
    мусор вида «"/320/276/…» и русские имена не находились вовсе."""
    from gc_tui.services import file_index
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
    assert str(tmp_path) not in file_index._slow_git_roots


def test_git_index_survives_unquoted_utf8_output(tmp_path, monkeypatch):
    """core.quotePath=false: git пишет имена сырым UTF-8. С text=True вывод
    декодировался локальной кодировкой (cp1251 на Windows) — падение или кракозябры."""
    from gc_tui.services.file_index import PathIndex

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    (tmp_path / "отчёт.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "core.quotePath", "false"], cwd=tmp_path, check=True
    )
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

        def is_symlink(self):
            if self.name == "bad":
                raise OSError("boom")
            return self._entry.is_symlink()

        def __getattr__(self, name):
            return getattr(self._entry, name)

    class _Scan:
        """Как настоящий scandir: и контекстный менеджер, и итератор.

        Path.walk и уборка каталогов pytest итерируют сам объект
        (``for entry in os.scandir(path)``), а не значение из ``__enter__``.
        """

        def __init__(self, path):
            self._it = real_scandir(path)

        def __iter__(self):
            return self

        def __next__(self):
            return _Entry(next(self._it))

        def __enter__(self):
            self._it.__enter__()
            return self

        def __exit__(self, *exc):
            return self._it.__exit__(*exc)

    # Подмена глобальная (file_index.os — модуль os). Снять её надо в теле
    # теста: при KeyboardInterrupt pytest не доходит до teardown фикстур,
    # и финал сессии падает на ещё висящей подмене scandir.
    monkeypatch.setattr(file_index.os, "scandir", _Scan)
    try:
        paths, truncated = file_index._walk_paths(str(tmp_path))
        assert sorted(paths) == ["aaa.py", "bad/", "zzz.py"]
        assert truncated is False
    finally:
        monkeypatch.undo()


def test_rank_with_limit_equals_sorted_prefix():
    paths = [
        "b/agent.py",
        "agents/",
        "a/x/agent_old.py",
        "Agent.md",
        "zz/reagent.py",
        "agents/agent.py",
        "docs/agent/",
        "AGENT.txt",
        "docs/Agent.md",
        "docs/agent.md",
    ]
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
    assert (
        names_existing_dir(root, "agents/agent.py/x") is False
    )  # файл, не папка
    assert names_existing_dir(root, "agent") is False
    assert names_existing_dir(root, "/etc/pas") is False
    assert names_existing_dir(root, "C:/Us") is False


import pytest


def test_stream_bounded_kills_whole_process_tree_on_timeout(tmp_path):
    """Команда не уложилась в таймаут, а её ДОЧЕРНИЙ процесс держит stdout (так
    ведёт себя git из PATH на Windows — обёртка над настоящим git): вызов обязан
    вернуться около таймаута, а не ждать закрытия пайпа, и потомка не оставить."""
    import sys
    import time

    from gc_tui.services import file_index

    beat = tmp_path / "beat.txt"
    # Потомок и родитель самоограничены (~20 с): если гашение дерева сломают,
    # тест честно упадёт на времени, а не повесит прогон и не оставит сирот.
    child = "import time, sys\nfor _ in range(200):\n    open(sys.argv[1], 'a').write('x')\n    time.sleep(0.1)\n"
    parent = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}, sys.argv[1]])\n"
        "time.sleep(20)\n"
    )
    started = time.perf_counter()
    with pytest.raises(subprocess.TimeoutExpired):
        file_index._stream_bounded(
            [sys.executable, "-c", parent, str(beat)],
            str(tmp_path),
            3.0,
            lambda chunk: None,
        )
    elapsed = time.perf_counter() - started
    assert elapsed < 10
    assert beat.exists()  # потомок успел родиться — есть кого гасить
    time.sleep(0.5)
    size = beat.stat().st_size
    time.sleep(0.7)
    assert beat.stat().st_size == size  # потомок мёртв
    assert file_index._running == set()


def test_parent_dir_queries_are_not_listed(tmp_path):
    """«..» выводит за корень так же, как D: и абсолютный путь: пикер не
    подсказывает то, что лежит выше проекта (внешние пути набираются руками)."""
    from gc_tui.services.file_index import names_existing_dir

    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    r = str(root)
    assert list_dir(r, "..") == []
    assert list_dir(r, "src/../..") == []
    assert fuzzy_search(r, "../out") == []
    assert fuzzy_search(r, "src/../../out") == []
    assert names_existing_dir(r, "../x") is False
    # внутри корня «..» законен: src/../src — это сам проект
    assert list_dir(r, "src/../src") == ["src/../src/a.py"]
    # имя, начинающееся с двух точек, — не выход за корень
    (root / "..cfg").write_text("x", encoding="utf-8")
    assert fuzzy_search(r, "..cf") == ["..cfg"]


def test_shallowest_paths_equals_stable_sort_prefix():
    """Отборщик даёт ровно то же, что sorted(по глубине)[:n], включая порядок и
    ничьи внутри глубины; пока вход влезает — порядок прихода сохранён."""
    from gc_tui.services.file_index import _ShallowestPaths

    paths = [
        "a/b/c/1",
        "x",
        "a/b/2",
        "a/3",
        "y",
        "a/b/c/4",
        "a/b/5",
        "z/6",
        "a/b/c/d/7",
        "w",
    ]
    for n in range(1, len(paths) + 2):
        sel = _ShallowestPaths(n)
        for p in paths:
            sel.add(p)
        expected = sorted(paths, key=lambda f: f.count("/"))[:n]
        if n >= len(paths):
            assert sel.result() == paths and sel.truncated is False
        else:
            assert sel.result() == expected and sel.truncated is True


def test_shallowest_paths_memory_is_bounded():
    """Сколько бы путей ни пришло, в памяти остаётся не больше потолка."""
    from gc_tui.services.file_index import _ShallowestPaths

    sel = _ShallowestPaths(1000)
    biggest = 0
    for i in range(200_000):
        sel.add(f"d{i % 7}/" * (i % 5) + f"f{i}.txt")
        if i % 5000 == 0:
            biggest = max(biggest, len(sel))
    assert biggest <= 1000
    assert len(sel) == 1000 and sel.truncated is True


def test_git_stream_survives_any_chunk_boundary(tmp_path, monkeypatch):
    """Граница куска посреди пути и посреди многобайтного символа пути не портит."""
    from gc_tui.services import file_index
    from gc_tui.services.file_index import PathIndex

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    (tmp_path / "доки").mkdir()
    (tmp_path / "доки" / "план.md").write_text("x", encoding="utf-8")
    (tmp_path / "отчёт.md").write_text("x", encoding="utf-8")
    (tmp_path / "ignored.log").write_text("x", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    monkeypatch.setattr(file_index, "_GIT_CHUNK", 1)
    idx = PathIndex(str(tmp_path))
    assert idx.search("отч") == ["отчёт.md"]
    assert idx.search("план") == ["доки/план.md"]
    assert str(tmp_path) not in file_index._slow_git_roots
    assert (
        idx.search("ignored") == []
    )  # обход диска нашёл бы его — значит, отработала именно git-ветка


def test_partial_git_output_is_discarded_on_timeout(tmp_path, monkeypatch):
    """Таймаут посреди потока: обрывок вывода git в индекс не попадает — индекс
    собирается обходом диска, факт таймаута виден."""
    from gc_tui.services import file_index
    from gc_tui.services.file_index import PathIndex

    (tmp_path / "real.py").write_text("x", encoding="utf-8")

    def half_then_timeout(cmd, cwd, timeout, on_chunk):
        on_chunk(b"ghost.py\0real")
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(file_index, "_stream_bounded", half_then_timeout)
    idx = PathIndex(str(tmp_path))
    assert idx.search("ghost") == []
    assert idx.search("real") == ["real.py"]
    assert str(tmp_path) in file_index._slow_git_roots


def test_stream_bounded_wait_is_bounded_even_if_tree_kill_fails(
    tmp_path, monkeypatch
):
    """Гашение дерева не удалось (taskkill запрещён/завис), дочерний процесс держит
    пайп: вызов всё равно обязан вернуться около таймаута — читает пайп служебный
    поток, а не вызывающий."""
    import sys
    import time

    from gc_tui.services import file_index

    child = "import time, sys\nfor _ in range(200):\n    time.sleep(0.1)\n"
    parent = f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {child!r}])\ntime.sleep(20)\n"
    monkeypatch.setattr(
        file_index, "_kill_process_tree", lambda proc: proc.kill()
    )
    started = time.perf_counter()
    with pytest.raises(subprocess.TimeoutExpired):
        file_index._stream_bounded(
            [sys.executable, "-c", parent],
            str(tmp_path),
            2.0,
            lambda chunk: None,
        )
    assert time.perf_counter() - started < 8
    assert file_index._running == set()


def test_stream_bounded_stops_feeding_chunks_after_timeout(
    tmp_path, monkeypatch
):
    """Гашение дерева не удалось, дочерний процесс продолжает писать: после
    таймаута его вывод уже никому не нужен — служебный поток не должен дальше
    звать on_chunk (иначе разбор вывода git идёт вхолостую до конца процесса)."""
    import sys
    import time

    from gc_tui.services import file_index

    child = "import time, sys\nfor _ in range(80):\n    sys.stdout.write('x' * 100); sys.stdout.flush(); time.sleep(0.1)\n"
    monkeypatch.setattr(file_index, "_kill_process_tree", lambda proc: None)
    chunks = []
    with pytest.raises(subprocess.TimeoutExpired):
        file_index._stream_bounded(
            [sys.executable, "-c", child], str(tmp_path), 0.5, chunks.append
        )
    seen = len(chunks)
    assert seen > 0
    time.sleep(0.6)
    assert len(chunks) == seen
    assert file_index._running == set()


def test_stream_bounded_reader_failure_propagates_and_unregisters(tmp_path):
    """Сбой разбора вывода (исключение в on_chunk) уходит вызывающему, а процесс
    снимается с учёта — реестр не копит мёртвые записи."""
    import sys
    from gc_tui.services import file_index

    def broken(chunk):
        raise ValueError("parse failed")

    with pytest.raises(ValueError):
        file_index._stream_bounded(
            [sys.executable, "-c", "print('x' * 100)"],
            str(tmp_path),
            10.0,
            broken,
        )
    assert file_index._running == set()


def test_kill_running_processes_kills_tree_of_running_command(tmp_path):
    """Выход из программы посреди команды индекса: гасится всё дерево (git из PATH на
    Windows — обёртка над настоящим git), вызов сразу возвращает False, реестр пуст.
    Флаг «выходим» после теста сбрасывает автофикстура conftest."""
    import sys
    import threading
    import time
    from gc_tui.services import file_index

    beat = tmp_path / "beat.txt"
    # Потомок и родитель самоограничены (~20 с): если гашение сломают, тест упадёт на
    # времени, а не повесит прогон и не оставит вечных сирот.
    child = "import time, sys\nfor _ in range(200):\n    open(sys.argv[1], 'a').write('x')\n    time.sleep(0.1)\n"
    parent = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}, sys.argv[1]])\n"
        "time.sleep(20)\n"
    )
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            file_index._stream_bounded(
                [sys.executable, "-c", parent, str(beat)],
                str(tmp_path),
                30.0,
                lambda chunk: None,
            )
        ),
        daemon=True,
    )
    worker.start()
    deadline = time.monotonic() + 10
    while (
        not beat.exists() and time.monotonic() < deadline
    ):  # ждём рождения потомка
        time.sleep(0.05)
    assert beat.exists()
    file_index.kill_running_processes()
    worker.join(10)
    assert not worker.is_alive()
    assert result == [False]
    assert file_index._running == set()
    time.sleep(0.5)
    size = beat.stat().st_size
    time.sleep(0.7)
    assert beat.stat().st_size == size  # потомок мёртв


def test_no_command_starts_once_exit_began(tmp_path):
    """Обычный прогон реестр за собой чистит; после начала выхода индекс новых команд
    не запускает — git, стартующий в ту же миллисекунду, не проскочит."""
    import sys
    from gc_tui.services import file_index

    assert (
        file_index._stream_bounded(
            [sys.executable, "-c", "print('x')"],
            str(tmp_path),
            10.0,
            lambda chunk: None,
        )
        is True
    )
    assert file_index._running == set()
    marker = tmp_path / "started.txt"
    file_index.kill_running_processes()
    assert (
        file_index._stream_bounded(
            [
                sys.executable,
                "-c",
                "import sys; open(sys.argv[1], 'w').close()",
                str(marker),
            ],
            str(tmp_path),
            10.0,
            lambda chunk: None,
        )
        is False
    )
    assert not marker.exists()


def test_index_is_not_built_once_exit_began(tmp_path):
    """После начала выхода индекс не обходит диск — ни вместо git, которого уже не
    запустят, ни сразу (проект не под git): в умирающем процессе список никому не нужен,
    а процессор нужен остальной уборке при выходе."""
    from gc_tui.services import file_index

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x", encoding="utf-8")
    file_index.kill_running_processes()
    assert file_index._all_paths(str(tmp_path)) == ([], True)
    assert file_index._all_paths(str(tmp_path), use_git=False) == ([], True)


def test_walk_already_running_stops_once_exit_began(tmp_path, monkeypatch):
    """Выход начался посреди обхода (проект не под git или git недавно не уложился —
    сборка сразу идёт по диску): обход останавливается на следующей папке, а не
    дочитывает проект в умирающем процессе. Собранное — с пометкой «неполный»."""
    from gc_tui.services import file_index

    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "deep.py").write_text("x", encoding="utf-8")
    (tmp_path / "top.py").write_text("x", encoding="utf-8")
    real_scandir = file_index.os.scandir

    def scandir_then_exit(path):
        it = real_scandir(path)
        file_index.kill_running_processes()  # выход начался, пока читали первую папку
        return it

    monkeypatch.setattr(file_index.os, "scandir", scandir_then_exit)
    paths, truncated = file_index._walk_paths(str(tmp_path))
    assert sorted(paths) == ["a/", "top.py"]
    assert truncated is True


def test_shallowest_paths_equals_stable_sort_prefix_on_random_inputs():
    """Свойство отборщика на случайных входах (та же логика, что у fixed-example
    теста выше, только вход случайный): пока путей не больше потолка — порядок
    прихода сохранён; при превышении — ровно sorted(по глубине)[:n]. Включает
    n=0 (потолок <= 0) и пустой вход."""
    import random

    from gc_tui.services.file_index import _ShallowestPaths

    rnd = random.Random(20260921)
    for _ in range(2000):
        count = rnd.randint(0, 40)
        paths = []
        for k in range(count):
            depth = rnd.randint(0, 5)
            paths.append("/".join(["d"] * depth + [f"f{k}"]))
        n = rnd.randint(0, 45)
        sel = _ShallowestPaths(n)
        for p in paths:
            sel.add(p)
        if count <= n:
            assert sel.result() == paths and sel.truncated is False
        else:
            assert (
                sel.result() == sorted(paths, key=lambda f: f.count("/"))[:n]
            )
            assert sel.truncated is True


def test_walk_respects_simple_gitignore_dir_names(tmp_path):
    """Обход диска (запасной путь без git) не тащит в индекс то, что проект сам
    объявил мусором; листинг папки руками при этом не меняется."""
    from gc_tui.services.file_index import PathIndex

    (tmp_path / ".gitignore").write_text(
        "target/\nout\n/gen\n", encoding="utf-8"
    )
    for d in ("target", "out", "gen", "src/target", "src/gen", "src/keep"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "inner.py").write_text("x", encoding="utf-8")
    idx = PathIndex(str(tmp_path), use_git=False)
    assert idx.search("inner") == ["src/gen/inner.py", "src/keep/inner.py"]
    assert list_dir(str(tmp_path), "target") == ["target/inner.py"]


def test_gitignore_patterns_we_do_not_understand_are_skipped(tmp_path):
    """Шаблоны, пути с '/' в середине и экранирование — не наш случай: при
    сомнении показываем."""
    from gc_tui.services.file_index import PathIndex

    (tmp_path / ".gitignore").write_text(
        "*.log\nfoo*/\ndocs/tmp/\nba\\r/\n", encoding="utf-8"
    )
    for d in ("foo1", "docs/tmp", "bar"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "inner.py").write_text("x", encoding="utf-8")
    idx = PathIndex(str(tmp_path), use_git=False)
    assert idx.search("inner") == [
        "bar/inner.py",
        "foo1/inner.py",
        "docs/tmp/inner.py",
    ]


def test_gitignore_negation_keeps_the_dir(tmp_path):
    from gc_tui.services.file_index import PathIndex

    (tmp_path / ".gitignore").write_text(
        "out/\n!out/\ngen/\n!ge*\njunk/\n", encoding="utf-8"
    )
    for d in ("out", "gen", "junk"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "inner.py").write_text("x", encoding="utf-8")
    idx = PathIndex(str(tmp_path), use_git=False)
    assert idx.search("inner") == ["gen/inner.py", "out/inner.py"]


def test_gitignore_with_bom_crlf_and_comments(tmp_path):
    from gc_tui.services.file_index import PathIndex

    (tmp_path / ".gitignore").write_bytes(
        "\ufeff# мусор\r\n\r\ntarget/  \r\n".encode("utf-8")
    )
    for d in ("target", "src"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "inner.py").write_text("x", encoding="utf-8")
    assert PathIndex(str(tmp_path), use_git=False).search("inner") == [
        "src/inner.py"
    ]


def test_gitignore_name_hides_dirs_not_files(tmp_path):
    from gc_tui.services.file_index import PathIndex

    (tmp_path / ".gitignore").write_text("outfile\n", encoding="utf-8")
    (tmp_path / "outfile").write_text("x", encoding="utf-8")
    assert PathIndex(str(tmp_path), use_git=False).search("outfi") == [
        "outfile"
    ]


def test_git_timeout_fallback_respects_gitignore(tmp_path, monkeypatch):
    """Главный случай: git не уложился в таймаут — обход не должен отличаться от
    git тем, что проект объявил мусором."""
    from gc_tui.services import file_index
    from gc_tui.services.file_index import PathIndex

    (tmp_path / ".gitignore").write_text("junk/\n", encoding="utf-8")
    for d in ("junk", "src"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "inner.py").write_text("x", encoding="utf-8")

    def timed_out(root, max_paths):
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(file_index, "_git_ls_files", timed_out)
    idx = PathIndex(str(tmp_path))
    assert idx.search("inner") == ["src/inner.py"]
    assert str(tmp_path) in file_index._slow_git_roots


def test_gitignore_negation_with_path_keeps_every_dir_of_that_name(tmp_path):
    """`lib/` + `!src/lib/` — git показывает src/lib, значит и обход не прячет.
    Отрицание сверяем по последнему сегменту и без учёта регистра (на Windows и
    macOS git так и делает): лишнее отрицание безвредно, пропущенное прячет код."""
    from gc_tui.services.file_index import PathIndex

    (tmp_path / ".gitignore").write_text(
        "lib/\n!src/lib/\nout/\n!OUT/\njunk/\n", encoding="utf-8"
    )
    for d in ("lib", "src/lib", "out", "junk"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "inner.py").write_text("x", encoding="utf-8")
    idx = PathIndex(str(tmp_path), use_git=False)
    assert idx.search("inner") == [
        "lib/inner.py",
        "out/inner.py",
        "src/lib/inner.py",
    ]


def test_gitignore_lines_with_odd_whitespace_are_skipped(tmp_path):
    """git снимает только хвостовые пробелы: ведущий пробел и таб — часть
    шаблона, а \\x0c внутри имени — не конец строки. Сомнение — показываем."""
    from gc_tui.services.file_index import PathIndex

    (tmp_path / ".gitignore").write_bytes(
        b"  target/\nout\t\nfoo\x0cbar/\ngen\r\r\njunk/   \n"
    )
    for d in ("target", "out", "foo", "bar", "gen", "junk"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "inner.py").write_text("x", encoding="utf-8")
    idx = PathIndex(str(tmp_path), use_git=False)
    assert idx.search("inner") == [
        "bar/inner.py",
        "foo/inner.py",
        "gen/inner.py",
        "out/inner.py",
        "target/inner.py",
    ]


def test_gitignore_negation_in_dialect_we_do_not_speak_lifts_all_ignores(
    tmp_path,
):
    """`[^x]` и экранирование git понимает, fnmatch — нет: чем гадать, какую папку
    вернуло такое отрицание, не прячем ничего."""
    from gc_tui.services.file_index import PathIndex

    (tmp_path / ".gitignore").write_text(
        "lib/\n!li[^x]/\njunk/\n", encoding="utf-8"
    )
    for d in ("lib", "junk"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "inner.py").write_text("x", encoding="utf-8")
    idx = PathIndex(str(tmp_path), use_git=False)
    assert idx.search("inner") == ["junk/inner.py", "lib/inner.py"]


def test_oversized_gitignore_is_not_trusted(tmp_path, monkeypatch):
    """Файл не влез в лимит чтения: обрезанная строка стала бы другим именем, а
    отрицание за границей потерялось бы — не верим такому файлу вовсе."""
    from gc_tui.services import file_index
    from gc_tui.services.file_index import PathIndex

    monkeypatch.setattr(file_index, "_GITIGNORE_MAX_BYTES", 16)
    for d in ("junk", "src"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "inner.py").write_text("x", encoding="utf-8")
    (tmp_path / ".gitignore").write_bytes(
        b"junk/\n" + b"#" * 10
    )  # ровно лимит — читаем
    assert PathIndex(str(tmp_path), use_git=False).search("inner") == [
        "src/inner.py"
    ]
    (tmp_path / ".gitignore").write_bytes(
        b"junk/\n" + b"#" * 11
    )  # на байт больше — нет
    assert PathIndex(str(tmp_path), use_git=False).search("inner") == [
        "junk/inner.py",
        "src/inner.py",
    ]


def test_ignored_dirs_do_not_consume_the_fuse(tmp_path):
    from gc_tui.services.file_index import _walk_paths

    # Игнорируемые — по обе стороны от src: порядок выдачи диска не важен.
    for d in ("a_junk", "src", "z_junk"):
        (tmp_path / d).mkdir()
    paths, truncated = _walk_paths(
        str(tmp_path), 1, ignore_anywhere=frozenset({"a_junk", "z_junk"})
    )
    assert (paths, truncated) == (["src/"], False)


def test_gitignore_is_not_read_when_git_answered(tmp_path, monkeypatch):
    """У git своя, полная семантика игнора — упрощённый разбор ему не нужен."""
    from gc_tui.services import file_index
    from gc_tui.services.file_index import PathIndex

    calls = []
    monkeypatch.setattr(
        file_index,
        "_git_ls_files",
        lambda root, max_paths: (["src/inner.py"], False),
    )
    monkeypatch.setattr(
        file_index,
        "_simple_gitignore_dirs",
        lambda root: calls.append(root) or (frozenset(), frozenset()),
    )
    assert PathIndex(str(tmp_path)).search("inner") == ["src/inner.py"]
    assert calls == []
