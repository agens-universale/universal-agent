"""Поведение glob_to_regex/compile_glob (перенесено из модуля, где жило как самопроверка)."""

from gc_core.utils.glob_to_regex import compile_glob


def test_single_star():
    pattern = "*.py"
    regex = compile_glob(pattern)
    assert regex.match("hello.py") is not None
    assert regex.match("lib/hello.py") is None
    assert regex.match("hello.txt") is None

    pattern = "src/*.py"
    regex = compile_glob(pattern)
    assert regex.match("src/hello.py") is not None
    assert regex.match("src/sub/hello.py") is None
    assert regex.match("hello.py") is None


def test_double_star_recursive():
    pattern = "**/*.py"
    regex = compile_glob(pattern)
    assert regex.match("hello.py") is not None
    assert regex.match("src/hello.py") is not None
    assert regex.match("src/sub/world.py") is not None
    assert regex.match("src/sub/") is None
    assert regex.match("data.txt") is None


def test_double_star_prefix():
    pattern = "**/src/*.py"
    regex = compile_glob(pattern)
    assert regex.match("src/hello.py") is not None
    assert regex.match("a/src/hello.py") is not None
    assert regex.match("a/b/src/hello.py") is not None
    assert regex.match("a/b/c/hello.py") is None
    assert regex.match("src/") is None


def test_double_star_suffix():
    pattern = "src/**"
    regex = compile_glob(pattern)
    # print(pattern, regex)
    assert regex.match("src/") is not None
    assert regex.match("src/hello.py") is not None
    assert regex.match("src/sub/world.py") is not None
    assert regex.match("other/src/sub/") is None


def test_question_mark():
    pattern = "file?.txt"
    regex = compile_glob(pattern)
    assert regex.match("file1.txt") is not None
    assert regex.match("file.txt") is None
    assert regex.match("file12.txt") is None
    assert regex.match("fileA.txt") is not None


def test_char_class():
    pattern = "file[0-9].txt"
    regex = compile_glob(pattern)
    # print(pattern, regex)
    assert regex.match("file1.txt") is not None
    assert regex.match("file9.txt") is not None
    assert regex.match("fileA.txt") is None

    pattern = "file[!0-9].txt"
    regex = compile_glob(pattern)
    # print(pattern, regex)
    assert regex.match("fileA.txt") is not None
    assert regex.match("file1.txt") is None


def test_exact_dot_files():
    pattern = ".*"
    regex = compile_glob(pattern)
    assert regex.match(".gitignore") is not None
    assert regex.match(".config") is not None
    assert regex.match("normal.txt") is None

    pattern = ".config"
    regex = compile_glob(pattern)
    assert regex.match(".config") is not None
    assert regex.match("config") is None


def test_literal_matching():
    pattern = "data.log"
    regex = compile_glob(pattern)
    assert regex.match("data.log") is not None
    assert regex.match("dataXlog") is None
    assert regex.match("datalog") is None


def test_complex_patterns():
    pattern = "src/**/*.py"
    regex = compile_glob(pattern)
    assert regex.match("src/hello.py") is not None
    assert regex.match("src/lib/util.py") is not None
    assert regex.match("src/") is None
    assert regex.match("tests/src/util.py") is None

    pattern = "src/**/util.py"
    regex = compile_glob(pattern)
    assert regex.match("src/util.py") is not None
    assert regex.match("src/a/b/util.py") is not None
    assert regex.match("src/x/util.py") is not None
    assert regex.match("util.py") is None


def test_no_special_chars():
    pattern = "hello.txt"
    regex = compile_glob(pattern)
    assert regex.match("hello.txt") is not None
    assert regex.match("helloXtxt") is None


def test_compile_glob():
    # 1. Простое совпадение файла в подкаталоге
    pattern = "src/**/*.py"
    regex = compile_glob(pattern)
    assert regex.match("src/hello.py") is not None
    assert regex.match("src/lib/util.py") is not None
    assert regex.match("src/") is None
    assert regex.match("tests/src/util.py") is None

    # 2. Рекурсивный поиск по имени файла
    pattern = "**/util.py"
    regex = compile_glob(pattern)
    assert regex.match("util.py") is not None
    assert regex.match("src/util.py") is not None
    assert regex.match("src/sub/util.py") is not None
    assert regex.match("src/sub.txt") is None

    # 3. Только файлы .log в любой директории
    pattern = "**/*.log"
    regex = compile_glob(pattern)
    assert regex.match("app.log") is not None
    assert regex.match("logs/app.log") is not None
    assert regex.match("logs/archive/debug.log") is not None
    assert regex.match("logs.txt") is None

    # 4. Конкретный путь с рекурсивным спуском
    pattern = "data/**/processed/*.csv"
    regex = compile_glob(pattern)
    assert regex.match("data/processed/file.csv") is not None
    assert regex.match("data/year/month/processed/file.csv") is not None
    assert (
        regex.match("data/processed/sub/file.csv") is None
    )  # нет /processed/ в конце пути?
    assert regex.match("other/data/processed/file.csv") is None

    # 5. Одиночная звезда — только один уровень
    pattern = "projects/*/main.py"
    regex = compile_glob(pattern)
    assert regex.match("projects/web/main.py") is not None
    assert regex.match("projects/mobile/main.py") is not None
    assert regex.match("projects/web/tests/main.py") is None
    assert regex.match("projects/main.py") is None

    # 6. Вложенный одиночный уровень
    pattern = "a/*/b.txt"
    regex = compile_glob(pattern)
    assert regex.match("a/x/b.txt") is not None
    assert regex.match("a/123/b.txt") is not None
    assert regex.match("a/x/y/b.txt") is None
    assert regex.match("a/b.txt") is None

    # 7. Поиск скрытых файлов
    pattern = ".*/config"
    regex = compile_glob(pattern)
    assert regex.match(".git/config") is not None
    assert regex.match(".vscode/settings.json") is None
    assert regex.match("config") is None

    # 8. Точное совпадение с точкой
    pattern = "version.txt"
    regex = compile_glob(pattern)
    assert regex.match("version.txt") is not None
    assert regex.match("version.TXT") is None
    assert regex.match("version.text") is None
    assert regex.match("src/version.txt") is None

    # 9. Использование ?
    pattern = "script?.sh"
    regex = compile_glob(pattern)
    assert regex.match("script1.sh") is not None
    assert regex.match("scriptA.sh") is not None
    assert regex.match("script.sh") is None
    assert regex.match("script12.sh") is None

    # 10. Несколько ? подряд
    pattern = "file??.dat"
    regex = compile_glob(pattern)
    assert regex.match("file01.dat") is not None
    assert regex.match("fileAB.dat") is not None
    assert regex.match("file1.dat") is None
    assert regex.match("file123.dat") is None

    # 11. Класс символов [0-9]
    pattern = "backup-[0-9].tar"
    regex = compile_glob(pattern)
    assert regex.match("backup-5.tar") is not None
    assert regex.match("backup-0.tar") is not None
    assert regex.match("backup-A.tar") is None
    assert regex.match("backup-10.tar") is None

    # 12. Класс символов [a-z]
    pattern = "[a-z]/temp.log"
    regex = compile_glob(pattern)
    assert regex.match("x/temp.log") is not None
    assert regex.match("a/temp.log") is not None
    assert regex.match("A/temp.log") is None
    assert regex.match("1/temp.log") is None

    # 13. Отрицание в классе [!...]
    pattern = "log[!0-9].txt"
    regex = compile_glob(pattern)
    assert regex.match("logA.txt") is not None
    assert regex.match("log_.txt") is not None
    assert regex.match("log1.txt") is None
    assert (
        regex.match("log.txt") is None
    )  # потому что после log должен быть один символ

    # 14. Многосимвольный класс
    pattern = "[abc][0-9]/data"
    regex = compile_glob(pattern)
    assert regex.match("a5/data") is not None
    assert regex.match("b1/data") is not None
    assert regex.match("d5/data") is None
    assert regex.match("aX/data") is None

    # 15. Директория и всё внутри (с слешем)
    pattern = "dist/**"
    regex = compile_glob(pattern)
    # print(pattern, regex)
    assert regex.match("dist") is not None
    assert regex.match("dist/") is not None
    assert regex.match("dist/app.js") is not None
    assert regex.match("dist/js/main.js") is not None
    assert regex.match("build/dist/app.js") is None

    # 16. Рекурсивный поиск по расширению вне корня
    pattern = "src/**/*test*.py"
    regex = compile_glob(pattern)
    # print(pattern, regex)
    assert regex.match("src/test_utils.py") is not None
    assert regex.match("src/unit_test.py") is not None
    assert regex.match("src/tests/helpers/test_db.py") is not None
    assert regex.match("src/utils.py") is None

    # 17. Глоб на полное дерево с фиксированным суффиксом
    pattern = "**/output/*.json"
    regex = compile_glob(pattern)
    assert regex.match("output/results.json") is not None
    assert regex.match("data/output/info.json") is not None
    assert (
        regex.match("data/output/sub/info.json") is None
    )  # sub не допускается после output/
    assert regex.match("result.json") is None

    # 18. Составной путь с несколькими **
    pattern = "a/**/b/**/c.txt"
    regex = compile_glob(pattern)
    assert regex.match("a/b/c.txt") is not None
    assert regex.match("a/x/b/y/c.txt") is not None
    assert regex.match("a/b/y/c.txt") is not None
    assert regex.match("a/x/c.txt") is None
    assert regex.match("b/a/c.txt") is None

    # 19. Точка в имени файла — должна экранироваться
    pattern = "config.*"
    regex = compile_glob(pattern)
    # print(pattern, regex)
    assert regex.match("config.ini") is not None
    assert (
        regex.match("config.local.json") is not None
    )  # если хотим несколько точек — нужно **
    assert regex.match("config") is None
    assert regex.match("config.") is not None

    # 20. Полный путь с экранированием спецсимволов
    pattern = "lib/[a-zA-Z]_mod/*.so"
    regex = compile_glob(pattern)
    assert regex.match("lib/A_mod/math.so") is not None
    assert regex.match("lib/z_mod/io.so") is not None
    assert regex.match("lib/1_mod/base.so") is None
    assert regex.match("lib/A.mod/math.so") is None  # точка вместо _
    assert regex.match("lib/A_mod/helper.c") is None
