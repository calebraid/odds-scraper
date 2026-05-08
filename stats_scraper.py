"""
stats_scraper.py
----------------
Pulls NBA data from stats.nba.com via the nba_api package.
Runs on an interval and saves JSON files consumed by the predictor.

Output files:
  stats/team_stats.json          - team records, scoring, advanced ratings
  stats/player_stats.json        - player per-game averages
  stats/player_stats_advanced.json - estimated metrics (PER-like)
  stats/today_games.json         - today's scheduled/live games
  stats/recent_games.json        - last 10 games per team (form guide)
"""

import json
import os
import time
import sys
from datetime import datetime

# ── nba_api imports ────────────────────────────────────────────────────────────
from nba_api.stats.endpoints import (
    LeagueDashTeamStats,
    LeagueDashPlayerStats,
    TeamEstimatedMetrics,
    LeagueStandings,
    TeamGameLog,
)
from nba_api.stats.static import teams as nba_teams
from nba_api.live.nba.endpoints import scoreboard as live_scoreboard

# ── Config ─────────────────────────────────────────────────────────────────────
SEASON = "2024-25"
STATS_DIR = "stats"
SLEEP_BETWEEN_CALLS = 1.0   # seconds — be polite to stats.nba.com
REQUEST_TIMEOUT = 60
RETRY_ATTEMPTS = 5
RETRY_DELAY = 15.0
PROXY = os.getenv("SCRAPER_PROXY", None)

# Custom headers to avoid 403s from stats.nba.com
NBA_HEADERS = {
    "Host": "stats.nba.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
    "Referer": "https://www.nba.com/",
    "Connection": "keep-alive",
    "Origin": "https://www.nba.com",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def ensure_stats_dir():
    os.makedirs(STATS_DIR, exist_ok=True)


def save_json(filename: str, data):
    path = os.path.join(STATS_DIR, filename)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  saved {path}  ({len(data) if isinstance(data, list) else 'dict'})")


def retry_call(fn, *args, **kwargs):
    """Call fn(*args, **kwargs) with retries on progressive backoff."""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    if PROXY:
        kwargs.setdefault("proxy", PROXY)
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            result = fn(*args, **kwargs)
            time.sleep(SLEEP_BETWEEN_CALLS)
            return result
        except Exception as e:
            print(f"  attempt {attempt}/{RETRY_ATTEMPTS} failed: {e}")
            if attempt < RETRY_ATTEMPTS:
                delay = RETRY_DELAY * attempt
                time.sleep(delay)
    raise RuntimeError(f"All {RETRY_ATTEMPTS} attempts failed for {fn.__name__}")


# ── Scrapers ───────────────────────────────────────────────────────────────────

def scrape_team_stats():
    """
    Pulls per-game team stats + advanced estimated metrics,
    merges them, and saves to team_stats.json.
    """
    print("  fetching team dash stats...")
    dash = retry_call(
        LeagueDashTeamStats,
        season=SEASON,
        per_mode_detailed="PerGame",
        headers=NBA_HEADERS,
        timeout=REQUEST_TIMEOUT,
        proxy=PROXY,
    )
    dash_df = dash.league_dash_team_stats.get_data_frame()

    print("  fetching team estimated metrics...")
    metrics = retry_call(
        TeamEstimatedMetrics,
        season=SEASON,
        headers=NBA_HEADERS,
        timeout=REQUEST_TIMEOUT,
        proxy=PROXY,
    )
    metrics_df = metrics.team_estimated_metrics.get_data_frame()

    print("  fetching league standings...")
    standings = retry_call(
        LeagueStandings,
        season=SEASON,
        headers=NBA_HEADERS,
        timeout=REQUEST_TIMEOUT,
        proxy=PROXY,
    )
    standings_df = standings.standings.get_data_frame()

    # Build a lookup: team_id -> standing info
    standing_lookup = {}
    for _, row in standings_df.iterrows():
        standing_lookup[row["TeamID"]] = {
            "home_record": row.get("HOME", ""),
            "road_record": row.get("ROAD", ""),
            "last_10": row.get("L10", ""),
            "streak": row.get("CurrentStreak", ""),
            "conference": row.get("Conference", ""),
            "division": row.get("Division", ""),
        }

    # Build a lookup: team_id -> estimated metrics
    metrics_lookup = {}
    for _, row in metrics_df.iterrows():
        metrics_lookup[row["TEAM_ID"]] = {
            "e_off_rating": round(float(row.get("E_OFF_RATING", 0) or 0), 2),
            "e_def_rating": round(float(row.get("E_DEF_RATING", 0) or 0), 2),
            "e_net_rating": round(float(row.get("E_NET_RATING", 0) or 0), 2),
            "e_pace": round(float(row.get("E_PACE", 0) or 0), 2),
        }

    # Merge everything into one record per team
    team_stats = []
    for _, row in dash_df.iterrows():
        team_id = int(row["TEAM_ID"])
        extra = standing_lookup.get(team_id, {})
        adv = metrics_lookup.get(team_id, {})

        team_stats.append({
            "team_id": team_id,
            "team_name": row["TEAM_NAME"],
            "gp": int(row.get("GP", 0) or 0),
            "wins": int(row.get("W", 0) or 0),
            "losses": int(row.get("L", 0) or 0),
            "win_pct": round(float(row.get("W_PCT", 0) or 0), 3),
            "pts": round(float(row.get("PTS", 0) or 0), 1),
            "opp_pts": round(float(row.get("OPP_PTS", 0) or 0), 1),
            "reb": round(float(row.get("REB", 0) or 0), 1),
            "ast": round(float(row.get("AST", 0) or 0), 1),
            "stl": round(float(row.get("STL", 0) or 0), 1),
            "blk": round(float(row.get("BLK", 0) or 0), 1),
            "tov": round(float(row.get("TOV", 0) or 0), 1),
            "fg_pct": round(float(row.get("FG_PCT", 0) or 0), 3),
            "fg3_pct": round(float(row.get("FG3_PCT", 0) or 0), 3),
            "ft_pct": round(float(row.get("FT_PCT", 0) or 0), 3),
            "plus_minus": round(float(row.get("PLUS_MINUS", 0) or 0), 1),
            # advanced
            "e_off_rating": adv.get("e_off_rating", 0),
            "e_def_rating": adv.get("e_def_rating", 0),
            "e_net_rating": adv.get("e_net_rating", 0),
            "e_pace": adv.get("e_pace", 0),
            # standings
            "home_record": extra.get("home_record", ""),
            "road_record": extra.get("road_record", ""),
            "last_10": extra.get("last_10", ""),
            "streak": extra.get("streak", ""),
            "conference": extra.get("conference", ""),
            "division": extra.get("division", ""),
            "updated_at": datetime.utcnow().isoformat(),
        })

    save_json("team_stats.json", team_stats)
    print(f"  team_stats: {len(team_stats)} teams")
    return team_stats


def scrape_player_stats():
    """
    Pulls per-game player stats for all active players this season.
    Saves to player_stats.json.
    """
    print("  fetching player stats (regular season)...")
    dash = retry_call(
        LeagueDashPlayerStats,
        season=SEASON,
        per_mode_detailed="PerGame",
        headers=NBA_HEADERS,
        timeout=REQUEST_TIMEOUT,
        proxy=PROXY,
    )
    df = dash.league_dash_player_stats.get_data_frame()

    players = []
    for _, row in df.iterrows():
        players.append({
            "player_id": int(row["PLAYER_ID"]),
            "player_name": row["PLAYER_NAME"],
            "team_id": int(row.get("TEAM_ID", 0) or 0),
            "team": row.get("TEAM_ABBREVIATION", ""),
            "age": float(row.get("AGE", 0) or 0),
            "gp": int(row.get("GP", 0) or 0),
            "min": round(float(row.get("MIN", 0) or 0), 1),
            "pts": round(float(row.get("PTS", 0) or 0), 1),
            "reb": round(float(row.get("REB", 0) or 0), 1),
            "ast": round(float(row.get("AST", 0) or 0), 1),
            "stl": round(float(row.get("STL", 0) or 0), 1),
            "blk": round(float(row.get("BLK", 0) or 0), 1),
            "tov": round(float(row.get("TOV", 0) or 0), 1),
            "fg_pct": round(float(row.get("FG_PCT", 0) or 0), 3),
            "fg3_pct": round(float(row.get("FG3_PCT", 0) or 0), 3),
            "ft_pct": round(float(row.get("FT_PCT", 0) or 0), 3),
            "plus_minus": round(float(row.get("PLUS_MINUS", 0) or 0), 1),
            "fantasy_pts": round(float(row.get("NBA_FANTASY_PTS", 0) or 0), 1),
            "updated_at": datetime.utcnow().isoformat(),
        })

    save_json("player_stats.json", players)
    print(f"  player_stats: {len(players)} players")
    return players


def scrape_player_stats_advanced():
    """
    Pulls advanced player stats (usage %, assist %, etc).
    Saves to player_stats_advanced.json.
    """
    print("  fetching player advanced stats...")
    dash = retry_call(
        LeagueDashPlayerStats,
        season=SEASON,
        per_mode_detailed="PerGame",
        measure_type_detailed_defense="Advanced",
        headers=NBA_HEADERS,
        timeout=REQUEST_TIMEOUT,
        proxy=PROXY,
    )
    df = dash.league_dash_player_stats.get_data_frame()

    players = []
    for _, row in df.iterrows():
        players.append({
            "player_id": int(row["PLAYER_ID"]),
            "player_name": row["PLAYER_NAME"],
            "team": row.get("TEAM_ABBREVIATION", ""),
            "gp": int(row.get("GP", 0) or 0),
            "min": round(float(row.get("MIN", 0) or 0), 1),
            "off_rating": round(float(row.get("OFF_RATING", 0) or 0), 1),
            "def_rating": round(float(row.get("DEF_RATING", 0) or 0), 1),
            "net_rating": round(float(row.get("NET_RATING", 0) or 0), 1),
            "ast_pct": round(float(row.get("AST_PCT", 0) or 0), 3),
            "ast_tov": round(float(row.get("AST_TO", 0) or 0), 2),
            "oreb_pct": round(float(row.get("OREB_PCT", 0) or 0), 3),
            "dreb_pct": round(float(row.get("DREB_PCT", 0) or 0), 3),
            "reb_pct": round(float(row.get("REB_PCT", 0) or 0), 3),
            "tov_pct": round(float(row.get("TM_TOV_PCT", 0) or 0), 3),
            "efg_pct": round(float(row.get("EFG_PCT", 0) or 0), 3),
            "ts_pct": round(float(row.get("TS_PCT", 0) or 0), 3),
            "usg_pct": round(float(row.get("USG_PCT", 0) or 0), 3),
            "pace": round(float(row.get("PACE", 0) or 0), 1),
            "pie": round(float(row.get("PIE", 0) or 0), 3),
            "updated_at": datetime.utcnow().isoformat(),
        })

    save_json("player_stats_advanced.json", players)
    print(f"  player_stats_advanced: {len(players)} players")
    return players


def scrape_today_games():
    """
    Pulls today's games from the NBA live data CDN (cdn.nba.com).
    Uses a different server than stats.nba.com so it is far less likely
    to be blocked on datacenter IPs.  Saves to today_games.json.
    """
    print("  fetching today's games (live endpoint)...")

    kwargs = {}
    if PROXY:
        kwargs["proxy"] = PROXY

    sb = live_scoreboard.ScoreBoard(**kwargs)
    data = sb.get_dict()
    game_list = data.get("scoreboard", {}).get("games", [])

    today_games = []
    for g in game_list:
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

    save_json("today_games.json", today_games)
    print(f"  today_games: {len(today_games)} game(s)")
    return today_games


def scrape_recent_games(all_team_ids: list):
    """
    For every team, pulls their last 10 game results.
    Saves to recent_games.json as a dict keyed by team_id.
    """
    print(f"  fetching last-10 game logs for {len(all_team_ids)} teams...")
    recent = {}

    for i, team_id in enumerate(all_team_ids):
        try:
            log = retry_call(
                TeamGameLog,
                team_id=team_id,
                season=SEASON,
                headers=NBA_HEADERS,
                timeout=REQUEST_TIMEOUT,
                proxy=PROXY,
            )
            df = log.team_game_log.get_data_frame().head(10)

            games = []
            for _, row in df.iterrows():
                games.append({
                    "game_id": row.get("Game_ID", ""),
                    "game_date": row.get("GAME_DATE", ""),
                    "matchup": row.get("MATCHUP", ""),
                    "wl": row.get("WL", ""),
                    "pts": int(row.get("PTS", 0) or 0),
                    "reb": int(row.get("REB", 0) or 0),
                    "ast": int(row.get("AST", 0) or 0),
                    "stl": int(row.get("STL", 0) or 0),
                    "blk": int(row.get("BLK", 0) or 0),
                    "fg_pct": round(float(row.get("FG_PCT", 0) or 0), 3),
                    "fg3_pct": round(float(row.get("FG3_PCT", 0) or 0), 3),
                    "plus_minus": int(row.get("PLUS_MINUS", 0) or 0),
                })

            recent[str(team_id)] = {
                "team_id": team_id,
                "games": games,
                "last_10_record": f"{sum(1 for g in games if g['wl'] == 'W')}-{sum(1 for g in games if g['wl'] == 'L')}",
                "avg_pts_last_10": round(sum(g["pts"] for g in games) / len(games), 1) if games else 0,
                "updated_at": datetime.utcnow().isoformat(),
            }

            if (i + 1) % 10 == 0:
                print(f"    {i + 1}/{len(all_team_ids)} teams done...")

        except Exception as e:
            print(f"  WARNING: could not fetch game log for team {team_id}: {e}")
            recent[str(team_id)] = {"team_id": team_id, "games": [], "error": str(e)}

    save_json("recent_games.json", recent)
    print(f"  recent_games: {len(recent)} teams")
    return recent


# ── Main run loop ──────────────────────────────────────────────────────────────

def run_once():
    print(f"\n[stats] scrape started at {datetime.utcnow().isoformat()}")
    ensure_stats_dir()

    errors = []

    # 1. Team stats (standings + advanced ratings)
    try:
        team_data = scrape_team_stats()
        team_ids = [t["team_id"] for t in team_data]
    except Exception as e:
        print(f"  ERROR scraping team stats: {e}")
        errors.append(f"team_stats: {e}")
        # Fall back to static team list if team stats fail
        team_ids = [t["id"] for t in nba_teams.get_teams()]

    # 2. Player stats (per game)
    try:
        scrape_player_stats()
    except Exception as e:
        print(f"  ERROR scraping player stats: {e}")
        errors.append(f"player_stats: {e}")

    # 3. Player advanced stats
    try:
        scrape_player_stats_advanced()
    except Exception as e:
        print(f"  ERROR scraping advanced stats: {e}")
        errors.append(f"player_stats_advanced: {e}")

    # 4. Today's games
    try:
        scrape_today_games()
    except Exception as e:
        print(f"  ERROR scraping today's games: {e}")
        errors.append(f"today_games: {e}")

    # 5. Recent game logs per team (slowest — 30 API calls)
    try:
        scrape_recent_games(team_ids)
    except Exception as e:
        print(f"  ERROR scraping recent games: {e}")
        errors.append(f"recent_games: {e}")

    if errors:
        print(f"\n[stats] completed with {len(errors)} error(s):")
        for err in errors:
            print(f"  - {err}")
    else:
        print(f"\n[stats] all scrapes completed successfully")


def run_loop(interval_seconds: int = 3600):
    """Run the scraper on a loop every interval_seconds."""
    print(f"[stats] starting  |  interval={interval_seconds}s")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[stats] unexpected error in run loop: {e}")
        print(f"[stats] sleeping {interval_seconds}s...")
        time.sleep(interval_seconds)


async def main():
    """
    Async entry point called by main.py.
    Runs the scrape loop in a thread so it doesn't block the event loop.
    """
    import asyncio
    interval = int(os.getenv("STATS_INTERVAL", "3600"))
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, run_loop, interval)


if __name__ == "__main__":
    # Pass --once to run a single scrape and exit (useful for testing)
    if "--once" in sys.argv:
        run_once()
    else:
        interval = int(os.getenv("STATS_INTERVAL", "3600"))
        run_loop(interval)
