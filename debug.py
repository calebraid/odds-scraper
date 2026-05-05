"""Capture HTML snapshots of both DraftKings NBA sub-pages for selector development."""
import asyncio
import re
from playwright.async_api import async_playwright

NBA_GAME_LINES = "https://sportsbook.draftkings.com/leagues/basketball/nba?category=games&subcategory=game-lines"
NBA_PLAYER_PROPS = "https://sportsbook.draftkings.com/leagues/basketball/nba?category=games&subcategory=player-props"

BROWSER_CONTEXT = dict(
    user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    viewport={"width": 1280, "height": 900},
)


async def probe(page, url: str, out_file: str, wait_sels: list[str]) -> None:
    print(f"\n── {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

    # Try each selector until one resolves
    for sel in wait_sels:
        try:
            await page.wait_for_selector(sel, timeout=10_000)
            print(f"  ready ({sel})")
            break
        except Exception:
            continue

    # Scroll to load deferred content
    await asyncio.sleep(3)
    for _ in range(10):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)

    html = await page.content()
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  saved {out_file} ({len(html):,} bytes)")

    # Quick stats
    for sel_label, pattern in [
        ("game containers (static)", r"cms-market-selector-static__event-wrapper"),
        ("event accordions",         r"sportsbook-event-accordion__wrapper"),
        ("sportsbook table rows",    r"sportsbook-table__row"),
        ("offer category panels",    r"sportsbook-offer-category-panel"),
        ("tab-switcher tabs",        r"tab-switcher-tab(?!-indicator)"),
        ("lp-nav-link items",        r"lp-nav-link"),
        ("market-template items",    r'data-testid="market-template"'),
    ]:
        count = len(re.findall(pattern, html))
        if count:
            print(f"  {sel_label}: {count}")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(**BROWSER_CONTEXT)
        page = await ctx.new_page()

        await probe(
            page,
            NBA_GAME_LINES,
            "debug_game_lines.html",
            [".cms-market-selector-static__event-wrapper", "[data-testid='marketboard']"],
        )

        await probe(
            page,
            NBA_PLAYER_PROPS,
            "debug_player_props.html",
            [
                ".sportsbook-event-accordion__wrapper",
                ".sportsbook-table",
                "[data-testid='marketboard']",
                ".cms-market-selector-content",
            ],
        )

        await browser.close()
        print("\nDone. Inspect debug_game_lines.html and debug_player_props.html to tune selectors.")


asyncio.run(main())
