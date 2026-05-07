import asyncio
import json
import os
from datetime import datetime, timezone

import httpx

STATS_DIR = "stats"
TEAM_STATS_OUTPUT = os.path.join(STATS_DIR, "team_stats.json")
PLAYER_STATS_OUTPUT = os.path.join(STATS_DIR, "player_stats.json")
PLAYER_ADV_OUTPUT = os.path.join(STATS_DIR, "player_stats_advanced.json")
RECENT_GAMES_OUTPUT = os.path.join(STATS_DIR, "recent_games.json")

BASE_URL = "https://api.server.nbaapi.com"
SEASON = 2025  # 2025-26 NBA season


def _extract_list(data) -> list[dict]:
    """Return list of dicts regardless of response shape.

    Handles three cases the nbaapi sometimes returns:
      1. Bare list of dicts  (normal)
      2. Wrapped object with a list under a known key
      3. List of JSON strings instead of dicts (API bug / double-encoding)
    """
    raw: list = []
    if isinstance(data, list):
        raw = data
    else:
        for key in ("data", "results", "players", "games", "items"):
            if isinstance(data.get(key), list):
                raw = data[key]
                break

    # Deserialize any items that arrived as JSON strings instead of dicts
    out: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError:
                continue
        if isinstance(item, dict):
            out.append(item)
    return out


async def _fetch_paginated(client: httpx.AsyncClient, path: str, params: dict) -> list[dict]:
    results: list[dict] = []
    page = 1
    page_size = int(params.get("pageSize", 100))
    while True:
        r = await client.get(f"{BASE_URL}{path}", params={**params, "page": page}, timeout=30)
        r.raise_for_status()
        chunk = _extract_list(r.json())
        results.extend(chunk)
        if len(chunk) < page_size:
            break
        page += 1
    return results


async def _fetch_player_totals(client: httpx.AsyncClient, playoff: bool = False) -> list[dict]:
    params = {"season": SEASON, "pageSize": 100, "sortBy": "points", "ascending": "false"}
    if playoff:
        params["isPlayoff"] = "true"
    rows = await _fetch_paginated(client, "/api/playertotals", params)
    label = "playoff" if playoff else "regular"
    print(f"  playertotals ({label}): {len(rows)} rows")
    return rows


async def _fetch_advanced(client: httpx.AsyncClient) -> list[dict]:
    params = {"season": SEASON, "pageSize": 100, "sortBy": "win_shares", "ascending": "false"}
    rows = await _fetch_paginated(client, "/api/playeradvancedstats", params)
    print(f"  playeradvancedstats: {len(rows)} rows")
    return rows


async def _fetch_games(client: httpx.AsyncClient) -> list[dict]:
    try:
        params = {"pageSize": 100, "ascending": "false", "include": "teamGameBasicStats"}
        rows = await _fetch_paginated(client, "/api/games", params)
        print(f"  games: {len(rows)} rows")
        if rows:
            first = rows[0]
            print(f"  games[0] keys: {list(first.keys())}")
            print(f"  games[0] sample: {json.dumps(first, default=str)[:500]}")
        return rows
    except Exception as exc:
        print(f"  WARN games endpoint failed: {exc}")
        return []


# ── parsers ───────────────────────────────────────────────────────────────────

def _parse_player_stats(totals: list[dict]) -> list[dict]:
    players = []
    for row in totals:
        gp = row.get("games")
        if not gp:
            continue
        gp = int(gp)

        def pg(field, already_rate=False):
            v = row.get(field)
            if v is None:
                return None
            return round(float(v) if already_rate else float(v) / gp, 1)

        players.append({
            "player_name": row.get("playerName", "Unknown"),
            "team": row.get("team", ""),
            "games_played": gp,
            "ppg":  pg("points"),
            "apg":  pg("assists"),
            "rpg":  pg("totalRb"),
            "spg":  pg("steals"),
            "bpg":  pg("blocks"),
            "mpg":  row.get("minutesPg"),        # already per game
            "fg_pct":  row.get("fieldPercent"),  # already a rate
            "three_pct": row.get("threePercent"),
        })

    players.sort(key=lambda p: p.get("ppg") or 0, reverse=True)
    return players


def _parse_advanced_stats(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        out.append({
            "player_name": row.get("playerName", "Unknown"),
            "team": row.get("team", ""),
            "games_played": row.get("games"),
            "per": row.get("per"),
            "usage_pct": row.get("usagePercent"),
            "win_shares": row.get("winShares"),
            "bpm": row.get("box"),
            "vorp": row.get("vorp"),
            "ts_pct": row.get("tsPercent"),
        })
    return out


def _record(acc: dict, abbr: str, name: str, pts: int, opp_pts: int,
            opp_abbr: str, date: str) -> None:
    if abbr not in acc:
        acc[abbr] = {"team_name": name or abbr, "wins": 0, "losses": 0,
                     "pts_for": [], "pts_against": [], "games": []}
    won = pts > opp_pts
    acc[abbr]["wins" if won else "losses"] += 1
    acc[abbr]["pts_for"].append(pts)
    acc[abbr]["pts_against"].append(opp_pts)
    acc[abbr]["games"].append({
        "date": date,
        "matchup": f"{abbr} vs {opp_abbr}",
        "wl": "W" if won else "L",
        "pts": pts,
        "opp_pts": opp_pts,
    })


def _parse_team_stats_from_games(games: list[dict]) -> tuple[list[dict], list[dict]]:
    acc: dict[str, dict] = {}
    parsed = skip_no_score = skip_no_teams = 0

    for g in games:
        try:
            if isinstance(g, str):
                g = json.loads(g)

            date = (g.get("date") or g.get("gameDate") or "")[:10]

            # --- Primary: flat homePts / visitorPts fields (confirmed API shape) ---
            home_abbr = g.get("homeTeam") or ""
            away_abbr = g.get("visitorTeam") or g.get("awayTeam") or ""
            home_pts  = g.get("homePts")  or g.get("homeScore")  or g.get("home_score")
            away_pts  = g.get("visitorPts") or g.get("awayScore") or g.get("away_score")

            # Convert to int if present
            try:
                home_pts = int(home_pts) if home_pts is not None else None
                away_pts = int(away_pts) if away_pts is not None else None
            except (TypeError, ValueError):
                home_pts = away_pts = None

            if not home_abbr or not away_abbr:
                skip_no_teams += 1
                continue
            if home_pts is None or away_pts is None or (home_pts == 0 and away_pts == 0):
                skip_no_score += 1
                continue

            _record(acc, home_abbr, home_abbr, home_pts, away_pts, away_abbr, date)
            _record(acc, away_abbr, away_abbr, away_pts, home_pts, home_abbr, date)
            parsed += 1

        except Exception as exc:
            print(f"  ERROR parsing game: {exc} | game={json.dumps(g, default=str)[:300]}")

    print(f"  game parsing: {parsed} parsed, {skip_no_score} no-score, {skip_no_teams} no-teams, {len(acc)} teams")

    teams_out, recent_out = [], []
    for abbr, a in acc.items():
        total = a["wins"] + a["losses"]
        win_pct = round(a["wins"] / total, 3) if total else None
        ppg = round(sum(a["pts_for"]) / len(a["pts_for"]), 1) if a["pts_for"] else None
        opp_ppg = round(sum(a["pts_against"]) / len(a["pts_against"]), 1) if a["pts_against"] else None
        teams_out.append({
            "team_id": abbr,
            "team_name": a["team_name"],
            "wins": a["wins"],
            "losses": a["losses"],
            "win_pct": win_pct,
            "ppg": ppg,
            "off_rtg": ppg,
            "def_rtg": opp_ppg,
            "net_rtg": round(ppg - opp_ppg, 1) if (ppg and opp_ppg) else None,
            "pace": None,
        })
        recent_out.append({
            "team_id": abbr,
            "team_name": a["team_name"],
            "games": a["games"][:30],
        })

    teams_out.sort(key=lambda t: t.get("win_pct") or 0, reverse=True)
    print(f"  teams calculated: {len(teams_out)}")
    if teams_out:
        print(f"  first team: {json.dumps(teams_out[0])}")
    else:
        print("  WARNING: 0 teams produced from game data — check game structure above")
    return teams_out, recent_out


def _team_stats_from_players(players: list[dict]) -> list[dict]:
    """Fallback: estimate team offensive stats by aggregating player totals."""
    by_team: dict[str, dict] = {}
    for p in players:
        team = p.get("team", "")
        if not team:
            continue
        if team not in by_team:
            by_team[team] = {"ppg_sum": 0.0, "count": 0}
        ppg = p.get("ppg") or 0
        by_team[team]["ppg_sum"] += ppg
        by_team[team]["count"] += 1

    out = []
    for abbr, d in by_team.items():
        out.append({
            "team_id": abbr,
            "team_name": abbr,
            "wins": None,
            "losses": None,
            "win_pct": None,
            "ppg": round(d["ppg_sum"], 1),
            "off_rtg": round(d["ppg_sum"], 1),
            "def_rtg": None,
            "net_rtg": None,
            "pace": None,
        })
    return out


# ── main fetch/save ───────────────────────────────────────────────────────────

async def fetch_nba_stats() -> dict:
    async with httpx.AsyncClient() as client:
        totals, adv, playoff_totals, games = await asyncio.gather(
            _fetch_player_totals(client, playoff=False),
            _fetch_advanced(client),
            _fetch_player_totals(client, playoff=True),
            _fetch_games(client),
        )

    players = _parse_player_stats(totals)
    players_adv = _parse_advanced_stats(adv)
    playoff_players = _parse_player_stats(playoff_totals)

    if games:
        teams, recent = _parse_team_stats_from_games(games)
        if not teams:
            print("  WARNING: games fetched but 0 teams parsed — falling back to player totals for team stats")
            teams = _team_stats_from_players(players)
            recent = []
    else:
        print("  using player totals fallback for team stats (no games data)")
        teams = _team_stats_from_players(players)
        recent = []

    return {
        "teams": teams,
        "recent": recent,
        "players": players,
        "players_adv": players_adv,
        "playoff_players": playoff_players,
    }


def save_stats(data: dict, timestamp: str) -> None:
    os.makedirs(STATS_DIR, exist_ok=True)
    print(f"  save_stats: {len(data['teams'])} teams, {len(data['players'])} players, {len(data['players_adv'])} adv")
    if data["teams"]:
        print(f"  first team entry: {json.dumps(data['teams'][0])}")
    else:
        print("  ERROR: teams list is empty — team_stats.json will be written but predictor will skip it")

    with open(TEAM_STATS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(
            {"scraped_at": timestamp, "count": len(data["teams"]), "teams": data["teams"]},
            f, indent=2,
        )

    with open(PLAYER_STATS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(
            {
                "scraped_at": timestamp,
                "season": SEASON,
                "count": len(data["players"]),
                "players": data["players"],
                "playoff_count": len(data["playoff_players"]),
                "playoff_players": data["playoff_players"],
            },
            f, indent=2,
        )

    with open(PLAYER_ADV_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(
            {"scraped_at": timestamp, "season": SEASON, "count": len(data["players_adv"]), "players": data["players_adv"]},
            f, indent=2,
        )

    with open(RECENT_GAMES_OUTPUT, "w", encoding="utf-8") as f:
        json.dump({"scraped_at": timestamp, "teams": data["recent"]}, f, indent=2)

    print(f"  saved {len(data['teams'])} teams | {len(data['players'])} players | {len(data['players_adv'])} adv")
    print(f"  team_stats.json written: {len(data['teams'])} teams -> {TEAM_STATS_OUTPUT}")


async def main():
    interval = 3600
    print(f"NBA Stats scraper (nbaapi.com)  |  interval={interval}s")
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
