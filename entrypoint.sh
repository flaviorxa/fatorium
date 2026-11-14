#!/bin/sh
set -e
mkdir -p /app/data
if [ ! -f /app/data/universo.md ]; then
  cp /app/config/universo.md /app/data/universo.md
fi
if [ ! -f /app/data/invest.db ] && [ "${INVEST_COLETAR_NO_BOOT:-1}" = "1" ]; then
  echo "primeira execucao: coleta inicial em segundo plano"
  ( python -m scripts.coletar && touch /app/data/.coleta-inicial-ok ) &
fi
exec python -m uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --workers 1
