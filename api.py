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


@app.get("/odds/nba/kalshi", summary="NBA prediction markets from Kalshi")
def get_nba_kalshi(auth: Annotated[dict, Depends(authenticate)], response: Response):
    _set_rate_limit_headers(response, auth)
    data = load_odds("kalshi_latest.json")
    warning = staleness_warning(data.get("scraped_at"))
    result = {
        "source": data.get("source"),
        "league": data.get("league"),
        "scraped_at": data.get("scraped_at"),
        "count": data.get("count", len(data.get("markets", []))),
        "markets": data.get("markets", []),
    }
    if warning:
        result["warning"] = warning
    return JSONResponse(content=result, headers=dict(response.headers))


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing_page():
    return HTMLResponse(content=_LANDING_HTML)


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
