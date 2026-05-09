import asyncio
import base64
import json
import os
import re
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
    "KXNBATEAMTOTAL": "team_total",
    "KXNBASERIESSPREAD": "series_spread",
    "KXNBA2HWINNER": "2h_winner",
    "KXNBARA": "reb_assists",
    "KXNBABLK": "blocks",
    "KXNBASTL": "steals",
    "KXNBA3D": "triple_double",
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


async def fetch_series(
    client: httpx.AsyncClient,
    series_ticker: str,
    status: str | None = "open",
) -> list[dict]:
    """Fetch markets for one series with cursor pagination.

    Pass status=None to skip the status filter (needed for KXNBAA winner markets
    which Kalshi may not mark as 'open' even when the game is upcoming).
    """
    markets: list[dict] = []
    cursor: str | None = None
    path = "/trade-api/v2/markets"

    while True:
        params: dict = {"series_ticker": series_ticker, "limit": 200}
        if status is not None:
            params["status"] = status
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


_WINNER_TITLE_RE = re.compile(r"Will (?:the )?(.+?) win\?", re.IGNORECASE)


def parse_market(m: dict, market_type: str) -> dict:
    yes_team = (m.get("yes_sub_title") or "").strip() or None
    no_team  = (m.get("no_sub_title")  or "").strip() or None

    # For winner markets Kalshi sometimes leaves subtitles empty; parse the title.
    # Title format: "Will the Indiana Pacers win?" or "Will Indiana Pacers win?"
    if market_type == "winner" and not yes_team:
        hit = _WINNER_TITLE_RE.search(m.get("title") or "")
        if hit:
            yes_team = hit.group(1).strip()

    # Filter out generic non-team strings in no_team
    if no_team and no_team.lower() in {"no", "no team", "other"}:
        no_team = None

    return {
        "ticker":        m.get("ticker"),
        "event_ticker":  m.get("event_ticker"),
        "market_type":   market_type,
        "title":         m.get("title"),
        "yes_team":      yes_team,
        "no_team":       no_team,
        "yes_bid":       m.get("yes_bid_dollars"),
        "yes_ask":       m.get("yes_ask_dollars"),
        "no_bid":        m.get("no_bid_dollars"),
        "no_ask":        m.get("no_ask_dollars"),
        "last_price":    m.get("last_price_dollars"),
        "close_time":    m.get("close_time"),
        "volume":        m.get("volume_fp"),
        "open_interest": m.get("open_interest_fp"),
        "status":        m.get("status"),
    }


def _pair_winner_markets(game_markets: list[dict]) -> None:
    """For winner markets that share an event_ticker, cross-populate no_team.

    Kalshi creates two winner markets per game — one per team.  Both belong to the
    same event_ticker.  If one market has yes_team but no no_team, fill in no_team
    from the other market's yes_team.
    """
    by_event: dict[str, list[dict]] = {}
    for m in game_markets:
        if m.get("market_type") == "winner":
            et = m.get("event_ticker") or m.get("ticker", "")
            by_event.setdefault(et, []).append(m)

    for mkts in by_event.values():
        if len(mkts) == 2:
            a, b = mkts
            if not a.get("no_team") and b.get("yes_team"):
                a["no_team"] = b["yes_team"]
            if not b.get("no_team") and a.get("yes_team"):
                b["no_team"] = a["yes_team"]


# Winner markets may not carry status="open" even when the game is upcoming,
# so skip the status filter for KXNBAA to avoid getting 0 results.
_SERIES_STATUS: dict[str, str | None] = {
    "KXNBAA": None,  # winner — fetch regardless of status
}


async def scrape_kalshi_nba() -> dict:
    """Fetch NBA futures (KXNBA) and game markets (KXNBAA/T/S) concurrently."""
    if not _private_key:
        raise RuntimeError("KALSHI_PRIVATE_KEY environment variable not set")
    if not _KEY_ID:
        raise RuntimeError("KALSHI_API_KEY environment variable not set")

    all_series = [FUTURES_SERIES] + list(GAME_SERIES.keys())

    async with httpx.AsyncClient(timeout=15) as client:
        results = await asyncio.gather(
            *[
                fetch_series(client, s, status=_SERIES_STATUS.get(s, "open"))
                for s in all_series
            ],
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

    _pair_winner_markets(game_markets)

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
