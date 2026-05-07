import asyncio
import json
import os
from datetime import datetime, timezone

from features import build_features, _load_teams, _load_recent
from model import predict

ODDS_DIR = "odds"
KALSHI_INPUT = os.path.join(ODDS_DIR, "kalshi_latest.json")
PREDICTIONS_OUTPUT = os.path.join(ODDS_DIR, "predictions_latest.json")

PREDICTABLE_TYPES = {"winner", "2h_winner", "spread", "series_spread", "total", "team_total"}


def run_predictions() -> list[dict]:
    if not os.path.exists(KALSHI_INPUT):
        print("  predictions: kalshi_latest.json not found, skipping")
        return []

    with open(KALSHI_INPUT, encoding="utf-8") as f:
        kalshi = json.load(f)

    game_markets = kalshi.get("game_markets", [])
    teams = _load_teams()
    recent = _load_recent()

    if not teams:
        print("  predictions: team_stats.json not found or empty, skipping")
        return []

    predictions: list[dict] = []

    for market in game_markets:
        mtype = market.get("market_type")
        if mtype not in PREDICTABLE_TYPES:
            continue

        features = build_features(market, teams=teams, recent=recent)
        if features is None:
            continue

        result = predict(features, mtype)

        predictions.append({
            "ticker": market.get("ticker"),
            "event_ticker": market.get("event_ticker"),
            "market_type": mtype,
            "title": market.get("title"),
            "yes_team": market.get("yes_team"),
            "no_team": market.get("no_team"),
            "yes_ask": market.get("yes_ask"),
            "no_ask": market.get("no_ask"),
            "prediction": result.get("prediction"),
            "confidence": result.get("confidence"),
            "reasoning": result.get("reasoning"),
            "method": result.get("method"),
            "predicted_value": result.get("predicted_value"),
            "features_snapshot": features,
        })

    return predictions


def save_predictions(predictions: list[dict], timestamp: str) -> None:
    os.makedirs(ODDS_DIR, exist_ok=True)
    by_type: dict[str, int] = {}
    for p in predictions:
        mt = p.get("market_type", "unknown")
        by_type[mt] = by_type.get(mt, 0) + 1

    payload = {
        "generated_at": timestamp,
        "count": len(predictions),
        "by_type": by_type,
        "predictions": predictions,
    }
    with open(PREDICTIONS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"  {len(predictions)} predictions -> {PREDICTIONS_OUTPUT} | {by_type}")


async def main():
    initial_delay = 30
    interval = 60
    print(f"Predictor  |  initial_delay={initial_delay}s  interval={interval}s")
    await asyncio.sleep(initial_delay)

    run = 0
    while True:
        run += 1
        ts = datetime.now(timezone.utc).isoformat()
        print(f"\n[{ts}] predictor run #{run}")
        try:
            predictions = run_predictions()
            save_predictions(predictions, ts)
        except Exception as exc:
            print(f"  ERROR: {exc}")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
