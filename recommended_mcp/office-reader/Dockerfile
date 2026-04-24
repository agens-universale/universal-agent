FROM python:3.12-slim

# LibreOffice нужен только для .doc (legacy). Остальные форматы —
# mammoth / openpyxl / pdfminer работают в чистом Python.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-core \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

RUN useradd -m -u 1000 app \
    && mkdir -p /workspace \
    && chown app:app /workspace
USER app

VOLUME ["/workspace"]
EXPOSE 8080

CMD ["python", "-m", "mcp_office_reader", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--allowed-dirs", "/workspace"]
