import json
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
        return json.load(f).get("teams", [])


def _load_recent() -> dict[str, list[dict]]:
    path = os.path.join(_STATS_DIR, "recent_games.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {entry["team_name"]: entry["games"] for entry in data.get("teams", []) if entry.get("team_name")}


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


def build_features(market: dict, teams: list[dict] | None = None, recent: dict | None = None) -> dict | None:
    if market.get("market_type") in PROP_TYPES:
        return None

    if teams is None:
        teams = _load_teams()
    if recent is None:
        recent = _load_recent()

    yes_label = market.get("yes_team") or ""
    no_label = market.get("no_team") or ""

    t1 = find_team(yes_label, teams)
    t2 = find_team(no_label, teams)

    if t1 is None or t2 is None:
        return None

    t1_games = recent.get(t1.get("team_name"), [])
    t2_games = recent.get(t2.get("team_name"), [])

    def safe(v, default=0.0) -> float:
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    t1_win_pct = safe(t1.get("win_pct"), 0.5)
    t2_win_pct = safe(t2.get("win_pct"), 0.5)
    t1_off = safe(t1.get("off_rtg"), 110.0)
    t2_off = safe(t2.get("off_rtg"), 110.0)
    t1_def = safe(t1.get("def_rtg"), 110.0)
    t2_def = safe(t2.get("def_rtg"), 110.0)
    t1_net = safe(t1.get("net_rtg"), 0.0)
    t2_net = safe(t2.get("net_rtg"), 0.0)
    t1_pace = safe(t1.get("pace"), 100.0)
    t2_pace = safe(t2.get("pace"), 100.0)
    t1_ppg = safe(t1.get("ppg"), 110.0)
    t2_ppg = safe(t2.get("ppg"), 110.0)

    return {
        "t1_win_pct": t1_win_pct,
        "t2_win_pct": t2_win_pct,
        "t1_off_rtg": t1_off,
        "t2_off_rtg": t2_off,
        "t1_def_rtg": t1_def,
        "t2_def_rtg": t2_def,
        "t1_net_rtg": t1_net,
        "t2_net_rtg": t2_net,
        "t1_pace": t1_pace,
        "t2_pace": t2_pace,
        "t1_ppg": t1_ppg,
        "t2_ppg": t2_ppg,
        "t1_last5": _last_n_wins(t1_games, 5),
        "t2_last5": _last_n_wins(t2_games, 5),
        "win_pct_diff": t1_win_pct - t2_win_pct,
        "net_rtg_diff": t1_net - t2_net,
        "off_rtg_diff": t1_off - t2_off,
        "pace_avg": (t1_pace + t2_pace) / 2,
        "kalshi_line": _kalshi_line(market),
    }
