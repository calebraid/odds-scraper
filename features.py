import json
import math
import os
import re

_STATS_DIR = "stats"

TEAM_MAP: dict[str, str] = {
    "atlanta": "Atlanta Hawks",
    "boston": "Boston Celtics",
    "brooklyn": "Brooklyn Nets",
    "charlotte": "Charlotte Hornets",
    "chicago": "Chicago Bulls",
    "cleveland": "Cleveland Cavaliers",
    "dallas": "Dallas Mavericks",
    "denver": "Denver Nuggets",
    "detroit": "Detroit Pistons",
    "golden state": "Golden State Warriors",
    "houston": "Houston Rockets",
    "indiana": "Indiana Pacers",
    "los angeles c": "Los Angeles Clippers",
    "los angeles l": "Los Angeles Lakers",
    "la clippers": "Los Angeles Clippers",
    "la lakers": "Los Angeles Lakers",
    "memphis": "Memphis Grizzlies",
    "miami": "Miami Heat",
    "milwaukee": "Milwaukee Bucks",
    "minnesota": "Minnesota Timberwolves",
    "new orleans": "New Orleans Pelicans",
    "new york": "New York Knicks",
    "oklahoma city": "Oklahoma City Thunder",
    "orlando": "Orlando Magic",
    "philadelphia": "Philadelphia 76ers",
    "phoenix": "Phoenix Suns",
    "portland": "Portland Trail Blazers",
    "sacramento": "Sacramento Kings",
    "san antonio": "San Antonio Spurs",
    "toronto": "Toronto Raptors",
    "utah": "Utah Jazz",
    "washington": "Washington Wizards",
}

PROP_TYPES = {"reb_assists", "blocks", "steals", "triple_double"}


def _load_teams() -> list[dict]:
    path = os.path.join(_STATS_DIR, "team_stats.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    return data.get("teams", [])


def _load_recent() -> dict[str, list[dict]]:
    path = os.path.join(_STATS_DIR, "recent_games.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {entry["team_name"]: entry["games"] for entry in data.get("teams", []) if entry.get("team_name")}


def _load_players() -> list[dict]:
    path = os.path.join(_STATS_DIR, "player_stats.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("players", [])


def find_team(name: str, teams: list[dict]) -> dict | None:
    if not name:
        return None
    low = name.lower().strip()

    for key, full in TEAM_MAP.items():
        if key in low:
            for t in teams:
                if t.get("team_name") == full:
                    return t

    clean = re.sub(r"[^a-z ]", "", low).strip()
    for t in teams:
        tname = (t.get("team_name") or "").lower()
        if clean in tname or tname.split()[-1] in clean:
            return t

    return None


def _parse_record(record: str | None, default: float = 0.5) -> float:
    """Parse 'W-L' record string to win percentage. '32-8' → 0.8"""
    if not record:
        return default
    try:
        parts = str(record).split("-")
        w, l = int(parts[0]), int(parts[1])
        total = w + l
        return round(w / total, 3) if total else default
    except Exception:
        return default


def _player_features(team_id: int | None, players: list[dict]) -> dict:
    team_ps = sorted(
        [p for p in players if p.get("team_id") == team_id],
        key=lambda p: p.get("pts") or 0,
        reverse=True,
    )
    top3 = team_ps[:3]
    top3_avg = round(sum(p.get("pts") or 0 for p in top3) / len(top3), 1) if top3 else 0.0
    top_scorer = float(top3[0].get("pts") or 0) if top3 else 0.0
    total_stl = round(sum(p.get("stl") or 0 for p in team_ps), 1)
    total_blk = round(sum(p.get("blk") or 0 for p in team_ps), 1)
    return {
        "top3_avg_pts": top3_avg,
        "top_scorer_pts": top_scorer,
        "total_stl": total_stl,
        "total_blk": total_blk,
    }


def _last_n_wins(games: list[dict], n: int = 5) -> float:
    results = [g.get("wl") for g in games[:n]]
    wins = sum(1 for r in results if r == "W")
    total = sum(1 for r in results if r in ("W", "L"))
    return wins / total if total else 0.5


def _kalshi_line(market: dict) -> float | None:
    title = market.get("title") or ""
    m = re.search(r"([\d.]+)", title)
    if m:
        return float(m.group(1))
    return None


def build_features(
    market: dict,
    teams: list[dict] | None = None,
    recent: dict | None = None,
    players: list[dict] | None = None,
) -> dict | None:
    if market.get("market_type") in PROP_TYPES:
        return None

    if teams is None:
        teams = _load_teams()
    if recent is None:
        recent = _load_recent()
    if players is None:
        players = _load_players()

    yes_label = market.get("yes_team") or ""
    no_label = market.get("no_team") or ""

    t1 = find_team(yes_label, teams)
    t2 = find_team(no_label, teams)

    if t1 is None or t2 is None:
        return None

    def safe(v, default=0.0) -> float:
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def _pick(team: dict, *keys, default: float = 0.0) -> float:
        for k in keys:
            v = team.get(k)
            if v is not None:
                return safe(v, default)
        return default

    # Team stats
    t1_win_pct  = _pick(t1, "win_pct", default=0.5)
    t2_win_pct  = _pick(t2, "win_pct", default=0.5)
    t1_wins     = _pick(t1, "wins", default=0.0)
    t2_wins     = _pick(t2, "wins", default=0.0)
    t1_losses   = _pick(t1, "losses", default=0.0)
    t2_losses   = _pick(t2, "losses", default=0.0)
    t1_pts      = _pick(t1, "pts", default=110.0)
    t2_pts      = _pick(t2, "pts", default=110.0)
    t1_opp_pts  = _pick(t1, "opp_pts", default=110.0)
    t2_opp_pts  = _pick(t2, "opp_pts", default=110.0)
    t1_reb      = _pick(t1, "reb", default=44.0)
    t2_reb      = _pick(t2, "reb", default=44.0)
    t1_ast      = _pick(t1, "ast", default=25.0)
    t2_ast      = _pick(t2, "ast", default=25.0)
    t1_stl      = _pick(t1, "stl", default=8.0)
    t2_stl      = _pick(t2, "stl", default=8.0)
    t1_blk      = _pick(t1, "blk", default=5.0)
    t2_blk      = _pick(t2, "blk", default=5.0)
    t1_tov      = _pick(t1, "tov", default=14.0)
    t2_tov      = _pick(t2, "tov", default=14.0)
    t1_fg_pct   = _pick(t1, "fg_pct", default=0.46)
    t2_fg_pct   = _pick(t2, "fg_pct", default=0.46)
    t1_fg3_pct  = _pick(t1, "fg3_pct", default=0.36)
    t2_fg3_pct  = _pick(t2, "fg3_pct", default=0.36)
    t1_ft_pct   = _pick(t1, "ft_pct", default=0.78)
    t2_ft_pct   = _pick(t2, "ft_pct", default=0.78)
    t1_off      = _pick(t1, "e_off_rating", "off_rtg", default=110.0)
    t2_off      = _pick(t2, "e_off_rating", "off_rtg", default=110.0)
    t1_def      = _pick(t1, "e_def_rating", "def_rtg", default=110.0)
    t2_def      = _pick(t2, "e_def_rating", "def_rtg", default=110.0)
    t1_net      = _pick(t1, "e_net_rating", "net_rtg", default=0.0)
    t2_net      = _pick(t2, "e_net_rating", "net_rtg", default=0.0)
    t1_pace     = _pick(t1, "e_pace", "pace", default=100.0)
    t2_pace     = _pick(t2, "e_pace", "pace", default=100.0)

    t1_home_pct   = _parse_record(t1.get("home_record"))
    t2_home_pct   = _parse_record(t2.get("home_record"))
    t1_away_pct   = _parse_record(t1.get("away_record"))
    t2_away_pct   = _parse_record(t2.get("away_record"))
    t1_last10_pct = _parse_record(t1.get("last_10"))
    t2_last10_pct = _parse_record(t2.get("last_10"))
    t1_streak     = safe(t1.get("streak"), 0.0)
    t2_streak     = safe(t2.get("streak"), 0.0)

    # Player features
    t1_pf = _player_features(t1.get("team_id"), players)
    t2_pf = _player_features(t2.get("team_id"), players)

    # Market context
    kalshi_yes_price = safe(market.get("yes_ask"), 0.5)
    market_line_val  = _kalshi_line(market) or 0.0

    # Estimated win probability via sigmoid of win% and net rating spread
    win_score    = (t1_win_pct - t2_win_pct) * 3.0 + (t1_net - t2_net) * 0.1
    our_win_prob = round(1.0 / (1.0 + math.exp(-win_score)), 3)
    edge         = round(our_win_prob - kalshi_yes_price, 3)

    t1_pts_diff = t1_pts - t1_opp_pts
    t2_pts_diff = t2_pts - t2_opp_pts

    return {
        # YES-team
        "t1_win_pct": t1_win_pct, "t1_wins": t1_wins, "t1_losses": t1_losses,
        "t1_pts": t1_pts, "t1_opp_pts": t1_opp_pts, "t1_pts_differential": t1_pts_diff,
        "t1_reb": t1_reb, "t1_ast": t1_ast,
        "t1_stl": t1_stl, "t1_blk": t1_blk, "t1_tov": t1_tov,
        "t1_fg_pct": t1_fg_pct, "t1_fg3_pct": t1_fg3_pct, "t1_ft_pct": t1_ft_pct,
        "t1_off_rtg": t1_off, "t1_def_rtg": t1_def, "t1_net_rtg": t1_net, "t1_pace": t1_pace,
        "t1_home_pct": t1_home_pct, "t1_away_pct": t1_away_pct,
        "t1_last10_pct": t1_last10_pct, "t1_streak": t1_streak,
        # NO-team
        "t2_win_pct": t2_win_pct, "t2_wins": t2_wins, "t2_losses": t2_losses,
        "t2_pts": t2_pts, "t2_opp_pts": t2_opp_pts, "t2_pts_differential": t2_pts_diff,
        "t2_reb": t2_reb, "t2_ast": t2_ast,
        "t2_stl": t2_stl, "t2_blk": t2_blk, "t2_tov": t2_tov,
        "t2_fg_pct": t2_fg_pct, "t2_fg3_pct": t2_fg3_pct, "t2_ft_pct": t2_ft_pct,
        "t2_off_rtg": t2_off, "t2_def_rtg": t2_def, "t2_net_rtg": t2_net, "t2_pace": t2_pace,
        "t2_home_pct": t2_home_pct, "t2_away_pct": t2_away_pct,
        "t2_last10_pct": t2_last10_pct, "t2_streak": t2_streak,
        # Matchup differentials
        "win_pct_diff": t1_win_pct - t2_win_pct,
        "pts_diff": t1_pts - t2_pts,
        "pts_against_diff": t1_opp_pts - t2_opp_pts,
        "net_rtg_diff": t1_net - t2_net,
        "off_rtg_diff": t1_off - t2_off,
        "pace_diff": t1_pace - t2_pace,
        "stl_diff": t1_stl - t2_stl,
        "blk_diff": t1_blk - t2_blk,
        "tov_diff": t1_tov - t2_tov,
        # Player
        "t1_top3_avg_pts": t1_pf["top3_avg_pts"],
        "t2_top3_avg_pts": t2_pf["top3_avg_pts"],
        "t1_top_scorer_pts": t1_pf["top_scorer_pts"],
        "t2_top_scorer_pts": t2_pf["top_scorer_pts"],
        "t1_total_stl": t1_pf["total_stl"],
        "t2_total_stl": t2_pf["total_stl"],
        "t1_total_blk": t1_pf["total_blk"],
        "t2_total_blk": t2_pf["total_blk"],
        "player_pts_diff": t1_pf["top3_avg_pts"] - t2_pf["top3_avg_pts"],
        # Market context
        "kalshi_yes_price": kalshi_yes_price,
        "market_line": market_line_val,
        "our_win_prob": our_win_prob,
        "edge": edge,
        # Retained for baseline functions and backward compat
        "kalshi_line": _kalshi_line(market),
        "t1_ppg": t1_pts, "t2_ppg": t2_pts,
        "pace_avg": (t1_pace + t2_pace) / 2,
    }
