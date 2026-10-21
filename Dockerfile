# ══════════════════════════════════════════════════════════════
# STAGE 1: builder
# ══════════════════════════════════════════════════════════════
FROM nexus.isb/library/python:3.13-alpine AS builder

WORKDIR /app

COPY CA1.crt /usr/local/share/ca-certificates/nexus-ca.crt
COPY ca-bundle.crt /usr/local/share/ca-certificates/GS.crt
RUN update-ca-certificates

ENV PIP_CERT=/usr/local/share/ca-certificates/nexus-ca.crt \
    PIP_INDEX_URL=https://nexus.isb/repository/pypiorg-pypi-proxy/simple \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Создаём venv
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ══════════════════════════════════════════════════════════════
# STAGE 2: runtime
# ══════════════════════════════════════════════════════════════
FROM nexus.isb/library/python:3.13-alpine

WORKDIR /app

COPY CA1.crt /usr/local/share/ca-certificates/nexus-ca.crt
COPY ca-bundle.crt /usr/local/share/ca-certificates/GS.crt
RUN update-ca-certificates

# Копируем готовый venv
COPY --from=builder /venv /venv

# Прописываем venv в PATH — теперь alembic, uvicorn, gunicorn доступны
ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    REQUESTS_CA_BUNDLE=/usr/local/share/ca-certificates/nexus-ca.crt \
    SSL_CERT_FILE=/app/ca-bundle.crt

COPY . .

# EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; \
    sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "main_support:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]