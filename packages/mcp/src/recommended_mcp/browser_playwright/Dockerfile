FROM node:22-bookworm-slim
WORKDIR /app

RUN npm install -g @playwright/mcp@0.0.70

RUN npx playwright install --with-deps chromium

ENV DEBUG=pw:mcp*

EXPOSE 8931

CMD ["npx", "@playwright/mcp", \
     "--port", "8931", \
     "--host", "0.0.0.0", \
     "--browser", "chromium", \
     "--headless", \
     "--isolated", \
     "--output-dir", "/tmp/playwright-mcp"]
