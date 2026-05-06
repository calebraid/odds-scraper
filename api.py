import json
import os
from datetime import datetime, timezone, timedelta
from typing import Annotated

from fastapi import FastAPI, HTTPException, Security, Depends, Response
from fastapi.security import APIKeyHeader
from fastapi.responses import JSONResponse, HTMLResponse

app = FastAPI(title="Sports Odds API", version="1.0.0")

_BASE = os.path.dirname(os.path.abspath(__file__))
ODDS_DIR = os.path.join(_BASE, "odds")

RATE_LIMITS: dict[str, int] = {
    "free": 100,
    "pro": 10_000,
}

SPORT_FILES: dict[str, str] = {
    "nba": "latest.json",
}

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

# In-memory usage store: key -> (utc_date_str, count)
# Resets automatically when the date rolls over.
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
        count = 0  # new day — reset
    count += 1
    _usage[raw_key] = (today, count)

    if count > limit:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {limit} requests/day for '{tier}' tier.",
            headers={"Retry-After": str(_seconds_until_midnight_utc())},
        )

    return {"tier": tier, "usage": count, "limit": limit}


# ── helpers ──────────────────────────────────────────────────────────────────

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


@app.get("/odds", summary="All available sports odds")
def get_all_odds(auth: Annotated[dict, Depends(authenticate)], response: Response):
    _set_rate_limit_headers(response, auth)
    results = []
    for sport, filename in SPORT_FILES.items():
        try:
            data = load_odds(filename)
            data["sport"] = sport
            warning = staleness_warning(data.get("scraped_at"))
            if warning:
                data["warning"] = warning
            results.append(data)
        except HTTPException:
            results.append({"sport": sport, "error": "data unavailable"})
    return JSONResponse(content={"sports": results, "count": len(results)}, headers=dict(response.headers))


@app.get("/odds/preview", summary="Public preview of NBA odds (no auth)")
def get_preview_odds():
    """Returns current NBA odds without auth — used by the landing page ticker."""
    try:
        data = load_odds(SPORT_FILES["nba"])
        return JSONResponse(content={
            "games": data.get("games", []),
            "scraped_at": data.get("scraped_at"),
        })
    except HTTPException:
        return JSONResponse(content={"games": []})


@app.get("/odds/nba/props", summary="NBA player props from The Odds API")
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


@app.get("/odds/{sport}/props", summary="Player props for a specific sport")
def get_sport_props(sport: str, auth: Annotated[dict, Depends(authenticate)], response: Response):
    _set_rate_limit_headers(response, auth)
    slug = sport.lower()
    if slug not in SPORT_FILES:
        raise HTTPException(
            status_code=404,
            detail=f"Sport '{sport}' not found. Available: {list(SPORT_FILES.keys())}",
        )
    data = load_odds(SPORT_FILES[slug])
    props = data.get("player_props", [])
    warning = staleness_warning(data.get("scraped_at"))
    result = {
        "source": data.get("source"),
        "league": data.get("league"),
        "scraped_at": data.get("scraped_at"),
        "game_count": len(props),
        "player_props": props,
    }
    if warning:
        result["warning"] = warning
    return JSONResponse(content=result, headers=dict(response.headers))


@app.get("/odds/{sport}", summary="Odds for a specific sport")
def get_sport_odds(sport: str, auth: Annotated[dict, Depends(authenticate)], response: Response):
    _set_rate_limit_headers(response, auth)
    slug = sport.lower()
    if slug not in SPORT_FILES:
        raise HTTPException(
            status_code=404,
            detail=f"Sport '{sport}' not found. Available: {list(SPORT_FILES.keys())}",
        )
    data = load_odds(SPORT_FILES[slug])
    warning = staleness_warning(data.get("scraped_at"))
    if warning:
        data["warning"] = warning
    return JSONResponse(content=data, headers=dict(response.headers))


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing_page():
    return HTMLResponse(content=_LANDING_HTML)


_LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OddsAPI — Real-Time Sports Odds</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0a;--surface:#111;--green:#00ff88;--green-glow:rgba(0,255,136,.15);--text:#fff;--dim:#888;--border:rgba(255,255,255,.08)}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:'Inter',-apple-system,sans-serif;overflow-x:hidden}

/* ── NAV ── */
nav{position:fixed;top:0;left:0;right:0;z-index:100;padding:1rem 2rem;display:flex;align-items:center;justify-content:space-between;background:rgba(10,10,10,.85);backdrop-filter:blur(20px);border-bottom:1px solid var(--border)}
.logo{font-size:1.2rem;font-weight:900;color:var(--green);letter-spacing:-.5px;text-decoration:none}
.nav-links{display:flex;gap:2rem;align-items:center}
.nav-links a{color:var(--dim);text-decoration:none;font-size:.875rem;font-weight:500;transition:color .2s}
.nav-links a:hover{color:var(--text)}
.nav-cta{background:var(--green)!important;color:#000!important;padding:.45rem 1.2rem;border-radius:6px;font-weight:700!important}
.nav-cta:hover{opacity:.85}

/* ── HERO ── */
.hero{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:8rem 2rem 0;position:relative;overflow:hidden}
.hero-glow{position:absolute;width:700px;height:700px;background:radial-gradient(circle,rgba(0,255,136,.1) 0%,transparent 70%);top:15%;left:50%;transform:translateX(-50%);pointer-events:none}
.hero-badge{display:inline-flex;align-items:center;gap:.5rem;background:rgba(0,255,136,.08);border:1px solid rgba(0,255,136,.25);color:var(--green);padding:.35rem 1rem;border-radius:100px;font-size:.75rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:2rem}
.pulse{width:7px;height:7px;background:var(--green);border-radius:50%;animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.7)}}
h1{font-size:clamp(2.5rem,8vw,5.5rem);font-weight:900;letter-spacing:-3px;line-height:1;margin-bottom:1.5rem;background:linear-gradient(135deg,#fff 0%,#bbb 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
h1 em{font-style:normal;background:linear-gradient(135deg,var(--green) 0%,#00cc70 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero-sub{font-size:1.2rem;color:var(--dim);max-width:540px;margin:0 auto 2.5rem;line-height:1.75}
.cta-row{display:flex;gap:1rem;justify-content:center;flex-wrap:wrap;margin-bottom:4rem}
.btn-green{background:var(--green);color:#000;border:none;padding:.9rem 2rem;border-radius:8px;font-size:1rem;font-weight:700;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:.5rem;transition:all .2s;box-shadow:0 0 30px rgba(0,255,136,.3)}
.btn-green:hover{transform:translateY(-2px);box-shadow:0 0 55px rgba(0,255,136,.5)}
.btn-outline{background:transparent;color:var(--text);border:1px solid var(--border);padding:.9rem 2rem;border-radius:8px;font-size:1rem;font-weight:600;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:.5rem;transition:all .2s}
.btn-outline:hover{border-color:rgba(255,255,255,.3);background:rgba(255,255,255,.05)}

/* ── TICKER ── */
.ticker-wrap{width:100%;overflow:hidden;background:rgba(0,255,136,.04);border-top:1px solid rgba(0,255,136,.12);border-bottom:1px solid rgba(0,255,136,.12);padding:.7rem 0;margin-top:0}
.ticker-track{display:flex;animation:scroll-left 50s linear infinite;white-space:nowrap;will-change:transform}
.ticker-track:hover{animation-play-state:paused}
@keyframes scroll-left{0%{transform:translateX(0)}100%{transform:translateX(-50%)}}
.tick{display:inline-flex;align-items:center;gap:.875rem;padding:0 3rem;font-size:.8125rem;font-family:'JetBrains Mono',monospace;border-right:1px solid rgba(255,255,255,.07)}
.tick-match{color:var(--text);font-weight:500}
.tick-spread{color:var(--green);font-weight:700}
.tick-ml{color:var(--dim)}

/* ── SECTIONS ── */
.sec{padding:7rem 2rem;max-width:1200px;margin:0 auto}
.sec-head{text-align:center;margin-bottom:4rem}
.sec-label{color:var(--green);font-size:.75rem;font-weight:700;letter-spacing:2.5px;text-transform:uppercase;margin-bottom:.875rem}
.sec-title{font-size:clamp(1.75rem,5vw,3rem);font-weight:800;letter-spacing:-1.5px;margin-bottom:1rem;line-height:1.1}
.sec-desc{color:var(--dim);font-size:1.1rem;max-width:480px;margin:0 auto;line-height:1.75}

/* ── FEATURE CARDS ── */
.feat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:1.25rem;margin-bottom:3.5rem}
.feat-card{background:rgba(255,255,255,.025);border:1px solid var(--border);border-radius:16px;padding:1.875rem;transition:all .3s;position:relative;overflow:hidden}
.feat-card::after{content:'';position:absolute;inset:0;background:linear-gradient(135deg,rgba(0,255,136,.07) 0%,transparent 55%);opacity:0;transition:opacity .3s}
.feat-card:hover{border-color:rgba(0,255,136,.35);transform:translateY(-5px);box-shadow:0 25px 50px rgba(0,0,0,.5)}
.feat-card:hover::after{opacity:1}
.feat-icon{font-size:2rem;margin-bottom:1.25rem;display:block}
.feat-card h3{font-size:1.0625rem;font-weight:700;margin-bottom:.625rem}
.feat-card p{color:var(--dim);font-size:.9rem;line-height:1.65}

/* ── STATS ── */
.stats-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1.25rem}
.stat-card{background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:12px;padding:2rem;text-align:center}
.stat-val{font-size:2.75rem;font-weight:900;color:var(--green);letter-spacing:-1px;line-height:1;margin-bottom:.5rem}
.stat-lbl{color:var(--dim);font-size:.9rem}

/* ── DEMO ── */
.demo-bg{background:rgba(255,255,255,.012);border-top:1px solid var(--border);border-bottom:1px solid var(--border);padding:7rem 2rem}
.demo-inner{max-width:1000px;margin:0 auto}
.picker{display:flex;gap:.75rem;margin-bottom:2rem;flex-wrap:wrap}
.sport-btn{background:rgba(255,255,255,.05);border:1px solid var(--border);color:var(--dim);padding:.5rem 1.25rem;border-radius:6px;cursor:pointer;font-size:.875rem;font-weight:600;transition:all .2s;font-family:inherit}
.sport-btn:hover{color:var(--text);border-color:rgba(255,255,255,.2)}
.sport-btn.active{background:rgba(0,255,136,.1);border-color:rgba(0,255,136,.5);color:var(--green)}
.sport-btn:disabled{opacity:.38;cursor:not-allowed}
.demo-grid{display:grid;grid-template-columns:1fr 1fr;gap:1.25rem}
.code-box{background:#0d1117;border:1px solid rgba(255,255,255,.07);border-radius:12px;overflow:hidden}
.code-hdr{display:flex;align-items:center;justify-content:space-between;padding:.7rem 1rem;background:rgba(255,255,255,.025);border-bottom:1px solid rgba(255,255,255,.06)}
.code-lang{font-size:.7rem;color:var(--dim);font-weight:700;letter-spacing:1.5px;text-transform:uppercase}
.copy-btn{background:transparent;border:1px solid var(--border);color:var(--dim);padding:.2rem .7rem;border-radius:4px;cursor:pointer;font-size:.7rem;font-family:inherit;transition:all .2s}
.copy-btn:hover{color:var(--text);border-color:rgba(255,255,255,.2)}
.code-body{padding:1.25rem;font-family:'JetBrains Mono',monospace;font-size:.8rem;line-height:1.75;overflow-x:auto;max-height:380px;overflow-y:auto}

/* ── API Tester panel ── */
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
.key-input:focus{border-color:rgba(0,255,136,.4);background:rgba(255,255,255,.06)}
.key-input::placeholder{color:rgba(255,255,255,.22)}
.try-demo-btn{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);color:var(--dim);padding:.6rem .875rem;border-radius:6px;cursor:pointer;font-size:.78rem;font-weight:600;white-space:nowrap;font-family:inherit;transition:all .2s;flex-shrink:0}
.try-demo-btn:hover{color:var(--green);border-color:rgba(0,255,136,.35);background:rgba(0,255,136,.06)}
.fetch-btn{background:var(--green);color:#000;border:none;border-radius:8px;padding:.85rem 1rem;font-size:.9375rem;font-weight:700;cursor:pointer;font-family:inherit;transition:all .25s;box-shadow:0 0 25px rgba(0,255,136,.2);width:100%;display:flex;align-items:center;justify-content:center;gap:.5rem}
.fetch-btn:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 0 45px rgba(0,255,136,.45)}
.fetch-btn:disabled{opacity:.55;cursor:not-allowed;transform:none;box-shadow:none}
.tester-meta{font-size:.76rem;color:var(--dim);min-height:1.1em;font-family:'JetBrains Mono',monospace;line-height:1.5}
.spin{display:inline-block;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── Odds cards (demo panel) ── */
.odds-panel{background:#0d1117;border:1px solid rgba(255,255,255,.07);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.odds-panel-hdr{display:flex;align-items:center;justify-content:space-between;padding:.7rem 1rem;background:rgba(255,255,255,.025);border-bottom:1px solid rgba(255,255,255,.06);flex-shrink:0}
.live-label{display:inline-flex;align-items:center;gap:.35rem;color:var(--green);font-size:.7rem;font-family:'JetBrains Mono',monospace;font-weight:700;letter-spacing:1px}
.odds-cards{padding:.875rem;display:flex;flex-direction:column;gap:.75rem;overflow-y:auto;max-height:380px}
.game-card{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:10px;padding:1rem 1.125rem;transition:border-color .2s}
.game-card:hover{border-color:rgba(0,255,136,.25)}
.game-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:.875rem;gap:.5rem;flex-wrap:wrap}
.game-matchup{font-size:.9rem;font-weight:700;color:var(--text);line-height:1.3}
.game-status{font-size:.68rem;font-weight:700;letter-spacing:.5px;padding:.2rem .55rem;border-radius:4px;white-space:nowrap;flex-shrink:0}
.status-live{background:rgba(255,80,80,.15);color:#ff6060;border:1px solid rgba(255,80,80,.3)}
.status-upcoming{background:rgba(255,255,255,.06);color:var(--dim);border:1px solid rgba(255,255,255,.08)}
.odds-cols{display:grid;grid-template-columns:repeat(3,1fr);gap:.5rem}
.odds-col{background:rgba(255,255,255,.025);border-radius:7px;padding:.6rem .7rem}
.odds-col-lbl{font-size:.65rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--green);margin-bottom:.5rem}
.odds-row{display:flex;justify-content:space-between;align-items:center;padding:.15rem 0}
.odds-row+.odds-row{border-top:1px solid rgba(255,255,255,.05)}
.team-side{font-size:.7rem;color:var(--dim);font-weight:500}
.odds-val{font-size:.8rem;font-weight:700;color:var(--text);font-family:'JetBrains Mono',monospace;text-align:right}
.odds-val .muted{color:var(--dim);font-weight:400;font-size:.72rem;margin-left:.15rem}
.odds-pos{color:var(--green)}
.odds-neg{color:#ff8080}
.odds-loading{padding:2rem;text-align:center;color:var(--dim);font-size:.875rem}

/* syntax */
.kw{color:#ff7b72}.str{color:#a5d6ff}.num{color:#79c0ff}.key{color:#d2a8ff}.green-hi{color:var(--green)}.gray{color:#6e7681}

/* ── PRICING ── */
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

/* ── FOOTER ── */
footer{border-top:1px solid var(--border);padding:2.5rem 2rem;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:1rem;max-width:1200px;margin:0 auto}
.ft-links{display:flex;gap:2rem}
.ft-links a{color:var(--dim);text-decoration:none;font-size:.875rem;transition:color .2s}
.ft-links a:hover{color:var(--text)}
.ft-copy{color:var(--dim);font-size:.875rem}

/* ── ANIMATIONS ── */
.fade-up{opacity:0;transform:translateY(28px);transition:opacity .65s ease,transform .65s ease}
.fade-up.in{opacity:1;transform:translateY(0)}

/* ── RESPONSIVE ── */
@media(max-width:768px){
  .demo-grid{grid-template-columns:1fr}
  .stats-grid{grid-template-columns:1fr}
  .nav-links{display:none}
  footer{flex-direction:column;text-align:center}
  .ft-links{justify-content:center}
}
@media(max-width:480px){h1{letter-spacing:-1.5px}}

/* scrollbar */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:3px}
</style>
</head>
<body>

<!-- NAV -->
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

<!-- HERO -->
<div class="hero">
  <div class="hero-glow"></div>
  <div class="hero-badge"><span class="pulse"></span>Live Data &bull; Updated Every 60 Seconds</div>
  <h1>Real-Time<br><em>Sports Odds</em> API</h1>
  <p class="hero-sub">Professional-grade sports betting data for developers. Live odds, spreads, and moneylines from top sportsbooks&mdash;delivered in milliseconds.</p>
  <div class="cta-row">
    <a href="#pricing" class="btn-green">&#128640; Get Free API Key</a>
    <a href="/docs" class="btn-outline">&#128196; View Docs &rarr;</a>
  </div>
</div>

<!-- TICKER (outside hero so it spans full width) -->
<div class="ticker-wrap">
  <div class="ticker-track" id="ticker"></div>
</div>

<!-- FEATURES -->
<section class="sec" id="features">
  <div class="sec-head fade-up">
    <div class="sec-label">Why Choose Us</div>
    <h2 class="sec-title">Everything you need to build<br>sports betting applications</h2>
    <p class="sec-desc">Reliable, fast, and comprehensive sports odds data via a clean REST API.</p>
  </div>
  <div class="feat-grid">
    <div class="feat-card fade-up">
      <span class="feat-icon">&#9889;</span>
      <h3>Live Odds</h3>
      <p>Real-time odds from DraftKings, refreshed every 60 seconds. Spreads, moneylines, and totals all in a single response.</p>
    </div>
    <div class="feat-card fade-up">
      <span class="feat-icon">&#127942;</span>
      <h3>Multiple Sports</h3>
      <p>NBA available now. NFL and MLB coming soon. One unified API format across all sports for seamless integration.</p>
    </div>
    <div class="feat-card fade-up">
      <span class="feat-icon">&#128640;</span>
      <h3>Fast Response</h3>
      <p>Sub-100ms API responses served from global infrastructure. Never miss a line movement again.</p>
    </div>
    <div class="feat-card fade-up">
      <span class="feat-icon">&#128274;</span>
      <h3>Reliable Uptime</h3>
      <p>99.9% uptime SLA with health monitoring and automatic restarts. Built on Railway with redundant infrastructure.</p>
    </div>
  </div>
  <div class="stats-grid fade-up">
    <div class="stat-card">
      <div class="stat-val" data-count="3">0</div>
      <div class="stat-lbl">Sports Available</div>
    </div>
    <div class="stat-card">
      <div class="stat-val">60s</div>
      <div class="stat-lbl">Refresh Rate</div>
    </div>
    <div class="stat-card">
      <div class="stat-val">99.9%</div>
      <div class="stat-lbl">Uptime SLA</div>
    </div>
  </div>
</section>

<!-- LIVE DEMO -->
<div class="demo-bg" id="demo">
  <div class="demo-inner">
    <div class="sec-head fade-up">
      <div class="sec-label">Live Demo</div>
      <h2 class="sec-title">Try it right now</h2>
      <p class="sec-desc">See exactly what our API returns. Pick a sport, grab the curl command.</p>
    </div>
    <div class="picker fade-up">
      <button class="sport-btn active" onclick="selectSport('nba',this)">&#127936; NBA</button>
      <button class="sport-btn" disabled title="Coming soon">&#127944; NFL &mdash; Soon</button>
      <button class="sport-btn" disabled title="Coming soon">&#9917; MLB &mdash; Soon</button>
    </div>
    <div class="demo-grid fade-up">
      <div class="tester-panel">
        <div class="tester-hdr">
          <div class="tester-hdr-left">
            <span class="method-badge">GET</span>
            <span class="tester-endpoint" id="tester-endpoint">/odds/nba</span>
          </div>
          <span class="tester-status" id="tester-status"></span>
        </div>
        <div class="tester-body">
          <div>
            <label class="tester-label" for="api-key-input">X-API-Key</label>
            <div class="key-input-row">
              <input type="text" id="api-key-input" class="key-input" placeholder="Paste your API key&hellip;" autocomplete="off" spellcheck="false">
              <button class="try-demo-btn" onclick="fillDemoKey()">Try Free Key</button>
            </div>
          </div>
          <button class="fetch-btn" id="fetch-btn" onclick="fetchOdds()">
            <span id="fetch-label">&#9889; Fetch Live Odds</span>
          </button>
          <div class="tester-meta" id="tester-meta"></div>
        </div>
      </div>
      <div class="odds-panel">
        <div class="odds-panel-hdr">
          <span class="code-lang">Live Response</span>
          <span class="live-label"><span class="pulse"></span>LIVE</span>
        </div>
        <div class="odds-cards" id="odds-cards">
          <div class="odds-loading">Loading live data&hellip;</div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- PRICING -->
<section class="sec" id="pricing">
  <div class="sec-head fade-up">
    <div class="sec-label">Pricing</div>
    <h2 class="sec-title">Simple, transparent pricing</h2>
    <p class="sec-desc">Start free, scale when you need it. No hidden fees, no lock-in.</p>
  </div>
  <div class="price-grid fade-up">
    <div class="price-card">
      <div class="plan-name">Free</div>
      <div class="price-amt">$0</div>
      <div class="price-per">forever &mdash; no credit card</div>
      <ul class="feat-list">
        <li><span class="chk">&#10003;</span> 100 requests / day</li>
        <li><span class="chk">&#10003;</span> NBA live odds</li>
        <li><span class="chk">&#10003;</span> Spreads, moneylines &amp; totals</li>
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
        <li><span class="chk">&#10003;</span> All sports (NBA, NFL, MLB)</li>
        <li><span class="chk">&#10003;</span> Spreads, moneylines &amp; totals</li>
        <li><span class="chk">&#10003;</span> Historical data access</li>
        <li><span class="chk">&#10003;</span> Priority support &amp; SLA</li>
        <li><span class="chk">&#10003;</span> Webhooks &amp; streaming</li>
      </ul>
      <a href="mailto:jbraid061@gmail.com?subject=Pro API Key Request" class="btn-plan btn-solid">Get Pro Access &rarr;</a>
    </div>
  </div>
</section>

<!-- FOOTER -->
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
// ── Ticker ───────────────────────────────────────────────────────────────────
(async function buildTicker(){
  let games=[];
  try{const r=await fetch('/odds/preview');if(r.ok){const d=await r.json();games=d.games||[];}}catch(_){}
  if(!games.length){
    games=[
      {matchup:'MIN Timberwolves @ SA Spurs',spread:{away:{line:'+4.5',odds:-110},home:{line:'-4.5',odds:-120}},moneyline:{away:180,home:-238}},
      {matchup:'CLE Cavaliers @ DET Pistons',spread:{away:{line:'+3.5',odds:-115},home:{line:'-3.5',odds:-105}},moneyline:{away:124,home:-148}},
      {matchup:'PHI 76ers @ NY Knicks',spread:{away:{line:'+7.5',odds:-115},home:{line:'-7.5',odds:-105}},moneyline:{away:225,home:-278}},
    ];
  }
  const fmt=n=>n>0?'+'+n:String(n);
  const mk=g=>`<span class="tick">
    <span class="tick-match">${g.matchup}</span>
    <span class="tick-spread">&#9670; ${g.spread?.away?.line} (${g.spread?.away?.odds})</span>
    <span class="tick-ml">ML ${fmt(g.moneyline?.away)} / ${fmt(g.moneyline?.home)}</span>
  </span>`;
  const html=games.map(mk).join('');
  const track=document.getElementById('ticker');
  track.innerHTML=html+html+html;
})();

// ── API Tester ────────────────────────────────────────────────────────────────
let sport='nba';
const DEMO_KEY='free_demo_preview';

function selectSport(s,btn){
  sport=s;
  document.querySelectorAll('.sport-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('tester-endpoint').textContent='/odds/'+s;
}

function fillDemoKey(){
  const inp=document.getElementById('api-key-input');
  inp.value=DEMO_KEY;
  inp.focus();
  inp.select();
}

async function fetchOdds(){
  const key=document.getElementById('api-key-input').value.trim();
  const btn=document.getElementById('fetch-btn');
  const lbl=document.getElementById('fetch-label');
  const statusEl=document.getElementById('tester-status');
  const metaEl=document.getElementById('tester-meta');
  const cardsEl=document.getElementById('odds-cards');

  btn.disabled=true;
  lbl.innerHTML='<span class="spin">&#9696;</span> Fetching&hellip;';
  cardsEl.innerHTML='<div class="odds-loading">Fetching live odds&hellip;</div>';
  statusEl.className='tester-status';
  statusEl.textContent='';
  metaEl.textContent='';

  const t0=performance.now();
  try{
    const usePreview=!key||key===DEMO_KEY;
    const url=usePreview?'/odds/preview':'/odds/'+sport;
    const opts=usePreview?{}:{headers:{'X-API-Key':key}};
    const r=await fetch(url,opts);
    const ms=Math.round(performance.now()-t0);
    const data=await r.json();
    if(r.ok){
      statusEl.className='tester-status status-ok';
      statusEl.textContent=r.status+' OK';
      const games=data.games||[];
      metaEl.textContent='Responded in '+ms+'ms · '+games.length+' game'+(games.length!==1?'s':'');
      renderCards(games);
    }else{
      statusEl.className='tester-status status-err';
      statusEl.textContent=r.status+' Error';
      metaEl.textContent=data.detail||'Request failed';
      cardsEl.innerHTML='<div class="odds-loading">'+(data.detail||'Error fetching odds.')+'</div>';
    }
  }catch(_){
    statusEl.className='tester-status status-err';
    statusEl.textContent='Network Error';
    metaEl.textContent='Could not reach the API';
    cardsEl.innerHTML='<div class="odds-loading">Network error — check your connection.</div>';
  }
  btn.disabled=false;
  lbl.innerHTML='&#9889; Fetch Live Odds';
}

function renderCards(games){
  const el=document.getElementById('odds-cards');
  if(!games.length){el.innerHTML='<div class="odds-loading">No games available.</div>';return;}
  const fmt=n=>n==null?'N/A':n>0?'+'+n:String(n);
  const mlCls=n=>n==null?'':n>0?'odds-pos':'odds-neg';
  const isLive=t=>t&&/quarter|half|period|inning|OT/i.test(t);
  el.innerHTML=games.map(g=>{
    const[away,home]=g.matchup.split(' @ ');
    const live=isLive(g.game_time);
    const statusCls=live?'status-live':'status-upcoming';
    const statusTxt=live?g.game_time:'Upcoming';
    return`<div class="game-card">
  <div class="game-hdr">
    <span class="game-matchup">${away||''} <span style="color:var(--dim);font-weight:400">@</span> ${home||g.matchup}</span>
    <span class="game-status ${statusCls}">${statusTxt}</span>
  </div>
  <div class="odds-cols">
    <div class="odds-col">
      <div class="odds-col-lbl">Spread</div>
      <div class="odds-row"><span class="team-side">Away</span><span class="odds-val">${g.spread?.away?.line||'—'}<span class="muted">${g.spread?.away?.odds!=null?'('+g.spread.away.odds+')':''}</span></span></div>
      <div class="odds-row"><span class="team-side">Home</span><span class="odds-val">${g.spread?.home?.line||'—'}<span class="muted">${g.spread?.home?.odds!=null?'('+g.spread.home.odds+')':''}</span></span></div>
    </div>
    <div class="odds-col">
      <div class="odds-col-lbl">Moneyline</div>
      <div class="odds-row"><span class="team-side">Away</span><span class="odds-val ${mlCls(g.moneyline?.away)}">${fmt(g.moneyline?.away)}</span></div>
      <div class="odds-row"><span class="team-side">Home</span><span class="odds-val ${mlCls(g.moneyline?.home)}">${fmt(g.moneyline?.home)}</span></div>
    </div>
    <div class="odds-col">
      <div class="odds-col-lbl">Total</div>
      <div class="odds-row"><span class="team-side">Over</span><span class="odds-val">${g.total?.over?.line||'—'}<span class="muted">${g.total?.over?.odds!=null?'('+g.total.over.odds+')':''}</span></span></div>
      <div class="odds-row"><span class="team-side">Under</span><span class="odds-val">${g.total?.under?.line||'—'}<span class="muted">${g.total?.under?.odds!=null?'('+g.total.under.odds+')':''}</span></span></div>
    </div>
  </div>
</div>`;
  }).join('');
}

fetchOdds();

// ── Scroll fade-in ────────────────────────────────────────────────────────────
const obs=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting){e.target.classList.add('in');obs.unobserve(e.target);}}),{threshold:.1});
document.querySelectorAll('.fade-up').forEach(el=>obs.observe(el));

// ── Count-up animation ────────────────────────────────────────────────────────
const cObs=new IntersectionObserver(es=>es.forEach(e=>{
  if(e.isIntersecting&&e.target.dataset.count){
    const t=+e.target.dataset.count,el=e.target;
    let v=0;const step=t/40;
    const id=setInterval(()=>{v+=step;if(v>=t){v=t;clearInterval(id);}el.textContent=Math.round(v);},30);
    cObs.unobserve(el);
  }
}),{threshold:.5});
document.querySelectorAll('[data-count]').forEach(el=>cObs.observe(el));
</script>
</body>
</html>"""
