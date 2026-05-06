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
    """Parse player props from DraftKings' normalized flat API response.

    Actual shape (confirmed from live interception):
      {
        "sports": [...],
        "leagues": [...],
        "events":     [{ "id": "34074632", "name": "LA Lakers @ OKC Thunder", ... }],
        "markets":    [{ "id": "...", "eventId": "34074632", "name": "...", ... }],
        "selections": [{ "id": "...", "marketId": "...", "points": 25.5, ... }],
        "subscriptionPartials": [...]
      }

    Join path: events[id] <- markets[eventId] <- selections[marketId]

    Player name is embedded in markets[].name (common formats:
      "Anthony Edwards - Points"  or  "Anthony Edwards Points O/U").
    Prop type (e.g. "Points") comes from markets[].groupName when present,
    otherwise inferred by stripping the player name from markets[].name.
    """
    results: list[dict] = []
    seen: set[str] = set()

    def _first(obj: dict, *keys):
        for k in keys:
            v = obj.get(k)
            if v is not None:
                return v
        return None

    def coerce_odds(val) -> int | None:
        return None if val is None else parse_american_odds(str(val))

    for url, body in captured:
        if not isinstance(body, dict):
            continue

        raw_events     = body.get("events") or []
        raw_markets    = body.get("markets") or []
        raw_selections = body.get("selections") or []

        if not raw_events or not raw_markets:
            continue

        # ── Index selections by marketId ──────────────────────────────────
        sels_by_market: dict[str, list[dict]] = {}
        for sel in raw_selections:
            if not isinstance(sel, dict):
                continue
            mid = str(_first(sel, "marketId", "market_id") or "")
            if mid:
                sels_by_market.setdefault(mid, []).append(sel)

        # ── Index markets by eventId ──────────────────────────────────────
        markets_by_event: dict[str, list[dict]] = {}
        for m in raw_markets:
            if not isinstance(m, dict):
                continue
            eid = str(_first(m, "eventId", "event_id") or "")
            if eid:
                markets_by_event.setdefault(eid, []).append(m)

        # ── Process each event ────────────────────────────────────────────
        for event in raw_events:
            if not isinstance(event, dict):
                continue
            eid  = str(_first(event, "id", "eventId") or "")
            name = (_first(event, "name", "eventName") or "").strip()
            if not name or name in seen:
                continue

            event_markets = markets_by_event.get(eid, [])
            if not event_markets:
                continue

            # prop_type -> player_name -> entry
            prop_groups: dict[str, dict[str, dict]] = {}

            for market in event_markets:
                mid         = str(_first(market, "id", "marketId") or "")
                market_name = (_first(market, "name", "marketName") or "").strip()
                # groupName / subcategoryName gives the prop type directly
                group_name  = (
                    _first(market, "groupName", "subcategoryName",
                           "betOfferTypeName", "typeName") or ""
                ).strip()

                # ── Extract player name and prop type from market name ────
                # Formats seen: "Anthony Edwards - Points"
                #               "Anthony Edwards Points O/U"
                player_name = (_first(market, "participant", "playerName") or "").strip()
                prop_type   = group_name or ""

                if not player_name and " - " in market_name:
                    left, right = [p.strip() for p in market_name.split(" - ", 1)]
                    # Shorter right side → it's the prop type; left is the player
                    if len(right.split()) <= 4:
                        player_name = left
                        if not prop_type:
                            prop_type = right.replace(" O/U", "").replace(" Over/Under", "")
                    else:
                        player_name = right
                        if not prop_type:
                            prop_type = left.replace(" O/U", "").replace(" Over/Under", "")
                elif not player_name and group_name and market_name:
                    # market name IS the player name when group_name is set separately
                    player_name = market_name.replace(" O/U", "").replace(" Over/Under", "").strip()

                if not player_name or not prop_type:
                    continue

                # ── Join selections ───────────────────────────────────────
                selections = sels_by_market.get(mid, [])
                if not selections:
                    continue

                entry: dict = {"player": player_name}
                for sel in selections:
                    label = (_first(sel, "name", "label", "type") or "").lower()
                    odds  = coerce_odds(_first(sel, "oddsAmerican", "americanOdds", "odds", "price"))
                    line  = _first(sel, "points", "line", "handicap", "spread")

                    if "over" in label:
                        entry["over_odds"] = odds
                        if line is not None:
                            try:
                                entry["line"] = float(line)
                            except (ValueError, TypeError):
                                pass
                    elif "under" in label:
                        entry["under_odds"] = odds
                    elif "odds" not in entry:
                        entry["odds"] = odds

                if entry.get("over_odds") is not None or entry.get("odds") is not None:
                    prop_groups.setdefault(prop_type, {})[player_name] = entry

            if prop_groups:
                seen.add(name)
                results.append({
                    "matchup": name,
                    "markets": {k: list(v.values()) for k, v in prop_groups.items()},
                })

    return results


async def scrape_player_props(page) -> list[dict]:
    """Intercept DraftKings' internal API calls to extract player props.

    Avoids individual event pages (blocked for headless browsers) by
    capturing the JSON responses the props subcategory page fetches itself.
    """
    captured = await _intercept_props_api(page)

    print(f"  [props] intercepted {len(captured)} API response(s)")
    for url, body in captured:
        if not isinstance(body, dict):
            continue
        n_events  = len(body.get("events") or [])
        n_markets = len(body.get("markets") or [])
        n_sels    = len(body.get("selections") or [])
        print(f"    {url}")
        print(f"      keys={list(body.keys())}  "
              f"events={n_events}  markets={n_markets}  selections={n_sels}")
        # Log first market and selection to confirm field names
        if n_markets:
            print(f"      market[0]={json.dumps(body['markets'][0])[:200]}")
        if n_sels:
            print(f"      selection[0]={json.dumps(body['selections'][0])[:200]}")

    results = _parse_dk_api_props(captured)

    if not results and captured:
        url, body = captured[0]
        print(f"  [props] 0 results — first response body[:800]: {json.dumps(body)[:800]}")

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
