"""FS-утилиты voice_runtime: удаление git-клона на Windows.

git помечает файлы .git/objects/pack read-only, а shutil.rmtree на Windows
read-only не удаляет — «удаление» молча оставляет огрызок каталога.
Снимаем read-only пре-проходом и удаляем; версионно-нейтрально
(onerror deprecated в 3.12, onexc нет в 3.11 — обходимся без обоих).
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


def force_rmtree(path: Path) -> bool:
    """Удалить дерево, снимая read-only. True — каталога больше нет."""
    if not path.is_dir():
        return True
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                os.chmod(os.path.join(base, name), stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)
    return not path.is_dir()
