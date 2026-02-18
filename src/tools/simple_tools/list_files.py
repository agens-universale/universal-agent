import json
import os
import shutil
import stat
from typing import List, Dict, Any
from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool


class ListFiles(AbstractSimpleTool):
    """
    Tool to list files and directories in a given path.
    Supports both flat and recursive listing.
    """

    @classmethod
    def tool_description(cls):
        return {
            "type": "function",
            "name": "list_files",
            "description": "Lists files and directories within a specified absolute path. Supports both flat listing (default) and recursive traversal. Includes metadata such as type ('file' or 'directory') and whether the item is hidden.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The absolute path of the directory to list contents from. Must be a valid existing directory."
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "If true, performs a recursive traversal of all subdirectories. If false or omitted, only lists immediate children"
                    }
                },
                "required": ["path"],
                "additionalProperties": False
            },
            "strict": True
        }

    @classmethod
    def prompt_description(cls):
        description = '''Tool name: list_files  
Tool description: Lists all files and directories in the specified directory. Can perform recursive traversal if requested.
Input schema: {"type": "object", "properties": {"path": {"type": "string", "description": "The directory absolute path to list contents for."}, "recursive": {"type": "boolean", "description": "If true, lists contents recursively."}}, "required": ["path"], "additionalProperties": false}

Usage:
- Use this tool to explore directory structure.
- Always provide an **absolute path**.
- When `recursive=True`, it returns full tree including subdirectories. Default `recursive=False`. 
- Returns a list of entries with:
  - `path`: full path of file/directory
  - `type`: 'file' or 'directory'
  - `is_hidden`: boolean indicating if file is hidden (e.g., starts with '.' on Unix or marked hidden on Windows)

Important Rules:
- **NEVER** use external commands like `ls`, `dir`, `find`, etc. — this tool is safe and cross-platform.
- The `path` must be a valid directory.
- If the directory does not exist or is inaccessible, an error will be raised.
- Hidden files (like `.gitignore`) are included regardless of platform.
- On Windows, uses file attributes to detect hidden files.

Examples:
List top-level contents:
{
  "path": "/home/user/project"
}

Recursively list all files:
{
  "path": "/home/user/project",
  "recursive": true
}
'''
        return description

    def __call__(self, path: str, recursive: bool = False, **kwargs) -> str:
        # Input validation
        if not isinstance(path, str):
            return f"Error: Argument 'path' must be a string. Got: {type(path)} ({path})"
        if not isinstance(recursive, bool) and recursive is not None:
            return f"Error: Argument 'recursive' must be a boolean or omitted. Got: {type(recursive)} ({recursive})"

        path = os.path.normpath(path)

        if not os.path.exists(path):
            return f"Error: Path not found: {path}"
        if not os.path.isdir(path):
            return f"Error: Path is not a directory: {path}"

        result = []
        try:
            if recursive:
                self._walk_directory(path, result)
            else:
                self._list_directory(path, result)
        except PermissionError as e:
            return f"Error: Permission denied while accessing '{path}': {str(e)}"
        except Exception as e:
            raise RuntimeError(f"Unexpected error while listing files in '{path}': {str(e)}")

        return json.dumps(result, ensure_ascii=False)

    def _is_hidden_unix(self, name: str) -> bool:
        """Check if file is hidden on Unix-like systems (starts with .)."""
        return name.startswith('.')

    def _is_hidden_windows(self, filepath: str) -> bool:
        """Check if file is marked as hidden on Windows via file attributes."""
        try:
            attrs = os.stat(filepath).st_file_attributes  # Windows-specific
            return bool(attrs & stat.FILE_ATTRIBUTE_HIDDEN)
        except AttributeError:
            # Fallback for non-Windows platforms
            basename = os.path.basename(filepath)
            return basename.startswith('.')
        except Exception:
            # If we can't read attributes, assume not hidden
            return False

    def _is_hidden(self, filepath: str) -> bool:
        """Cross-platform check for hidden files."""
        if os.name == 'nt':
            return self._is_hidden_windows(filepath)
        else:
            basename = os.path.basename(filepath)
            return self._is_hidden_unix(basename)

    def _get_entry_info(self, filepath: str) -> Dict[str, Any]:
        """Build metadata for a single file or directory."""
        try:
            st = os.lstat(filepath)  # Follow symlinks? No — use lstat
            is_dir = stat.S_ISDIR(st.st_mode)
            entry_type = "directory" if is_dir else "file"
            is_hidden = self._is_hidden(filepath)
        except OSError:
            # If file becomes inaccessible during listing (rare but possible)
            entry_type = "file"
            is_hidden = self._is_hidden(filepath)

        return {
            "path": filepath.replace(os.sep, '/'),  # Normalize to forward slashes
            "type": entry_type,
            "is_hidden": is_hidden
        }

    def _list_directory(self, root_path: str, result: List[Dict[str, Any]]):
        """List only immediate children of the directory."""
        try:
            names = os.listdir(root_path)
        except PermissionError as e:
            raise PermissionError(f"Permission denied: cannot list contents of '{root_path}'")
        except Exception as e:
            raise RuntimeError(f"Failed to list directory '{root_path}': {str(e)}")

        for name in sorted(names):  # Sort for deterministic output
            filepath = os.path.join(root_path, name)
            entry = self._get_entry_info(filepath)
            result.append(entry)

    def _walk_directory(self, root_path: str, result: List[Dict[str, Any]]):
        """Recursively walk through directory and list all entries."""
        for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
            # Sort for consistent ordering
            dirnames.sort()
            filenames.sort()

            # Add directories first
            for dirname in dirnames:
                dpath = os.path.join(dirpath, dirname)
                result.append(self._get_entry_info(dpath))

            # Then add files
            for filename in filenames:
                fpath = os.path.join(dirpath, filename)
                result.append(self._get_entry_info(fpath))



# Фиксированная директория для тестов
TEST_BASE_DIR = os.path.join(os.path.dirname(__file__), "test_list_files")

def setup_test_dir():
    """Удаляет и создаёт чистую тестовую директорию."""
    if os.path.exists(TEST_BASE_DIR):
        shutil.rmtree(TEST_BASE_DIR)
    os.makedirs(TEST_BASE_DIR, exist_ok=True)

def cleanup():
    """Удаляет тестовую директорию."""
    if os.path.exists(TEST_BASE_DIR):
        shutil.rmtree(TEST_BASE_DIR)

def test_list_files_basic():
    setup_test_dir()
    test_dir = TEST_BASE_DIR

    # Создаём структуру
    os.makedirs(os.path.join(test_dir, "subdir"), exist_ok=True)
    os.makedirs(os.path.join(test_dir, ".git"), exist_ok=True)

    with open(os.path.join(test_dir, "file1.txt"), "w") as f:
        f.write("hello")
    with open(os.path.join(test_dir, "script.py"), "w") as f:
        f.write("print(42)")
    with open(os.path.join(test_dir, ".hidden_file"), "w") as f:
        f.write("secret")

    tool = ListFiles()
    result = tool(path=test_dir)

    assert isinstance(result, list), "Результат должен быть списком"
    assert len(result) == 5, f"Ожидалось 5 элементов, получено: {len(result)}"

    paths = [item["path"] for item in result]
    expected_paths = [
        f"{test_dir}/file1.txt",
        f"{test_dir}/script.py",
        f"{test_dir}/subdir",
        f"{test_dir}/.git",
        f"{test_dir}/.hidden_file"
    ]
    expected_paths = [p.replace(os.sep, '/') for p in expected_paths]

    for p in expected_paths:
        assert p in paths, f"Путь не найден: {p}"

    # Проверяем is_hidden
    for item in result:
        basename = os.path.basename(item["path"])
        if basename == ".git" or ".hidden_file" in item["path"]:
            assert item["is_hidden"] is True, f"Скрытый объект должен иметь is_hidden=True: {item}"
        elif item["path"].endswith("file1.txt") or item["path"].endswith("script.py"):
            assert item["is_hidden"] is False, f"Обычный файл не должен быть скрытым: {item}"

    print("test_list_files_basic passed")


def test_list_files_recursive():
    setup_test_dir()
    test_dir = TEST_BASE_DIR

    os.makedirs(os.path.join(test_dir, "a"))
    os.makedirs(os.path.join(test_dir, "a", "b"))
    os.makedirs(os.path.join(test_dir, ".config"))

    with open(os.path.join(test_dir, "top.txt"), "w") as f:
        f.write("top")
    with open(os.path.join(test_dir, "a", "f1.txt"), "w") as f:
        f.write("deep1")
    with open(os.path.join(test_dir, "a", "b", "f2.txt"), "w") as f:
        f.write("deeper2")
    with open(os.path.join(test_dir, ".config", "cfg"), "w") as f:
        f.write("settings")

    tool = ListFiles()
    result = tool(path=test_dir, recursive=True)

    expected_rel = [
        "a",
        "a/b",
        "a/f1.txt",
        "a/b/f2.txt",
        ".config",
        ".config/cfg",
        "top.txt"
    ]

    base = test_dir.replace(os.sep, '/')
    expected_full = [f"{base}/{p}".replace(os.sep, '/') for p in expected_rel]

    result_paths = [item["path"] for item in result]
    for p in expected_full:
        assert p in result_paths, f"Не найден путь: {p}"

    # Скрытые помечены правильно
    config_path = f"{base}/.config".replace(os.sep, '/')
    cfg_file_path = f"{base}/.config/cfg".replace(os.sep, '/')
    # print(cfg_file_path)
    # print(result)
    assert any(r["path"] == config_path and r["is_hidden"] is True for r in result)
    assert any(r["path"] == cfg_file_path and r["is_hidden"] is False for r in result)

    print("test_list_files_recursive passed")


def test_list_files_empty_dir():
    setup_test_dir()
    test_dir = TEST_BASE_DIR

    tool = ListFiles()
    result = tool(path=test_dir)
    assert isinstance(result, list)
    assert len(result) == 0, "Пустая директория должна вернуть пустой список"

    print("test_list_files_empty_dir passed")


def test_list_files_input_validation():
    tool = ListFiles()

    # Неверный тип path
    try:
        tool(path=123)
        assert False, "Должна быть ошибка при передаче числа как path"
    except ValueError as e:
        assert "must be a string" in str(e), f"Неверное сообщение об ошибке: {e}"

    # Не существует
    try:
        tool(path=os.path.join(TEST_BASE_DIR, "not_exist"))
        assert False, "Должна быть ошибка для несуществующего пути"
    except FileNotFoundError:
        pass  # OK

    # Это файл, а не директория
    file_path = os.path.join(TEST_BASE_DIR, "not_a_dir.txt")
    os.makedirs(TEST_BASE_DIR, exist_ok=True)
    with open(file_path, "w") as f:
        f.write("dummy")
    try:
        tool(path=file_path)
        assert False, "Должна быть ошибка при передаче файла вместо директории"
    except NotADirectoryError:
        pass  # OK
    os.unlink(file_path)

    # Неверный тип recursive
    try:
        tool(path=TEST_BASE_DIR, recursive="yes")
        assert False, "Должна быть ошибка при передаче строки как recursive"
    except ValueError as e:
        assert "must be a boolean" in str(e), f"Неверное сообщение: {e}"

    print("test_list_files_input_validation passed")


def test_list_files_no_recursive_default():
    setup_test_dir()
    test_dir = TEST_BASE_DIR

    with open(os.path.join(test_dir, "x.txt"), "w") as f:
        f.write("data")

    tool = ListFiles()
    result = tool(path=test_dir)  # recursive по умолчанию False
    assert len(result) == 1
    assert result[0]["path"].endswith("/x.txt") or result[0]["path"].endswith("\\x.txt"), "Файл x.txt должен быть найден"

    print("test_list_files_no_recursive_default passed")

def run_test():
    try:
        test_list_files_basic()
        test_list_files_recursive()
        test_list_files_empty_dir()
        test_list_files_input_validation()
        test_list_files_no_recursive_default()
        print("Все тесты пройдены успешно!")
    finally:
        cleanup()  # Убедимся, что тестовая папка удалена

if __name__ == "__main__":
    run_test()
