import asyncio
from playwright.async_api import async_playwright

NBA_URL = "https://sportsbook.draftkings.com/leagues/basketball/nba"

async def main():
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
        print("Navigating...")
        await page.goto(NBA_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(4)

        # Dump the page title and a slice of the HTML to understand structure
        title = await page.title()
        print(f"Title: {title}")

        # Check what table-like elements exist
        for sel in [
            ".sportsbook-table",
            ".sportsbook-table-row",
            ".sportsbook-outcome-cell",
            "[class*='table']",
            "[class*='event']",
            "[class*='game']",
            "[class*='odds']",
            "[class*='market']",
        ]:
            els = await page.query_selector_all(sel)
            print(f"  {sel!r:45s}  -> {len(els)} elements")

        # Save full HTML for inspection
        html = await page.content()
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nFull HTML saved to debug_page.html ({len(html):,} bytes)")
        await browser.close()

asyncio.run(main())
