import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

NBA_BASE = "https://sportsbook.draftkings.com/leagues/basketball/nba"
NBA_GAME_LINES = f"{NBA_BASE}?category=games&subcategory=game-lines"
NBA_PLAYER_PROPS = f"{NBA_BASE}?category=games&subcategory=player-props"
OUTPUT_DIR = "odds"
INTERVAL_SECONDS = 60
DEBUG = "--debug" in sys.argv


def parse_american_odds(text: str | None) -> int | None:
    if not text:
        return None
    text = text.strip().replace("−", "-").replace("–", "-").replace(" ", "")
    if text in ("", "—", "-"):
        return None
    if text == "EVEN":
        return 100
    match = re.search(r"[+\-]?\d+", text)
    if not match:
        return None
    try:
        return int(match.group())
    except ValueError:
        return None


async def _text(el) -> str | None:
    if el is None:
        return None
    return (await el.inner_text()).strip() or None


async def scroll_to_bottom(page, container_sel: str, max_rounds: int = 15) -> None:
    """Scroll until no new containers appear or max_rounds hit."""
    prev = 0
    for _ in range(max_rounds):
        count = len(await page.query_selector_all(container_sel))
        if count > 0 and count == prev:
            break
        prev = count
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)


# ── Game Lines ────────────────────────────────────────────────────────────────

async def extract_game(container) -> dict | None:
    # Team names — first = away, second = home
    team_els = await container.query_selector_all(".cb-market__label-inner--parlay")
    if len(team_els) < 2:
        return None
    away_team = await _text(team_els[0])
    home_team = await _text(team_els[1])
    if not away_team or not home_team:
        return None

    # Game time (pre-game) or live status
    time_el = await container.query_selector(".cb-event-cell__start-time")
    status_el = await container.query_selector(".cb-event-cell__status")
    raw_time = await _text(time_el) or await _text(status_el)
    game_time = raw_time.split("\n")[0].strip() if raw_time else None

    # Period / quarter (live only)
    period_el = await container.query_selector(".cb-event-cell__period")
    period = await _text(period_el)

    # Live score
    score_els = await container.query_selector_all(".cb-market__scoreboard-team-score")
    score = None
    if len(score_els) >= 2:
        s1 = await _text(score_els[0])
        s2 = await _text(score_els[1])
        if s1 and s2:
            score = {"away": s1, "home": s2}

    # 6 market buttons: [away_spread, over, away_ml, home_spread, under, home_ml]
    buttons = await container.query_selector_all(".cb-market__button")
    if len(buttons) < 6:
        return None

    async def btn_data(btn):
        points = await _text(await btn.query_selector(".cb-market__button-points"))
        odds = parse_american_odds(await _text(await btn.query_selector(".cb-market__button-odds")))
        title = await _text(await btn.query_selector(".cb-market__button-title"))
        return {"title": title, "points": points, "odds": odds}

    b = [await btn_data(buttons[i]) for i in range(6)]

    game = {
        "matchup": f"{away_team} @ {home_team}",
        "away_team": away_team,
        "home_team": home_team,
        "game_time": game_time,
        "score": score,
        "spread": {
            "away": {"line": b[0]["points"], "odds": b[0]["odds"]},
            "home": {"line": b[3]["points"], "odds": b[3]["odds"]},
        },
        "moneyline": {
            "away": b[2]["odds"],
            "home": b[5]["odds"],
        },
        "total": {
            "over":  {"line": b[1]["points"], "odds": b[1]["odds"]},
            "under": {"line": b[4]["points"], "odds": b[4]["odds"]},
        },
    }
    if period:
        game["period"] = period
    return game


async def scrape_game_lines(page) -> list[dict]:
    try:
        await page.goto(NBA_GAME_LINES, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector(".cms-market-selector-static__event-wrapper", timeout=20_000)
    except PlaywrightTimeoutError:
        print("  [game-lines] Timed out waiting for containers.")
        return []

    await asyncio.sleep(2)
    await scroll_to_bottom(page, ".cms-market-selector-static__event-wrapper")

    if DEBUG:
        _save_debug_html(await page.content(), "debug_game_lines.html")

    containers = await page.query_selector_all(".cms-market-selector-static__event-wrapper")
    games = []
    for c in containers:
        game = await extract_game(c)
        if game:
            games.append(game)
    return games


# ── Player Props ──────────────────────────────────────────────────────────────

# Game-section selectors: most- to least-specific.
# The props tab reuses the same outer game-wrapper as game-lines, then uses
# either sportsbook-table (older DK layout) or cb-* tables inside.
_PROP_GAME_SELECTORS = [
    ".sportsbook-event-accordion__wrapper",
    "[class*='event-accordion__wrapper']",
    "[class*='event-accordion']",
    ".cms-market-selector-static__event-wrapper",  # same wrapper as game-lines
    "[data-testid='componentid-210']",              # known componentid for game blocks
]

_PROP_MARKET_SELECTORS = [
    ".sportsbook-offer-category-panel",
    "[class*='offer-category-panel']",
    "[class*='market-category']",
    "[class*='offer-category']",
    ".cb-market__template",                         # cb-* table header on props tab
    "[class*='cb-market__template']",
]

_PROP_ROW_SELECTORS = [
    ".sportsbook-table__row",
    "[class*='table__row']",
    "[class*='outcome-row']",
    ".cb-market__button",                           # props may reuse game-lines button rows
]


async def _wait_for_props_content(page) -> bool:
    candidates = [
        ".sportsbook-event-accordion__wrapper",
        ".sportsbook-table",
        ".cms-market-selector-static__event-wrapper",
        "[data-testid='marketboard']",
        ".cms-market-selector-content",
    ]
    for sel in candidates:
        try:
            await page.wait_for_selector(sel, timeout=8_000)
            print(f"  [props] content ready ({sel})")
            return True
        except PlaywrightTimeoutError:
            continue
    return False


async def _log_props_page_classes(page) -> None:
    """Emit the first 30 unique class tokens found inside the marketboard — used
    to diagnose selector mismatches without needing a full HTML dump."""
    try:
        raw = await page.evaluate("""() => {
            const board = document.querySelector('[data-testid="marketboard"]')
                       || document.querySelector('.cms-market-selector-content')
                       || document.body;
            const seen = new Set();
            for (const el of board.querySelectorAll('[class]')) {
                for (const cls of el.className.split(' ')) {
                    if (cls) seen.add(cls);
                    if (seen.size >= 30) return [...seen];
                }
            }
            return [...seen];
        }""")
        print(f"  [props] classes on page: {raw}")
    except Exception as exc:
        print(f"  [props] class probe failed: {exc}")


async def extract_prop_outcome(row_el) -> dict | None:
    label_el = (
        await row_el.query_selector(".sportsbook-outcome-cell__label")
        or await row_el.query_selector("[class*='outcome-cell__label']")
    )
    player = await _text(label_el)
    if not player:
        return None

    line_el = (
        await row_el.query_selector(".sportsbook-outcome-cell__line")
        or await row_el.query_selector("[class*='outcome-cell__line']")
    )
    line_text = await _text(line_el)
    try:
        line = float(line_text) if line_text else None
    except ValueError:
        line = None

    odds_els = await row_el.query_selector_all(".sportsbook-odds, [class*='sportsbook-odds']")
    odds_vals = [parse_american_odds(await _text(o)) for o in odds_els]

    result: dict = {"player": player}
    if line is not None:
        result["line"] = line
    if len(odds_vals) >= 2:
        result["over_odds"] = odds_vals[0]
        result["under_odds"] = odds_vals[1]
    elif len(odds_vals) == 1:
        result["odds"] = odds_vals[0]
    return result


async def scrape_player_props(page) -> list[dict]:
    """Returns list of {matchup, markets: {market_name: [outcomes]}}."""
    try:
        await page.goto(NBA_PLAYER_PROPS, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeoutError:
        print("  [props] Navigation timed out.")
        return []

    if not await _wait_for_props_content(page):
        print("  [props] No recognisable content — skipping.")
        await _log_props_page_classes(page)
        if DEBUG:
            _save_debug_html(await page.content(), "debug_player_props.html")
        return []

    await asyncio.sleep(3)

    # Discover which game-section selector the page uses
    game_sel = None
    for sel in _PROP_GAME_SELECTORS:
        if await page.query_selector(sel):
            game_sel = sel
            print(f"  [props] game selector: {sel!r}")
            break

    if not game_sel:
        print("  [props] No game sections found — selector tuning needed.")
        await _log_props_page_classes(page)
        if DEBUG:
            _save_debug_html(await page.content(), "debug_player_props.html")
        return []

    await scroll_to_bottom(page, game_sel, max_rounds=12)

    if DEBUG:
        _save_debug_html(await page.content(), "debug_player_props.html")

    game_sections = await page.query_selector_all(game_sel)
    print(f"  [props] {len(game_sections)} game section(s) found with {game_sel!r}")
    if not game_sections:
        await _log_props_page_classes(page)
    results = []

    for section in game_sections:
        # Try to grab the matchup from the section header
        header_el = None
        for h_sel in [
            ".sportsbook-event-accordion__title",
            "[class*='accordion__title']",
            ".event-cell__name-text",
            "[class*='event-cell__name']",
        ]:
            header_el = await section.query_selector(h_sel)
            if header_el:
                break
        matchup = re.sub(r"\s+", " ", (await _text(header_el) or "Unknown")).strip()

        markets: dict[str, list] = {}

        # Try to find market-category panels
        market_panel_sel = None
        for sel in _PROP_MARKET_SELECTORS:
            panels = await section.query_selector_all(sel)
            if panels:
                market_panel_sel = sel
                break

        panels = await section.query_selector_all(market_panel_sel) if market_panel_sel else []

        for panel in panels:
            name_el = None
            for n_sel in [
                ".sportsbook-offer-category-panel__header",
                "[class*='category-header']",
                "[class*='panel-header']",
                "[class*='category-title']",
                "h4",
            ]:
                name_el = await panel.query_selector(n_sel)
                if name_el:
                    break
            market_name = (await _text(name_el) or "Unknown").strip()

            row_sel = None
            for sel in _PROP_ROW_SELECTORS:
                rows = await panel.query_selector_all(sel)
                if rows:
                    row_sel = sel
                    break

            rows = await panel.query_selector_all(row_sel) if row_sel else []
            outcomes = []
            for row in rows:
                outcome = await extract_prop_outcome(row)
                if outcome:
                    outcomes.append(outcome)

            if outcomes:
                markets[market_name] = outcomes

        if markets:
            results.append({"matchup": matchup, "markets": markets})

    return results


# ── Persistence ───────────────────────────────────────────────────────────────

def _save_debug_html(html: str, filename: str) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  [debug] saved {path}")


def save(games: list[dict], player_props: list[dict], timestamp: str) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    payload = {
        "source": "DraftKings",
        "league": "NBA",
        "scraped_at": timestamp,
        "game_count": len(games),
        "games": games,
        "player_props": player_props,
    }
    latest = os.path.join(OUTPUT_DIR, "latest.json")
    with open(latest, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    slug = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot = os.path.join(OUTPUT_DIR, f"nba_{slug}.json")
    with open(snapshot, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    return latest


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main():
    print(f"NBA odds scraper  |  interval={INTERVAL_SECONDS}s  |  source=DraftKings")
    if DEBUG:
        print("  DEBUG: HTML snapshots saved to odds/")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        run = 0
        while True:
            run += 1
            ts = datetime.now(timezone.utc).isoformat()
            print(f"\n[{ts}] run #{run}")

            try:
                games = await scrape_game_lines(page)
                print(f"  game lines: {len(games)} game(s)")
                for g in games:
                    live = (
                        f"  [LIVE {g['score']['away']}-{g['score']['home']}]"
                        if g.get("score")
                        else f"  {g.get('game_time') or ''}"
                    )
                    print(f"  {g['matchup']}{live}")
                    print(
                        f"    spread    away {g['spread']['away']['line']} ({g['spread']['away']['odds']})  "
                        f"home {g['spread']['home']['line']} ({g['spread']['home']['odds']})"
                    )
                    print(f"    moneyline away {g['moneyline']['away']}  home {g['moneyline']['home']}")
                    print(
                        f"    total     O{g['total']['over']['line']} ({g['total']['over']['odds']})  "
                        f"U{g['total']['under']['line']} ({g['total']['under']['odds']})"
                    )

                player_props = await scrape_player_props(page)
                print(f"  player props: {len(player_props)} game(s)")
                for gp in player_props:
                    mkts = list(gp.get("markets", {}).keys())
                    print(f"    {gp['matchup']}: {len(mkts)} markets — {', '.join(mkts[:6])}")

                out = save(games, player_props, ts)
                print(f"  saved -> {out}")

            except Exception as exc:
                print(f"  ERROR: {exc}")

            print(f"  sleeping {INTERVAL_SECONDS}s ...")
            await asyncio.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
