from tui.services.mcp_progress import McpProgress, format_mcp_progress, format_mcp_settle


def test_sink_begin_update_snapshot():
    p = McpProgress()
    p.begin(["a", "b"])
    servers, active = p.snapshot()
    assert active and servers == {"a": None, "b": None}
    assert not p.all_reported()
    p.update("a", {"status": "connected"})
    p.update("b", {"status": "failed", "reason": "x"})
    assert p.all_reported()
    servers, _ = p.snapshot()
    assert servers["a"]["status"] == "connected"


def test_format_progress_multiline_block():
    text = format_mcp_progress({"a": {"status": "connected"}, "b": None,
                                "c": {"status": "failed"}})
    lines = text.splitlines()
    assert lines[0].startswith("подключаю MCP")          # заголовок
    assert lines[1] == ""                                 # отступ-строка после заголовка
    assert len(lines) == 5                                # заголовок + отступ + 3 сервера
    body = lines[2:]
    assert any("a" in ln and "✓" in ln for ln in body)   # каждый сервер на своей строке
    assert any("b" in ln and "⏳" in ln for ln in body)
    assert any("c" in ln and "✗" in ln for ln in body)


def test_format_progress_caps_long_list():
    # >5 серверов: показываем первые 4 + строку-счётчик «… ещё +N»,
    # чтобы блок MCP не превышал высоту бренда (заголовок + 4 + счётчик = 6).
    servers = {f"s{i}": {"status": "connected"} for i in range(8)}
    lines = format_mcp_progress(servers).splitlines()
    assert lines[0].startswith("подключаю MCP")
    assert lines[1] == ""                    # отступ после заголовка
    assert len(lines) == 7                    # заголовок + отступ + 4 + счётчик
    assert lines[-1] == "… ещё +4"          # 8 всего − 4 показанных = 4 скрытых
    assert sum(1 for ln in lines if "✓" in ln) == 4


def test_format_progress_no_cap_at_threshold():
    # ровно 5 серверов — показываем все, без счётчика.
    servers = {f"s{i}": {"status": "connected"} for i in range(5)}
    lines = format_mcp_progress(servers).splitlines()
    assert len(lines) == 7                   # заголовок + отступ + 5 серверов
    assert not any("ещё" in ln for ln in lines)


def test_format_settle_all_ok():
    # Контракт: только строка статуса MCP + «/mcp (F3)» (готовность — в карточке).
    text = format_mcp_settle({"a": {"status": "connected"}})
    assert text == "✓ MCP: все подключены · /mcp (F3)"
    assert "готов" not in text


def test_format_settle_failed():
    text = format_mcp_settle({"a": {"status": "connected"}, "office": {"status": "failed"}})
    # счётчик N/M вместо имён серверов, хинт /mcp, без строки готовности
    assert "⚠" in text and "1/2" in text and "/mcp" in text
    assert "готов" not in text


def test_format_settle_zero_connected():
    # все упали — специальная фраза «ни один не подключился»
    text = format_mcp_settle({"office": {"status": "failed"}, "computer": {"status": "failed"}})
    assert "⚠" in text and "0/2" in text and "/mcp" in text
    assert "ни один" in text
