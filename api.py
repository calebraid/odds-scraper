import json
import os
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


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return HTMLResponse(content=_DASHBOARD_HTML)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing_page():
    return HTMLResponse(content=_LANDING_HTML)


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


_LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OddsAPI — Kalshi NBA Markets</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0a;--surface:#111;--green:#00ff88;--green-glow:rgba(0,255,136,.15);--text:#fff;--dim:#888;--border:rgba(255,255,255,.08)}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:'Inter',-apple-system,sans-serif;overflow-x:hidden}

nav{position:fixed;top:0;left:0;right:0;z-index:100;padding:1rem 2rem;display:flex;align-items:center;justify-content:space-between;background:rgba(10,10,10,.85);backdrop-filter:blur(20px);border-bottom:1px solid var(--border)}
.logo{font-size:1.2rem;font-weight:900;color:var(--green);letter-spacing:-.5px;text-decoration:none}
.nav-links{display:flex;gap:2rem;align-items:center}
.nav-links a{color:var(--dim);text-decoration:none;font-size:.875rem;font-weight:500;transition:color .2s}
.nav-links a:hover{color:var(--text)}
.nav-cta{background:var(--green)!important;color:#000!important;padding:.45rem 1.2rem;border-radius:6px;font-weight:700!important}

.hero{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:8rem 2rem 4rem;position:relative;overflow:hidden}
.hero-glow{position:absolute;width:700px;height:700px;background:radial-gradient(circle,rgba(0,255,136,.1) 0%,transparent 70%);top:15%;left:50%;transform:translateX(-50%);pointer-events:none}
.hero-badge{display:inline-flex;align-items:center;gap:.5rem;background:rgba(0,255,136,.08);border:1px solid rgba(0,255,136,.25);color:var(--green);padding:.35rem 1rem;border-radius:100px;font-size:.75rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:2rem}
.pulse{width:7px;height:7px;background:var(--green);border-radius:50%;animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.7)}}
h1{font-size:clamp(2.5rem,8vw,5.5rem);font-weight:900;letter-spacing:-3px;line-height:1;margin-bottom:1.5rem;background:linear-gradient(135deg,#fff 0%,#bbb 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
h1 em{font-style:normal;background:linear-gradient(135deg,var(--green) 0%,#00cc70 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero-sub{font-size:1.2rem;color:var(--dim);max-width:540px;margin:0 auto 2.5rem;line-height:1.75}
.cta-row{display:flex;gap:1rem;justify-content:center;flex-wrap:wrap}
.btn-green{background:var(--green);color:#000;border:none;padding:.9rem 2rem;border-radius:8px;font-size:1rem;font-weight:700;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:.5rem;transition:all .2s;box-shadow:0 0 30px rgba(0,255,136,.3)}
.btn-green:hover{transform:translateY(-2px);box-shadow:0 0 55px rgba(0,255,136,.5)}
.btn-outline{background:transparent;color:var(--text);border:1px solid var(--border);padding:.9rem 2rem;border-radius:8px;font-size:1rem;font-weight:600;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:.5rem;transition:all .2s}
.btn-outline:hover{border-color:rgba(255,255,255,.3);background:rgba(255,255,255,.05)}

.sec{padding:7rem 2rem;max-width:1200px;margin:0 auto}
.sec-head{text-align:center;margin-bottom:4rem}
.sec-label{color:var(--green);font-size:.75rem;font-weight:700;letter-spacing:2.5px;text-transform:uppercase;margin-bottom:.875rem}
.sec-title{font-size:clamp(1.75rem,5vw,3rem);font-weight:800;letter-spacing:-1.5px;margin-bottom:1rem;line-height:1.1}
.sec-desc{color:var(--dim);font-size:1.1rem;max-width:480px;margin:0 auto;line-height:1.75}

.feat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:1.25rem;margin-bottom:3.5rem}
.feat-card{background:rgba(255,255,255,.025);border:1px solid var(--border);border-radius:16px;padding:1.875rem;transition:all .3s}
.feat-card:hover{border-color:rgba(0,255,136,.35);transform:translateY(-5px)}
.feat-icon{font-size:2rem;margin-bottom:1.25rem;display:block}
.feat-card h3{font-size:1.0625rem;font-weight:700;margin-bottom:.625rem}
.feat-card p{color:var(--dim);font-size:.9rem;line-height:1.65}

.demo-bg{background:rgba(255,255,255,.012);border-top:1px solid var(--border);border-bottom:1px solid var(--border);padding:7rem 2rem}
.demo-inner{max-width:1000px;margin:0 auto}
.demo-grid{display:grid;grid-template-columns:1fr 1fr;gap:1.25rem}

.tester-panel{background:#0d1117;border:1px solid rgba(255,255,255,.07);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.tester-hdr{display:flex;align-items:center;justify-content:space-between;padding:.7rem 1rem;background:rgba(255,255,255,.025);border-bottom:1px solid rgba(255,255,255,.06);gap:.75rem}
.tester-hdr-left{display:flex;align-items:center;gap:.625rem;min-width:0}
.method-badge{background:rgba(0,255,136,.12);color:var(--green);border:1px solid rgba(0,255,136,.3);padding:.15rem .5rem;border-radius:4px;font-size:.67rem;font-weight:800;letter-spacing:1px;font-family:'JetBrains Mono',monospace;flex-shrink:0}
.tester-endpoint{font-family:'JetBrains Mono',monospace;font-size:.8rem;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tester-status{font-size:.7rem;font-family:'JetBrains Mono',monospace;font-weight:700;white-space:nowrap;flex-shrink:0;letter-spacing:.5px}
.status-ok{color:var(--green)}
.status-err{color:#ff8080}
.tester-body{padding:1.25rem;display:flex;flex-direction:column;gap:1rem;flex:1}
.tester-label{display:block;font-size:.68rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim);margin-bottom:.4rem}
.key-input-row{display:flex;gap:.5rem}
.key-input{flex:1;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);border-radius:6px;padding:.6rem .875rem;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:.8rem;outline:none;transition:border-color .2s;min-width:0}
.key-input:focus{border-color:rgba(0,255,136,.4)}
.key-input::placeholder{color:rgba(255,255,255,.22)}
.fetch-btn{background:var(--green);color:#000;border:none;border-radius:8px;padding:.85rem 1rem;font-size:.9375rem;font-weight:700;cursor:pointer;font-family:inherit;transition:all .25s;box-shadow:0 0 25px rgba(0,255,136,.2);width:100%;display:flex;align-items:center;justify-content:center;gap:.5rem}
.fetch-btn:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 0 45px rgba(0,255,136,.45)}
.fetch-btn:disabled{opacity:.55;cursor:not-allowed}
.tester-meta{font-size:.76rem;color:var(--dim);min-height:1.1em;font-family:'JetBrains Mono',monospace}
.spin{display:inline-block;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.odds-panel{background:#0d1117;border:1px solid rgba(255,255,255,.07);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.odds-panel-hdr{display:flex;align-items:center;justify-content:space-between;padding:.7rem 1rem;background:rgba(255,255,255,.025);border-bottom:1px solid rgba(255,255,255,.06);flex-shrink:0}
.live-label{display:inline-flex;align-items:center;gap:.35rem;color:var(--green);font-size:.7rem;font-family:'JetBrains Mono',monospace;font-weight:700;letter-spacing:1px}
.code-lang{font-size:.7rem;color:var(--dim);font-weight:700;letter-spacing:1.5px;text-transform:uppercase}
.markets-list{padding:.875rem;display:flex;flex-direction:column;gap:.625rem;overflow-y:auto;max-height:420px}
.market-card{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:10px;padding:.875rem 1rem;transition:border-color .2s}
.market-card:hover{border-color:rgba(0,255,136,.25)}
.market-title{font-size:.85rem;font-weight:600;color:var(--text);margin-bottom:.625rem;line-height:1.4}
.market-prices{display:flex;gap:.5rem}
.price-pill{flex:1;background:rgba(255,255,255,.04);border-radius:6px;padding:.4rem .6rem;text-align:center}
.price-lbl{font-size:.62rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--green);margin-bottom:.2rem}
.price-val{font-size:.9rem;font-weight:700;font-family:'JetBrains Mono',monospace}
.price-vol{font-size:.7rem;color:var(--dim);font-family:'JetBrains Mono',monospace}
.odds-loading{padding:2rem;text-align:center;color:var(--dim);font-size:.875rem}

.price-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1.5rem;max-width:780px;margin:0 auto}
.price-card{background:rgba(255,255,255,.025);border:1px solid var(--border);border-radius:20px;padding:2.5rem;transition:transform .3s}
.price-card:hover{transform:translateY(-5px)}
.price-card.hot{background:rgba(0,255,136,.05);border-color:rgba(0,255,136,.4);position:relative;box-shadow:0 0 70px rgba(0,255,136,.1)}
.hot-badge{position:absolute;top:-13px;left:50%;transform:translateX(-50%);background:var(--green);color:#000;padding:.25rem 1rem;border-radius:100px;font-size:.7rem;font-weight:800;letter-spacing:1px;text-transform:uppercase;white-space:nowrap}
.plan-name{font-size:.75rem;font-weight:700;letter-spacing:2.5px;text-transform:uppercase;color:var(--dim);margin-bottom:1.25rem}
.price-card.hot .plan-name{color:var(--green)}
.price-amt{font-size:3.75rem;font-weight:900;letter-spacing:-2px;line-height:1;margin-bottom:.25rem}
.price-per{color:var(--dim);font-size:.875rem;margin-bottom:2rem}
.feat-list{list-style:none;margin-bottom:2rem}
.feat-list li{display:flex;align-items:center;gap:.75rem;padding:.575rem 0;border-bottom:1px solid var(--border);font-size:.9125rem;color:var(--dim)}
.feat-list li:last-child{border-bottom:none}
.chk{color:var(--green);font-size:.9rem;flex-shrink:0;font-weight:700}
.btn-plan{width:100%;padding:.875rem;border-radius:8px;font-size:.9375rem;font-weight:700;cursor:pointer;transition:all .2s;font-family:inherit;text-align:center;text-decoration:none;display:block}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--text)}
.btn-ghost:hover{border-color:rgba(255,255,255,.3);background:rgba(255,255,255,.05)}
.btn-solid{background:var(--green);border:none;color:#000;box-shadow:0 0 30px rgba(0,255,136,.3)}
.btn-solid:hover{transform:translateY(-2px);box-shadow:0 0 55px rgba(0,255,136,.5)}

footer{border-top:1px solid var(--border);padding:2.5rem 2rem;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:1rem;max-width:1200px;margin:0 auto}
.ft-links{display:flex;gap:2rem}
.ft-links a{color:var(--dim);text-decoration:none;font-size:.875rem;transition:color .2s}
.ft-links a:hover{color:var(--text)}
.ft-copy{color:var(--dim);font-size:.875rem}

.fade-up{opacity:0;transform:translateY(28px);transition:opacity .65s ease,transform .65s ease}
.fade-up.in{opacity:1;transform:translateY(0)}

@media(max-width:768px){.demo-grid{grid-template-columns:1fr}.nav-links{display:none}footer{flex-direction:column;text-align:center}.ft-links{justify-content:center}}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:3px}
</style>
</head>
<body>

<nav>
  <a href="/" class="logo">&#9889; OddsAPI</a>
  <div class="nav-links">
    <a href="#features">Features</a>
    <a href="#demo">Demo</a>
    <a href="#pricing">Pricing</a>
    <a href="/docs">Docs</a>
    <a href="#pricing" class="nav-cta">Get API Key</a>
  </div>
</nav>

<div class="hero">
  <div class="hero-glow"></div>
  <div class="hero-badge"><span class="pulse"></span>Live Data &bull; Updated Every 60 Seconds</div>
  <h1>NBA Prediction<br><em>Markets API</em></h1>
  <p class="hero-sub">Real-time Kalshi NBA prediction market data for developers. Yes/No prices, volume, and open interest&mdash;delivered in milliseconds.</p>
  <div class="cta-row">
    <a href="#pricing" class="btn-green">&#128640; Get Free API Key</a>
    <a href="/docs" class="btn-outline">&#128196; View Docs &rarr;</a>
  </div>
</div>

<section class="sec" id="features">
  <div class="sec-head fade-up">
    <div class="sec-label">Why Choose Us</div>
    <h2 class="sec-title">Kalshi NBA markets,<br>ready to query</h2>
    <p class="sec-desc">Clean REST API over Kalshi&rsquo;s KXNBA series, refreshed every 60 seconds.</p>
  </div>
  <div class="feat-grid">
    <div class="feat-card fade-up">
      <span class="feat-icon">&#9889;</span>
      <h3>Live Markets</h3>
      <p>All open KXNBA prediction markets from Kalshi, refreshed every 60 seconds with current yes/no prices.</p>
    </div>
    <div class="feat-card fade-up">
      <span class="feat-icon">&#128200;</span>
      <h3>Yes &amp; No Prices</h3>
      <p>Ask prices for both sides of every market, plus volume and open interest for liquidity analysis.</p>
    </div>
    <div class="feat-card fade-up">
      <span class="feat-icon">&#128640;</span>
      <h3>Fast Response</h3>
      <p>Sub-100ms API responses served from global infrastructure. Simple JSON format, easy to integrate.</p>
    </div>
    <div class="feat-card fade-up">
      <span class="feat-icon">&#128274;</span>
      <h3>Reliable Uptime</h3>
      <p>99.9% uptime with health monitoring and automatic restarts. Built on Railway.</p>
    </div>
  </div>
</section>

<div class="demo-bg" id="demo">
  <div class="demo-inner">
    <div class="sec-head fade-up">
      <div class="sec-label">Live Demo</div>
      <h2 class="sec-title">Try it right now</h2>
      <p class="sec-desc">Enter your API key and fetch live Kalshi NBA markets.</p>
    </div>
    <div class="demo-grid fade-up">
      <div class="tester-panel">
        <div class="tester-hdr">
          <div class="tester-hdr-left">
            <span class="method-badge">GET</span>
            <span class="tester-endpoint">/odds/nba/kalshi</span>
          </div>
          <span class="tester-status" id="tester-status"></span>
        </div>
        <div class="tester-body">
          <div>
            <label class="tester-label" for="api-key-input">X-API-Key</label>
            <div class="key-input-row">
              <input type="text" id="api-key-input" class="key-input" placeholder="Paste your API key&hellip;" autocomplete="off" spellcheck="false">
            </div>
          </div>
          <button class="fetch-btn" id="fetch-btn" onclick="fetchMarkets()">
            <span id="fetch-label">&#9889; Fetch Live Markets</span>
          </button>
          <div class="tester-meta" id="tester-meta"></div>
        </div>
      </div>
      <div class="odds-panel">
        <div class="odds-panel-hdr">
          <span class="code-lang">Live Response</span>
          <span class="live-label"><span class="pulse"></span>LIVE</span>
        </div>
        <div class="markets-list" id="markets-list">
          <div class="odds-loading">Enter your API key and click Fetch.</div>
        </div>
      </div>
    </div>
  </div>
</div>

<section class="sec" id="pricing">
  <div class="sec-head fade-up">
    <div class="sec-label">Pricing</div>
    <h2 class="sec-title">Simple, transparent pricing</h2>
    <p class="sec-desc">Start free, scale when you need it.</p>
  </div>
  <div class="price-grid fade-up">
    <div class="price-card">
      <div class="plan-name">Free</div>
      <div class="price-amt">$0</div>
      <div class="price-per">forever &mdash; no credit card</div>
      <ul class="feat-list">
        <li><span class="chk">&#10003;</span> 100 requests / day</li>
        <li><span class="chk">&#10003;</span> Kalshi NBA markets</li>
        <li><span class="chk">&#10003;</span> Yes/No prices &amp; volume</li>
        <li><span class="chk">&#10003;</span> JSON REST API</li>
        <li><span class="chk">&#10003;</span> Basic email support</li>
      </ul>
      <a href="mailto:jbraid061@gmail.com?subject=Free API Key Request" class="btn-plan btn-ghost">Get Started Free</a>
    </div>
    <div class="price-card hot">
      <div class="hot-badge">&#9889; Most Popular</div>
      <div class="plan-name">Pro</div>
      <div class="price-amt">$29</div>
      <div class="price-per">per month &mdash; cancel anytime</div>
      <ul class="feat-list">
        <li><span class="chk">&#10003;</span> 10,000 requests / day</li>
        <li><span class="chk">&#10003;</span> All Kalshi NBA markets</li>
        <li><span class="chk">&#10003;</span> Yes/No prices &amp; volume</li>
        <li><span class="chk">&#10003;</span> Open interest data</li>
        <li><span class="chk">&#10003;</span> Priority support &amp; SLA</li>
      </ul>
      <a href="mailto:jbraid061@gmail.com?subject=Pro API Key Request" class="btn-plan btn-solid">Get Pro Access &rarr;</a>
    </div>
  </div>
</section>

<footer>
  <a href="/" class="logo">&#9889; OddsAPI</a>
  <div class="ft-links">
    <a href="/docs">Documentation</a>
    <a href="/health">Health</a>
    <a href="mailto:jbraid061@gmail.com">Contact</a>
  </div>
  <div class="ft-copy">&copy; 2026 OddsAPI. All rights reserved.</div>
</footer>

<script>
async function fetchMarkets() {
  const key = document.getElementById('api-key-input').value.trim();
  if (!key) { alert('Please enter your API key.'); return; }

  const btn = document.getElementById('fetch-btn');
  const lbl = document.getElementById('fetch-label');
  const statusEl = document.getElementById('tester-status');
  const metaEl = document.getElementById('tester-meta');
  const listEl = document.getElementById('markets-list');

  btn.disabled = true;
  lbl.innerHTML = '<span class="spin">&#9696;</span> Fetching&hellip;';
  listEl.innerHTML = '<div class="odds-loading">Fetching live markets&hellip;</div>';
  statusEl.className = 'tester-status';
  statusEl.textContent = '';
  metaEl.textContent = '';

  const t0 = performance.now();
  try {
    const r = await fetch('/odds/nba/kalshi', { headers: { 'X-API-Key': key } });
    const ms = Math.round(performance.now() - t0);
    const data = await r.json();
    if (r.ok) {
      statusEl.className = 'tester-status status-ok';
      statusEl.textContent = r.status + ' OK';
      const markets = data.markets || [];
      metaEl.textContent = 'Responded in ' + ms + 'ms · ' + markets.length + ' market' + (markets.length !== 1 ? 's' : '');
      renderMarkets(markets);
    } else {
      statusEl.className = 'tester-status status-err';
      statusEl.textContent = r.status + ' Error';
      metaEl.textContent = data.detail || 'Request failed';
      listEl.innerHTML = '<div class="odds-loading">' + (data.detail || 'Error fetching markets.') + '</div>';
    }
  } catch (_) {
    statusEl.className = 'tester-status status-err';
    statusEl.textContent = 'Network Error';
    metaEl.textContent = 'Could not reach the API';
    listEl.innerHTML = '<div class="odds-loading">Network error — check your connection.</div>';
  }
  btn.disabled = false;
  lbl.innerHTML = '&#9889; Fetch Live Markets';
}

function renderMarkets(markets) {
  const el = document.getElementById('markets-list');
  if (!markets.length) { el.innerHTML = '<div class="odds-loading">No open markets found.</div>'; return; }
  const fmt = v => v == null ? 'N/A' : v + '¢';
  const fmtVol = v => v == null ? '' : 'Vol: ' + v.toLocaleString();
  el.innerHTML = markets.map(m => `<div class="market-card">
  <div class="market-title">${m.title || m.ticker || '—'}</div>
  <div class="market-prices">
    <div class="price-pill"><div class="price-lbl">Yes</div><div class="price-val">${fmt(m.yes_price)}</div></div>
    <div class="price-pill"><div class="price-lbl">No</div><div class="price-val">${fmt(m.no_price)}</div></div>
    <div class="price-pill" style="flex:1.5"><div class="price-lbl">Volume</div><div class="price-vol">${fmtVol(m.volume)}</div></div>
  </div>
</div>`).join('');
}

const obs = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); obs.unobserve(e.target); } }), { threshold: .1 });
document.querySelectorAll('.fade-up').forEach(el => obs.observe(el));
</script>
</body>
</html>"""
