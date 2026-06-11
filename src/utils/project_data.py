import os
import shutil
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from utils.directory_write_access import directory_allows_probe_write

cut_first = 100
cut_second = 30

stop_folders = {
        '.git', '.hg', '.svn', '.osc',
        '__pycache__', 'venv', '.venv', '.env',
        'node_modules', 'bower_components',
        'dist', 'build', 'out', 'target', 'bin', 'obj',
        '.vscode', '.idea', '.fleet',
        '.mypy_cache', '.pytest_cache', '.coverage',
        '.gradle', '.classpath', '.project', '.settings',
        'packages', '.nuget', 'TestResults',
        'coverage', '.nyc_output',
        'logs', 'tmp', 'temp', 'dump.rdb',
        '.dockerignore', '.env.local', '.env.*',
        '.github', '.gitlab', '.circleci', '.travis',
        'Thumbs.db', 'Desktop.ini', '.DS_Store',
        '$RECYCLE.BIN', 'System Volume Information',
        '.Trash-*', '.local/share/Trash',
        'patchwork', '.verdaccio', '.pnp', '.pnpify',
        'Cargo.lock', 'target', 'target_debug', 'target_release',
        'vendor'
    }


def get_listing(dir_path: str) -> str:
    files = os.listdir(dir_path)
    res = []
    for i,f in enumerate(files):
        if f in stop_folders:
            continue
        if i >= cut_first:
            res.append(('...', []))
            break

        pt = os.path.join(dir_path, f)
        if os.path.isdir(pt):
            sub_files = os.listdir(pt)
            sub_res = []
            for j, ff in enumerate(sub_files):
                if ff in stop_folders:
                    continue
                if j >= cut_second:
                    sub_res.append('...')
                    break
                spt = os.path.join(pt, ff)
                if os.path.isdir(spt):
                    sub_res.append(ff + '/')
                else:
                    sub_res.append(ff)
            res.append((f + '/', sub_res))
        else:
            res.append((f, []))

    tpl = ''
    for f, sub_files in res:
        tpl += '\n' + f
        for sf in sub_files:
            tpl += '\n    ' + sf

    return tpl.strip()


def _dir_writable_probe(path: str) -> bool:
    return directory_allows_probe_write(path)


def _dedupe_ordered_abs_paths(paths: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for raw in paths:
        candidate = os.path.abspath(os.path.expanduser(os.path.normpath(str(raw))))
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _resolve_workspace_dir(config_dict: Optional[Dict[str, Any]]) -> str:
    raw: Optional[Any] = None
    if isinstance(config_dict, dict):
        raw = config_dict.get("launch_root") or config_dict.get("project_root")
    if raw:
        return os.path.abspath(os.path.expanduser(os.path.normpath(str(raw))))
    return os.path.abspath(os.getcwd())


def _extra_write_roots(config_dict: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(config_dict, dict):
        return []
    write_guard = config_dict.get("write_guard")
    if not isinstance(write_guard, dict):
        return []
    extra = write_guard.get("extra_paths")
    return [str(x) for x in (extra or []) if x is not None and str(x).strip()]


def os_label() -> str:
    """Человекочитаемое имя ОС из ``sys.platform`` (константа интерпретатора).

    Намеренно НЕ используем ``platform.system()``/``uname()``: на Windows+CPython
    этот путь уходит в WMI-запрос (``platform._wmi_query`` → служба ``winmgmt``),
    который при подвисшей службе блокируется намертво и замораживает старт агента.
    ``sys.platform`` зашит при сборке интерпретатора — без службы и без syscall.
    """
    if sys.platform == "win32":
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("linux"):
        return "Linux"
    return sys.platform


def get_project_data(workspace_config: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """
    return project data like
    {
      "OS": "Linux",
      "shell": "bash",
      "projectDir": "/home/user/project",
      "projectDirWritable": "true",
      "allowed_write_paths": "/tmp/extra_allowed",
      "currentDate": "2023-10-05",
      "isGitProject": "true",
      "projectDirListing": "folder/\nfolder/file1\nfile2..."
    }

    workspace_config: optional CLI/server config snapshot (launch_root, project_root,
    write_guard.extra_paths) used for projectDir / allowed writes.

    ``allowed_write_paths`` lists only directories where a probe file can be created and removed
    (same meaning on Linux, macOS, and Windows); non-writable roots are omitted.
    """
    os_str = os_label()

    # Оболочка (shell)
    shell = os.environ.get("SHELL", "unknown")  # для Unix-систем
    if not shell or shell == "unknown" and sys.platform == "win32":
        shell = os.environ.get("COMSPEC", "cmd.exe")

    project_dir = _resolve_workspace_dir(workspace_config)
    if not os.path.isdir(project_dir):
        project_dir = os.path.abspath(os.getcwd())

    # Проверка, является ли проект git-репозиторием
    is_git_project = "true" if os.path.exists(os.path.join(project_dir, ".git")) else "false"

    current_date = datetime.now().strftime('%Y-%m-%d')
    try:
        project_dir_listing = get_listing(project_dir)
    except OSError:
        project_dir_listing = ""

    allowed_roots = _dedupe_ordered_abs_paths([project_dir, *_extra_write_roots(workspace_config)])
    writable_roots = [p for p in allowed_roots if _dir_writable_probe(p)]
    allowed_write_paths = "\n".join(writable_roots)
    project_dir_writable = "true" if _dir_writable_probe(project_dir) else "false"

    data = {
        'OS': os_str,
        'shell': shell,
        'projectDir': project_dir,
        'projectDirWritable': project_dir_writable,
        'allowed_write_paths': allowed_write_paths,
        'currentDate': current_date,
        'isGitProject': is_git_project,
        'projectDirListing': project_dir_listing
    }

    return data


def run_tests():
    # === Тест get_listing ===

    # Создаём временную структуру каталогов
    test_dir = "test_temp_dir"
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)
    os.mkdir(test_dir)

    # Создаём файлы и подкаталоги
    open(os.path.join(test_dir, "file1.txt"), "w").close()
    open(os.path.join(test_dir, "file2.py"), "w").close()
    os.mkdir(os.path.join(test_dir, "subdir1"))
    os.mkdir(os.path.join(test_dir, "subdir2"))

    with open(os.path.join(test_dir, "subdir1", "script.py"), "w") as f:
        f.write("print('hello')")
    with open(os.path.join(test_dir, "subdir1", "data.txt"), "w") as f:
        f.write("data")
    with open(os.path.join(test_dir, "subdir2", "config.json"), "w") as f:
        f.write("{}")

    # Тест 1: обычное содержимое каталога
    listing = get_listing(test_dir)
    expected_lines = [
        'file1.txt',
        'file2.py',
        'subdir1/',
        '    script.py',
        '    data.txt',
        'subdir2/',
        '    config.json'
    ]
    actual_lines = listing.strip().split('\n')
    for l in expected_lines:
        assert l in actual_lines, f"Тест 1 провален:\n{l}\nне в \n{actual_lines}"

    for l in actual_lines:
        assert l in expected_lines, f"Тест 1 провален:\n{l}\nне в \n{expected_lines}"

    # Тест 2: проверка ограничения cut_first (ограничение на количество элементов верхнего уровня)
    global cut_first, cut_second
    old_cut_first = cut_first
    cut_first = 2  # Ограничим до 2 элементов

    listing_limited = get_listing(test_dir)
    limited_lines = listing_limited.strip().split('\n')
    assert '...' in limited_lines, f"Тест 2 (cut_first) провален: {limited_lines}"

    cut_first = old_cut_first  # Восстанавливаем

    # Тест 3: проверка cut_second (ограничение на поддиректории)
    old_cut_second = cut_second
    cut_second = 1
    listing_sub_limited = get_listing(test_dir)
    sub_lines = listing_sub_limited.strip().split('\n')
    expected_sub_lines = [
        'file1.txt',
        'file2.py',
        'subdir1/',
        '    script.py',
        '    ...',
        'subdir2/',
        '    config.json',
    ]
    assert '    ...' in sub_lines
    assert  len(expected_sub_lines) == len(sub_lines)

    cut_second = old_cut_second  # Восстанавливаем

    # Тест 4: пустая директория
    empty_dir = os.path.join(test_dir, "empty_subdir")
    os.mkdir(empty_dir)
    listing_empty = get_listing(empty_dir)
    assert listing_empty == "", "Тест 4 (пустая директория) провален"

    # Тест 5: отсутствующая директория
    try:
        get_listing("non_existent_path_12345")
        assert False, "Тест 5 должен был вызвать исключение"
    except FileNotFoundError:
        pass  # OK
    except Exception as e:
        assert False, f"Тест 5: неправильное исключение: {e}"

    # === Тест get_project_data ===

    # Тест 6: структура данных
    data = get_project_data()
    required_keys = [
        'OS',
        'shell',
        'projectDir',
        'projectDirWritable',
        'allowed_write_paths',
        'currentDate',
        'isGitProject',
        'projectDirListing',
    ]
    for k in required_keys:
        assert k in data, f"Ключ {k} отсутствует в данных проекта"

    # Тест 7: проверка типа значений
    assert isinstance(data['OS'], str), "OS должно быть строкой"
    assert isinstance(data['shell'], str), "shell должно быть строкой"
    assert isinstance(data['projectDir'], str), "projectDir должно быть строкой"
    assert isinstance(data['currentDate'], str), "currentDate должно быть строкой"
    assert data['isGitProject'] in ['true', 'false'], "isGitProject должно быть 'true' или 'false'"
    assert isinstance(data['projectDirListing'], str), "projectDirListing должно быть строкой"

    # Тест 8: проверка currentDate — формат YYYY-MM-DD
    current_date = data['currentDate']
    try:
        datetime.strptime(current_date, '%Y-%m-%d')
    except ValueError:
        assert False, f"Неверный формат даты: {current_date}"

    # Тест 9: projectDir — существующий путь
    assert os.path.isdir(data['projectDir']), "projectDir должен указывать на существующую директорию"

    # Тест 10: isGitProject — зависит от наличия .git
    git_status = os.path.exists(os.path.join(data['projectDir'], ".git"))
    expected_git = "true" if git_status else "false"
    assert data['isGitProject'] == expected_git, f"isGitProject: ожидалось {expected_git}, получено {data['isGitProject']}"

    # Тест 11: projectDirListing — корректная строка, соответствует текущей директории (частично)
    listing_output = data['projectDirListing']
    files_in_current = os.listdir(data['projectDir'])
    listed_names = [line.strip().rstrip('/') for line in listing_output.split('\n') if line.strip() and not line.startswith('    ')]
    # Проверяем, что хотя бы некоторые реальные файлы/папки есть в выводе (если их < cut_first)
    if len(files_in_current) < cut_first:
        for f in files_in_current:
            base_name = f.rstrip('/')
            if base_name in stop_folders:
                continue
            assert any(base_name in listed for listed in listed_names), f"Файл {f} не найден в projectDirListing"
    else:
        assert '...' in listing_output, "Если файлов больше cut_first, должен быть '...'"

    # Удаляем временную директорию
    shutil.rmtree(test_dir)
    print("✅ Все тесты пройдены!")


# Запуск тестов
if __name__ == "__main__":
    run_tests()
    print(get_project_data())
    print(get_project_data()['projectDirListing'])
