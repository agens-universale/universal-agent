#!/usr/bin/env bash
# Переезд src/ -> packages/{core,cli,tui,server}/src/gc_*.
#
# Скрипт воспроизводимый: откатить можно `git reset --hard && rm -rf packages`,
# затем запустить заново. Порядок шагов важен — см. docs/module-split.md.
set -euo pipefail
cd "$(dirname "$0")/.."
MAP=tools_dev/module_map.json

echo "== 1. физический перенос =="
python3 - "$MAP" <<'PY' > /tmp/gc_mv.sh
import json, pathlib, sys
m = json.load(open(sys.argv[1]))
DIST = {"gc_core": "core", "gc_cli": "cli", "gc_tui": "tui", "gc_server": "server"}
src = pathlib.Path("src")
print("set -e")
for pkg, dist in DIST.items():
    print(f"mkdir -p packages/{dist}/src/{pkg}")
for name, pkg in sorted(m.items()):
    dist = DIST[pkg]
    if (src / name).is_dir():
        print(f'git mv "src/{name}" "packages/{dist}/src/{pkg}/{name}"')
    elif (src / f"{name}.py").is_file():
        print(f'git mv "src/{name}.py" "packages/{dist}/src/{pkg}/{name}.py"')
PY
bash /tmp/gc_mv.sh

echo "== 2. __init__.py пакетов =="
for p in core:gc_core cli:gc_cli tui:gc_tui server:gc_server; do
  d=${p%%:*}; pkg=${p##*:}
  [ -f "packages/$d/src/$pkg/__init__.py" ] || cat > "packages/$d/src/$pkg/__init__.py" <<EOF
"""Пакет $pkg. Пустой namespace-контейнер: модули импортируются напрямую
($pkg.<module>), реэкспортов здесь нет — чтобы импорт пакета не тянул за собой
весь агент."""
EOF
done
git rm -q --cached src/__init__.py 2>/dev/null || true
rm -rf src

echo "== 3. кодмод импортов =="
python3 tools_dev/codemod_imports.py --map "$MAP" \
  --root packages --root tests --root harbor --root conftest.py --apply

echo "== 4. схлопнуть gc_tui/tui -> gc_tui =="
# Иначе получается gc_tui.tui.screens.main — имя дублируется на ровном месте.
if [ -d packages/tui/src/gc_tui/tui ]; then
  ( cd packages/tui/src/gc_tui
    for f in tui/*; do
      b=$(basename "$f")
      if [ "$b" = "__init__.py" ]; then rm -f __init__.py; fi   # заглушку заменяет настоящая точка входа
      git mv "$f" "$b" 2>/dev/null || mv "$f" "$b"
    done
    rmdir tui )
  find packages tests harbor -name "*.py" -not -path "*__pycache__*" -print0 |
    xargs -0 perl -pi -e 's/\bgc_tui\.tui\b/gc_tui/g; s/\bfrom gc_tui import tui\b/import gc_tui/g'
fi

echo "== 5. идиома выселения: == \"mod\" or ...startswith(\"mod.\") =="
# Кодмод литералов видит только startswith. Вторая половина сравнения молча
# осталась бы со старым именем, и фильтр перестал бы совпадать — ровно тот
# тихий класс поломок, ради которого заведена сетка проверки.
python3 - "$MAP" <<'PY'
import json, pathlib, re, sys
m = json.load(open(sys.argv[1]))
FLAT = {"tui": "gc_tui"}
n = 0
for root in ("tests", "packages", "harbor"):
    for p in pathlib.Path(root).rglob("*.py"):
        if "__pycache__" in p.parts: continue
        t = old = p.read_text(encoding="utf-8")
        for name, pkg in m.items():
            new = FLAT.get(name) or f"{pkg}.{name}"
            t = re.sub(rf'== "{re.escape(name)}" or (\w+)\.startswith\("{re.escape(name)}\."\)',
                       rf'== "{new}" or \1.startswith("{new}.")', t)
        if t != old:
            p.write_text(t, encoding="utf-8"); n += 1
print(f"  файлов с идиомой поправлено: {n}")
PY

echo "== 6. кодмод строковых литералов =="
python3 tools_dev/rewrite_module_literals.py --map "$MAP" --apply

echo "== 7. проверка синтаксиса =="
python3 - <<'PY'
import pathlib, sys
bad = []
for root in ("packages", "tests", "harbor"):
    for p in pathlib.Path(root).rglob("*.py"):
        if "__pycache__" in p.parts: continue
        try: compile(p.read_text(encoding="utf-8"), str(p), "exec")
        except SyntaxError as e: bad.append(f"{p}:{e.lineno} {e.msg}")
print(f"  синтаксических ошибок: {len(bad)}")
for b in bad[:10]: print("   ", b)
sys.exit(1 if bad else 0)
PY

echo "== готово =="
