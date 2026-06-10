import sys
from pathlib import Path

# recommended_mcp/ живёт в корне репо, а не в src/. Корневой conftest.py
# добавляет только src/ в sys.path — для тестов этого пакета дополнительно
# кладём сюда корень репо, чтобы `from recommended_mcp.X import ...` работало.
_repo_root = Path(__file__).resolve().parents[2]
_src_root = _repo_root / "src"
for path in (_src_root, _repo_root):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
