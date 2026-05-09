import asyncio
import json
import os
import re as _re
from datetime import datetime, timezone

from features import (
    build_features, _load_teams, _load_recent, _load_players, find_team,
    _load_today_games, _parse_record, _determine_home_away,
)
from model import predict

ODDS_DIR = os.getenv("ODDS_DIR", "odds")
KALSHI_INPUT = os.path.join(ODDS_DIR, "kalshi_latest.json")
PREDICTIONS_OUTPUT = os.path.join(ODDS_DIR, "predictions_latest.json")

PREDICTABLE_TYPES = {"winner", "2h_winner", "spread", "series_spread", "total", "team_total"}

_WINNER_TITLE_RE = _re.compile(r"Will (?:the )?(.+?) win\?", _re.IGNORECASE)


def _direct_winner(market: dict, teams: list[dict]) -> dict | None:
    """Multi-factor winner prediction used when the full feature pipeline can't resolve both teams.

    Six-step formula using all available team_stats.json fields.
    Net rating proxy = pts - opp_pts (e_net_rating is null in live stats feed).
    """
    def _sf(v, d=0.0):
        try:
            return float(v) if v is not None else d
        except (TypeError, ValueError):
            return d

    yes_name = market.get("yes_team") or ""
    no_name  = market.get("no_team") or ""

    if not yes_name:
        hit = _WINNER_TITLE_RE.search(market.get("title") or "")
        yes_name = hit.group(1).strip() if hit else ""

    t1 = find_team(yes_name, teams) if yes_name else None
    if t1:
        print(f"  [winner] '{yes_name}' matched to {t1.get('team_name')} "
              f"(win_pct={t1.get('win_pct')})")
    else:
        print(f"  [winner] WARNING: no team match for '{yes_name}'")
        return None

    # When no_team is missing, infer opponent from today's schedule
    today_games = _load_today_games()
    if not no_name and today_games:
        t1_id   = t1.get("team_id")
        t1_abbr = (t1.get("abbreviation") or "").upper()
        for game in today_games:
            h_id   = game.get("home_team_id")
            a_id   = game.get("away_team_id")
            h_abbr = (game.get("home_team_abbrev") or "").upper()
            a_abbr = (game.get("away_team_abbrev") or "").upper()
            if (t1_id and t1_id == h_id) or (t1_abbr and t1_abbr == h_abbr):
                no_name = game.get("away_team_name") or a_abbr
                break
            if (t1_id and t1_id == a_id) or (t1_abbr and t1_abbr == a_abbr):
                no_name = game.get("home_team_name") or h_abbr
                break
        if no_name:
            print(f"  [winner] opponent inferred from schedule: '{no_name}'")

    t2 = find_team(no_name, teams) if no_name else None
    if t2:
        print(f"  [winner] '{no_name}' matched to {t2.get('team_name')} "
              f"(win_pct={t2.get('win_pct')})")
    elif no_name:
        print(f"  [winner] WARNING: no team match for opponent '{no_name}'")

    # --- Step 1: Base probability from scoring differential (net rating proxy) ---
    t1_pts     = _sf(t1.get("pts"), 110.0)
    t1_opp_pts = _sf(t1.get("opp_pts"), 110.0)
    t2_pts     = _sf(t2.get("pts"), 110.0)     if t2 else 110.0
    t2_opp_pts = _sf(t2.get("opp_pts"), 110.0) if t2 else 110.0

    t1_net_proxy = t1_pts - t1_opp_pts
    t2_net_proxy = t2_pts - t2_opp_pts
    net_diff     = t1_net_proxy - t2_net_proxy
    base_prob    = 0.5 + (net_diff * 0.033)

    # --- Step 2: Win percentage adjustment ---
    t1_win_pct  = _sf(t1.get("win_pct"), 0.5)
    t2_win_pct  = _sf(t2.get("win_pct"), 0.5) if t2 else 0.5
    win_pct_adj = (t1_win_pct - t2_win_pct) * 0.15

    # --- Step 3: Recent form (last 10 games) ---
    t1_last10  = _parse_record(t1.get("last_10"), 0.5)
    t2_last10  = _parse_record(t2.get("last_10"), 0.5) if t2 else 0.5
    form_adj   = (t1_last10 - t2_last10) * 0.10

    # --- Step 4: Home court advantage ---
    if t2:
        home_val = _determine_home_away(t1, t2, today_games)
    else:
        home_val = 0.5
    home_adj = 0.03 if home_val == 1.0 else (-0.03 if home_val == 0.0 else 0.0)

    # --- Step 5: Scoring offense differential ---
    scoring_adj = (t1_pts - t2_pts) * 0.008

    # --- Step 6: Defensive efficiency differential ---
    def_adj = (t2_opp_pts - t1_opp_pts) * 0.006

    final_prob = max(0.05, min(0.95,
        base_prob + win_pct_adj + form_adj + home_adj + scoring_adj + def_adj
    ))

    yes_ask = _sf(market.get("yes_ask"), 0.5)
    edge    = round(final_prob - yes_ask, 3)

    t1_label = t1.get("team_name") or yes_name
    t2_label = (t2.get("team_name") if t2 else None) or no_name or "opponent"
    venue    = "Home" if home_val == 1.0 else ("Away" if home_val == 0.0 else "Neutral")
    print(
        f"  [winner] {t1_label} vs {t2_label} ({venue}): "
        f"net_diff={net_diff:+.1f} base={base_prob:.3f} "
        f"win_pct={win_pct_adj:+.3f} form={form_adj:+.3f} "
        f"home={home_adj:+.3f} scoring={scoring_adj:+.3f} def={def_adj:+.3f} "
        f"final={final_prob:.3f} edge={edge:+.3f}"
    )

    prediction = "YES" if final_prob >= 0.5 else "NO"
    raw_conf   = final_prob if prediction == "YES" else (1.0 - final_prob)
    confidence = round(min(95.0, max(51.0, raw_conf * 100)), 1)

    reasoning = (
        f"Scoring diff {net_diff:+.1f} pts/g | "
        f"Win% diff {t1_win_pct - t2_win_pct:+.1%} | "
        f"Form diff {t1_last10 - t2_last10:+.1%} | "
        f"{venue} ({home_adj:+.2f}) → {final_prob:.1%} win prob"
    )

    return {
        "prediction": prediction,
        "confidence": confidence,
        "reasoning":  reasoning,
        "method":     "formula",
        "features_snapshot": {
            "t1_net_proxy":     round(t1_net_proxy, 2),
            "t2_net_proxy":     round(t2_net_proxy, 2),
            "net_rtg_diff":     round(net_diff, 2),
            "win_pct_diff":     round(t1_win_pct - t2_win_pct, 3),
            "t1_last10":        round(t1_last10, 3),
            "t2_last10":        round(t2_last10, 3),
            "form_diff":        round(t1_last10 - t2_last10, 3),
            "home_adj":         home_adj,
            "is_home_team":     home_val,
            "our_win_prob":     round(final_prob, 3),
            "kalshi_yes_price": yes_ask,
            "edge":             edge,
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
