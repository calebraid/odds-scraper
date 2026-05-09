import asyncio
import json
import os
import re as _re
from datetime import datetime, timezone

from features import build_features, _load_teams, _load_recent, _load_players, find_team
from model import predict

ODDS_DIR = "odds"
KALSHI_INPUT = os.path.join(ODDS_DIR, "kalshi_latest.json")
PREDICTIONS_OUTPUT = os.path.join(ODDS_DIR, "predictions_latest.json")

PREDICTABLE_TYPES = {"winner", "2h_winner", "spread", "series_spread", "total", "team_total"}

_WINNER_TITLE_RE = _re.compile(r"Will (?:the )?(.+?) win\?", _re.IGNORECASE)


def _direct_winner(market: dict, teams: list[dict]) -> dict | None:
    """Formula-based winner prediction for when the feature pipeline can't resolve both teams.

    Parses the YES-team from the market title when yes_team is absent, then uses:
        win_prob = 0.5 + (t1_net_rtg - t2_net_rtg) * 0.033   capped [0.1, 0.9]
    """
    yes_name = market.get("yes_team") or ""
    no_name  = market.get("no_team") or ""

    if not yes_name:
        hit = _WINNER_TITLE_RE.search(market.get("title") or "")
        yes_name = hit.group(1).strip() if hit else ""

    t1 = find_team(yes_name, teams) if yes_name else None
    t2 = find_team(no_name, teams)  if no_name  else None

    if not t1:
        return None

    def _sf(v, d=0.0):
        try:
            return float(v) if v is not None else d
        except (TypeError, ValueError):
            return d

    t1_net = _sf(t1.get("e_net_rating") or t1.get("net_rtg"))
    t2_net = _sf(t2.get("e_net_rating") or t2.get("net_rtg")) if t2 else 0.0

    net_diff = t1_net - t2_net
    win_prob = max(0.1, min(0.9, 0.5 + net_diff * 0.033))
    yes_ask  = _sf(market.get("yes_ask"), 0.5)
    edge     = round(win_prob - yes_ask, 3)

    prediction = "YES" if win_prob >= 0.5 else "NO"
    raw_conf   = win_prob if prediction == "YES" else (1.0 - win_prob)
    confidence = round(min(95.0, max(51.0, raw_conf * 100)), 1)

    t1_win_pct = _sf(t1.get("win_pct"), 0.5)
    t2_win_pct = _sf(t2.get("win_pct"), 0.5) if t2 else 0.5

    return {
        "prediction": prediction,
        "confidence": confidence,
        "reasoning":  f"Net Rtg diff {net_diff:+.1f} → {win_prob:.1%} win prob",
        "method":     "formula",
        "features_snapshot": {
            "t1_net_rtg":       round(t1_net, 2),
            "t2_net_rtg":       round(t2_net, 2),
            "net_rtg_diff":     round(net_diff, 2),
            "win_pct_diff":     round(t1_win_pct - t2_win_pct, 3),
            "our_win_prob":     round(win_prob, 3),
            "kalshi_yes_price": yes_ask,
            "edge":             edge,
            "is_home_team":     0.5,
        },
    }


def run_predictions() -> list[dict]:
    if not os.path.exists(KALSHI_INPUT):
        print("  predictions: kalshi_latest.json not found, skipping")
        return []

    with open(KALSHI_INPUT, encoding="utf-8") as f:
        kalshi = json.load(f)

    game_markets = kalshi.get("game_markets", [])
    teams = _load_teams()
    recent = _load_recent()
    players = _load_players()

    if not teams:
        print("  predictions: team_stats.json not found or empty, skipping")
        return []

    predictions: list[dict] = []
    winner_fallbacks = 0

    for market in game_markets:
        mtype = market.get("market_type")
        if mtype not in PREDICTABLE_TYPES:
            continue

        features = build_features(market, teams=teams, recent=recent, players=players)

        if features is None:
            if mtype == "winner":
                direct = _direct_winner(market, teams)
                if direct is None:
                    continue
                winner_fallbacks += 1
                snap = direct.pop("features_snapshot", {})
                predictions.append({
                    "ticker":            market.get("ticker"),
                    "event_ticker":      market.get("event_ticker"),
                    "market_type":       mtype,
                    "title":             market.get("title"),
                    "yes_team":          market.get("yes_team"),
                    "no_team":           market.get("no_team"),
                    "yes_ask":           market.get("yes_ask"),
                    "no_ask":            market.get("no_ask"),
                    "prediction":        direct["prediction"],
                    "confidence":        direct["confidence"],
                    "reasoning":         direct["reasoning"],
                    "method":            direct["method"],
                    "predicted_value":   None,
                    "features_snapshot": snap,
                })
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

    if winner_fallbacks:
        print(f"  winner formula fallback used for {winner_fallbacks} market(s)")
    if predictions:
        p = predictions[0]
        print(f"  sample: [{p['market_type']}] {p['title']} -> "
              f"{p['prediction']} ({p['confidence']:.0f}% conf, method={p['method']})")

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
