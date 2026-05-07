import asyncio
import json
import os
from datetime import datetime, timezone

import httpx

OUTPUT_DIR = "odds"
INTERVAL_SECONDS = 60
KALSHI_API_KEY = os.environ.get("KALSHI_API_KEY", "")
KALSHI_OUTPUT = os.path.join(OUTPUT_DIR, "kalshi_latest.json")


async def scrape_kalshi_nba() -> list[dict]:
    """Fetch all open KXNBA markets from Kalshi, handling cursor pagination."""
    if not KALSHI_API_KEY:
        raise RuntimeError("KALSHI_API_KEY environment variable not set")

    markets: list[dict] = []
    cursor: str | None = None

    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            params: dict = {"status": "open", "series_ticker": "KXNBA", "limit": 200}
            if cursor:
                params["cursor"] = cursor

            resp = await client.get(
                "https://trading-api.kalshi.com/trade-api/v2/markets",
                params=params,
                headers={"Authorization": KALSHI_API_KEY},
            )
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
