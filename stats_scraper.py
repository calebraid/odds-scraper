"""
stats_scraper.py
----------------
Uses ONLY live nba_api endpoints (cdn.nba.com). No stats.nba.com calls.

Data flow per run:
  1. ScoreBoard  → today_games.json  +  per-team win/loss records
  2. BoxScore x N → richer team stats + player stats for today's games
  3. Static team list → seeds all 30 teams; teams not playing get zeros

Output files:
  stats/team_stats.json            - all 30 teams (win/loss + today's stats)
  stats/player_stats.json          - players from today's live box scores
  stats/player_stats_advanced.json - written empty (no live source)
  stats/today_games.json           - today's scheduled / live / final games
  stats/recent_games.json          - written empty (no live source)
"""

import json
import os
import pkgutil
import sys
import time
from datetime import datetime

# ── nba_api imports ────────────────────────────────────────────────────────────
import nba_api.live.nba.endpoints as _live_ep
from nba_api.live.nba.endpoints import boxscore as live_boxscore
from nba_api.live.nba.endpoints import scoreboard as live_scoreboard
from nba_api.stats.static import teams as nba_teams  # static JSON, no HTTP call

# ── Config ─────────────────────────────────────────────────────────────────────
STATS_DIR = "stats"
REQUEST_TIMEOUT = 60
PROXY = os.getenv("SCRAPER_PROXY", None)
print(f"[stats] proxy configured: {bool(PROXY)}")
print(f"[stats] SCRAPER_PROXY set: {'yes - ' + PROXY[:20] + '...' if PROXY else 'NO - will hit CDN directly'}")
if PROXY:
    os.environ["HTTPS_PROXY"] = PROXY
    os.environ["HTTP_PROXY"] = PROXY

_live_modules = [
    m.name for m in pkgutil.iter_modules(_live_ep.__path__)
    if not m.name.startswith("_")
]
print(f"[stats] nba_api live endpoints available: {_live_modules}")


# ── Helpers ────────────────────────────────────────────────────────────────────

def ensure_stats_dir():
    os.makedirs(STATS_DIR, exist_ok=True)


def save_json(filename: str, data):
    path = os.path.join(STATS_DIR, filename)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    size = len(data) if isinstance(data, list) else len(data) if isinstance(data, dict) else "?"
    print(f"  saved {path}  ({size} items)")


def _live_kwargs() -> dict:
    kw: dict = {"timeout": REQUEST_TIMEOUT}
    if PROXY:
        kw["proxy"] = PROXY
    return kw


def _parse_minutes(value: str) -> float:
    """'PT35M30.00S' → 35.5, 'PT12M' → 12.0, '' → 0.0"""
    if not value:
        return 0.0
    try:
        s = value.replace("PT", "")
        minutes = 0.0
        if "M" in s:
            m_part, rest = s.split("M", 1)
            minutes = float(m_part)
            if rest.endswith("S"):
                minutes += float(rest[:-1]) / 60
        elif s.endswith("S"):
            minutes = float(s[:-1]) / 60
        return round(minutes, 1)
    except Exception:
        return 0.0


# ── Live data fetchers ─────────────────────────────────────────────────────────

def _fetch_scoreboard() -> dict:
    """Return the inner 'scoreboard' dict from today's live scoreboard."""
    sb = live_scoreboard.ScoreBoard(**_live_kwargs())
    return sb.get_dict().get("scoreboard", {})


def _fetch_boxscore(game_id: str) -> dict:
    """Return the inner 'game' dict from a live box score."""
    bs = live_boxscore.BoxScore(game_id=game_id, **_live_kwargs())
    return bs.get_dict().get("game", {})


# ── Parsers ────────────────────────────────────────────────────────────────────

def _parse_today_games(sb: dict) -> list:
    today_games = []
    for g in sb.get("games", []):
        home = g.get("homeTeam", {})
        away = g.get("awayTeam", {})
        today_games.append({
            "game_id": g.get("gameId", ""),
            "game_status": g.get("gameStatusText", ""),
            "game_status_code": g.get("gameStatus", 0),
            "game_time_et": g.get("gameEt", g.get("gameTimeUTC", "")),
            "home_team_id": home.get("teamId", 0),
            "home_team": home.get("teamName", ""),
            "home_team_abbrev": home.get("teamTricode", ""),
            "home_score": home.get("score", 0),
            "away_team_id": away.get("teamId", 0),
            "away_team": away.get("teamName", ""),
            "away_team_abbrev": away.get("teamTricode", ""),
            "away_score": away.get("score", 0),
            "period": g.get("period", 0),
            "game_clock": g.get("gameClock", ""),
            "updated_at": datetime.utcnow().isoformat(),
        })
    return today_games


def _team_entry_from_scoreboard(t: dict) -> dict:
    """Minimal team record seeded from a scoreboard homeTeam/awayTeam object."""
    wins = int(t.get("wins", 0) or 0)
    losses = int(t.get("losses", 0) or 0)
    total = wins + losses
    city = t.get("teamCity", "")
    name = t.get("teamName", "")
    return {
        "team_id": t.get("teamId"),
        "team_name": f"{city} {name}".strip() if city else name,
        "abbreviation": t.get("teamTricode", ""),
        "gp": total,
        "wins": wins,
        "losses": losses,
        "win_pct": round(wins / total, 3) if total else 0.0,
        "pts": None, "reb": None, "ast": None, "stl": None, "blk": None,
        "tov": None, "fg_pct": None, "fg3_pct": None, "ft_pct": None,
        "e_off_rating": None, "e_def_rating": None,
        "e_net_rating": None, "e_pace": None,
        "updated_at": datetime.utcnow().isoformat(),
    }


def _team_entry_from_boxscore(team: dict) -> dict:
    """Full team record from a boxscore homeTeam/awayTeam object."""
    wins = int(team.get("wins", 0) or 0)
    losses = int(team.get("losses", 0) or 0)
    total = wins + losses
    city = team.get("teamCity", "")
    name = team.get("teamName", "")
    s = team.get("statistics", {})

    def _f(key):
        v = s.get(key)
        return round(float(v), 3) if v is not None else None

    return {
        "team_id": team.get("teamId"),
        "team_name": f"{city} {name}".strip() if city else name,
        "abbreviation": team.get("teamTricode", ""),
        "gp": total,
        "wins": wins,
        "losses": losses,
        "win_pct": round(wins / total, 3) if total else 0.0,
        # Today's game totals (not season averages, but real numbers)
        "pts": s.get("points"),
        "reb": s.get("reboundsTotal"),
        "ast": s.get("assists"),
        "stl": s.get("steals"),
        "blk": s.get("blocks"),
        "tov": s.get("turnovers"),
        "fg_pct": _f("fieldGoalsPercentage"),
        "fg3_pct": _f("threePointersPercentage"),
        "ft_pct": _f("freeThrowsPercentage"),
        "e_off_rating": None, "e_def_rating": None,
        "e_net_rating": None, "e_pace": None,
        "updated_at": datetime.utcnow().isoformat(),
    }


def _players_from_boxscore(team: dict) -> list:
    """Extract played-today player stats from a boxscore team object."""
    team_id = team.get("teamId")
    team_abbr = team.get("teamTricode", "")
    players = []
    for p in team.get("players", []):
        if p.get("played") != "1":
            continue
        s = p.get("statistics", {})

        def _f(key, default=0):
            v = s.get(key, default)
            return round(float(v), 3) if v is not None else default

        players.append({
            "player_id": p.get("personId"),
            "player_name": p.get("name", ""),
            "team_id": team_id,
            "team": team_abbr,
            "min": _parse_minutes(s.get("minutesCalculated", "")),
            "pts": s.get("points", 0),
            "reb": s.get("reboundsTotal", 0),
            "ast": s.get("assists", 0),
            "stl": s.get("steals", 0),
            "blk": s.get("blocks", 0),
            "tov": s.get("turnovers", 0),
            "fg_pct": _f("fieldGoalsPercentage"),
            "fg3_pct": _f("threePointersPercentage"),
            "ft_pct": _f("freeThrowsPercentage"),
            "plus_minus": s.get("plusMinusPoints", 0),
            "updated_at": datetime.utcnow().isoformat(),
        })
    return players


# ── Main run logic ─────────────────────────────────────────────────────────────

def run_once():
    print(f"\n[stats] scrape started at {datetime.utcnow().isoformat()}")
    ensure_stats_dir()

    try:
        import requests
        r = requests.get(
            "https://ipv4.webshare.io/",
            proxies={"https": PROXY} if PROXY else None,
            timeout=10,
        )
        print(f"[stats] proxy test IP: {r.text.strip()}")
    except Exception as e:
        print(f"[stats] proxy test failed: {e}")

    # ── 1. Scoreboard ──────────────────────────────────────────────────────────
    print("  fetching scoreboard (cdn.nba.com)...")
    try:
        sb = _fetch_scoreboard()
    except Exception as e:
        print(f"  ERROR fetching scoreboard: {e}")
        sb = {}

    games = sb.get("games", [])
    print(f"  scoreboard: {len(games)} game(s) today")

    today_games = _parse_today_games(sb)
    save_json("today_games.json", today_games)

    # ── 2. Seed team records from scoreboard ───────────────────────────────────
    # wins/losses are already embedded in each homeTeam/awayTeam object
    live_teams: dict[int, dict] = {}
    for g in games:
        for side in ("homeTeam", "awayTeam"):
            t = g.get(side, {})
            tid = t.get("teamId")
            if tid:
                live_teams[tid] = _team_entry_from_scoreboard(t)

    # ── 3. Fetch box scores — overrides seeded records with richer data ────────
    all_players: list = []
    for g in games:
        game_id = g.get("gameId", "")
        if not game_id:
            continue
        print(f"  fetching boxscore {game_id}...")
        try:
            box = _fetch_boxscore(game_id)
            for side in ("homeTeam", "awayTeam"):
                team = box.get(side, {})
                tid = team.get("teamId")
                if not tid:
                    continue
                live_teams[tid] = _team_entry_from_boxscore(team)
                all_players.extend(_players_from_boxscore(team))
        except Exception as e:
            print(f"  WARNING: boxscore {game_id} failed: {e}")

    print(f"  live data: {len(live_teams)} teams, {len(all_players)} players")

    # ── 4. Build team_stats.json — all 30 teams ────────────────────────────────
    static_teams = {t["id"]: t for t in nba_teams.get_teams()}
    team_stats: list = []

    for tid, static in static_teams.items():
        if tid in live_teams:
            team_stats.append(live_teams[tid])
        else:
            team_stats.append({
                "team_id": tid,
                "team_name": static["full_name"],
                "abbreviation": static["abbreviation"],
                "gp": 0,
                "wins": 0,
                "losses": 0,
                "win_pct": 0.0,
                "pts": None, "reb": None, "ast": None, "stl": None, "blk": None,
                "tov": None, "fg_pct": None, "fg3_pct": None, "ft_pct": None,
                "e_off_rating": None, "e_def_rating": None,
                "e_net_rating": None, "e_pace": None,
                "updated_at": datetime.utcnow().isoformat(),
            })

    team_stats.sort(key=lambda t: t.get("win_pct") or 0, reverse=True)
    save_json("team_stats.json", team_stats)
    print(f"  team_stats: {len(live_teams)} with live records, "
          f"{len(team_stats) - len(live_teams)} placeholder-only")

    # ── 5. Player stats from today's box scores ────────────────────────────────
    save_json("player_stats.json", all_players)

    # ── 6. Empty stubs for outputs we can't populate from live endpoints ───────
    save_json("player_stats_advanced.json", [])
    save_json("recent_games.json", {})

    print("\n[stats] scrape complete")


def run_loop(interval_seconds: int = 3600):
    print(f"[stats] starting  |  interval={interval_seconds}s")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[stats] unexpected error in run loop: {e}")
        print(f"[stats] sleeping {interval_seconds}s...")
        time.sleep(interval_seconds)


async def main():
    """Async entry point called by main.py via thread executor."""
    import asyncio
    interval = int(os.getenv("STATS_INTERVAL", "3600"))
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_loop, interval)


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        interval = int(os.getenv("STATS_INTERVAL", "3600"))
        run_loop(interval)
