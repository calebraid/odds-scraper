#!/bin/sh
export PYTHONUNBUFFERED=1

(
  while true; do
    echo "[scraper] starting"
    python -u scraper.py 2>&1
    echo "[scraper] exited (code $?), restarting in 15s"
    sleep 15
  done
) &

(
  while true; do
    echo "[stats] starting"
    python -u stats_scraper.py 2>&1
    echo "[stats] exited (code $?), restarting in 30s"
    sleep 30
  done
) &

(
  while true; do
    echo "[predictor] starting"
    python -u predictor.py 2>&1
    echo "[predictor] exited (code $?), restarting in 15s"
    sleep 15
  done
) &

(
  while true; do
    echo "[tracker] starting"
    python -u tracker.py 2>&1
    echo "[tracker] exited (code $?), restarting in 60s"
    sleep 60
  done
) &

echo "[api] starting"
exec uvicorn api:app --host 0.0.0.0 --port "${PORT:-8000}"
