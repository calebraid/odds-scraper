import json
import os
import traceback
from datetime import datetime, timezone, timedelta
from typing import Annotated

from fastapi import FastAPI, HTTPException, Security, Depends, Response
from fastapi.security import APIKeyHeader
from fastapi.responses import JSONResponse, HTMLResponse

app = FastAPI(title="Sports Odds API", version="2.0.0")

_BASE = os.path.dirname(os.path.abspath(__file__))
ODDS_DIR = os.path.join(_BASE, "odds")
STATS_DIR = os.path.join(_BASE, "stats")

RATE_LIMITS: dict[str, int] = {
    "free": 100,
    "pro": 10_000,
}

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

_usage: dict[str, tuple[str, int]] = {}


def _seconds_until_midnight_utc() -> int:
    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - now).total_seconds())


def _midnight_utc_timestamp() -> int:
    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _set_rate_limit_headers(response: Response, auth: dict) -> None:
    response.headers["X-RateLimit-Limit"] = str(auth["limit"])
    response.headers["X-RateLimit-Remaining"] = str(max(0, auth["limit"] - auth["usage"]))
    response.headers["X-RateLimit-Reset"] = str(_midnight_utc_timestamp())


def load_keys() -> dict:
    raw = os.environ.get("API_KEYS_JSON")
    if not raw:
        raise HTTPException(status_code=500, detail="API_KEYS_JSON environment variable not set.")
    return json.loads(raw)


def authenticate(raw_key: Annotated[str, Security(api_key_header)]) -> dict:
    keys = load_keys()
    if raw_key not in keys:
        raise HTTPException(status_code=401, detail="Invalid API key.")

    meta = keys[raw_key]
    tier = meta.get("tier", "free")
    limit = RATE_LIMITS.get(tier, RATE_LIMITS["free"])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date, count = _usage.get(raw_key, (today, 0))
    if date != today:
        count = 0
    count += 1
    _usage[raw_key] = (today, count)

    if count > limit:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {limit} requests/day for '{tier}' tier.",
            headers={"Retry-After": str(_seconds_until_midnight_utc())},
        )

    return {"tier": tier, "usage": count, "limit": limit}


def load_stats(filename: str) -> dict:
    path = os.path.join(STATS_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(
            status_code=503,
            detail="Stats data unavailable — scraper has not produced output yet.",
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_odds(filename: str) -> dict:
    path = os.path.join(ODDS_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(
            status_code=503,
            detail="Odds data unavailable — scraper has not produced output yet.",
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def staleness_warning(scraped_at: str | None) -> str | None:
    if not scraped_at:
        return None
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(scraped_at)
        if age.total_seconds() > 300:
            return f"Data is {int(age.total_seconds())}s old — scraper may be down."
    except ValueError:
        pass
    return None


# ── routes ───────────────────────────────────────────────────────────────────

@app.get("/health", summary="Health check (no auth required)")
def health():
    return {"status": "ok"}


@app.get("/usage", summary="Your current rate-limit usage")
def get_usage(auth: Annotated[dict, Depends(authenticate)], response: Response):
    _set_rate_limit_headers(response, auth)
    return {
        "tier": auth["tier"],
        "usage": auth["usage"],
        "limit": auth["limit"],
        "remaining": max(0, auth["limit"] - auth["usage"]),
        "resets_at": _midnight_utc_timestamp(),
    }


@app.get("/odds/nba", summary="NBA game lines")
def get_nba_odds(auth: Annotated[dict, Depends(authenticate)], response: Response):
    _set_rate_limit_headers(response, auth)
    data = load_odds("latest.json")
    warning = staleness_warning(data.get("scraped_at"))
    if warning:
        data["warning"] = warning
    return JSONResponse(content=data, headers=dict(response.headers))


@app.get("/odds/nba/props", summary="NBA player props")
def get_nba_props(auth: Annotated[dict, Depends(authenticate)], response: Response):
    _set_rate_limit_headers(response, auth)
    data = load_odds("player_props_latest.json")
    warning = staleness_warning(data.get("scraped_at"))
    result = {
        "source": data.get("source"),
        "league": data.get("league"),
        "scraped_at": data.get("scraped_at"),
        "count": data.get("count", len(data.get("props", []))),
        "props": data.get("props", []),
    }
    if warning:
        result["warning"] = warning
    return JSONResponse(content=result, headers=dict(response.headers))


@app.get("/odds/nba/kalshi", summary="NBA prediction markets from Kalshi")
def get_nba_kalshi(auth: Annotated[dict, Depends(authenticate)], response: Response):
    _set_rate_limit_headers(response, auth)
    data = load_odds("kalshi_latest.json")
    warning = staleness_warning(data.get("scraped_at"))
    result = {
        "source": data.get("source"),
        "league": data.get("league"),
        "scraped_at": data.get("scraped_at"),
        "futures_count": data.get("futures_count", len(data.get("futures", []))),
        "game_markets_count": data.get("game_markets_count", len(data.get("game_markets", []))),
        "futures": data.get("futures", []),
        "game_markets": data.get("game_markets", []),
    }
    if warning:
        result["warning"] = warning
    return JSONResponse(content=result, headers=dict(response.headers))


@app.get("/stats/players", summary="NBA player season per-game averages")
def get_player_stats(auth: Annotated[dict, Depends(authenticate)], response: Response):
    _set_rate_limit_headers(response, auth)
    data = load_stats("player_stats.json")
    warning = staleness_warning(data.get("scraped_at"))
    result = {
        "scraped_at": data.get("scraped_at"),
        "season": data.get("season"),
        "count": data.get("count", len(data.get("players", []))),
        "players": data.get("players", []),
    }
    if warning:
        result["warning"] = warning
    return JSONResponse(content=result, headers=dict(response.headers))


@app.get("/predictions", summary="Latest AI predictions for Kalshi markets")
def get_predictions(auth: Annotated[dict, Depends(authenticate)], response: Response):
    _set_rate_limit_headers(response, auth)
    data = load_odds("predictions_latest.json")
    warning = staleness_warning(data.get("generated_at"))
    result = {
        "generated_at": data.get("generated_at"),
        "count": data.get("count", len(data.get("predictions", []))),
        "by_type": data.get("by_type", {}),
        "predictions": data.get("predictions", []),
    }
    if warning:
        result["warning"] = warning
    return JSONResponse(content=result, headers=dict(response.headers))


@app.get("/predictions/accuracy", summary="Prediction model accuracy stats")
def get_predictions_accuracy(auth: Annotated[dict, Depends(authenticate)], response: Response):
    _set_rate_limit_headers(response, auth)
    path = os.path.join(STATS_DIR, "prediction_history.json")
    if not os.path.exists(path):
        return JSONResponse(
            content={"total_predictions": 0, "resolved_predictions": 0, "correct": 0, "accuracy_pct": None, "edge_accuracy_pct": None, "by_type": {}},
            headers=dict(response.headers),
        )
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    result = {
        "updated_at": data.get("updated_at"),
        "total_predictions": data.get("total_predictions", 0),
        "resolved_predictions": data.get("resolved_predictions", 0),
        "correct": data.get("correct", 0),
        "accuracy_pct": data.get("accuracy_pct"),
        "edge_accuracy_pct": data.get("edge_accuracy_pct"),
        "positive_edge_total": data.get("positive_edge_total", 0),
        "positive_edge_correct": data.get("positive_edge_correct", 0),
        "by_type": data.get("by_type", {}),
    }
    return JSONResponse(content=result, headers=dict(response.headers))


@app.get("/model/features", summary="Feature importance from the latest ML training run")
def get_model_features(auth: Annotated[dict, Depends(authenticate)], response: Response):
    _set_rate_limit_headers(response, auth)
    path = os.path.join(STATS_DIR, "feature_importance.json")
    if not os.path.exists(path):
        raise HTTPException(
            status_code=503,
            detail="Feature importance unavailable — model has not been trained yet.",
        )
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return JSONResponse(content=data, headers=dict(response.headers))


@app.get("/predictions/edge", summary="High-value predictions where our probability differs from Kalshi by >10%")
def get_predictions_edge(auth: Annotated[dict, Depends(authenticate)], response: Response):
    _set_rate_limit_headers(response, auth)
    data = load_odds("predictions_latest.json")
    warning = staleness_warning(data.get("generated_at"))

    edge_predictions = []
    for p in data.get("predictions", []):
        features = p.get("features_snapshot") or {}
        edge = features.get("edge")

        if edge is None:
            # Derive from confidence and direction when features snapshot lacks edge
            conf = (p.get("confidence") or 50) / 100
            yes_ask = float(p.get("yes_ask") or 0.5)
            our_prob = conf if p.get("prediction") == "YES" else (1.0 - conf)
            edge = our_prob - yes_ask

        if abs(edge) > 0.10:
            yes_ask = float(p.get("yes_ask") or 0.5)
            edge_predictions.append({
                "ticker": p.get("ticker"),
                "event_ticker": p.get("event_ticker"),
                "market_type": p.get("market_type"),
                "title": p.get("title"),
                "yes_team": p.get("yes_team"),
                "no_team": p.get("no_team"),
                "prediction": p.get("prediction"),
                "confidence": p.get("confidence"),
                "method": p.get("method"),
                "yes_ask": p.get("yes_ask"),
                "no_ask": p.get("no_ask"),
                "our_win_prob": features.get("our_win_prob"),
                "kalshi_implied_pct": round(yes_ask * 100, 1),
                "edge": round(float(edge), 3),
                "edge_pct": round(float(edge) * 100, 1),
            })

    edge_predictions.sort(key=lambda p: abs(p.get("edge") or 0), reverse=True)

    result = {
        "generated_at": data.get("generated_at"),
        "count": len(edge_predictions),
        "threshold_pct": 10,
    }
    if warning:
        result["warning"] = warning
    result["predictions"] = edge_predictions
    return JSONResponse(content=result, headers=dict(response.headers))


@app.get("/debug/markets", summary="Raw kalshi_latest.json grouped by market type (no auth)")
def debug_markets():
    path = os.path.join(ODDS_DIR, "kalshi_latest.json")
    if not os.path.exists(path):
        return JSONResponse(content={"error": "kalshi_latest.json not found — scraper has not run yet"})
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        game_markets = data.get("game_markets", [])
        by_type: dict[str, list] = {}
        for m in game_markets:
            mt = m.get("market_type", "unknown")
            by_type.setdefault(mt, []).append({
                "ticker":    m.get("ticker"),
                "title":     m.get("title"),
                "yes_team":  m.get("yes_team"),
                "no_team":   m.get("no_team"),
                "yes_ask":   m.get("yes_ask"),
                "status":    m.get("status"),
            })
        return JSONResponse(content={
            "scraped_at":         data.get("scraped_at"),
            "futures_count":      data.get("futures_count", 0),
            "game_markets_total": len(game_markets),
            "type_counts":        {mt: len(mkts) for mt, mkts in sorted(by_type.items())},
            "by_type": {
                mt: {"count": len(mkts), "sample": mkts[:3]}
                for mt, mkts in sorted(by_type.items())
            },
        })
    except Exception as exc:
        traceback.print_exc()
        return JSONResponse(content={"error": str(exc)}, status_code=500)


@app.get("/api/predictions", summary="Raw predictions for the dashboard (no auth required)")
def api_predictions_raw():
    _empty = {
        "predictions": [],
        "accuracy": {},
        "system": {"status": "starting", "cache_size": 0, "last_scrape": None, "proxy_configured": False},
        "count": 0,
        "by_type": {},
        "generated_at": None,
    }
    try:
        result: dict = {
            "generated_at": None,
            "count": 0,
            "by_type": {},
            "predictions": [],
            "accuracy": {},
            "system": {
                "cache_size": 0,
                "last_scrape": None,
                "proxy_configured": bool(os.environ.get("SCRAPER_PROXY")),
            },
        }

        pred_path = os.path.join(ODDS_DIR, "predictions_latest.json")
        if os.path.exists(pred_path):
            try:
                with open(pred_path, encoding="utf-8") as f:
                    pred_data = json.load(f)
                result["generated_at"] = pred_data.get("generated_at")
                result["count"] = pred_data.get("count", 0)
                result["by_type"] = pred_data.get("by_type", {})
                for p in pred_data.get("predictions", []):
                    features = p.get("features_snapshot") or {}
                    result["predictions"].append({
                        "ticker": p.get("ticker"),
                        "event_ticker": p.get("event_ticker"),
                        "market_type": p.get("market_type"),
                        "title": p.get("title"),
                        "yes_team": p.get("yes_team"),
                        "no_team": p.get("no_team"),
                        "yes_ask": p.get("yes_ask"),
                        "no_ask": p.get("no_ask"),
                        "prediction": p.get("prediction"),
                        "confidence": p.get("confidence"),
                        "method": p.get("method"),
                        "predicted_value": p.get("predicted_value"),
                        "edge": features.get("edge"),
                        "our_win_prob": features.get("our_win_prob"),
                    })
                result["predictions"].sort(
                    key=lambda p: float(p.get("confidence") or 0), reverse=True
                )
            except Exception as exc:
                print(f"  /api/predictions: failed to read predictions_latest.json: {exc}")

        hist_path = os.path.join(STATS_DIR, "prediction_history.json")
        if os.path.exists(hist_path):
            try:
                with open(hist_path, encoding="utf-8") as f:
                    hist = json.load(f)
                result["accuracy"] = {
                    "accuracy_pct": hist.get("accuracy_pct"),
                    "total": hist.get("resolved_predictions", 0),
                    "correct": hist.get("correct", 0),
                    "edge_accuracy_pct": hist.get("edge_accuracy_pct"),
                    "positive_edge_total": hist.get("positive_edge_total", 0),
                }
            except Exception as exc:
                print(f"  /api/predictions: failed to read prediction_history.json: {exc}")

        cache_path = os.path.join(STATS_DIR, "boxscore_cache.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as f:
                    cache = json.load(f)
                result["system"]["cache_size"] = len(cache)
            except Exception as exc:
                print(f"  /api/predictions: failed to read boxscore_cache.json: {exc}")

        today_path = os.path.join(STATS_DIR, "today_games.json")
        if os.path.exists(today_path):
            try:
                with open(today_path, encoding="utf-8") as f:
                    today_data = json.load(f)
                if isinstance(today_data, list) and today_data:
                    result["system"]["last_scrape"] = today_data[0].get("updated_at")
            except Exception as exc:
                print(f"  /api/predictions: failed to read today_games.json: {exc}")

        return JSONResponse(content=result)

    except Exception as exc:
        traceback.print_exc()
        err = _empty.copy()
        err["error"] = str(exc)
        return JSONResponse(content=err)


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return HTMLResponse(content=_DASHBOARD_HTML)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing_page():
    try:
        return HTMLResponse(content=_PREDICTIONS_DASHBOARD_HTML)
    except Exception as exc:
        traceback.print_exc()
        return HTMLResponse(
            content=f"<h1>Dashboard Error</h1><pre>{exc}</pre>",
            status_code=500,
        )


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>OddsAPI — Live Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0a;--card:#111111;--gold:#FFD700;--gold-dim:rgba(255,215,0,.1);--gold-glow:rgba(255,215,0,.07);--text:#fff;--dim:#666;--border:rgba(255,215,0,.12);--border-h:rgba(255,215,0,.5);--pos:#00c853;--neg:#ff4444}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:'Inter',-apple-system,sans-serif;min-height:100vh}

nav{position:sticky;top:0;z-index:100;display:flex;align-items:center;justify-content:space-between;padding:.875rem 2rem;background:rgba(10,10,10,.92);backdrop-filter:blur(20px);border-bottom:1px solid var(--border)}
.logo{font-size:1.1rem;font-weight:900;color:var(--gold);text-decoration:none;letter-spacing:-.5px}
.nav-links{display:flex;gap:2rem}
.nav-links a{color:var(--dim);text-decoration:none;font-size:.875rem;font-weight:500;transition:color .2s}
.nav-links a:hover{color:var(--text)}
.nav-links a.active{color:var(--gold)}
.nav-status{display:flex;align-items:center;gap:.5rem;font-size:.75rem;font-family:'JetBrains Mono',monospace;color:var(--dim)}
.live-dot{width:6px;height:6px;background:var(--pos);border-radius:50%;animation:blink 2s infinite;flex-shrink:0}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

main{max-width:1400px;margin:0 auto;padding:2.5rem 2rem 5rem;display:flex;flex-direction:column;gap:3.5rem}

.sec-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:1.25rem;gap:1rem}
.sec-title{font-size:1.25rem;font-weight:800;letter-spacing:-.5px}
.sec-title em{font-style:normal;color:var(--gold)}
.src-badge{background:var(--gold-dim);border:1px solid var(--border);color:var(--gold);font-size:.65rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;padding:.2rem .65rem;border-radius:4px;white-space:nowrap}

.cards-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:1.25rem}

.game-card{background:rgba(17,17,17,.9);border:1px solid var(--border);border-radius:16px;padding:1.375rem 1.5rem;backdrop-filter:blur(10px);transition:transform .25s,border-color .25s,box-shadow .25s}
.game-card:hover{transform:translateY(-3px);border-color:var(--border-h);box-shadow:0 0 40px var(--gold-glow),0 20px 40px rgba(0,0,0,.5)}
.game-card-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:1.125rem;gap:.75rem}
.matchup{font-size:1rem;font-weight:700;line-height:1.3}
.g-badge{font-size:.68rem;font-weight:700;letter-spacing:.5px;padding:.2rem .6rem;border-radius:4px;white-space:nowrap;flex-shrink:0}
.b-live{background:rgba(255,68,68,.15);color:#ff6060;border:1px solid rgba(255,68,68,.3)}
.b-up{background:rgba(255,255,255,.05);color:var(--dim);border:1px solid rgba(255,255,255,.08)}
.odds-cols{display:grid;grid-template-columns:repeat(3,1fr);gap:.75rem}
.ocol-lbl{font-size:.62rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--gold);margin-bottom:.5rem}
.orow{display:flex;justify-content:space-between;align-items:center;padding:.3rem 0}
.orow+.orow{border-top:1px solid rgba(255,255,255,.05)}
.side{font-size:.72rem;color:var(--dim);font-weight:500}
.oval{font-size:.82rem;font-weight:700;font-family:'JetBrains Mono',monospace}
.lval{font-size:.78rem;color:var(--text);font-family:'JetBrains Mono',monospace;margin-right:.25rem}
.pos{color:var(--pos)}
.neg{color:var(--neg)}

.props-game{margin-bottom:1.75rem}
.props-game-title{font-size:.875rem;font-weight:700;color:var(--gold);margin-bottom:.75rem;padding-bottom:.5rem;border-bottom:1px solid var(--border)}
.props-stat{margin-bottom:1rem}
.props-stat-lbl{font-size:.65rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim);margin-bottom:.5rem}
.props-tbl{background:rgba(17,17,17,.9);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.prow{display:grid;grid-template-columns:1fr 60px 90px 90px;gap:.75rem;align-items:center;padding:.6rem 1rem;font-size:.82rem;transition:background .15s}
.prow:hover{background:rgba(255,215,0,.03)}
.prow+.prow{border-top:1px solid rgba(255,255,255,.04)}
.pname{font-weight:600}
.pline{font-family:'JetBrains Mono',monospace;color:var(--dim);text-align:right}
.plbl{font-size:.65rem;color:var(--dim);margin-left:.25rem}

.kalshi-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:1rem}
.k-card{background:rgba(17,17,17,.9);border:1px solid var(--border);border-radius:16px;padding:1.375rem;backdrop-filter:blur(10px);transition:transform .25s,border-color .25s,box-shadow .25s;display:flex;flex-direction:column;gap:.875rem}
.k-card:hover{transform:translateY(-3px);border-color:var(--border-h);box-shadow:0 0 40px var(--gold-glow),0 20px 40px rgba(0,0,0,.5)}
.k-team{font-size:.95rem;font-weight:800;letter-spacing:-.3px}
.k-pct{font-size:2.5rem;font-weight:900;color:var(--gold);letter-spacing:-2px;line-height:1}
.k-pct sup{font-size:1rem;font-weight:500;color:rgba(255,215,0,.6);letter-spacing:0}
.bar-track{height:6px;background:rgba(255,255,255,.06);border-radius:3px;overflow:hidden;margin-top:.375rem}
.bar-fill{height:100%;background:linear-gradient(90deg,var(--gold),#ffb300);border-radius:3px;transition:width .6s ease}
.k-stats{display:grid;grid-template-columns:1fr 1fr;gap:.5rem}
.k-stat{background:rgba(255,255,255,.03);border-radius:8px;padding:.5rem .625rem}
.k-stat-full{grid-column:1/-1}
.k-mrows{padding:.125rem 0}
.k-mrow{display:grid;grid-template-columns:100px 1fr 1fr;gap:.5rem;padding:.35rem .75rem;align-items:center;font-size:.82rem}
.k-mrow+.k-mrow{border-top:1px solid rgba(255,255,255,.04)}
.k-mlbl{font-size:.63rem;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--gold);opacity:.75}
.k-mside{display:flex;align-items:center;gap:.35rem}
.k-msublbl{font-size:.7rem;color:var(--dim);max-width:80px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.kprop-grid{display:flex;flex-direction:column;gap:.75rem}
.kprop-group{background:rgba(17,17,17,.9);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.kprop-ghdr{padding:.6rem 1rem;background:rgba(255,215,0,.05);border-bottom:1px solid var(--border);font-size:.68rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--gold)}
.kprop-row{display:grid;grid-template-columns:1fr auto auto;gap:1rem;padding:.55rem 1rem;align-items:center;font-size:.82rem;transition:background .15s}
.kprop-row:hover{background:rgba(255,215,0,.03)}
.kprop-row+.kprop-row{border-top:1px solid rgba(255,255,255,.04)}
.kprop-title{font-weight:500;line-height:1.3}
.kprop-price{display:flex;align-items:center;gap:.3rem;font-family:'JetBrains Mono',monospace}
.kprop-lbl{font-size:.65rem;color:var(--dim)}
.subsec-hdr{font-size:.7rem;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--gold);opacity:.7;margin-bottom:.75rem}
.k-slbl{font-size:.6rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim);margin-bottom:.2rem}
.k-sval{font-size:.85rem;font-weight:700;font-family:'JetBrains Mono',monospace}

.empty{grid-column:1/-1;background:rgba(17,17,17,.6);border:1px dashed rgba(255,255,255,.08);border-radius:12px;padding:2.5rem;text-align:center;color:var(--dim);font-size:.875rem}
.empty-icon{font-size:2rem;margin-bottom:.75rem;opacity:.4}
.loading{grid-column:1/-1;display:flex;gap:.75rem;justify-content:center;padding:2rem;color:var(--dim);font-size:.875rem;align-items:center}
.spinner{width:16px;height:16px;border:2px solid rgba(255,215,0,.2);border-top-color:var(--gold);border-radius:50%;animation:spin .7s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}

@media(max-width:900px){.cards-grid{grid-template-columns:1fr}.odds-cols{gap:.5rem}}
@media(max-width:768px){.nav-links{display:none}.kalshi-grid{grid-template-columns:repeat(2,1fr)}main{padding:1.5rem 1rem 3rem}}
@media(max-width:480px){.kalshi-grid{grid-template-columns:1fr}.prow{grid-template-columns:1fr 50px 70px 70px;gap:.5rem;font-size:.78rem}}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:#222;border-radius:3px}
</style>
</head>
<body>

<nav>
  <a href="/" class="logo">&#9889; OddsAPI</a>
  <div class="nav-links">
    <a href="/dashboard" class="active">Dashboard</a>
    <a href="/docs">Docs</a>
    <a href="/">Home</a>
  </div>
  <div class="nav-status">
    <span class="live-dot"></span>
    <span id="ts">Loading&hellip;</span>
  </div>
</nav>

<main>
  <section>
    <div class="sec-hdr">
      <h2 class="sec-title">Game <em>Lines</em></h2>
      <span class="src-badge">DraftKings</span>
    </div>
    <div class="cards-grid" id="gl-grid">
      <div class="loading"><div class="spinner"></div>Loading game lines&hellip;</div>
    </div>
  </section>

  <section>
    <div class="sec-hdr">
      <h2 class="sec-title">Player <em>Props</em></h2>
      <span class="src-badge">The Odds API</span>
    </div>
    <div id="props-wrap">
      <div class="loading"><div class="spinner"></div>Loading player props&hellip;</div>
    </div>
  </section>

  <section>
    <div class="sec-hdr">
      <h2 class="sec-title">Kalshi <em>Markets</em></h2>
      <span class="src-badge">Kalshi</span>
    </div>
    <div class="subsec-hdr">Game Lines</div>
    <div class="cards-grid" id="k-game-grid" style="margin-bottom:2rem">
      <div class="loading"><div class="spinner"></div>Loading game markets&hellip;</div>
    </div>
    <div class="subsec-hdr">Player Props</div>
    <div id="k-props-wrap" style="margin-bottom:2rem">
      <div class="loading"><div class="spinner"></div>Loading player props&hellip;</div>
    </div>
    <div class="subsec-hdr">NBA Finals Futures</div>
    <div class="kalshi-grid" id="k-grid">
      <div class="loading"><div class="spinner"></div>Loading futures&hellip;</div>
    </div>
  </section>
</main>

<script>
const KEY = 'free-demo-key-abc123';
let lastFetch = null;

const fmtOdds = n => n == null ? '&mdash;' : n > 0 ? '+' + n : String(n);
const oc = n => n == null ? '' : n > 0 ? 'pos' : 'neg';
const pct = v => Math.round(parseFloat(v || 0) * 100);
const fmtVol = v => {
  const n = parseFloat(v);
  if (isNaN(n) || n === 0) return '&mdash;';
  if (n >= 1e6) return '$' + (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return '$' + (n / 1e3).toFixed(1) + 'K';
  return '$' + n.toFixed(0);
};

async function get(path) {
  const r = await fetch(path, {headers: {'X-API-Key': KEY}});
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

function renderGL(data) {
  const el = document.getElementById('gl-grid');
  if (!data?.games) {
    el.innerHTML = '<div class="empty"><div class="empty-icon">&#127936;</div>Game lines unavailable &mdash; data source not active.</div>';
    return;
  }
  if (!data.games.length) {
    el.innerHTML = '<div class="empty"><div class="empty-icon">&#127936;</div>No games scheduled right now.</div>';
    return;
  }
  const orow = (side, line, odds) =>
    `<div class="orow"><span class="side">${side}</span><span>${line != null ? `<span class="lval">${line}</span>` : ''}<span class="oval ${oc(odds)}">${fmtOdds(odds)}</span></span></div>`;
  const ocol = (lbl, rows) =>
    `<div><div class="ocol-lbl">${lbl}</div>${rows}</div>`;
  el.innerHTML = data.games.map(g => {
    const live = g.score != null;
    const badge = live
      ? `<span class="g-badge b-live">LIVE ${g.score.away}&ndash;${g.score.home}</span>`
      : `<span class="g-badge b-up">${g.game_time || 'Upcoming'}</span>`;
    return `<div class="game-card">
  <div class="game-card-hdr"><span class="matchup">${g.matchup}</span>${badge}</div>
  <div class="odds-cols">
    ${ocol('Spread', orow('Away', g.spread?.away?.line, g.spread?.away?.odds) + orow('Home', g.spread?.home?.line, g.spread?.home?.odds))}
    ${ocol('Moneyline', orow('Away', null, g.moneyline?.away) + orow('Home', null, g.moneyline?.home))}
    ${ocol('Total', orow('Over', g.total?.over?.line, g.total?.over?.odds) + orow('Under', g.total?.under?.line, g.total?.under?.odds))}
  </div>
</div>`;
  }).join('');
}

function renderProps(data) {
  const el = document.getElementById('props-wrap');
  if (!data?.props) {
    el.innerHTML = '<div class="empty"><div class="empty-icon">&#128202;</div>Player props unavailable &mdash; data source not active.</div>';
    return;
  }
  if (!data.props.length) {
    el.innerHTML = '<div class="empty"><div class="empty-icon">&#128202;</div>No player props available right now.</div>';
    return;
  }
  const byGame = {};
  for (const p of data.props) {
    const g = p.matchup || 'Unknown', s = p.stat_type || 'Other';
    ((byGame[g] = byGame[g] || {})[s] = byGame[g]?.[s] || []).push(p);
  }
  el.innerHTML = Object.entries(byGame).map(([game, stats]) =>
    `<div class="props-game">
  <div class="props-game-title">${game}</div>
  ${Object.entries(stats).map(([stat, players]) =>
    `<div class="props-stat">
  <div class="props-stat-lbl">${stat}</div>
  <div class="props-tbl">
    ${players.map(p => `<div class="prow">
      <span class="pname">${p.player}</span>
      <span class="pline">${p.line ?? '&mdash;'}</span>
      <span class="oval pos">${p.over_odds != null ? fmtOdds(p.over_odds) : '&mdash;'}<span class="plbl">O</span></span>
      <span class="oval neg">${p.under_odds != null ? fmtOdds(p.under_odds) : '&mdash;'}<span class="plbl">U</span></span>
    </div>`).join('')}
  </div>
</div>`).join('')}
</div>`).join('');
}

const GAME_MTYPES = {winner:'Winner', total:'Total', spread:'Spread', team_total:'Team Total', series_spread:'Series Spread', '2h_winner':'2H Winner'};
const PROP_MTYPES  = {reb_assists:'Reb + Assists', blocks:'Blocks', steals:'Steals', triple_double:'Triple Double'};

function renderKalshiGames(gameMarkets) {
  const el = document.getElementById('k-game-grid');
  const gmkts = (gameMarkets || []).filter(m => GAME_MTYPES[m.market_type]);
  if (!gmkts.length) {
    el.innerHTML = '<div class="empty"><div class="empty-icon">&#127936;</div>No game markets available right now.</div>';
    return;
  }
  const byEvent = {};
  for (const m of gmkts) {
    (byEvent[m.event_ticker] = byEvent[m.event_ticker] || {})[m.market_type] = m;
  }
  el.innerHTML = Object.values(byEvent).map(ev => {
    const ref = ev.winner || Object.values(ev)[0];
    const ct = ref?.close_time;
    const ctStr = ct ? new Date(ct).toLocaleString('en-US', {timeZone:'America/New_York', month:'short', day:'numeric', hour:'numeric', minute:'2-digit'}) : '';
    const rows = Object.keys(GAME_MTYPES).filter(t => ev[t]).map(t => {
      const m = ev[t];
      return `<div class="k-mrow">
  <span class="k-mlbl">${GAME_MTYPES[t]}</span>
  <span class="k-mside">${m.yes_team ? `<span class="k-msublbl">${m.yes_team}</span>` : ''}<span class="oval pos">${pct(m.yes_ask)}&cent;</span></span>
  <span class="k-mside">${m.no_team ? `<span class="k-msublbl">${m.no_team}</span>` : ''}<span class="oval neg">${pct(m.no_ask)}&cent;</span></span>
</div>`;
    }).join('');
    return `<div class="game-card">
  <div class="game-card-hdr">
    <span class="matchup">${ev.winner ? `${ev.winner.yes_team} vs ${ev.winner.no_team}` : (ref?.title || `${ref?.yes_team || '?'} vs ${ref?.no_team || '?'}`)}</span>
    ${ctStr ? `<span class="g-badge b-up">${ctStr} ET</span>` : ''}
  </div>
  <div class="k-mrows">${rows}</div>
</div>`;
  }).join('');
}

function renderKalshiProps(gameMarkets) {
  const el = document.getElementById('k-props-wrap');
  const pmkts = (gameMarkets || []).filter(m => PROP_MTYPES[m.market_type]);
  if (!pmkts.length) {
    el.innerHTML = '<div class="empty"><div class="empty-icon">&#128202;</div>No player prop markets available right now.</div>';
    return;
  }
  const byType = {};
  for (const m of pmkts) {
    (byType[m.market_type] = byType[m.market_type] || []).push(m);
  }
  el.innerHTML = `<div class="kprop-grid">${Object.entries(byType).map(([type, mkts]) =>
    `<div class="kprop-group">
  <div class="kprop-ghdr">${PROP_MTYPES[type]}</div>
  ${mkts.map(m => `<div class="kprop-row">
  <span class="kprop-title">${m.title || m.ticker}</span>
  <span class="kprop-price"><span class="kprop-lbl">Yes</span><span class="oval pos">${pct(m.yes_ask)}&cent;</span></span>
  <span class="kprop-price"><span class="kprop-lbl">No</span><span class="oval neg">${pct(m.no_ask)}&cent;</span></span>
</div>`).join('')}
</div>`).join('')}</div>`;
}

function renderKalshiFutures(futures) {
  const el = document.getElementById('k-grid');
  if (!futures?.length) {
    el.innerHTML = '<div class="empty"><div class="empty-icon">&#127942;</div>No futures markets available.</div>';
    return;
  }
  const mkts = [...futures].sort((a, b) => parseFloat(b.yes_ask || 0) - parseFloat(a.yes_ask || 0));
  el.innerHTML = mkts.map(m => {
    const y = pct(m.yes_ask), n = pct(m.no_ask);
    return `<div class="k-card">
  <div class="k-team">${m.yes_team || m.ticker}</div>
  <div>
    <div class="k-pct">${y}<sup>&cent;</sup></div>
    <div class="bar-track"><div class="bar-fill" style="width:${Math.min(y, 100)}%"></div></div>
  </div>
  <div class="k-stats">
    <div class="k-stat"><div class="k-slbl">Yes Ask</div><div class="k-sval pos">${y}&cent;</div></div>
    <div class="k-stat"><div class="k-slbl">No Ask</div><div class="k-sval neg">${n}&cent;</div></div>
    <div class="k-stat k-stat-full"><div class="k-slbl">Volume</div><div class="k-sval">${fmtVol(m.volume)}</div></div>
  </div>
</div>`;
  }).join('');
}

async function fetchAll() {
  const [gl, props, kalshi] = await Promise.allSettled([
    get('/odds/nba'),
    get('/odds/nba/props'),
    get('/odds/nba/kalshi'),
  ]);
  renderGL(gl.status === 'fulfilled' ? gl.value : null);
  renderProps(props.status === 'fulfilled' ? props.value : null);
  const kd = kalshi.status === 'fulfilled' ? kalshi.value : null;
  renderKalshiGames(kd?.game_markets ?? null);
  renderKalshiProps(kd?.game_markets ?? null);
  renderKalshiFutures(kd?.futures ?? null);
  lastFetch = Date.now();
}

setInterval(() => {
  if (!lastFetch) return;
  const s = Math.floor((Date.now() - lastFetch) / 1000);
  document.getElementById('ts').textContent = 'Updated ' + s + 's ago';
}, 1000);

setInterval(fetchAll, 60_000);
fetchAll();
</script>
</body>
</html>"""


_PREDICTIONS_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NBA Prediction Engine</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#080810;--surface:#10101c;--surface2:#16162a;--border:rgba(255,255,255,0.07);--border-h:rgba(255,255,255,0.14);--text:#e8e8f0;--muted:#6b6b7e;--accent:#818cf8;--accent-dim:rgba(129,140,248,0.12);--green:#4ade80;--red:#f87171;--amber:#fbbf24;--row-yes:rgba(74,222,128,0.055);--row-no:rgba(248,113,113,0.055);--row-neutral:rgba(251,191,36,0.045)}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:"Inter",-apple-system,sans-serif;font-size:14px;line-height:1.5;min-height:100vh}

header{background:rgba(8,8,16,0.96);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:50;padding:14px 24px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.header-left h1{font-size:18px;font-weight:800;letter-spacing:-0.5px}
.header-left h1 span{color:var(--accent)}
.header-subtitle{font-size:12px;color:var(--muted);margin-top:2px;font-family:"JetBrains Mono",monospace}
.header-right{display:flex;align-items:center;gap:16px;flex-shrink:0}
.counter-chip{background:var(--accent-dim);border:1px solid rgba(129,140,248,0.25);border-radius:100px;padding:4px 14px;font-size:13px;font-weight:600;color:var(--accent);font-family:"JetBrains Mono",monospace}
.live-badge{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);font-family:"JetBrains Mono",monospace}
.dot-live{width:6px;height:6px;background:var(--green);border-radius:50%;animation:blink 2s infinite;flex-shrink:0}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0.3}}

main{max-width:1440px;margin:0 auto;padding:24px 24px 60px}

.stats-bar{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 20px}
.stat-label{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.stat-value{font-size:28px;font-weight:800;letter-spacing:-1px;line-height:1}
.sv-accent{color:var(--accent)}
.sv-green{color:var(--green)}
.sv-amber{color:var(--amber)}
.stat-sub{font-size:12px;color:var(--muted);margin-top:4px}

.controls-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.filter-tabs{display:flex;gap:6px;flex-wrap:wrap}
.tab-btn{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 13px;font-size:13px;font-weight:500;color:var(--muted);cursor:pointer;transition:all 0.15s;display:inline-flex;align-items:center;gap:6px;white-space:nowrap;font-family:inherit}
.tab-btn:hover{border-color:var(--border-h);color:var(--text)}
.tab-btn.active{background:var(--accent-dim);border-color:rgba(129,140,248,0.4);color:var(--accent)}
.tab-count{background:rgba(255,255,255,0.08);border-radius:100px;padding:1px 7px;font-size:11px;font-weight:700;font-family:"JetBrains Mono",monospace}
.tab-btn.active .tab-count{background:rgba(129,140,248,0.2)}
.next-refresh{font-size:12px;color:var(--muted);font-family:"JetBrains Mono",monospace}

.table-wrap{background:var(--surface);border:1px solid var(--border);border-radius:16px;overflow:hidden;overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:680px}
thead tr{border-bottom:1px solid var(--border)}
th{padding:12px 16px;text-align:left;font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--muted);white-space:nowrap;background:var(--surface2)}
tbody tr{border-bottom:1px solid rgba(255,255,255,0.04);transition:filter 0.12s}
tbody tr:last-child{border-bottom:none}
tbody tr.row-yes{background:var(--row-yes)}
tbody tr.row-no{background:var(--row-no)}
tbody tr.row-neutral{background:var(--row-neutral)}
tbody tr.bet-highlight td:nth-child(6){background:rgba(74,222,128,0.06)}
tbody tr:hover{filter:brightness(1.18)}
td{padding:11px 16px;vertical-align:middle}

.type-badge{display:inline-block;padding:3px 8px;border-radius:5px;font-size:11px;font-weight:700;letter-spacing:0.3px;white-space:nowrap}
.b-winner{background:rgba(129,140,248,0.12);color:var(--accent);border:1px solid rgba(129,140,248,0.22)}
.b-2hw{background:rgba(167,139,250,0.1);color:#a78bfa;border:1px solid rgba(167,139,250,0.2)}
.b-total{background:rgba(251,191,36,0.1);color:var(--amber);border:1px solid rgba(251,191,36,0.2)}
.b-spread{background:rgba(56,189,248,0.1);color:#38bdf8;border:1px solid rgba(56,189,248,0.2)}
.b-other{background:rgba(255,255,255,0.05);color:var(--muted);border:1px solid var(--border)}

.title-cell{max-width:300px}
.title-main{font-weight:500;color:var(--text);line-height:1.35;word-break:break-word}
.title-teams{font-size:11px;color:var(--muted);margin-top:2px}

.pred-pill{display:inline-flex;align-items:center;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:700}
.pred-yes{background:rgba(74,222,128,0.14);color:var(--green);border:1px solid rgba(74,222,128,0.24)}
.pred-no{background:rgba(248,113,113,0.14);color:var(--red);border:1px solid rgba(248,113,113,0.24)}

.conf-wrap{display:flex;align-items:center;gap:8px;min-width:100px}
.conf-bar{flex:1;height:4px;background:rgba(255,255,255,0.08);border-radius:2px;overflow:hidden;max-width:54px}
.conf-fill{height:100%;border-radius:2px}
.conf-pct{font-size:13px;font-weight:700;font-family:"JetBrains Mono",monospace;min-width:36px}

.edge-pos{color:var(--green);font-weight:600;font-family:"JetBrains Mono",monospace;font-size:13px}
.edge-neg{color:var(--red);font-weight:600;font-family:"JetBrains Mono",monospace;font-size:13px}
.edge-nil{color:var(--muted);font-family:"JetBrains Mono",monospace;font-size:13px}

.price-val{font-family:"JetBrains Mono",monospace;font-size:13px}

.method-ml{display:inline-block;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:0.5px;background:rgba(129,140,248,0.12);color:var(--accent)}
.method-base{display:inline-block;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:0.5px;background:rgba(255,255,255,0.05);color:var(--muted)}

.empty-state{text-align:center;padding:60px 24px;color:var(--muted)}
.empty-icon{font-size:40px;margin-bottom:12px;opacity:0.35}
.empty-title{font-size:16px;font-weight:600;color:var(--text);margin-bottom:6px}
.empty-sub{font-size:13px;color:var(--muted);margin-top:6px}

.section-label{font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:10px}
.best-bets-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px;margin-bottom:24px}
.bet-card{background:var(--surface);border:2px solid transparent;border-radius:14px;padding:16px 18px;position:relative}
.bet-card.bet-pos{border-color:rgba(74,222,128,0.28);background:rgba(74,222,128,0.035)}
.bet-card.bet-neg{border-color:rgba(248,113,113,0.28);background:rgba(248,113,113,0.035)}
.bet-edge-badge{position:absolute;top:12px;right:14px;padding:3px 9px;border-radius:6px;font-size:11px;font-weight:800;font-family:"JetBrains Mono",monospace}
.badge-pos{background:rgba(74,222,128,0.14);color:var(--green)}
.badge-neg{background:rgba(248,113,113,0.14);color:var(--red)}
.bet-title{font-size:13px;font-weight:600;margin-bottom:4px;line-height:1.35;color:var(--text);padding-right:64px}
.bet-meta{font-size:11px;color:var(--muted);margin-bottom:12px}
.bet-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.bet-stat-lbl{font-size:9px;font-weight:700;letter-spacing:0.8px;text-transform:uppercase;color:var(--muted);margin-bottom:2px}
.bet-stat-val{font-size:16px;font-weight:800;font-family:"JetBrains Mono",monospace;line-height:1}
.bet-win-indicator{display:inline-block;margin-top:8px;padding:3px 10px;border-radius:5px;font-size:11px;font-weight:700}
.win-good{background:rgba(74,222,128,0.14);color:var(--green)}
.win-pass{background:rgba(255,255,255,0.06);color:var(--muted)}
.wp-val{font-family:"JetBrains Mono",monospace;font-size:13px;font-weight:700}
.wp-high{color:var(--green)}.wp-mid{color:var(--amber)}.wp-low{color:var(--red)}

footer{border-top:1px solid var(--border);padding:20px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;max-width:1440px;margin:0 auto}
.f-items{display:flex;gap:20px;flex-wrap:wrap}
.f-item{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--muted)}
.f-dot{width:5px;height:5px;border-radius:50%;background:var(--muted);flex-shrink:0}
.f-dot.on{background:var(--green)}
.f-val{color:var(--text);font-weight:600;font-family:"JetBrains Mono",monospace;font-size:12px}
.f-links{display:flex;gap:16px}
.f-links a{font-size:12px;color:var(--muted);text-decoration:none;transition:color 0.15s}
.f-links a:hover{color:var(--text)}
.f-links a.hi{color:var(--accent)}

#loading{position:fixed;inset:0;background:var(--bg);display:flex;align-items:center;justify-content:center;z-index:100;transition:opacity 0.35s}
#loading.gone{opacity:0;pointer-events:none}
.spinner{width:32px;height:32px;border:3px solid rgba(129,140,248,0.2);border-top-color:var(--accent);border-radius:50%;animation:spin 0.75s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

@media(max-width:900px){.stats-bar{grid-template-columns:repeat(2,1fr)}}
@media(max-width:600px){.stats-bar{grid-template-columns:1fr 1fr};header{padding:10px 14px};main{padding:14px 14px 40px};.header-subtitle{display:none};.stat-value{font-size:22px}}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:#1e1e30;border-radius:3px}
</style>
</head>
<body>

<div id="loading"><div class="spinner"></div></div>

<header>
  <div class="header-left">
    <h1>NBA <span>Prediction Engine</span></h1>
    <div class="header-subtitle" id="last-updated">Loading&hellip;</div>
  </div>
  <div class="header-right">
    <div class="counter-chip" id="pred-count">—</div>
    <div class="live-badge">
      <span class="dot-live"></span>
      <span id="live-status">connecting&hellip;</span>
    </div>
  </div>
</header>

<main>
  <div class="stats-bar">
    <div class="stat-card">
      <div class="stat-label">Active Markets</div>
      <div class="stat-value sv-accent" id="s-markets">—</div>
      <div class="stat-sub">predictions tracked</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Market Types</div>
      <div class="stat-value" id="s-types">—</div>
      <div class="stat-sub" id="s-types-sub">categories</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Overall Accuracy</div>
      <div class="stat-value sv-green" id="s-acc">—</div>
      <div class="stat-sub" id="s-acc-sub">resolved markets</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Edge Accuracy</div>
      <div class="stat-value sv-amber" id="s-edge">—</div>
      <div class="stat-sub" id="s-edge-sub">positive-edge calls</div>
    </div>
  </div>

  <div id="best-bets-section" style="display:none">
    <div class="section-label">Best Bets &mdash; Highest Edge vs Kalshi</div>
    <div class="best-bets-grid" id="best-bets-grid"></div>
  </div>

  <div class="controls-bar">
    <div class="filter-tabs" id="filter-tabs"></div>
    <div class="next-refresh" id="next-refresh"></div>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Type</th>
          <th>Market</th>
          <th>Call</th>
          <th>Confidence</th>
          <th>Win Prob</th>
          <th>Edge</th>
          <th>Kalshi</th>
          <th>Method</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</main>

<footer>
  <div class="f-items">
    <div class="f-item"><span class="f-dot" id="fd-cache"></span>Cache&nbsp;<span class="f-val" id="f-cache">—</span>&nbsp;games</div>
    <div class="f-item"><span class="f-dot"></span>Last scrape&nbsp;<span class="f-val" id="f-scrape">—</span></div>
    <div class="f-item"><span class="f-dot" id="fd-proxy"></span>Proxy&nbsp;<span class="f-val" id="f-proxy">—</span></div>
  </div>
  <div class="f-links">
    <a href="/dashboard" class="hi">Kalshi Markets</a>
    <a href="/docs">API Docs</a>
    <a href="/health">Health</a>
  </div>
</footer>

<script>
const LABELS = {winner:"Winner","2h_winner":"2H Winner",series_spread:"Series Spread",spread:"Spread",total:"Total",team_total:"Team Total",reb_assists:"Reb+Ast",blocks:"Blocks",steals:"Steals",triple_double:"Triple-Dbl"};
const BADGE  = {winner:"b-winner","2h_winner":"b-2hw",series_spread:"b-spread",spread:"b-spread",total:"b-total",team_total:"b-total"};
const TABS = [
  {k:"all",l:"All"},
  {k:"winner",l:"Winner"},
  {k:"2h_winner",l:"2H Winner"},
  {k:"spread",l:"Spread"},
  {k:"total",l:"Total"},
  {k:"team_total",l:"Team Total"},
  {k:"series_spread",l:"Series Spread"},
  {k:"steals",l:"Steals"},
  {k:"blocks",l:"Blocks"},
  {k:"triple_double",l:"Triple-Dbl"},
];

let preds = [], activeFilter = "all", countdown = 30, cdTimer = null, refreshTimer = null;

const esc = s => String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");

function fmtAge(iso) {
  if (!iso) return "—";
  try {
    const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 5)    return "just now";
    if (s < 60)   return s + "s ago";
    if (s < 3600) return Math.floor(s/60) + "m ago";
    return Math.floor(s/3600) + "h ago";
  } catch(e) { return "—"; }
}

function confColor(c) {
  if (c >= 75) return "#4ade80";
  if (c >= 65) return "#6ee7b7";
  if (c >= 55) return "#fbbf24";
  return "#94a3b8";
}

function rowCls(p) {
  const c = p.confidence || 50;
  if (c <= 65) return "row-neutral";
  return p.prediction === "YES" ? "row-yes" : "row-no";
}

function edgeHtml(e) {
  if (e == null) return "<span class='edge-nil'>—</span>";
  const pp = (e * 100).toFixed(1);
  if (e >  0.005) return "<span class='edge-pos'>+" + pp + "pp</span>";
  if (e < -0.005) return "<span class='edge-neg'>" + pp + "pp</span>";
  return "<span class='edge-nil'>~0</span>";
}

function winProbHtml(p) {
  const wp = p.our_win_prob;
  if (wp == null) return "<span class='edge-nil'>—</span>";
  const pct = Math.round(wp * 100);
  const cls = pct >= 60 ? "wp-high" : pct <= 40 ? "wp-low" : "wp-mid";
  return "<span class='wp-val " + cls + "'>" + pct + "%</span>";
}

function renderRow(p) {
  const mtype = p.market_type || "";
  const badge = BADGE[mtype] || "b-other";
  const label = LABELS[mtype] || mtype;
  const conf  = p.confidence || 50;
  const cc    = confColor(conf);
  const teams = (p.yes_team && p.no_team) ? esc(p.yes_team) + " vs " + esc(p.no_team) : "";
  const price = p.yes_ask != null ? Math.round(parseFloat(p.yes_ask) * 100) + "&cent;" : "—";
  const edgeVal = parseFloat(p.edge);
  const isGoodBet = !isNaN(edgeVal) && edgeVal > 0.05;
  const mHtml = p.method === "ml_model"
    ? "<span class='method-ml'>ML</span>"
    : p.method === "formula"
    ? "<span class='method-ml' style='background:rgba(251,191,36,0.12);color:var(--amber)'>FML</span>"
    : "<span class='method-base'>BASE</span>";
  return "<tr class='" + rowCls(p) + (isGoodBet ? " bet-highlight" : "") + "'>"
    + "<td><span class='type-badge " + badge + "'>" + label + "</span></td>"
    + "<td class='title-cell'><div class='title-main'>" + esc(p.title||"—") + "</div>"
    + (teams ? "<div class='title-teams'>" + teams + "</div>" : "") + "</td>"
    + "<td><span class='pred-pill " + (p.prediction==="YES"?"pred-yes":"pred-no") + "'>" + esc(p.prediction||"—") + "</span></td>"
    + "<td><div class='conf-wrap'><div class='conf-bar'><div class='conf-fill' style='width:" + Math.min(100,conf) + "%;background:" + cc + "'></div></div>"
    + "<span class='conf-pct' style='color:" + cc + "'>" + conf.toFixed(0) + "%</span></div></td>"
    + "<td>" + winProbHtml(p) + "</td>"
    + "<td>" + edgeHtml(p.edge) + (isGoodBet ? " <span style='font-size:10px;color:var(--green)'>&#10003;</span>" : "") + "</td>"
    + "<td><span class='price-val'>" + price + "</span></td>"
    + "<td>" + mHtml + "</td>"
    + "</tr>";
}

// Tabs that are always visible regardless of count (for debugging connectivity)
const ALWAYS_VISIBLE = new Set(["all", "winner"]);

function buildFilters(byType) {
  document.getElementById("filter-tabs").innerHTML = TABS.map(function(t) {
    const cnt = t.k === "all" ? preds.length : (byType[t.k] || 0);
    if (!ALWAYS_VISIBLE.has(t.k) && cnt === 0) return "";
    const dimAttr = cnt === 0 ? " style='opacity:0.45;cursor:default'" : "";
    return "<button class='tab-btn" + (activeFilter===t.k?" active":"") + "' data-k='" + t.k + "' onclick='setFilter(this.dataset.k)'" + dimAttr + ">"
      + t.l + "<span class='tab-count'>" + cnt + "</span></button>";
  }).join("");
}

function buildTable() {
  const rows = activeFilter === "all" ? preds : preds.filter(function(p){ return p.market_type === activeFilter; });
  if (!rows.length) {
    document.getElementById("tbody").innerHTML =
      "<tr><td colspan='8' style='padding:0'><div class='empty-state'>"
      + "<div class='empty-icon'>&#128202;</div>"
      + "<div class='empty-title'>" + (activeFilter==="all" ? "No predictions yet" : "No predictions for this type") + "</div>"
      + "<div class='empty-sub'>Waiting for the predictor to run&hellip;</div></div></td></tr>";
    return;
  }
  document.getElementById("tbody").innerHTML = rows.map(renderRow).join("");
}

function setFilter(k) {
  activeFilter = k;
  const byType = {};
  preds.forEach(function(p){ byType[p.market_type] = (byType[p.market_type]||0)+1; });
  buildFilters(byType);
  buildTable();
}

function buildBestBets(predictions) {
  var section = document.getElementById("best-bets-section");
  var grid    = document.getElementById("best-bets-grid");
  var scored  = predictions.map(function(p) {
    var e = p.edge;
    var absE = (e != null && !isNaN(parseFloat(e))) ? Math.abs(parseFloat(e)) : 0;
    return {p: p, absE: absE};
  }).filter(function(x) { return x.absE >= 0.04; });
  scored.sort(function(a, b) { return b.absE - a.absE; });
  var top = scored.slice(0, 5);
  if (!top.length) { section.style.display = "none"; return; }
  section.style.display = "";
  grid.innerHTML = top.map(function(x) {
    var p = x.p;
    var edge = parseFloat(p.edge);
    var isPos = edge > 0;
    var edgePct = (edge * 100).toFixed(1);
    var conf = p.confidence || 50;
    var kalshi = p.yes_ask != null ? Math.round(parseFloat(p.yes_ask) * 100) : null;
    var mtype = LABELS[p.market_type] || p.market_type || "";
    var goodBet = isPos && edge > 0.05;
    var wp = p.our_win_prob;
    var wpStr = (wp != null && !isNaN(parseFloat(wp))) ? Math.round(parseFloat(wp) * 100) + "%" : "—";
    return "<div class='bet-card " + (isPos ? "bet-pos" : "bet-neg") + "'>"
      + "<div class='bet-edge-badge " + (isPos ? "badge-pos" : "badge-neg") + "'>"
      + (isPos ? "+" : "") + edgePct + "pp</div>"
      + "<div class='bet-title'>" + esc(p.title || "—") + "</div>"
      + "<div class='bet-meta'>" + mtype
      + (p.yes_team ? " &middot; " + esc(p.yes_team) : "")
      + (p.no_team  ? " vs " + esc(p.no_team)  : "")
      + "</div>"
      + "<div class='bet-stats'>"
      + "<div><div class='bet-stat-lbl'>Call</div><div class='bet-stat-val' style='color:" + (p.prediction==="YES"?"var(--green)":"var(--red)") + "'>" + esc(p.prediction || "—") + "</div></div>"
      + "<div><div class='bet-stat-lbl'>Conf</div><div class='bet-stat-val'>" + conf.toFixed(0) + "%</div></div>"
      + "<div><div class='bet-stat-lbl'>Win%</div><div class='bet-stat-val'>" + wpStr + "</div></div>"
      + "<div><div class='bet-stat-lbl'>Kalshi</div><div class='bet-stat-val'>" + (kalshi != null ? kalshi + "&cent;" : "—") + "</div></div>"
      + "</div>"
      + (goodBet ? "<div class='bet-win-indicator win-good'>&#10003; Bet this</div>" : "<div class='bet-win-indicator win-pass'>Watch only</div>")
      + "</div>";
  }).join("");
}

function showTableMsg(icon, title, sub) {
  document.getElementById("tbody").innerHTML =
    "<tr><td colspan='8' style='padding:0'><div class='empty-state'>"
    + "<div class='empty-icon'>" + icon + "</div>"
    + "<div class='empty-title'>" + title + "</div>"
    + "<div class='empty-sub'>" + sub + "</div>"
    + "</div></td></tr>";
}

async function fetchData() {
  var controller = new AbortController();
  var tid = setTimeout(function() { controller.abort(); }, 10000);
  try {
    var r = await fetch("/api/predictions", { signal: controller.signal });
    clearTimeout(tid);
    if (!r.ok) throw new Error("HTTP " + r.status);
    var d = await r.json();

    preds = d.predictions || [];

    buildBestBets(preds);

    document.getElementById("last-updated").textContent = d.generated_at ? "Updated " + fmtAge(d.generated_at) : "Awaiting first run";
    document.getElementById("pred-count").textContent = preds.length + (preds.length===1?" prediction":" predictions");

    document.getElementById("s-markets").textContent = d.count || "—";
    var bt = d.by_type || {};
    var tc = Object.keys(bt).length;
    document.getElementById("s-types").textContent    = tc || "—";
    document.getElementById("s-types-sub").textContent = tc + " market type" + (tc!==1?"s":"");

    var acc = d.accuracy;
    if (acc && acc.accuracy_pct != null) {
      document.getElementById("s-acc").textContent     = acc.accuracy_pct.toFixed(1) + "%";
      document.getElementById("s-acc-sub").textContent = acc.correct + "/" + acc.total + " correct";
    } else {
      document.getElementById("s-acc").textContent     = "—";
      document.getElementById("s-acc-sub").textContent = "no history yet";
    }
    if (acc && acc.edge_accuracy_pct != null) {
      document.getElementById("s-edge").textContent     = acc.edge_accuracy_pct.toFixed(1) + "%";
      document.getElementById("s-edge-sub").textContent = (acc.positive_edge_total||0) + " edge call" + ((acc.positive_edge_total||0)!==1?"s":"");
    } else {
      document.getElementById("s-edge").textContent     = "—";
      document.getElementById("s-edge-sub").textContent = "no edge data yet";
    }

    buildFilters(bt);
    buildTable();

    var sys = d.system || {};
    document.getElementById("f-cache").textContent  = sys.cache_size || "0";
    document.getElementById("f-scrape").textContent = sys.last_scrape ? fmtAge(sys.last_scrape) : "—";
    document.getElementById("f-proxy").textContent  = sys.proxy_configured ? "configured" : "direct";
    document.getElementById("fd-proxy").className   = "f-dot" + (sys.proxy_configured ? " on" : "");
    document.getElementById("fd-cache").className   = "f-dot" + ((sys.cache_size||0)>0 ? " on" : "");

    document.getElementById("live-status").textContent = "live";
    if (d.error) {
      showTableMsg("&#9888;", "Backend error", esc(d.error));
    }
  } catch(e) {
    clearTimeout(tid);
    console.error("fetchData:", e);
    var msg = e.name === "AbortError" ? "Request timed out (10s)" : esc(String(e));
    document.getElementById("live-status").textContent = "error";
    showTableMsg("&#9888;", "Could not load predictions", msg + " — will retry in 30s");
  } finally {
    document.getElementById("loading").classList.add("gone");
  }
}

function startCountdown() {
  if (cdTimer) clearInterval(cdTimer);
  countdown = 30;
  var el = document.getElementById("next-refresh");
  var tick = function() {
    el.textContent = "refresh in " + countdown + "s";
    if (countdown > 0) countdown--;
  };
  tick();
  cdTimer = setInterval(tick, 1000);
}

function scheduleRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async function() {
    document.getElementById("live-status").textContent = "refreshing…";
    await fetchData();
    startCountdown();
    scheduleRefresh();
  }, 30000);
}

// Show empty state immediately so spinner is never the final state
showTableMsg("&#9200;", "No predictions yet — system is warming up", "Auto-refreshes every 30 seconds");
fetchData().then(function() { startCountdown(); scheduleRefresh(); });
</script>
</body>
</html>
"""
