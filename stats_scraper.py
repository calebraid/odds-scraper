"""
stats_scraper.py
----------------
Uses ONLY live nba_api endpoints (cdn.nba.com). No stats.nba.com calls.

Data flow per run:
  1. Load stats/boxscore_cache.json  (persists Final games across restarts)
  2. ScoreBoard  → today_games.json + seed live W/L for teams playing today
  3. BoxScore x N:
       • Final game already in cache  → skip fetch, use cached data
       • Final game not yet cached    → fetch, add to cache
       • Live / upcoming game         → fetch every run (scores change)
  4. Compute team & player averages from all cached Final games
  5. Override W/L with live scoreboard values (current season total)
  6. Merge into team_stats.json (all 30 teams) and player_stats.json
  7. Save updated cache

Output files:
  stats/boxscore_cache.json        - all Final box scores seen so far
  stats/team_stats.json            - 30 teams with accumulated averages
  stats/player_stats.json          - season averages from cached games
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
STATS_DIR = os.getenv("STATS_DIR", "/data")
CACHE_FILE = os.path.join(STATS_DIR, "boxscore_cache.json")
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
    n = len(data) if isinstance(data, (list, dict)) else "?"
    print(f"  saved {path}  ({n} items)")


def _live_kwargs() -> dict:
    kw: dict = {"timeout": REQUEST_TIMEOUT}
    if PROXY:
        kw["proxy"] = PROXY
    return kw


def _parse_minutes(value: str) -> float:
    """'PT35M30.00S' → 35.5,  'PT12M' → 12.0,  '' → 0.0"""
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


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ── Cache I/O ──────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  WARNING: could not load cache ({e}), starting fresh")
        return {}


def _save_cache(cache: dict) -> None:
    os.makedirs(STATS_DIR, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)
    print(f"  cache: {len(cache)} games stored → {CACHE_FILE}")


def _build_cache_entry(game_id: str, game_date: str, box: dict) -> dict:
    """Build a storable cache entry from a boxscore 'game' dict (Final games only)."""
    home = box.get("homeTeam", {})
    away = box.get("awayTeam", {})

    def _team_stats(s: dict) -> dict:
        return {
            "points":                   s.get("points"),
            "reboundsTotal":            s.get("reboundsTotal"),
            "assists":                  s.get("assists"),
            "steals":                   s.get("steals"),
            "blocks":                   s.get("blocks"),
            "turnovers":                s.get("turnovers"),
            "fieldGoalsPercentage":     s.get("fieldGoalsPercentage"),
            "threePointersPercentage":  s.get("threePointersPercentage"),
            "freeThrowsPercentage":     s.get("freeThrowsPercentage"),
        }

    def _parse_players(team: dict) -> list:
        tid   = team.get("teamId")
        abbr  = team.get("teamTricode", "")
        out   = []
        for p in team.get("players", []):
            if p.get("played") != "1":
                continue
            s = p.get("statistics", {})
            out.append({
                "player_id":   p.get("personId"),
                "player_name": p.get("name", ""),
                "team_id":     tid,
                "team":        abbr,
                "min":  _parse_minutes(s.get("minutesCalculated", "")),
                "pts":  s.get("points", 0),
                "reb":  s.get("reboundsTotal", 0),
                "ast":  s.get("assists", 0),
                "stl":  s.get("steals", 0),
                "blk":  s.get("blocks", 0),
                "tov":  s.get("turnovers", 0),
                "fg_pct":  round(_safe_float(s.get("fieldGoalsPercentage")), 3),
                "fg3_pct": round(_safe_float(s.get("threePointersPercentage")), 3),
                "ft_pct":  round(_safe_float(s.get("freeThrowsPercentage")), 3),
                "plus_minus": s.get("plusMinusPoints", 0),
            })
        return out

    home_city = home.get("teamCity", "")
    away_city = away.get("teamCity", "")
    home_nm   = home.get("teamName", "")
    away_nm   = away.get("teamName", "")

    home_score = home.get("score") if home.get("score") is not None else (
        home.get("statistics", {}).get("points", 0)
    )
    away_score = away.get("score") if away.get("score") is not None else (
        away.get("statistics", {}).get("points", 0)
    )

    return {
        "game_id":         game_id,
        "game_date":       game_date,
        "home_team_id":    home.get("teamId"),
        "home_team_abbr":  home.get("teamTricode", ""),
        "home_team_name":  f"{home_city} {home_nm}".strip() if home_city else home_nm,
        "home_wins":       int(home.get("wins", 0) or 0),
        "home_losses":     int(home.get("losses", 0) or 0),
        "away_team_id":    away.get("teamId"),
        "away_team_abbr":  away.get("teamTricode", ""),
        "away_team_name":  f"{away_city} {away_nm}".strip() if away_city else away_nm,
        "away_wins":       int(away.get("wins", 0) or 0),
        "away_losses":     int(away.get("losses", 0) or 0),
        "home_score":      home_score,
        "away_score":      away_score,
        "home_stats":      _team_stats(home.get("statistics", {})),
        "away_stats":      _team_stats(away.get("statistics", {})),
        "players":         _parse_players(home) + _parse_players(away),
        "scraped_at":      datetime.utcnow().isoformat(),
    }


# ── Aggregate from cache ───────────────────────────────────────────────────────

def _compute_team_stats_from_cache(cache: dict) -> dict:
    """
    Roll up per-game stats for every team across all cached Final games.
    Returns dict keyed by team_id.
    """
    # Collect each team's games in chronological order
    team_games: dict[int, list] = {}

    for entry in cache.values():
        h_id   = entry.get("home_team_id")
        a_id   = entry.get("away_team_id")
        h_pts  = _safe_float(entry.get("home_score"))
        a_pts  = _safe_float(entry.get("away_score"))
        date   = entry.get("game_date", "")
        h_wins = entry.get("home_wins", 0)
        h_loss = entry.get("home_losses", 0)
        a_wins = entry.get("away_wins", 0)
        a_loss = entry.get("away_losses", 0)

        if h_id:
            team_games.setdefault(h_id, []).append({
                "date":        date,
                "is_home":     True,
                "pts_for":     h_pts,
                "pts_against": a_pts,
                "won":         h_pts > a_pts,
                "wins":        h_wins,
                "losses":      h_loss,
                "team_name":   entry.get("home_team_name", ""),
                "abbr":        entry.get("home_team_abbr", ""),
                "stats":       entry.get("home_stats", {}),
            })
        if a_id:
            team_games.setdefault(a_id, []).append({
                "date":        date,
                "is_home":     False,
                "pts_for":     a_pts,
                "pts_against": h_pts,
                "won":         a_pts > h_pts,
                "wins":        a_wins,
                "losses":      a_loss,
                "team_name":   entry.get("away_team_name", ""),
                "abbr":        entry.get("away_team_abbr", ""),
                "stats":       entry.get("away_stats", {}),
            })

    result: dict[int, dict] = {}

    for tid, games in team_games.items():
        games_sorted = sorted(games, key=lambda g: g["date"], reverse=True)
        n_total = len(games_sorted)

        # W/L from most recent cached game (season total at that point)
        latest = games_sorted[0]
        cached_wins   = latest["wins"]
        cached_losses = latest["losses"]
        gp_season     = cached_wins + cached_losses
        win_pct       = round(cached_wins / gp_season, 3) if gp_season else 0.0

        # Last-10 record from our cache window
        last10 = games_sorted[:10]
        l10w = sum(1 for g in last10 if g["won"])
        last_10 = f"{l10w}-{len(last10) - l10w}"

        # Current streak: consecutive wins (positive) or losses (negative)
        streak = 0
        if games_sorted:
            is_win = games_sorted[0]["won"]
            for g in games_sorted:
                if g["won"] == is_win:
                    streak += 1 if is_win else -1
                else:
                    break

        # Home / away records from cache window
        home_g = [g for g in games_sorted if g["is_home"]]
        hw = sum(1 for g in home_g if g["won"])
        home_record = f"{hw}-{len(home_g) - hw}"

        away_g = [g for g in games_sorted if not g["is_home"]]
        aw = sum(1 for g in away_g if g["won"])
        away_record = f"{aw}-{len(away_g) - aw}"

        # Rolling averages over last 20 cached games
        window = games_sorted[:20]

        def _avg(fn, decimals=1):
            vals = [fn(g) for g in window]
            vals = [v for v in vals if v is not None]
            return round(sum(vals) / len(vals), decimals) if vals else None

        result[tid] = {
            "team_id":     tid,
            "team_name":   latest["team_name"],
            "abbreviation": latest["abbr"],
            "gp":          gp_season,
            "wins":        cached_wins,
            "losses":      cached_losses,
            "win_pct":     win_pct,
            "pts":         _avg(lambda g: _safe_float(g["pts_for"])),
            "opp_pts":     _avg(lambda g: _safe_float(g["pts_against"])),
            "reb":         _avg(lambda g: g["stats"].get("reboundsTotal")),
            "ast":         _avg(lambda g: g["stats"].get("assists")),
            "stl":         _avg(lambda g: g["stats"].get("steals")),
            "blk":         _avg(lambda g: g["stats"].get("blocks")),
            "tov":         _avg(lambda g: g["stats"].get("turnovers")),
            "fg_pct":      _avg(lambda g: g["stats"].get("fieldGoalsPercentage"), 3),
            "fg3_pct":     _avg(lambda g: g["stats"].get("threePointersPercentage"), 3),
            "ft_pct":      _avg(lambda g: g["stats"].get("freeThrowsPercentage"), 3),
            "last_10":     last_10,
            "home_record": home_record,
            "away_record": away_record,
            "e_off_rating": None,
            "e_def_rating": None,
            "e_net_rating": None,
            "e_pace":       None,
            "streak":       streak,
            "cache_games":  n_total,
            "updated_at":   datetime.utcnow().isoformat(),
        }

    return result


def _compute_player_stats_from_cache(cache: dict) -> list:
    """
    Average each player's per-game stats across all cached games.
    Returns a list sorted by pts desc.
    """
    # player_id → list of single-game stat dicts
    pg: dict[int, list] = {}
    meta: dict[int, dict] = {}

    for entry in cache.values():
        for p in entry.get("players", []):
            pid = p.get("player_id")
            if not pid:
                continue
            # Always update meta so it reflects most recent team
            meta[pid] = {
                "player_name": p.get("player_name", meta.get(pid, {}).get("player_name", "")),
                "team_id":     p.get("team_id"),
                "team":        p.get("team", ""),
            }
            pg.setdefault(pid, []).append(p)

    players = []
    for pid, games in pg.items():
        m    = meta[pid]
        gp   = len(games)

        def _avg(key, decimals=1):
            vals = [_safe_float(g.get(key)) for g in games if g.get(key) is not None]
            return round(sum(vals) / len(vals), decimals) if vals else 0.0

        players.append({
            "player_id":   pid,
            "player_name": m["player_name"],
            "team_id":     m["team_id"],
            "team":        m["team"],
            "gp":          gp,
            "min":         _avg("min"),
            "pts":         _avg("pts"),
            "reb":         _avg("reb"),
            "ast":         _avg("ast"),
            "stl":         _avg("stl"),
            "blk":         _avg("blk"),
            "tov":         _avg("tov"),
            "fg_pct":      _avg("fg_pct", 3),
            "fg3_pct":     _avg("fg3_pct", 3),
            "ft_pct":      _avg("ft_pct", 3),
            "plus_minus":  _avg("plus_minus"),
            "updated_at":  datetime.utcnow().isoformat(),
        })

    players.sort(key=lambda p: p.get("pts") or 0, reverse=True)
    return players


# ── Live data fetchers ─────────────────────────────────────────────────────────

def _fetch_scoreboard() -> dict:
    sb = live_scoreboard.ScoreBoard(**_live_kwargs())
    return sb.get_dict().get("scoreboard", {})


def _fetch_boxscore(game_id: str) -> dict:
    bs = live_boxscore.BoxScore(game_id=game_id, **_live_kwargs())
    return bs.get_dict().get("game", {})


# ── Scoreboard parsers ─────────────────────────────────────────────────────────

def _parse_today_games(sb: dict) -> list:
    today_games = []
    for g in sb.get("games", []):
        home = g.get("homeTeam", {})
        away = g.get("awayTeam", {})
        today_games.append({
            "game_id":          g.get("gameId", ""),
            "game_status":      g.get("gameStatusText", ""),
            "game_status_code": g.get("gameStatus", 0),
            "game_time_et":     g.get("gameEt", g.get("gameTimeUTC", "")),
            "home_team_id":     home.get("teamId", 0),
            "home_team":        home.get("teamName", ""),
            "home_team_abbrev": home.get("teamTricode", ""),
            "home_score":       home.get("score", 0),
            "away_team_id":     away.get("teamId", 0),
            "away_team":        away.get("teamName", ""),
            "away_team_abbrev": away.get("teamTricode", ""),
            "away_score":       away.get("score", 0),
            "period":           g.get("period", 0),
            "game_clock":       g.get("gameClock", ""),
            "updated_at":       datetime.utcnow().isoformat(),
        })
    return today_games


def _team_entry_from_sb(t: dict) -> dict:
    """Minimal team entry seeded from a scoreboard homeTeam/awayTeam dict."""
    wins   = int(t.get("wins", 0) or 0)
    losses = int(t.get("losses", 0) or 0)
    total  = wins + losses
    city   = t.get("teamCity", "")
    name   = t.get("teamName", "")
    return {
        "team_id":     t.get("teamId"),
        "team_name":   f"{city} {name}".strip() if city else name,
        "abbreviation": t.get("teamTricode", ""),
        "gp":    total,
        "wins":  wins,
        "losses": losses,
        "win_pct": round(wins / total, 3) if total else 0.0,
    }


# ── Playoff cache backfill ────────────────────────────────────────────────────

def _backfill_playoff_cache(cache: dict, limit: int = 60) -> int:
    """Seed the cache with 2025 NBA playoff boxscores on first deploy.

    Iterates game IDs 0042500101–0042500499 (format: 004250{round:02d}{game:02d}).
    Skips IDs that error or are not yet Final. Stops once `limit` games cached.
    """
    cached = 0
    for n in range(42500101, 42500500):
        if cached >= limit:
            break
        game_id = f"{n:010d}"
        if game_id in cache:
            continue
        try:
            box = _fetch_boxscore(game_id)
            if not box:
                time.sleep(0.5)
                continue
            if box.get("gameStatus") != 3:
                time.sleep(0.5)
                continue
            raw_ts = box.get("gameTimeUTC") or box.get("gameEt") or ""
            game_date = raw_ts[:10] if len(raw_ts) >= 10 else datetime.utcnow().date().isoformat()
            cache[game_id] = _build_cache_entry(game_id, game_date, box)
            cached += 1
            print(f"  backfill: {cached}/{limit} games cached")
        except Exception:
            pass
        time.sleep(0.5)
    return cached


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

    # ── 1. Load cache (backfill on first deploy) ───────────────────────────────
    cache = _load_cache()
    print(f"  cache: {len(cache)} games stored")
    if not cache:
        print("  cache empty — running 2025 playoff backfill (up to 60 games)...")
        n = _backfill_playoff_cache(cache, limit=60)
        print(f"  backfill complete: {n} games added")
        if cache:
            _save_cache(cache)

    # ── 2. Scoreboard ──────────────────────────────────────────────────────────
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

    # Seed live W/L for all teams playing today from the scoreboard
    # (scoreboard always has current season totals)
    live_wl: dict[int, dict] = {}
    for g in games:
        for side in ("homeTeam", "awayTeam"):
            t = g.get(side, {})
            tid = t.get("teamId")
            if tid:
                live_wl[tid] = _team_entry_from_sb(t)

    # ── 3. Box scores ──────────────────────────────────────────────────────────
    # live_players: players from non-Final games (not yet cacheable)
    live_players: dict[int, dict] = {}  # player_id → stat dict

    fetched = skipped = cached_new = 0

    for g in games:
        game_id     = g.get("gameId", "")
        game_status = g.get("gameStatus", 0)   # 1=scheduled, 2=live, 3=final
        is_final    = (game_status == 3)

        if not game_id:
            continue

        if is_final and game_id in cache:
            skipped += 1
            print(f"  skip {game_id} (Final, cached)")
            continue

        print(f"  fetch boxscore {game_id} (status={game_status})...")
        try:
            box = _fetch_boxscore(game_id)

            if is_final:
                # Derive game_date from scoreboard timestamp
                ts = g.get("gameEt", "") or g.get("gameTimeUTC", "")
                game_date = ts[:10] if len(ts) >= 10 else datetime.utcnow().date().isoformat()
                cache[game_id] = _build_cache_entry(game_id, game_date, box)
                cached_new += 1
                print(f"  cached Final game {game_id} ({game_date})")
            else:
                # Live / upcoming — collect player stats for today's output
                for side in ("homeTeam", "awayTeam"):
                    for p in box.get(side, {}).get("players", []):
                        if p.get("played") != "1":
                            continue
                        pid  = p.get("personId")
                        s    = p.get("statistics", {})
                        if pid:
                            live_players[pid] = {
                                "player_id":   pid,
                                "player_name": p.get("name", ""),
                                "team_id":     box.get(side, {}).get("teamId"),
                                "team":        box.get(side, {}).get("teamTricode", ""),
                                "gp":  1,
                                "min": _parse_minutes(s.get("minutesCalculated", "")),
                                "pts": _safe_float(s.get("points")),
                                "reb": _safe_float(s.get("reboundsTotal")),
                                "ast": _safe_float(s.get("assists")),
                                "stl": _safe_float(s.get("steals")),
                                "blk": _safe_float(s.get("blocks")),
                                "tov": _safe_float(s.get("turnovers")),
                                "fg_pct":  round(_safe_float(s.get("fieldGoalsPercentage")), 3),
                                "fg3_pct": round(_safe_float(s.get("threePointersPercentage")), 3),
                                "ft_pct":  round(_safe_float(s.get("freeThrowsPercentage")), 3),
                                "plus_minus": _safe_float(s.get("plusMinusPoints")),
                                "updated_at": datetime.utcnow().isoformat(),
                            }

            fetched += 1

        except Exception as e:
            print(f"  WARNING: boxscore {game_id} failed: {e}")

    print(f"  boxscores: {fetched} fetched, {skipped} skipped, {cached_new} newly cached")
    print(f"  cache: {len(cache)} games stored")

    # ── 4. Compute historical team & player stats from cache ───────────────────
    historical = _compute_team_stats_from_cache(cache)
    print(f"  historical: {len(historical)} teams computed from cache")

    cached_players = _compute_player_stats_from_cache(cache)
    print(f"  historical: {len(cached_players)} players computed from cache")

    # ── 5. Merge live W/L into historical team records ─────────────────────────
    # The scoreboard always has the true season W/L total;
    # cache-derived W/L may be a subset if we missed earlier games.
    for tid, live in live_wl.items():
        if tid in historical:
            h = historical[tid]
            h["wins"]    = live["wins"]
            h["losses"]  = live["losses"]
            gp           = live["wins"] + live["losses"]
            h["gp"]      = gp
            h["win_pct"] = round(live["wins"] / gp, 3) if gp else 0.0
            # Keep cache-derived name/abbr only if live has blanks
            if live.get("team_name"):
                h["team_name"]   = live["team_name"]
            if live.get("abbreviation"):
                h["abbreviation"] = live["abbreviation"]
            h["updated_at"] = datetime.utcnow().isoformat()
        else:
            # Playing today but no cache data yet — use scoreboard seed
            historical[tid] = {
                **live,
                "pts": None, "opp_pts": None, "reb": None, "ast": None,
                "stl": None, "blk": None, "tov": None,
                "fg_pct": None, "fg3_pct": None, "ft_pct": None,
                "last_10": None, "home_record": None, "away_record": None,
                "e_off_rating": None, "e_def_rating": None,
                "e_net_rating": None, "e_pace": None,
                "streak": 0,
                "cache_games": 0,
                "updated_at": datetime.utcnow().isoformat(),
            }

    # ── 6. Build team_stats.json — all 30 teams ────────────────────────────────
    static_teams = {t["id"]: t for t in nba_teams.get_teams()}
    team_stats: list = []

    for tid, static in static_teams.items():
        if tid in historical:
            entry = historical[tid]
            if not entry.get("team_name"):
                entry["team_name"] = static["full_name"]
            if not entry.get("abbreviation"):
                entry["abbreviation"] = static["abbreviation"]
            team_stats.append(entry)
        else:
            team_stats.append({
                "team_id":     tid,
                "team_name":   static["full_name"],
                "abbreviation": static["abbreviation"],
                "gp": 0, "wins": 0, "losses": 0, "win_pct": 0.0,
                "pts": None, "opp_pts": None, "reb": None, "ast": None,
                "stl": None, "blk": None, "tov": None,
                "fg_pct": None, "fg3_pct": None, "ft_pct": None,
                "last_10": None, "home_record": None, "away_record": None,
                "e_off_rating": None, "e_def_rating": None,
                "e_net_rating": None, "e_pace": None,
                "streak": 0,
                "cache_games": 0,
                "updated_at": datetime.utcnow().isoformat(),
            })

    team_stats.sort(key=lambda t: t.get("win_pct") or 0, reverse=True)
    save_json("team_stats.json", team_stats)
    with_data = sum(1 for t in team_stats if (t.get("cache_games") or 0) > 0)
    print(f"  team_stats: {with_data}/{len(team_stats)} teams with cached game data")

    # ── 7. Player stats: cache averages ∪ today's live players ────────────────
    cached_pids = {p["player_id"] for p in cached_players}
    extra = [p for p in live_players.values() if p["player_id"] not in cached_pids]
    all_players = cached_players + extra
    all_players.sort(key=lambda p: p.get("pts") or 0, reverse=True)
    save_json("player_stats.json", all_players)
    print(f"  player_stats: {len(cached_players)} from cache + {len(extra)} live-only")

    # ── 8. Save updated cache ──────────────────────────────────────────────────
    _save_cache(cache)

    # ── 9. Empty stubs ─────────────────────────────────────────────────────────
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
