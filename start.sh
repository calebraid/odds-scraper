#!/bin/sh
export PYTHONUNBUFFERED=1

# Run scraper in a background restart loop. Stderr is merged into stdout so
# tracebacks appear in Railway logs. On crash, wait 15s then restart.
(
  while true; do
    echo "[scraper] starting"
    python -u scraper.py 2>&1
    echo "[scraper] exited (code $?), restarting in 15s"
    sleep 15
  done
) &

echo "[api] starting"
exec uvicorn api:app --host 0.0.0.0 --port "${PORT:-8000}"
