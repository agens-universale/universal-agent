import sys
from pathlib import Path


def _ensure_src_on_path() -> None:
    # pytest запускается из корня репозитория, но sys.path по умолчанию
    # не обязан содержать `src/`. Добавляем единообразно.
    root = Path(__file__).resolve().parent
    src = root / "src"
    src_str = str(src)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


_ensure_src_on_path()

