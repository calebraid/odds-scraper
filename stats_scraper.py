"""
stats_scraper.py
----------------
Pulls NBA data using nba_api.

Primary source: live endpoints hitting cdn.nba.com (never blocked).
Secondary source: stats endpoints hitting stats.nba.com (used when available,
skipped gracefully when blocked).

Output files:
  stats/team_stats.json            - team records, scoring, advanced ratings
  stats/player_stats.json          - player per-game averages
  stats/player_stats_advanced.json - estimated metrics
  stats/today_games.json           - today's scheduled/live games
  stats/recent_games.json          - last 10 games per team
"""

import json
import os
import pkgutil
import sys
import time
from datetime import datetime

# ── nba_api imports ────────────────────────────────────────────────────────────
import nba_api.live.nba.endpoints as _live_ep
from nba_api.live.nba.endpoints import scoreboard as live_scoreboard
from nba_api.stats.endpoints import (
    LeagueDashTeamStats,
    LeagueDashPlayerStats,
    TeamEstimatedMetrics,
    LeagueStandings,
    TeamGameLog,
)
from nba_api.stats.static import teams as nba_teams

# ── Config ─────────────────────────────────────────────────────────────────────
SEASON = "2024-25"
STATS_DIR = "stats"
SLEEP_BETWEEN_CALLS = 1.0
REQUEST_TIMEOUT = 60
RETRY_ATTEMPTS = 5
RETRY_DELAY = 15.0
PROXY = os.getenv("SCRAPER_PROXY", None)
print(f"[stats] proxy configured: {bool(PROXY)}")
print(f"[stats] SCRAPER_PROXY set: {'yes - ' + PROXY[:20] + '...' if PROXY else 'NO - will hit NBA directly'}")
if PROXY:
    os.environ["HTTPS_PROXY"] = PROXY
    os.environ["HTTP_PROXY"] = PROXY

_live_modules = [m.name for m in pkgutil.iter_modules(_live_ep.__path__) if not m.name.startswith("_")]
print(f"[stats] nba_api live endpoints available: {_live_modules}")

# Custom headers — only used for stats.nba.com endpoints
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
    """Call fn(*args, **kwargs) up to RETRY_ATTEMPTS times with progressive backoff."""
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


# ── Live data (cdn.nba.com — primary, never blocked) ──────────────────────────

def _fetch_live_scoreboard() -> dict:
    """Fetch today's scoreboard from cdn.nba.com. Returns the 'scoreboard' dict."""
    kwargs: dict = {"timeout": REQUEST_TIMEOUT}
    if PROXY:
        kwargs["proxy"] = PROXY
    sb = live_scoreboard.ScoreBoard(**kwargs)
    return sb.get_dict().get("scoreboard", {})


def _load_cached_team_stats() -> dict:
    """
    Read the most recent successful team_stats.json if it exists.
    Returns dict keyed by team_id so callers can patch stale entries.
    """
    path = os.path.join(STATS_DIR, "team_stats.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return {t["team_id"]: t for t in (data if isinstance(data, list) else [])}
    except Exception:
        return {}


# ── Scrapers ───────────────────────────────────────────────────────────────────

def scrape_today_games(sb: dict) -> list:
    """
    Parse today's games from the already-fetched live scoreboard dict.
    Output format is identical to the old ScoreboardV3-based version.
    """
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

    save_json("today_games.json", today_games)
    print(f"  today_games: {len(today_games)} game(s)")
    return today_games


def _live_records_from_scoreboard(sb: dict) -> dict:
    """
    Extract per-team win/loss records from the live scoreboard.
    The scoreboard embeds each team's current season record next to their score.
    Returns dict keyed by team_id.
    """
    records = {}
    for g in sb.get("games", []):
        for side in ("homeTeam", "awayTeam"):
            t = g.get(side, {})
            team_id = t.get("teamId")
            if not team_id:
                continue
            wins = int(t.get("wins", 0) or 0)
            losses = int(t.get("losses", 0) or 0)
            total = wins + losses
            city = t.get("teamCity", "")
            name = t.get("teamName", "")
            records[team_id] = {
                "team_id": team_id,
                "team_name": f"{city} {name}".strip() if city else name,
                "gp": total,
                "wins": wins,
                "losses": losses,
                "win_pct": round(wins / total, 3) if total else 0.0,
                # Placeholders — filled by stats endpoint when available
                "pts": None,
                "opp_pts": None,
                "e_off_rating": None,
                "e_def_rating": None,
                "e_net_rating": None,
                "e_pace": None,
                "home_record": "",
                "road_record": "",
                "last_10": "",
                "streak": "",
                "conference": "",
                "division": "",
                "updated_at": datetime.utcnow().isoformat(),
            }
    return records


def scrape_team_stats(sb: dict) -> list:
    """
    Build team stats using a two-pass strategy:

    Pass 1 (live, always succeeds): extract wins/losses from today's scoreboard.
      Covers only teams playing today, but gives us valid win_pct immediately.

    Pass 2 (stats endpoint, optional): fetch full standings + advanced metrics.
      If it succeeds, its richer data replaces the live records.
      If it fails, we keep the live records and fill remaining teams from cache
      or the static team list.
    """
    live_records = _live_records_from_scoreboard(sb)
    print(f"  live scoreboard: extracted records for {len(live_records)} teams playing today")

    # Pass 2 — try stats endpoints
    try:
        print("  attempting stats endpoint for full standings...")
        dash = retry_call(
            LeagueDashTeamStats,
            season=SEASON,
            per_mode_detailed="PerGame",
            headers=NBA_HEADERS,
            timeout=REQUEST_TIMEOUT,
            proxy=PROXY,
        )
        dash_df = dash.league_dash_team_stats.get_data_frame()

        metrics = retry_call(
            TeamEstimatedMetrics,
            season=SEASON,
            headers=NBA_HEADERS,
            timeout=REQUEST_TIMEOUT,
            proxy=PROXY,
        )
        metrics_df = metrics.team_estimated_metrics.get_data_frame()

        standings = retry_call(
            LeagueStandings,
            season=SEASON,
            headers=NBA_HEADERS,
            timeout=REQUEST_TIMEOUT,
            proxy=PROXY,
        )
        standings_df = standings.standings.get_data_frame()

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

        metrics_lookup = {}
        for _, row in metrics_df.iterrows():
            metrics_lookup[row["TEAM_ID"]] = {
                "e_off_rating": round(float(row.get("E_OFF_RATING", 0) or 0), 2),
                "e_def_rating": round(float(row.get("E_DEF_RATING", 0) or 0), 2),
                "e_net_rating": round(float(row.get("E_NET_RATING", 0) or 0), 2),
                "e_pace": round(float(row.get("E_PACE", 0) or 0), 2),
            }

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
                "e_off_rating": adv.get("e_off_rating", 0),
                "e_def_rating": adv.get("e_def_rating", 0),
                "e_net_rating": adv.get("e_net_rating", 0),
                "e_pace": adv.get("e_pace", 0),
                "home_record": extra.get("home_record", ""),
                "road_record": extra.get("road_record", ""),
                "last_10": extra.get("last_10", ""),
                "streak": extra.get("streak", ""),
                "conference": extra.get("conference", ""),
                "division": extra.get("division", ""),
                "updated_at": datetime.utcnow().isoformat(),
            })

        print(f"  stats endpoint: got full data for {len(team_stats)} teams")

    except Exception as e:
        print(f"  stats endpoint failed ({e})")
        print("  falling back to live scoreboard records + cache")

        # Merge live records with cached data for teams not playing today
        cached = _load_cached_team_stats()
        merged: dict = {}

        # Start from cache (stale but covers all 30 teams)
        merged.update(cached)

        # Override with today's live records (fresh wins/losses)
        for tid, rec in live_records.items():
            if tid in merged:
                merged[tid] = {**merged[tid], **rec}
            else:
                merged[tid] = rec

        # Fill any remaining teams from static list (no stats, just identity)
        existing = set(merged.keys())
        for t in nba_teams.get_teams():
            if t["id"] not in existing:
                merged[t["id"]] = {
                    "team_id": t["id"],
                    "team_name": t["full_name"],
                    "gp": None,
                    "wins": None,
                    "losses": None,
                    "win_pct": None,
                    "pts": None,
                    "opp_pts": None,
                    "e_off_rating": None,
                    "e_def_rating": None,
                    "e_net_rating": None,
                    "e_pace": None,
                    "home_record": "",
                    "road_record": "",
                    "last_10": "",
                    "streak": "",
                    "conference": "",
                    "division": "",
                    "updated_at": datetime.utcnow().isoformat(),
                }

        team_stats = list(merged.values())
        print(f"  fallback team_stats: {len(team_stats)} teams ({len(live_records)} with live records, {len(cached)} from cache)")

    save_json("team_stats.json", team_stats)
    print(f"  team_stats: {len(team_stats)} teams written")
    return team_stats


def scrape_player_stats() -> list:
    """
    Per-game player stats from stats endpoint.
    Writes empty list on failure so the predictor isn't blocked.
    """
    print("  fetching player stats (regular season)...")
    try:
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

    except Exception as e:
        print(f"  player stats failed ({e}) — writing empty list")
        save_json("player_stats.json", [])
        return []


def scrape_player_stats_advanced() -> list:
    """
    Advanced player stats from stats endpoint.
    Writes empty list on failure so the predictor isn't blocked.
    """
    print("  fetching player advanced stats...")
    try:
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

    except Exception as e:
        print(f"  advanced player stats failed ({e}) — writing empty list")
        save_json("player_stats_advanced.json", [])
        return []


def scrape_recent_games(all_team_ids: list) -> dict:
    """
    Last 10 games per team from stats endpoint.
    Writes empty dict on failure.
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
            print(f"  WARNING: game log failed for team {team_id}: {e}")
            recent[str(team_id)] = {"team_id": team_id, "games": [], "error": str(e)}

    save_json("recent_games.json", recent)
    print(f"  recent_games: {len(recent)} teams")
    return recent


# ── Main run loop ──────────────────────────────────────────────────────────────

def run_once():
    print(f"\n[stats] scrape started at {datetime.utcnow().isoformat()}")
    ensure_stats_dir()

    import requests
    try:
        r = requests.get("https://ipv4.webshare.io/", proxies={"https": PROXY} if PROXY else None, timeout=10)
        print(f"[stats] proxy test: {r.text.strip()}")
    except Exception as e:
        print(f"[stats] proxy test failed: {e}")

    errors = []

    # 1. Fetch live scoreboard once — reused by today_games and team_stats
    print("  fetching live scoreboard (cdn.nba.com)...")
    try:
        sb = _fetch_live_scoreboard()
        print(f"  scoreboard: {len(sb.get('games', []))} game(s) today")
    except Exception as e:
        print(f"  ERROR fetching live scoreboard: {e}")
        errors.append(f"scoreboard: {e}")
        sb = {}

    # 2. Today's games (live, from scoreboard)
    try:
        scrape_today_games(sb)
    except Exception as e:
        print(f"  ERROR scraping today's games: {e}")
        errors.append(f"today_games: {e}")

    # 3. Team stats (live records primary, stats endpoint secondary)
    try:
        team_data = scrape_team_stats(sb)
        team_ids = [t["team_id"] for t in team_data if t.get("team_id")]
    except Exception as e:
        print(f"  ERROR scraping team stats: {e}")
        errors.append(f"team_stats: {e}")
        team_ids = [t["id"] for t in nba_teams.get_teams()]

    # 4. Player stats (stats endpoint, empty list on failure)
    try:
        scrape_player_stats()
    except Exception as e:
        print(f"  ERROR scraping player stats: {e}")
        errors.append(f"player_stats: {e}")

    # 5. Advanced player stats (stats endpoint, empty list on failure)
    try:
        scrape_player_stats_advanced()
    except Exception as e:
        print(f"  ERROR scraping advanced stats: {e}")
        errors.append(f"player_stats_advanced: {e}")

    # 6. Recent game logs (stats endpoint, empty dict on failure)
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
        print("\n[stats] all scrapes completed successfully")


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
