import json
import os
from datetime import datetime, timezone, timedelta
from typing import Annotated

from fastapi import FastAPI, HTTPException, Security, Depends, Response
from fastapi.security import APIKeyHeader
from fastapi.responses import JSONResponse

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
