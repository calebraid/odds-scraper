import asyncio
import json
import os
import random
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


# ── Player Props (API interception) ──────────────────────────────────────────

# Subdomains DraftKings uses for its internal odds/event API.
# We only parse JSON from these — skipping analytics, ads, etc.
_DK_API_DOMAINS = ("sportsbook-nash.draftkings.com", "api.draftkings.com")


async def _intercept_props_api(page) -> list[tuple[str, any]]:
    """Navigate to the player-props subcategory page and capture every JSON
    response from DK's internal API domains.  The listener is registered
    before navigation so no early calls are missed.

    Returns a list of (url, parsed_body) pairs, largest bodies first so the
    main event-data response (usually the biggest) is tried first.
    """
    captured: list[tuple[str, any]] = []

    async def on_response(response):
        if response.status != 200:
            return
        url = response.url
        if not any(d in url for d in _DK_API_DOMAINS):
            return
        if "json" not in response.headers.get("content-type", ""):
            return
        try:
            body = await response.json()
        except Exception:
            return
        # Skip trivially small responses (analytics pings, feature flags, etc.)
        body_str = json.dumps(body)
        if len(body_str) < 500:
            return
        captured.append((url, body))

    page.on("response", on_response)
    try:
        await page.goto(NBA_PLAYER_PROPS, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector(
            ".cms-market-selector-static__event-wrapper", timeout=15_000
        )
    except PlaywrightTimeoutError:
        pass  # Carry on — API calls may still have fired

    # Extra wait + scroll to trigger any lazy-loaded prop requests
    await asyncio.sleep(3)
    await scroll_to_bottom(page, ".cms-market-selector-static__event-wrapper")
    await asyncio.sleep(2)

    page.remove_listener("response", on_response)

    # Largest responses first — the main odds payload is usually the biggest
    captured.sort(key=lambda t: len(json.dumps(t[1])), reverse=True)
    return captured


def _parse_dk_api_props(captured: list[tuple[str, any]]) -> list[dict]:
    """Parse player props out of captured DraftKings API responses.

    DK's sportsbook-nash API response shape (as of 2024-2026):
      {
        "eventGroup": {
          "events": [
            {
              "eventId": ...,
              "name": "Team A @ Team B",       # or teamName1/teamName2
              "eventCategories": [
                {
                  "name": "Player Props",
                  "componentizedOffers": [
                    {
                      "subcategoryName": "Points",
                      "offers": [
                        {
                          "outcomes": [
                            {
                              "participant": "Anthony Edwards",
                              "label": "Over",
                              "oddsAmerican": "-115",
                              "line": 25.5
                            },
                            { "participant": "...", "label": "Under", ... }
                          ]
                        }
                      ]
                    }
                  ]
                }
              ]
            }
          ]
        }
      }
    """
    results: list[dict] = []
    seen_matchups: set[str] = set()

    def coerce_odds(val) -> int | None:
        if val is None:
            return None
        return parse_american_odds(str(val))

    for url, body in captured:
        # Unwrap top-level envelope
        if not isinstance(body, dict):
            continue
        eg = body.get("eventGroup") or {}
        events = eg.get("events") or body.get("events") or []

        for event in events:
            if not isinstance(event, dict):
                continue

            # Build matchup label
            name = (event.get("name") or "").strip()
            if not name:
                t1 = event.get("teamName1") or (event.get("homeTeam") or {}).get("name", "")
                t2 = event.get("teamName2") or (event.get("awayTeam") or {}).get("name", "")
                name = f"{t2} @ {t1}" if t1 and t2 else ""
            if not name or name in seen_matchups:
                continue

            markets: dict[str, list] = {}

            for cat in (event.get("eventCategories") or []):
                cat_name = (cat.get("name") or cat.get("nameIdentifier") or "").lower()
                if "prop" not in cat_name:
                    continue

                for comp in (cat.get("componentizedOffers") or []):
                    market_name = (
                        comp.get("subcategoryName")
                        or comp.get("name")
                        or "Props"
                    )
                    outcomes_list: list[dict] = []

                    for offer in (comp.get("offers") or []):
                        raw_outcomes = offer.get("outcomes") or []

                        # Group by participant so each player becomes one row
                        by_player: dict[str, dict] = {}
                        for o in raw_outcomes:
                            player = (
                                o.get("participant")
                                or o.get("label", "").split(" Over ")[0].split(" Under ")[0]
                            ).strip()
                            if not player:
                                continue
                            label = (o.get("label") or o.get("type") or "").lower()
                            odds = coerce_odds(o.get("oddsAmerican") or o.get("odds"))
                            line = o.get("line")

                            entry = by_player.setdefault(player, {"player": player})
                            if "over" in label:
                                entry["over_odds"] = odds
                                if line is not None:
                                    try:
                                        entry["line"] = float(line)
                                    except (ValueError, TypeError):
                                        pass
                            elif "under" in label:
                                entry["under_odds"] = odds
                            else:
                                entry["odds"] = odds

                        for entry in by_player.values():
                            if entry.get("over_odds") is not None or entry.get("odds") is not None:
                                outcomes_list.append(entry)

                    if outcomes_list:
                        markets[market_name] = outcomes_list

            if markets:
                seen_matchups.add(name)
                results.append({"matchup": name, "markets": markets})

    return results


async def scrape_player_props(page) -> list[dict]:
    """Intercept DraftKings' internal API calls to extract player props.

    This avoids navigating to individual event pages (which DK blocks for
    headless browsers) by capturing the JSON the league page fetches itself.
    """
    captured = await _intercept_props_api(page)

    print(f"  [props] intercepted {len(captured)} API response(s)")
    for url, body in captured:
        eg = body.get("eventGroup", {}) if isinstance(body, dict) else {}
        n_events = len(eg.get("events") or body.get("events") or [])
        print(f"    {url}")
        print(f"      top-level keys: {list(body.keys()) if isinstance(body, dict) else 'list'}"
              f"  events: {n_events}")

    results = _parse_dk_api_props(captured)

    if not results and captured:
        # Dump the first (largest) response body for selector diagnosis
        url, body = captured[0]
        print(f"  [props] parse found 0 results — dumping first response ({url}):")
        print(f"    {json.dumps(body)[:800]}")

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
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        # Mask navigator.webdriver so DK's bot checks don't see a headless flag
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
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
