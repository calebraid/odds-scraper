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

_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_BASE_DIR, "odds")
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

# Alternative series tickers to try when KXNBAA returns 0 markets.
# Kalshi may rename series between seasons or for playoffs.
_WINNER_FALLBACK_SERIES = ["KXNBAWIN", "KXNBAWINNER", "KXNBAGAME", "KXNBA"]


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


async def _discover_nba_series(client: httpx.AsyncClient) -> list[str]:
    """Fetch /series and log every NBA-related series ticker.

    Called once per scrape run for visibility — the result guides which
    series tickers to use for game winner fetches.
    """
    path = "/trade-api/v2/series"
    try:
        resp = await client.get(
            f"{KALSHI_BASE}{path}",
            headers=make_kalshi_headers("GET", path),
        )
        resp.raise_for_status()
        data = resp.json()
        all_series = data.get("series", [])
        nba = [
            s for s in all_series
            if "NBA" in (s.get("ticker") or "").upper()
            or "NBA" in (s.get("title") or "").upper()
        ]
        if nba:
            print(f"  [series] {len(nba)} NBA series found:")
            for s in nba:
                print(f"    ticker={s.get('ticker')}  title={s.get('title')}")
        else:
            print(f"  [series] 0 NBA series found in {len(all_series)} total | "
                  f"sample: {[s.get('ticker') for s in all_series[:8]]}")
        return [s.get("ticker") for s in nba if s.get("ticker")]
    except Exception as exc:
        print(f"  [series] ERROR: {exc}")
        return []


async def _fetch_winner_markets_with_debug(client: httpx.AsyncClient) -> list[dict]:
    """Try KXNBAA and fallback tickers to find NBA winner markets.

    Logs the raw response preview so we can diagnose empty results.
    Returns the first non-empty result list found.
    """
    path = "/trade-api/v2/markets"
    all_tickers = ["KXNBAA"] + _WINNER_FALLBACK_SERIES

    for ticker in all_tickers:
        for status_val in [None, "open", "active"]:
            params: dict = {"series_ticker": ticker, "limit": 200}
            if status_val is not None:
                params["status"] = status_val
            try:
                resp = await client.get(
                    f"{KALSHI_BASE}{path}",
                    params=params,
                    headers=make_kalshi_headers("GET", path),
                )
                data = resp.json()
                markets = data.get("markets") or []
                label = f"series_ticker={ticker} status={status_val}"
                print(f"  [KXNBAA-debug] {label}: {len(markets)} market(s) | "
                      f"raw={str(data)[:300]}")
                if markets:
                    print(f"  [KXNBAA-debug] SUCCESS with {label}, using these markets")
                    return markets
            except Exception as exc:
                print(f"  [KXNBAA-debug] series_ticker={ticker} status={status_val}: ERROR {exc}")

    # Last-resort: try fetching by tickers= param (looks up a specific ticker, not series)
    try:
        params = {"tickers": "KXNBAA", "limit": 5}
        resp = await client.get(
            f"{KALSHI_BASE}{path}",
            params=params,
            headers=make_kalshi_headers("GET", path),
        )
        data = resp.json()
        print(f"  [KXNBAA-debug] tickers=KXNBAA lookup: {str(data)[:300]}")
    except Exception as exc:
        print(f"  [KXNBAA-debug] tickers=KXNBAA lookup: ERROR {exc}")

    return []


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

    # Non-winner series are fetched concurrently with status=open.
    non_winner_series = {k: v for k, v in GAME_SERIES.items() if k != "KXNBAA"}
    all_series = [FUTURES_SERIES] + list(non_winner_series.keys())

    async with httpx.AsyncClient(timeout=30) as client:
        # Run non-winner fetches + series discovery concurrently
        results_and_debug = await asyncio.gather(
            *[
                fetch_series(client, s, status=_SERIES_STATUS.get(s, "open"))
                for s in all_series
            ],
            _discover_nba_series(client),
            _fetch_winner_markets_with_debug(client),
            return_exceptions=True,
        )

    # Last two results are discovery (ignored) and winner markets
    *non_winner_results, _series_discovery, winner_raw = results_and_debug

    futures_raw, *game_raws = non_winner_results

    if isinstance(futures_raw, Exception):
        print(f"  ERROR fetching {FUTURES_SERIES}: {futures_raw}")
        futures = []
    else:
        futures = [parse_market(m, "futures") for m in futures_raw]

    game_markets: list[dict] = []
    for series_ticker, raw in zip(non_winner_series.keys(), game_raws):
        mtype = non_winner_series[series_ticker]
        if isinstance(raw, Exception):
            print(f"  ERROR fetching {series_ticker} ({mtype}): {raw}")
        else:
            game_markets.extend(parse_market(m, mtype) for m in raw)
            print(f"  {series_ticker} ({mtype}): {len(raw)} market(s)")

    # Winner markets (KXNBAA) — result from debug/fallback fetch
    if isinstance(winner_raw, Exception):
        print(f"  ERROR fetching winner markets: {winner_raw}")
        winner_raw = []
    winner_markets = [parse_market(m, "winner") for m in (winner_raw or [])]
    game_markets.extend(winner_markets)
    print(f"  KXNBAA (winner): {len(winner_markets)} market(s)")

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
