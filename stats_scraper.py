import asyncio
import json
import os
from datetime import datetime, timezone

import httpx

STATS_DIR = "stats"
TEAM_STATS_OUTPUT = os.path.join(STATS_DIR, "team_stats.json")
PLAYER_STATS_OUTPUT = os.path.join(STATS_DIR, "player_stats.json")
RECENT_GAMES_OUTPUT = os.path.join(STATS_DIR, "recent_games.json")

BASE_URL = "https://nba-go-api.onrender.com"
SEASON = 2025  # 2025-26 NBA season


def _get(*keys, obj, default=None):
    """Return first matching key from obj."""
    for k in keys:
        if k in obj:
            return obj[k]
    return default


def _rows(data: dict, *keys) -> list[dict]:
    """Extract list from response trying multiple root keys."""
    for k in keys:
        v = data.get(k)
        if isinstance(v, list):
            return v
    return []


async def _fetch_paginated(client: httpx.AsyncClient, path: str, params: dict) -> list[dict]:
    results: list[dict] = []
    page = 1
    while True:
        p = {**params, "page": page}
        r = await client.get(f"{BASE_URL}{path}", params=p, timeout=30)
        r.raise_for_status()
        data = r.json()

        chunk = _rows(data, "data", "games", "playerTotals", "players", "results")
        if not chunk:
            break
        results.extend(chunk)

        # Stop when we've received fewer items than pageSize (last page)
        page_size = params.get("pageSize", 100)
        if len(chunk) < page_size:
            break
        page += 1

    return results


async def _fetch_games(client: httpx.AsyncClient) -> list[dict]:
    games = await _fetch_paginated(
        client,
        "/api/games",
        {"pageSize": 100, "ascending": "false", "include": "teamGameBasicStats"},
    )
    print(f"  fetched {len(games)} games")
    return games


async def _fetch_player_totals(client: httpx.AsyncClient) -> list[dict]:
    totals = await _fetch_paginated(
        client,
        "/api/playertotals",
        {"season": SEASON, "pageSize": 100, "sortBy": "points"},
    )
    print(f"  fetched {len(totals)} player total rows")
    return totals


def _parse_game_teams(game: dict) -> tuple[dict, dict] | None:
    """
    Return (home_entry, away_entry) as dicts with keys:
    team_id, team_name, team_abbr, pts, opp_pts, is_home.

    Handles two layouts:
    A) game has teamGameBasicStats list of 2 items
    B) game has flat homeTeam/awayTeam sub-objects with score at top level
    """
    stats = game.get("teamGameBasicStats")
    if isinstance(stats, list) and len(stats) == 2:
        entries = []
        for s in stats:
            entries.append({
                "team_id": str(_get("teamId", "team_id", "id", obj=s, default="")),
                "team_name": _get("teamName", "team_name", "name", obj=s, default=""),
                "team_abbr": _get("teamAbbreviation", "team_abbr", "abbreviation", obj=s, default="?"),
                "pts": _get("points", "pts", "score", obj=s),
                "is_home": _get("isHome", "is_home", obj=s, default=False),
            })
        # Identify home / away
        home = next((e for e in entries if e["is_home"]), entries[0])
        away = next((e for e in entries if not e["is_home"]), entries[1])
        if home["pts"] is None or away["pts"] is None:
            return None
        home["opp_pts"] = away["pts"]
        away["opp_pts"] = home["pts"]
        return home, away

    # Flat layout: homeTeam / awayTeam sub-objects + top-level scores
    ht = game.get("homeTeam") or {}
    at = game.get("awayTeam") or game.get("visitorTeam") or {}
    hs = _get("homeScore", "home_score", "homePoints", obj=game) or _get("points", "score", obj=ht)
    as_ = _get("awayScore", "away_score", "visitorScore", "awayPoints", obj=game) or _get("points", "score", obj=at)

    if hs is None or as_ is None:
        return None

    home = {
        "team_id": str(_get("id", "teamId", "team_id", obj=ht, default="")),
        "team_name": _get("fullName", "full_name", "teamName", "name", obj=ht, default=""),
        "team_abbr": _get("abbreviation", "teamAbbreviation", "abbr", obj=ht, default="?"),
        "pts": hs,
        "opp_pts": as_,
        "is_home": True,
    }
    away = {
        "team_id": str(_get("id", "teamId", "team_id", obj=at, default="")),
        "team_name": _get("fullName", "full_name", "teamName", "name", obj=at, default=""),
        "team_abbr": _get("abbreviation", "teamAbbreviation", "abbr", obj=at, default="?"),
        "pts": as_,
        "opp_pts": hs,
        "is_home": False,
    }
    return home, away


def _compute_team_stats(games: list[dict]) -> tuple[list[dict], list[dict]]:
    # keyed by team_id string; store accumulated data
    acc: dict[str, dict] = {}

    # Games arrive newest-first (ascending=false)
    for g in games:
        date = (_get("date", "gameDate", "game_date", obj=g) or "")[:10]
        parsed = _parse_game_teams(g)
        if parsed is None:
            continue
        home, away = parsed

        for entry in (home, away):
            tid = entry["team_id"] or entry["team_abbr"]
            if not tid:
                continue
            if tid not in acc:
                acc[tid] = {
                    "team_id": tid,
                    "team_name": entry["team_name"],
                    "team_abbr": entry["team_abbr"],
                    "wins": 0,
                    "losses": 0,
                    "pts_for": [],
                    "pts_against": [],
                    "games": [],
                }
            a = acc[tid]
            won = entry["pts"] > entry["opp_pts"]
            if won:
                a["wins"] += 1
            else:
                a["losses"] += 1
            a["pts_for"].append(entry["pts"])
            a["pts_against"].append(entry["opp_pts"])
            opp_abbr = away["team_abbr"] if entry["is_home"] else home["team_abbr"]
            a["games"].append({
                "date": date,
                "matchup": f"{entry['team_abbr']} vs {opp_abbr}",
                "wl": "W" if won else "L",
                "pts": entry["pts"],
                "opp_pts": entry["opp_pts"],
            })

    teams_out: list[dict] = []
    recent_out: list[dict] = []

    for a in acc.values():
        total = a["wins"] + a["losses"]
        win_pct = round(a["wins"] / total, 3) if total else None
        ppg = round(sum(a["pts_for"]) / len(a["pts_for"]), 1) if a["pts_for"] else None
        opp_ppg = round(sum(a["pts_against"]) / len(a["pts_against"]), 1) if a["pts_against"] else None
        net = round(ppg - opp_ppg, 1) if (ppg and opp_ppg) else None

        teams_out.append({
            "team_id": a["team_id"],
            "team_name": a["team_name"],
            "wins": a["wins"],
            "losses": a["losses"],
            "win_pct": win_pct,
            "ppg": ppg,
            "off_rtg": ppg,
            "def_rtg": opp_ppg,
            "net_rtg": net,
            "pace": None,
        })

        recent_out.append({
            "team_id": a["team_id"],
            "team_name": a["team_name"],
            "games": a["games"][:30],  # last 30 stored, features.py uses 10
        })

    teams_out.sort(key=lambda t: t.get("win_pct") or 0, reverse=True)
    print(f"  computed stats for {len(teams_out)} teams")
    return teams_out, recent_out


def _compute_player_stats(totals: list[dict]) -> list[dict]:
    players: list[dict] = []
    for row in totals:
        gp = _get("gamesPlayed", "games_played", "gp", "games", obj=row)
        if not gp:
            continue
        gp = int(gp)

        def pg(field, *alt):
            raw = _get(field, *alt, obj=row)
            if raw is None:
                return None
            return round(float(raw) / gp, 1)

        players.append({
            "player_name": _get("playerName", "player_name", "name", "fullName", obj=row, default="Unknown"),
            "team": _get("teamAbbreviation", "team_abbr", "team", "teamAbbr", obj=row, default=""),
            "games_played": gp,
            "ppg": pg("points", "pts"),
            "apg": pg("assists", "ast"),
            "rpg": pg("totalRebounds", "rebounds", "reb", "total_rebounds"),
            "spg": pg("steals", "stl"),
            "bpg": pg("blocks", "blk"),
        })

    players.sort(key=lambda p: p.get("ppg") or 0, reverse=True)
    print(f"  computed per-game stats for {len(players)} players")
    return players


async def fetch_nba_stats() -> dict:
    async with httpx.AsyncClient() as client:
        games, totals = await asyncio.gather(
            _fetch_games(client),
            _fetch_player_totals(client),
        )
    teams, recent = _compute_team_stats(games)
    players = _compute_player_stats(totals)
    return {"teams": teams, "recent": recent, "players": players}


def save_stats(data: dict, timestamp: str) -> None:
    os.makedirs(STATS_DIR, exist_ok=True)

    with open(TEAM_STATS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(
            {"scraped_at": timestamp, "count": len(data["teams"]), "teams": data["teams"]},
            f, indent=2,
        )

    with open(PLAYER_STATS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(
            {"scraped_at": timestamp, "season": SEASON, "count": len(data["players"]), "players": data["players"]},
            f, indent=2,
        )

    with open(RECENT_GAMES_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(
            {"scraped_at": timestamp, "teams": data["recent"]},
            f, indent=2,
        )

    print(f"  saved {len(data['teams'])} teams, {len(data['players'])} players")


async def main():
    interval = 3600
    print(f"NBA Stats scraper (nba-go-api)  |  interval={interval}s")
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
