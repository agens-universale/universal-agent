# tools_dev — инструменты переезда на пакеты

Не входят в дистрибутивы, нужны только при разработке.

## check_module_literals.py

Сетка защиты для переезда `gc_core` / `gc_cli` / `gc_tui` / `gc_server`.

Кодмод правит узлы `import`. Но модули называются строками ещё в 353 местах:
`monkeypatch.setattr("tools.registry.x")`, `sys.modules["tools"]`,
`types.ModuleType("summarize")`, фильтры `startswith("tools.")`. Около 110 из
них при устаревшей строке **не падают** — цикл выселения становится пустым,
заглушки протекают между тестовыми файлами, набор остаётся зелёным.

Поэтому зелёные тесты после переезда сами по себе ничего не доказывают.

```bash
uv run python tools_dev/check_module_literals.py                 # отчёт
uv run python tools_dev/check_module_literals.py --compare \
    tools_dev/module_literals_before.json                        # сверка со снимком
```

`module_literals_before.json` — снимок до переезда (353 ссылки в 78 файлах).
После каждого шага сверяйтесь с ним: исчезновение живой ссылки означает, что
кодмод её пропустил или сломал.
