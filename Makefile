PORT     := 8931
BROWSER  := chromium
LOG_FILE := /tmp/playwright-mcp.log
OUT_DIR  := /tmp/playwright-mcp

.PHONY: start start-debug stop restart status logs

playwrrite_start:
	@if pgrep -f "playwright/mcp" > /dev/null; then \
		echo "Already running (use 'make restart' to restart)"; \
	else \
		npx @playwright/mcp@0.0.70 \
			--port $(PORT) \
			--browser $(BROWSER) \
			--isolated \
			--output-dir $(OUT_DIR) \
			> $(LOG_FILE) 2>&1 & \
		echo "Started on http://localhost:$(PORT)/mcp (log: $(LOG_FILE))"; \
	fi

# Запуск с выводом всех входящих запросов в терминал (не в фоне)
playwrrite_start-debug:
	@pkill -f "playwright/mcp" 2>/dev/null || true
	DEBUG=pw:mcp* npx @playwright/mcp@0.0.70 \
		--port $(PORT) \
		--browser $(BROWSER) \
		--isolated \
		--output-dir $(OUT_DIR)

playwrrite_stop:
	@pkill -f "playwright/mcp" && echo "Stopped" || echo "Not running"

playwrrite_restart: stop
	@sleep 1
	@$(MAKE) start

playwrrite_status:
	@pgrep -f "playwright/mcp" > /dev/null \
		&& echo "Running (PID: $$(pgrep -f 'playwright/mcp'))" \
		|| echo "Not running"

playwrrite_logs:
	@tail -f $(LOG_FILE)
