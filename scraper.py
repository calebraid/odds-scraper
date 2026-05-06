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
    """Navigate to the player-props page, then navigate directly to each
    subcategory URL found in the DOM to collect API responses for all prop
    types (Points, Rebounds, Assists, Threes, …).

    DK fires one API call per subcategory tab (keyed by subcategoryId).
    The initial page load only fires the default tab.  We find the other
    subcategory URLs from <a href> elements in the DOM after the first load,
    then navigate to each — avoiding stale DOM handle crashes that occur
    when clicking tab elements that get destroyed by navigation.
    """
    captured: list[tuple[str, any]] = []
    seen_urls: set[str] = set()

    async def on_response(response):
        if response.status != 200:
            return
        url = response.url
        if url in seen_urls:
            return
        if not any(d in url for d in _DK_API_DOMAINS):
            return
        if "json" not in response.headers.get("content-type", ""):
            return
        try:
            body = await response.json()
        except Exception:
            return
        if len(json.dumps(body)) < 500:
            return
        seen_urls.add(url)
        captured.append((url, body))

    page.on("response", on_response)

    # ── Initial load — captures the default subcategory (Points) ──────────
    try:
        await page.goto(NBA_PLAYER_PROPS, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector(".cms-market-selector-static__event-wrapper", timeout=15_000)
    except PlaywrightTimeoutError:
        pass
    await asyncio.sleep(3)

    if captured:
        print(f"  [props] initial API URL: {captured[0][0]}")

    # ── Find all subcategory URLs via DOM evaluation ───────────────────────
    # DK renders subcategory tabs as <a> elements whose href contains
    # subcategoryId.  We read URLs as strings here so there are no stale
    # DOM handles to worry about when we navigate later.
    found: list[dict] = await page.evaluate("""
        () => {
            const seen = new Set();
            const results = [];
            document.querySelectorAll('a').forEach(el => {
                const href = el.href || '';
                if (href.includes('subcategoryId') && !seen.has(href)) {
                    seen.add(href);
                    results.push({text: (el.textContent || '').trim(), href});
                }
            });
            // Fallback: elements with a data-subcategory-id attribute
            document.querySelectorAll('[data-subcategory-id]').forEach(el => {
                const id = el.dataset.subcategoryId;
                if (id && !seen.has(id)) {
                    seen.add(id);
                    results.push({text: (el.textContent || '').trim(), subcategoryId: id});
                }
            });
            return results;
        }
    """)

    print(f"  [props] subcategory links found in DOM: {len(found)}")
    for item in found[:10]:
        print(f"    {item!r}")

    # ── Navigate to each subcategory URL ──────────────────────────────────
    current_url = page.url
    nav_count = 0
    for item in found:
        href = item.get("href", "")
        sub_id = item.get("subcategoryId", "")
        label = (item.get("text") or "?")[:40]

        target = href or f"{NBA_PLAYER_PROPS}&subcategoryId={sub_id}"
        if not target or target == current_url:
            continue

        print(f"  [props] loading subcategory {label!r}")
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(2)
            current_url = page.url
            nav_count += 1
        except Exception as exc:
            print(f"  [props] failed {label!r}: {exc}")

    print(f"  [props] navigated to {nav_count} subcategory page(s)")
    page.remove_listener("response", on_response)

    captured.sort(key=lambda t: len(json.dumps(t[1])), reverse=True)
    return captured


def _parse_dk_api_props(captured: list[tuple[str, any]]) -> list[dict]:
    """Parse player props from DraftKings' normalized flat API response.

    DK may split markets across several API calls (one per prop category),
    so we merge all captured responses by ID before parsing — otherwise
    the second response for the same event would be skipped by a seen-set.

    Field paths (confirmed from live interception):
      events[]     : {id, name}
      markets[]    : {id, eventId, marketType: {name}}
      selections[] : {id, marketId, label, displayOdds: {american},
                      participants: [{name, type}], milestoneValue}

    Two selection styles:
      O/U       — label is "Over" / "Under"; merge into one entry per line
      Milestone — label is "5+", "10+", etc.; milestoneValue is the line
    """
    # ── Merge all captured responses, deduplicating by ID ─────────────────
    all_events:     dict[str, dict] = {}
    all_markets:    dict[str, dict] = {}
    all_selections: dict[str, dict] = {}

    for _url, body in captured:
        if not isinstance(body, dict):
            continue
        for ev in (body.get("events") or []):
            if isinstance(ev, dict) and ev.get("id"):
                all_events[str(ev["id"])] = ev
        for m in (body.get("markets") or []):
            if isinstance(m, dict) and m.get("id"):
                all_markets[str(m["id"])] = m
        for sel in (body.get("selections") or []):
            if isinstance(sel, dict) and sel.get("id"):
                all_selections[str(sel["id"])] = sel

    if not all_events or not all_markets:
        return []

    print(
        f"  [props] merged pool: {len(all_events)} events, "
        f"{len(all_markets)} markets, {len(all_selections)} selections"
    )

    # Debug: unique marketType names and sample market->prop_type mapping
    seen_mt: list[str] = []
    for m in all_markets.values():
        mt = (m.get("marketType") or {}).get("name") or ""
        if mt and mt not in seen_mt:
            seen_mt.append(mt)
    print(f"  [props] marketType names ({len(seen_mt)} unique): {seen_mt[:10]}")

    def coerce_odds(val) -> int | None:
        return None if val is None else parse_american_odds(str(val))

    def player_from_participants(participants) -> str:
        for p in (participants or []):
            if isinstance(p, dict) and p.get("type") == "Player":
                return (p.get("name") or "").strip()
        return ""

    def simplify_prop_type(raw: str) -> str:
        for suffix in (" Milestones", " O/U", " Over/Under", " Odds", " Props"):
            raw = raw.replace(suffix, "")
        return raw.strip()

    # ── Index selections by marketId ──────────────────────────────────────
    sels_by_market: dict[str, list[dict]] = {}
    for sel in all_selections.values():
        mid = str(sel.get("marketId") or "")
        if mid:
            sels_by_market.setdefault(mid, []).append(sel)

    # ── Index markets by eventId ──────────────────────────────────────────
    markets_by_event: dict[str, list[dict]] = {}
    for m in all_markets.values():
        eid = str(m.get("eventId") or "")
        if eid:
            markets_by_event.setdefault(eid, []).append(m)

    # ── Process each event ────────────────────────────────────────────────
    results: list[dict] = []
    _debug_market_count = 0

    for eid, event in all_events.items():
        name = (event.get("name") or "").strip()
        if not name:
            continue

        prop_groups: dict[str, list[dict]] = {}

        for market in markets_by_event.get(eid, []):
            mid       = str(market.get("id") or "")
            raw_mt    = (market.get("marketType") or {}).get("name") or ""
            prop_type = simplify_prop_type(raw_mt)
            if _debug_market_count < 3:
                print(
                    f"  [props] market sample: name={market.get('name')!r} "
                    f"marketType={raw_mt!r} -> prop_type={prop_type!r}"
                )
                _debug_market_count += 1
            if not prop_type:
                continue

            selections = sels_by_market.get(mid, [])
            if not selections:
                continue

            # Player name from selections' participants (most reliable)
            player = ""
            for sel in selections:
                player = player_from_participants(sel.get("participants"))
                if player:
                    break
            # Fallback: strip marketType name from market name
            if not player:
                player = market.get("name") or ""
                player = player.replace(
                    (market.get("marketType") or {}).get("name") or "", ""
                ).strip()
            if not player:
                continue

            labels = [(s.get("label") or "").strip() for s in selections]
            is_ou  = any(l.lower() in ("over", "under") for l in labels)

            if is_ou:
                by_line: dict[str, dict] = {}
                for sel in selections:
                    label    = (sel.get("label") or "").strip().lower()
                    odds     = coerce_odds((sel.get("displayOdds") or {}).get("american"))
                    raw_line = sel.get("milestoneValue") or sel.get("points")
                    line_key = str(raw_line) if raw_line is not None else "?"

                    entry = by_line.setdefault(line_key, {"player": player})
                    if raw_line is not None:
                        try:
                            entry["line"] = float(raw_line)
                        except (ValueError, TypeError):
                            pass
                    if label == "over":
                        entry["over_odds"] = odds
                    elif label == "under":
                        entry["under_odds"] = odds

                for entry in by_line.values():
                    if entry.get("over_odds") is not None or entry.get("under_odds") is not None:
                        prop_groups.setdefault(prop_type, []).append(entry)

            else:
                for sel in selections:
                    label    = (sel.get("label") or "").strip()
                    odds     = coerce_odds((sel.get("displayOdds") or {}).get("american"))
                    raw_line = sel.get("milestoneValue") or sel.get("points")

                    entry: dict = {"player": player, "label": label}
                    if raw_line is not None:
                        try:
                            entry["line"] = float(raw_line)
                        except (ValueError, TypeError):
                            pass
                    if label.endswith("+"):
                        entry["over_odds"] = odds
                    else:
                        entry["odds"] = odds

                    if entry.get("over_odds") is not None or entry.get("odds") is not None:
                        prop_groups.setdefault(prop_type, []).append(entry)

        if prop_groups:
            results.append({"matchup": name, "markets": prop_groups})

    return results


async def scrape_player_props(page) -> list[dict]:
    """Intercept DraftKings' internal API calls to extract player props.

    Avoids individual event pages (blocked for headless browsers) by
    capturing the JSON responses the props subcategory page fetches itself.
    """
    captured = await _intercept_props_api(page)
    print(f"  [props] intercepted {len(captured)} API response(s)")
    return _parse_dk_api_props(captured)


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
