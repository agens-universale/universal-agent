import os
import json
import shutil
import functools
from typing import Dict, Any
from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool


class SearchAndReplace(AbstractSimpleTool):
    """
    Tool: search_and_replace
    Performs a single search and replace operation in a file.
    Only the first occurrence is replaced.
    """

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "search_and_replace",
            "description": (
                "Replaces the first occurrence of exact text in a file. Only one replacement per call. "
                "Uses literal string matching (case-sensitive, no regex). "
                "Always returns a JSON string with fields: ok, matched, replaced, path, message, error. "
                "ok=true only when the file was modified."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The absolute path of the file to modify. Must be a valid file path; directories are not allowed."
                    },
                    "search": {
                        "type": "string",
                        "description": "The exact content to find and replace. Match is case-sensitive and literal (not regex). May be multiline. Cannot be empty."
                    },
                    "replace": {
                        "type": "string",
                        "description": "The new content to replace the found content with. May be multiline. Can be empty to remove the matched content."
                    }
                },
                "required": ["path", "search", "replace"],
                "additionalProperties": False
            },
            "strict": True
        }

    @classmethod
    def prompt_description(cls) -> str:
        description = '''Tool name: search_and_replace  
Tool description: Replaces the **first occurrence** of exact text in a file. Only one replacement per call.
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "The absolute path of the file to modify."}, "search": {"type": "string", "description": "The exact content to find and replace.  May be multiline."}, "replace": {"type": "string", "description": "The new content to replace the found content with.  May be multiline."}}, "required": ["path", "search", "replace"], "additionalProperties": false}

Usage:
- Modifies a **single file** by replacing the **first occurrence** of `search` string with `replace`.
- The `search` string must match **exactly** (case-sensitive).
- Only the **first match** is replaced — subsequent matches are left unchanged.
- File is rewritten safely (write + replace) to prevent data loss.
- Uses UTF-8 encoding with `errors='ignore'` for robustness.

Important Usage Rules
- **ALWAYS** use absolute paths for `path`.
- The `search` string is treated as **literal text**, not regex.
- To perform regex replacements, use `search_files` first to locate, then decide on literal replacement.
- If the file does not contain the `search` string, no changes are made.
- If the file is empty or unreadable, an error is raised.
- This tool ALWAYS returns a JSON string with fields:
  - ok: boolean — overall success (true means file was modified).
  - matched: boolean — whether `search` was found in the file.
  - replaced: integer — number of replacements applied (0 or 1).
  - path: string — the file path.
  - message: string — human-readable summary.
  - error: object|null — error details when ok=false due to an error (not "not found").
- **NEVER** use shell commands like `sed`, `perl -pi`, etc. — this tool is safe and cross-platform.
- For multiline replacements use line feed characters (e.g. \\n, \\r\\n depending on the platform)  in literal text for arguments `search` and `replace`

Examples
Replace database URL in config:
{
  "path": "/home/user/app/config.json",
  "search": "localhost:5432",
  "replace": "db.prod.internal:5432"
}

Fix a typo in source code:
{
  "path": "/home/user/project/main.py",
  "search": "prin('Hello')",
  "replace": "print('Hello')"
}

Update version string:
{
  "path": "/home/user/project/README.md",
  "search": "Version\\n1.0.0",
  "replace": "Version\\n1.0.1"
}
'''
        return description

    @staticmethod
    def _result(
        *,
        path: str,
        ok: bool,
        matched: bool,
        replaced: int,
        message: str,
        error: Dict[str, Any] | None = None,
    ) -> str:
        return json.dumps(
            {
                "tool": "search_and_replace",
                "ok": ok,
                "matched": matched,
                "replaced": replaced,
                "path": path,
                "message": message,
                "error": error,
            },
            ensure_ascii=False,
        )

    def __call__(self, path: str, search: str, replace: str, **kwargs) -> str:
        _result = functools.partial(self._result, path=path)

        # Input validation
        if not isinstance(path, str):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Argument 'path' must be a string.",
                error={"type": "invalid_argument", "field": "path"},
            )
        if not isinstance(search, str):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Argument 'search' must be a string.",
                error={"type": "invalid_argument", "field": "search"},
            )
        if not isinstance(replace, str):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Argument 'replace' must be a string.",
                error={"type": "invalid_argument", "field": "replace"},
            )

        if not path:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Argument 'path' cannot be an empty string.",
                error={"type": "invalid_argument", "field": "path"},
            )
        if not os.path.exists(path):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="File not found.",
                error={"type": "not_found"},
            )
        if not os.path.isfile(path):
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Path is not a file.",
                error={"type": "not_a_file"},
            )

        if search == '':
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Argument 'search' cannot be an empty string.",
                error={"type": "invalid_argument", "field": "search"},
            )

        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except Exception as e:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Failed to read file.",
                error={"type": "read_failed", "message": str(e)},
            )

        # Check if search string exists
        if search not in content:
            return _result(
                ok=False,
                matched=False,
                replaced=0,
                message="Search string not found in file. No changes made.",
                error=None,
            )

        # Perform replacement (only first occurrence)
        new_content = content.replace(search, replace, 1)

        # Write back only if changed
        if new_content == content:
            return _result(
                ok=False,
                matched=True,
                replaced=0,
                message="No change needed (content identical after replacement).",
                error=None,
            )

        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, 'w', encoding='utf-8', errors='replace') as f:
                f.write(new_content)
            os.replace(tmp_path, path)
        except Exception as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return _result(
                ok=False,
                matched=True,
                replaced=0,
                message="Failed to write file.",
                error={"type": "write_failed", "message": str(e)},
            )

        return _result(
            ok=True,
            matched=True,
            replaced=1,
            message="Text replaced successfully.",
            error=None,
        )


def test_search_and_replace_basic():
    test_file = "./test_replace.txt"

    # Создаём тестовый файл
    with open(test_file, 'w', encoding='utf-8') as f:
        f.write("Hello world!\nThis is a test.\nGoodbye everyone!")

    tool = SearchAndReplace()

    # Тест 1: успешная замена
    result = tool(path=test_file, search="world", replace="everyone")
    assert result["success"] is True
    assert result["file"] == test_file
    assert result["replaced"]["from"] == "world"
    assert result["replaced"]["to"] == "everyone"

    with open(test_file, 'r', encoding='utf-8') as f:
        content = f.read()
    assert content == "Hello everyone!\nThis is a test.\nGoodbye everyone!"

    # Тест 2: вторая замена (уже не найдёт первое вхождение)
    result2 = tool(path=test_file, search="world", replace="Mars")
    assert result2["success"] is False
    assert "not found" in result2["message"]

    # Проверим, что содержимое не изменилось
    with open(test_file, 'r', encoding='utf-8') as f:
        content_after = f.read()
    assert content == content_after

    os.remove(test_file)
    print("test_search_and_replace_basic passed.")


def test_search_and_replace_exact_match():
    test_file = "./test_exact.txt"
    with open(test_file, 'w', encoding='utf-8') as f:
        f.write("CaseSensitive\ncasesensitive\nCASESENSITIVE")

    tool = SearchAndReplace()

    # Тест: точное совпадение (регистрозависимо)
    result = tool(path=test_file, search="CaseSensitive", replace="Modified")
    assert result["success"] is True

    with open(test_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    assert lines[0].strip() == "Modified"
    assert lines[1].strip() == "casesensitive"
    assert lines[2].strip() == "CASESENSITIVE"

    os.remove(test_file)
    print("test_search_and_replace_exact_match passed.")


def test_search_and_replace_empty_strings():
    test_file = "./test_empty.txt"
    with open(test_file, 'w', encoding='utf-8') as f:
        f.write("data inside")

    tool = SearchAndReplace()

    # Пустой search — недопустим
    try:
        tool(path=test_file, search="", replace="filled")
        assert False, "Expected ValueError for empty search"
    except ValueError as e:
        assert "empty string" in str(e) or "cannot be" in str(e)

    # Пустой replace — разрешён
    result = tool(path=test_file, search="data", replace="")
    assert result["success"] is True

    with open(test_file, 'r', encoding='utf-8') as f:
        content = f.read()
    assert content == " inside"

    os.remove(test_file)
    print("test_search_and_replace_empty_strings passed.")


def test_search_and_replace_file_not_found():
    tool = SearchAndReplace()

    try:
        tool(path="./nonexistent.file", search="test", replace="ok")
        assert False, "Expected FileNotFoundError"
    except FileNotFoundError:
        pass  # OK

    try:
        tool(path="", search="test", replace="ok")
        assert False, "Expected ValueError for empty path"
    except ValueError as e:
        assert "empty" in str(e).lower()

    print("test_search_and_replace_file_not_found passed.")


def test_search_and_replace_directory_given():
    # Создаём директорию
    dir_path = "./test_dir_as_file"
    os.makedirs(dir_path, exist_ok=True)

    tool = SearchAndReplace()

    try:
        tool(path=dir_path, search="test", replace="ok")
        assert False, "Expected IsADirectoryError"
    except IsADirectoryError:
        pass  # OK

    shutil.rmtree(dir_path)
    print("test_search_and_replace_directory_given passed.")


def test_search_and_replace_unicode_and_special_chars():
    test_file = "./test_unicode.txt"
    unicode_text = "Привет, мир! 🌍\nLog: Error at λ=42\nPrice: $9.99"

    with open(test_file, 'w', encoding='utf-8') as f:
        f.write(unicode_text)

    tool = SearchAndReplace()

    # Замена с эмодзи и кириллицей
    result = tool(path=test_file, search="мир!", replace="другу!")
    assert result["success"] is True

    with open(test_file, 'r', encoding='utf-8') as f:
        content = f.read()
    assert "Привет, другу!" in content

    # Замена спецсимволов
    result2 = tool(path=test_file, search="$9.99", replace="FREE")
    assert result2["success"] is True

    with open(test_file, 'r', encoding='utf-8') as f:
        final = f.read()
    assert "Price: FREE" in final

    os.remove(test_file)
    print("test_search_and_replace_unicode_and_special_chars passed.")


def test_search_and_replace_no_modification_needed():
    test_file = "./test_no_change.txt"
    with open(test_file, 'w', encoding='utf-8') as f:
        f.write("abc def abc")

    tool = SearchAndReplace()

    # Заменяем на то же самое
    result = tool(path=test_file, search="def", replace="def")
    assert result["success"] is False
    assert "identical" in result["message"]

    # Проверяем, что файл не изменён
    with open(test_file, 'r', encoding='utf-8') as f:
        content = f.read()
    assert content == "abc def abc"

    os.remove(test_file)
    print("test_search_and_replace_no_modification_needed passed.")


def test_search_and_replace_permission_error():
    test_file = "./test_protected.txt"
    try:
        with open(test_file, 'w', encoding='utf-8') as f:
            f.write("protected")

        # Попробуем сделать файл только для чтения
        os.chmod(test_file, 0o444)  # Только чтение

        tool = SearchAndReplace()
        try:
            tool(path=test_file, search="protected", replace="unlocked")
            assert False, "Expected PermissionError"
        except (PermissionError, IOError):
            pass  # OK
        finally:
            # Восстанавливаем права для удаления
            os.chmod(test_file, 0o644)

    finally:
        if os.path.exists(test_file):
            os.remove(test_file)

    print("test_search_and_replace_permission_error passed.")


def test_search_and_replace_newline_in_search():
    test_file = "./test_multiline.txt"
    with open(test_file, 'w', encoding='utf-8') as f:
        f.write("Line 1\nLine 2\nLine 3")

    tool = SearchAndReplace()

    # Поиск с символами новой строки
    result = tool(path=test_file, search="Line 1\nLine 2", replace="Single line")
    assert result["success"] is True

    with open(test_file, 'r', encoding='utf-8') as f:
        content = f.read()
    assert content == "Single line\nLine 3"

    os.remove(test_file)
    print("test_search_and_replace_newline_in_search passed.")


def test_search_and_replace_long_string():
    test_file = "./test_long.txt"
    long_content = "A" * 10000 + "TARGET" + "B" * 10000
    with open(test_file, 'w', encoding='utf-8') as f:
        f.write(long_content)

    tool = SearchAndReplace()
    result = tool(path=test_file, search="TARGET", replace="REPLACED")
    assert result["success"] is True

    with open(test_file, 'r', encoding='utf-8') as f:
        content = f.read()
    assert "REPLACED" in content
    assert len(content) == len(long_content) - len("TARGET") + len("REPLACED")

    os.remove(test_file)
    print("test_search_and_replace_long_string passed.")


def run_tests():
    try:
        test_search_and_replace_basic()
        test_search_and_replace_exact_match()
        test_search_and_replace_empty_strings()
        test_search_and_replace_file_not_found()
        test_search_and_replace_directory_given()
        test_search_and_replace_unicode_and_special_chars()
        test_search_and_replace_no_modification_needed()
        test_search_and_replace_permission_error()
        test_search_and_replace_newline_in_search()
        test_search_and_replace_long_string()

        print("\nAll tests passed successfully.")
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        raise
    finally:
        if True:
            # Очистка
            for fname in [
                "./test_replace.txt",
                "./test_exact.txt",
                "./test_empty.txt",
                "./test_unicode.txt",
                "./test_no_change.txt",
                "./test_protected.txt",
                "./test_multiline.txt",
                "./test_long.txt"
            ]:
                if os.path.exists(fname):
                    os.remove(fname)


if __name__ == "__main__":
    # Запуск всех тестов
    run_tests()
