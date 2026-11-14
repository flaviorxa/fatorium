FROM python:3.12-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Sao_Paulo \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt

COPY app/ /app/app/
COPY config/ /app/config/
COPY scripts/ /app/scripts/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

RUN useradd --create-home --uid 10001 fatorium \
    && mkdir -p /app/data \
    && chown -R fatorium:fatorium /app
USER fatorium

ENV INVEST_HOST=0.0.0.0 \
    INVEST_PORTA=8000 \
    INVEST_LISTA_ATIVOS=/app/data/universo.md

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS -o /dev/null -u "${INVEST_USUARIO}:${INVEST_SENHA}" http://127.0.0.1:8000/saude

ENTRYPOINT ["/app/entrypoint.sh"]
