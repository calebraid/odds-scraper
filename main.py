import asyncio
import os

import uvicorn

import scraper
import stats_scraper
import predictor
import tracker


async def _run_with_restart(name: str, coro_fn, restart_delay: int):
    while True:
        print(f"[{name}] starting")
        try:
            await coro_fn()
            print(f"[{name}] exited cleanly, restarting in {restart_delay}s")
        except Exception as exc:
            print(f"[{name}] crashed: {exc}, restarting in {restart_delay}s")
        await asyncio.sleep(restart_delay)


async def _run_api():
    port = int(os.environ.get("PORT", 8000))
    config = uvicorn.Config("api:app", host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    print(f"[api] starting on port {port}")
    await server.serve()


async def main():
    await asyncio.gather(
        _run_with_restart("scraper", scraper.main, 15),
        _run_with_restart("stats", stats_scraper.main, 30),
        _run_with_restart("predictor", predictor.main, 15),
        _run_with_restart("tracker", tracker.main, 60),
        _run_api(),
    )


if __name__ == "__main__":
    asyncio.run(main())
