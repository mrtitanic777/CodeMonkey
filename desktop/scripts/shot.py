"""Sharp screenshots of the running web UI, for design review.

    python desktop/scripts/shot.py OUT.png [--js "code to run first"] [--width 1600 --height 1000]
                                   [--scale 2] [--url http://127.0.0.1:8080/] [--theme dark|light]

Opens the page in the installed Chrome at the given window size and pixel density (2 gives a
3200x2000 image for a 1600x1000 window), runs the optional JavaScript (it may await), waits for
animations to settle, and saves a PNG.
"""

from __future__ import annotations

import argparse
import asyncio


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--url", default="http://127.0.0.1:8080/")
    ap.add_argument("--js", default="")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--scale", type=float, default=2)
    ap.add_argument("--theme", choices=("dark", "light"), default="dark")
    args = ap.parse_args()

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome")
        page = await browser.new_page(viewport={"width": args.width, "height": args.height},
                                      device_scale_factor=args.scale, color_scheme=args.theme)
        await page.goto(args.url, wait_until="load")   # never "networkidle": the event stream stays open
        await page.wait_for_timeout(1200)
        if args.js:
            await page.evaluate("async () => {" + args.js + "}")
        await page.wait_for_timeout(1500)   # menus and panes settle in
        await page.screenshot(path=args.out)
        await browser.close()
    print(args.out)


if __name__ == "__main__":
    asyncio.run(main())
