import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Каталоги исходников всех пакетов. Раньше здесь был один `src/`; после
# разбиения на модули их четыре, и тестовый набор гоняется по ним, а не по
# установленным колёсам.
PACKAGE_SRC_DIRS = tuple(
    sorted(p for p in ROOT.glob("packages/*/src") if p.is_dir())
)


def _ensure_sources_on_path() -> None:
    # pytest запускается из корня репозитория, но sys.path по умолчанию не
    # обязан содержать каталоги пакетов. Плюс сам корень: `recommended_mcp` —
    # отдельный импортируемый пакет и лежит в корне, а не в packages/.
    for path in (*PACKAGE_SRC_DIRS, ROOT):
        s = str(path)
        if s not in sys.path:
            sys.path.insert(0, s)


_ensure_sources_on_path()
