import asyncio
import base64
import json
import os
import time
from datetime import datetime, timezone

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

OUTPUT_DIR = "odds"
INTERVAL_SECONDS = 60
KALSHI_OUTPUT = os.path.join(OUTPUT_DIR, "kalshi_latest.json")
KALSHI_BASE = "https://external-api.kalshi.com"

_KEY_ID = os.environ.get("KALSHI_API_KEY", "")
_KEY_PEM = os.environ.get("KALSHI_PRIVATE_KEY", "")

if _KEY_PEM and "\\n" in _KEY_PEM:
    _KEY_PEM = _KEY_PEM.replace("\\n", "\n")

_private_key = (
    serialization.load_pem_private_key(_KEY_PEM.encode(), password=None)
    if _KEY_PEM else None
)

FUTURES_SERIES = "KXNBA"
GAME_SERIES = {
    "KXNBAA": "winner",
    "KXNBAT": "total",
    "KXNBAS": "spread",
}


def make_kalshi_headers(method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    msg = (ts + method.upper() + path.split("?")[0]).encode()
    sig = _private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": _KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }


async def fetch_series(client: httpx.AsyncClient, series_ticker: str) -> list[dict]:
    """Fetch all open markets for one series, handling cursor pagination."""
    markets: list[dict] = []
    cursor: str | None = None
    path = "/trade-api/v2/markets"

    while True:
        params: dict = {"status": "open", "series_ticker": series_ticker, "limit": 200}
        if cursor:
            params["cursor"] = cursor

        resp = await client.get(
            f"{KALSHI_BASE}{path}",
            params=params,
            headers=make_kalshi_headers("GET", path),
        )
        resp.raise_for_status()
        data = resp.json()
        markets.extend(data.get("markets") or [])

        cursor = data.get("cursor")
        if not cursor:
            break

    return markets


def parse_market(m: dict, market_type: str) -> dict:
    return {
        "ticker": m.get("ticker"),
        "event_ticker": m.get("event_ticker"),
        "market_type": market_type,
        "title": m.get("title"),
        "yes_team": m.get("yes_sub_title"),
        "no_team": m.get("no_sub_title"),
        "yes_bid": m.get("yes_bid_dollars"),
        "yes_ask": m.get("yes_ask_dollars"),
        "no_bid": m.get("no_bid_dollars"),
        "no_ask": m.get("no_ask_dollars"),
        "last_price": m.get("last_price_dollars"),
        "close_time": m.get("close_time"),
        "volume": m.get("volume_fp"),
        "open_interest": m.get("open_interest_fp"),
        "status": m.get("status"),
    }


async def discover_series() -> None:
    """Fetch all series from Kalshi and log ticker + title for discovery."""
    path = "/trade-api/v2/series"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{KALSHI_BASE}{path}",
            headers=make_kalshi_headers("GET", path),
        )
        resp.raise_for_status()
        data = resp.json()

    series_list = data.get("series") or []
    print(f"  [discover] {len(series_list)} series total:")
    for s in series_list:
        print(f"    {s.get('ticker', '?'):20s}  {s.get('title', '')}")


async def scrape_kalshi_nba() -> dict:
    """Fetch NBA futures (KXNBA) and game markets (KXNBAA/T/S) concurrently."""
    if not _private_key:
        raise RuntimeError("KALSHI_PRIVATE_KEY environment variable not set")
    if not _KEY_ID:
        raise RuntimeError("KALSHI_API_KEY environment variable not set")

    all_series = [FUTURES_SERIES] + list(GAME_SERIES.keys())

    async with httpx.AsyncClient(timeout=15) as client:
        results = await asyncio.gather(
            *[fetch_series(client, s) for s in all_series],
            return_exceptions=True,
        )

    futures_raw, *game_raws = results

    if isinstance(futures_raw, Exception):
        print(f"  ERROR fetching {FUTURES_SERIES}: {futures_raw}")
        futures = []
    else:
        futures = [parse_market(m, "futures") for m in futures_raw]

    game_markets: list[dict] = []
    for series_ticker, raw in zip(GAME_SERIES.keys(), game_raws):
        mtype = GAME_SERIES[series_ticker]
        if isinstance(raw, Exception):
            print(f"  ERROR fetching {series_ticker} ({mtype}): {raw}")
        else:
            game_markets.extend(parse_market(m, mtype) for m in raw)
            print(f"  {series_ticker} ({mtype}): {len(raw)} market(s)")

    return {"futures": futures, "game_markets": game_markets}


def save(data: dict, timestamp: str) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    payload = {
        "source": "Kalshi",
        "league": "NBA",
        "scraped_at": timestamp,
        "futures_count": len(data["futures"]),
        "game_markets_count": len(data["game_markets"]),
        "futures": data["futures"],
        "game_markets": data["game_markets"],
    }
    with open(KALSHI_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return KALSHI_OUTPUT


async def main():
    print(f"Kalshi NBA scraper  |  interval={INTERVAL_SECONDS}s")
    try:
        await discover_series()
    except Exception as exc:
        print(f"  [discover] ERROR: {exc}")
    run = 0
    while True:
        run += 1
        ts = datetime.now(timezone.utc).isoformat()
        print(f"\n[{ts}] run #{run}")
        try:
            data = await scrape_kalshi_nba()
            out = save(data, ts)
            print(f"  {len(data['futures'])} futures, {len(data['game_markets'])} game markets -> {out}")
        except Exception as exc:
            print(f"  ERROR: {exc}")
        print(f"  sleeping {INTERVAL_SECONDS}s ...")
        await asyncio.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
