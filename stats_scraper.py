import asyncio
import json
import os
from datetime import datetime, timezone

import httpx

STATS_DIR = "stats"
TEAM_STATS_OUTPUT = os.path.join(STATS_DIR, "team_stats.json")
RECENT_GAMES_OUTPUT = os.path.join(STATS_DIR, "recent_games.json")

_NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.nba.com/",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Accept": "application/json",
}

SEASON = "2025-26"


def _rows_to_dicts(result_set: dict) -> list[dict]:
    headers = result_set["headers"]
    return [dict(zip(headers, row)) for row in result_set["rowSet"]]


async def _fetch_team_stats(client: httpx.AsyncClient, measure_type: str) -> list[dict]:
    for season_type in ("Playoffs", "Regular Season"):
        try:
            r = await client.get(
                "https://stats.nba.com/stats/leaguedashteamstats",
                params={
                    "MeasureType": measure_type,
                    "PerMode": "PerGame",
                    "Season": SEASON,
                    "SeasonType": season_type,
                    "LeagueID": "00",
                },
                headers=_NBA_HEADERS,
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            rows = _rows_to_dicts(data["resultSets"][0])
            if rows:
                print(f"  team_stats({measure_type}, {season_type}): {len(rows)} teams")
                return rows
        except Exception as exc:
            print(f"  WARN team_stats({measure_type}, {season_type}): {exc}")
    return []


async def _fetch_game_log(client: httpx.AsyncClient) -> list[dict]:
    for season_type in ("Playoffs", "Regular Season"):
        try:
            r = await client.get(
                "https://stats.nba.com/stats/leaguegamelog",
                params={
                    "Season": SEASON,
                    "SeasonType": season_type,
                    "LeagueID": "00",
                    "Sorter": "DATE",
                    "Direction": "DESC",
                },
                headers=_NBA_HEADERS,
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            rows = _rows_to_dicts(data["resultSets"][0])
            if rows:
                print(f"  game_log({season_type}): {len(rows)} entries")
                return rows
        except Exception as exc:
            print(f"  WARN game_log({season_type}): {exc}")
    return []


def _build_recent(game_log: list[dict], n: int = 10) -> dict[str, list[dict]]:
    recent: dict[str, list[dict]] = {}
    for row in game_log:
        tid = str(row.get("TEAM_ID", ""))
        if tid not in recent:
            recent[tid] = []
        if len(recent[tid]) >= n:
            continue
        pts = row.get("PTS")
        pm = row.get("PLUS_MINUS")
        opp_pts = (pts - pm) if (pts is not None and pm is not None) else None
        recent[tid].append({
            "date": row.get("GAME_DATE"),
            "matchup": row.get("MATCHUP"),
            "wl": row.get("WL"),
            "pts": pts,
            "opp_pts": opp_pts,
        })
    return recent


async def fetch_nba_stats() -> dict:
    async with httpx.AsyncClient() as client:
        base_task = asyncio.create_task(_fetch_team_stats(client, "Base"))
        adv_task = asyncio.create_task(_fetch_team_stats(client, "Advanced"))
        log_task = asyncio.create_task(_fetch_game_log(client))
        base_rows, adv_rows, game_log = await asyncio.gather(base_task, adv_task, log_task)

    adv_by_id = {str(r["TEAM_ID"]): r for r in adv_rows}
    recent = _build_recent(game_log)

    teams: list[dict] = []
    for row in base_rows:
        tid = str(row.get("TEAM_ID", ""))
        adv = adv_by_id.get(tid, {})
        teams.append({
            "team_id": tid,
            "team_name": row.get("TEAM_NAME"),
            "wins": row.get("W"),
            "losses": row.get("L"),
            "win_pct": row.get("W_PCT"),
            "ppg": row.get("PTS"),
            "off_rtg": adv.get("OFF_RATING"),
            "def_rtg": adv.get("DEF_RATING"),
            "net_rtg": adv.get("NET_RATING"),
            "pace": adv.get("PACE"),
        })

    recent_out: list[dict] = []
    for tid, games in recent.items():
        team_name = next((t["team_name"] for t in teams if t["team_id"] == tid), None)
        recent_out.append({"team_id": tid, "team_name": team_name, "games": games})

    return {"teams": teams, "recent": recent_out}


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
    print(f"NBA Stats scraper  |  interval={interval}s")
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
