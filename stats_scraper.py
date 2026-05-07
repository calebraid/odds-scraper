import asyncio
import json
import os
from datetime import datetime, timezone

import httpx

STATS_DIR = "stats"
TEAM_STATS_OUTPUT = os.path.join(STATS_DIR, "team_stats.json")
RECENT_GAMES_OUTPUT = os.path.join(STATS_DIR, "recent_games.json")

BDL_BASE = "https://api.balldontlie.io/v1"
SEASON = 2025  # 2025-26 NBA season

_BDL_KEY = os.environ.get("BALLDONTLIE_API_KEY", "")


def _headers() -> dict:
    h = {"Accept": "application/json"}
    if _BDL_KEY:
        h["Authorization"] = _BDL_KEY
    return h


async def _fetch_paginated(client: httpx.AsyncClient, path: str, params: dict) -> list[dict]:
    results: list[dict] = []
    cursor: int | None = None
    while True:
        p = dict(params)
        if cursor is not None:
            p["cursor"] = cursor
        r = await client.get(f"{BDL_BASE}{path}", params=p, headers=_headers(), timeout=20)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("data", []))
        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor:
            break
    return results


async def _fetch_teams(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(f"{BDL_BASE}/teams", params={"per_page": 100}, headers=_headers(), timeout=20)
    r.raise_for_status()
    return r.json().get("data", [])


async def _fetch_games(client: httpx.AsyncClient) -> list[dict]:
    return await _fetch_paginated(client, "/games", {"seasons[]": SEASON, "per_page": 100})


def _compute_stats(teams_raw: list[dict], games: list[dict]) -> dict:
    # Index teams by id
    team_by_id: dict[int, dict] = {t["id"]: t for t in teams_raw}

    # Per-team accumulators
    record: dict[int, dict] = {
        t["id"]: {"wins": 0, "losses": 0, "pts_for": [], "pts_against": [], "games": []}
        for t in teams_raw
    }

    # Sort games newest-first for recent form ordering
    sorted_games = sorted(games, key=lambda g: g.get("date", ""), reverse=True)

    for g in sorted_games:
        ht = g.get("home_team", {})
        vt = g.get("visitor_team", {})
        hs = g.get("home_team_score")
        vs = g.get("visitor_team_score")
        date = (g.get("date") or "")[:10]
        status = g.get("status", "")

        # Only count finished games (score present and non-zero)
        if hs is None or vs is None or (hs == 0 and vs == 0):
            continue
        # balldontlie marks finals as "Final" or contains "Final"
        if status and "Final" not in status and status not in ("", "final"):
            continue

        hid, vid = ht.get("id"), vt.get("id")
        if hid not in record or vid not in record:
            continue

        h_win = hs > vs
        habrv = ht.get("abbreviation", "?")
        vabrv = vt.get("abbreviation", "?")

        for tid, pts, opp, won, matchup in (
            (hid, hs, vs, h_win, f"{habrv} vs {vabrv}"),
            (vid, vs, hs, not h_win, f"{vabrv} vs {habrv}"),
        ):
            rec = record[tid]
            if won:
                rec["wins"] += 1
            else:
                rec["losses"] += 1
            rec["pts_for"].append(pts)
            rec["pts_against"].append(opp)
            rec["games"].append({
                "date": date,
                "matchup": matchup,
                "wl": "W" if won else "L",
                "pts": pts,
                "opp_pts": opp,
            })

    teams_out: list[dict] = []
    recent_out: list[dict] = []

    for t in teams_raw:
        tid = t["id"]
        rec = record[tid]
        wins = rec["wins"]
        losses = rec["losses"]
        total = wins + losses
        win_pct = round(wins / total, 3) if total else None

        pts_for = rec["pts_for"]
        pts_against = rec["pts_against"]
        ppg = round(sum(pts_for) / len(pts_for), 1) if pts_for else None
        opp_ppg = round(sum(pts_against) / len(pts_against), 1) if pts_against else None

        # Approximate rating proxies from scoring data (no pace data available)
        net_rtg = round(ppg - opp_ppg, 1) if (ppg and opp_ppg) else None

        full_name = t.get("full_name") or f"{t.get('city', '')} {t.get('name', '')}".strip()

        teams_out.append({
            "team_id": str(tid),
            "team_name": full_name,
            "wins": wins,
            "losses": losses,
            "win_pct": win_pct,
            "ppg": ppg,
            "off_rtg": ppg,       # proxy: pts scored
            "def_rtg": opp_ppg,   # proxy: pts allowed
            "net_rtg": net_rtg,
            "pace": None,
        })

        recent_out.append({
            "team_id": str(tid),
            "team_name": full_name,
            "games": rec["games"][:10],  # most recent 10
        })

    print(f"  computed stats for {len(teams_out)} teams from {len(games)} games")
    return {"teams": teams_out, "recent": recent_out}


async def fetch_nba_stats() -> dict:
    async with httpx.AsyncClient() as client:
        teams_raw, games = await asyncio.gather(
            _fetch_teams(client),
            _fetch_games(client),
        )
    print(f"  fetched {len(teams_raw)} teams, {len(games)} games")
    return _compute_stats(teams_raw, games)


def save_stats(data: dict, timestamp: str) -> None:
    os.makedirs(STATS_DIR, exist_ok=True)

    with open(TEAM_STATS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(
            {"scraped_at": timestamp, "count": len(data["teams"]), "teams": data["teams"]},
            f,
            indent=2,
        )

    with open(RECENT_GAMES_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(
            {"scraped_at": timestamp, "teams": data["recent"]},
            f,
            indent=2,
        )

    print(f"  saved {len(data['teams'])} teams -> {TEAM_STATS_OUTPUT}")


async def main():
    interval = 3600
    print(f"NBA Stats scraper (balldontlie)  |  interval={interval}s")
    run = 0
    while True:
        run += 1
        ts = datetime.now(timezone.utc).isoformat()
        print(f"\n[{ts}] stats run #{run}")
        try:
            data = await fetch_nba_stats()
            save_stats(data, ts)
        except Exception as exc:
            print(f"  ERROR: {exc}")
        print(f"  sleeping {interval}s ...")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
