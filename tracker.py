import asyncio
import json
import os
from datetime import datetime, timezone

ODDS_DIR = "odds"
STATS_DIR = "stats"
PREDICTIONS_INPUT = os.path.join(ODDS_DIR, "predictions_latest.json")
KALSHI_INPUT = os.path.join(ODDS_DIR, "kalshi_latest.json")
HISTORY_OUTPUT = os.path.join(STATS_DIR, "prediction_history.json")

_YES_THRESHOLD = 0.95
_NO_THRESHOLD = 0.05


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_history() -> list[dict]:
    data = _load_json(HISTORY_OUTPUT)
    return data.get("history", [])


def _save_history(history: list[dict]) -> None:
    os.makedirs(STATS_DIR, exist_ok=True)
    by_type: dict[str, dict] = {}
    for entry in history:
        mt = entry.get("market_type", "unknown")
        if mt not in by_type:
            by_type[mt] = {"correct": 0, "total": 0}
        by_type[mt]["total"] += 1
        if entry.get("correct"):
            by_type[mt]["correct"] += 1

    total = len(history)
    correct = sum(1 for e in history if e.get("correct"))
    accuracy = round(correct / total * 100, 1) if total else None

    accuracy_by_type = {}
    for mt, counts in by_type.items():
        t = counts["total"]
        c = counts["correct"]
        accuracy_by_type[mt] = {
            "correct": c,
            "total": t,
            "accuracy_pct": round(c / t * 100, 1) if t else None,
        }

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total_predictions": total,
        "resolved_predictions": total,
        "correct": correct,
        "accuracy_pct": accuracy,
        "by_type": accuracy_by_type,
        "history": history,
    }
    with open(HISTORY_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def resolve_predictions() -> int:
    predictions_data = _load_json(PREDICTIONS_INPUT)
    kalshi_data = _load_json(KALSHI_INPUT)

    predictions = predictions_data.get("predictions", [])
    game_markets = kalshi_data.get("game_markets", [])
    kalshi_by_ticker = {m["ticker"]: m for m in game_markets if m.get("ticker")}

    history = _load_history()
    resolved_tickers = {e["ticker"] for e in history}

    already_logged = len(resolved_tickers)
    unresolved = [p for p in predictions if p.get("ticker") not in resolved_tickers]
    settled = 0
    new_entries = 0

    for pred in unresolved:
        ticker = pred.get("ticker")
        if not ticker:
            continue

        market = kalshi_by_ticker.get(ticker)
        if not market:
            continue

        yes_ask = market.get("yes_ask")
        if yes_ask is None:
            continue

        try:
            price = float(yes_ask)
        except (TypeError, ValueError):
            continue

        if price >= _YES_THRESHOLD:
            outcome = "YES"
        elif price <= _NO_THRESHOLD:
            outcome = "NO"
        else:
            continue

        settled += 1
        raw_conf = pred.get("confidence") or 0
        confidence = min(max(float(raw_conf), 0), 100)

        correct = pred.get("prediction") == outcome
        history.append({
            "ticker": ticker,
            "market_type": pred.get("market_type"),
            "title": pred.get("title"),
            "prediction": pred.get("prediction"),
            "confidence": round(confidence, 1),
            "method": pred.get("method"),
            "outcome": outcome,
            "correct": correct,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        })
        resolved_tickers.add(ticker)
        new_entries += 1

    print(
        f"  tracker: {len(predictions)} predictions loaded | "
        f"{already_logged} already logged | "
        f"{len(unresolved)} checked | "
        f"{settled} settled (yes>={_YES_THRESHOLD} or <={_NO_THRESHOLD}) | "
        f"{new_entries} new history entries"
    )

    if new_entries:
        _save_history(history)

    return new_entries


async def main():
    initial_delay = 1800
    interval = 3600
    print(f"Tracker  |  initial_delay={initial_delay}s  interval={interval}s")
    await asyncio.sleep(initial_delay)

    run = 0
    while True:
        run += 1
        ts = datetime.now(timezone.utc).isoformat()
        print(f"\n[{ts}] tracker run #{run}")
        try:
            resolve_predictions()
        except Exception as exc:
            print(f"  ERROR: {exc}")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
