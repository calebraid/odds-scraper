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
KALSHI_BASE = "https://trading-api.kalshi.com"

_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
_KEY_PEM = os.environ.get("KALSHI_PRIVATE_KEY", "")

# Normalize PEM in case the env var stores literal \n instead of real newlines
if _KEY_PEM and "\\n" in _KEY_PEM:
    _KEY_PEM = _KEY_PEM.replace("\\n", "\n")

_private_key = (
    serialization.load_pem_private_key(_KEY_PEM.encode(), password=None)
    if _KEY_PEM else None
)


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


async def scrape_kalshi_nba() -> list[dict]:
    """Fetch all open KXNBA markets from Kalshi, handling cursor pagination."""
    if not _private_key:
        raise RuntimeError("KALSHI_PRIVATE_KEY environment variable not set")
    if not _KEY_ID:
        raise RuntimeError("KALSHI_API_KEY_ID environment variable not set")

    markets: list[dict] = []
    cursor: str | None = None
    path = "/trade-api/v2/markets"

    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            params: dict = {"status": "open", "series_ticker": "KXNBA", "limit": 200}
            if cursor:
                params["cursor"] = cursor

            hdrs = make_kalshi_headers("GET", path)
            print(f"  [kalshi] request headers: {hdrs}")
            resp = await client.get(
                f"{KALSHI_BASE}{path}",
                params=params,
                headers=hdrs,
            )
            print(f"  [kalshi] response status: {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()

            for m in (data.get("markets") or []):
                markets.append({
                    "ticker": m.get("ticker"),
                    "event_ticker": m.get("event_ticker"),
                    "title": m.get("title"),
                    "yes_price": m.get("yes_ask"),
                    "no_price": m.get("no_ask"),
                    "volume": m.get("volume"),
                    "open_interest": m.get("open_interest"),
                    "status": m.get("status"),
                })

            cursor = data.get("cursor")
            if not cursor:
                break

    return markets


def save(markets: list[dict], timestamp: str) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    payload = {
        "source": "Kalshi",
        "league": "NBA",
        "scraped_at": timestamp,
        "count": len(markets),
        "markets": markets,
    }
    with open(KALSHI_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return KALSHI_OUTPUT


async def main():
    print(f"Kalshi NBA scraper  |  interval={INTERVAL_SECONDS}s")
    print(f"  KALSHI_API_KEY_ID  : {_KEY_ID!r}")
    _key_preview = (_KEY_PEM[:20] + "...") if _KEY_PEM else "NOT SET"
    print(f"  KALSHI_PRIVATE_KEY : {_key_preview!r}")
    print(f"  private key loaded : {_private_key is not None}")
    run = 0
    while True:
        run += 1
        ts = datetime.now(timezone.utc).isoformat()
        print(f"\n[{ts}] run #{run}")
        try:
            markets = await scrape_kalshi_nba()
            out = save(markets, ts)
            print(f"  {len(markets)} market(s) -> {out}")
        except Exception as exc:
            print(f"  ERROR: {exc}")
        print(f"  sleeping {INTERVAL_SECONDS}s ...")
        await asyncio.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
