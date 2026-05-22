"""
NC SOS UCC Session Setup
Run this script once, manually solve the Cloudflare challenge in the browser,
then navigate to the UCC search page. Cookies are saved for the scraper.
"""
import asyncio, json, pathlib
from playwright.async_api import async_playwright

COOKIE_FILE = pathlib.Path(__file__).parent / "nc_session_cookies.json"
NC_UCC_URL  = "https://www.sosnc.gov/online_services/search/by_title/_UCC"

async def main():
    print("=" * 60)
    print("NC SOS SESSION SETUP")
    print("=" * 60)
    print()
    print("A browser window will open. You may need to:")
    print("  1. Solve a Cloudflare challenge (click 'Verify you are human')")
    print("  2. The UCC search page will load automatically")
    print()
    print("Once the search page loads, come back here and press ENTER.")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--start-maximized", "--no-sandbox",
                  "--disable-blink-features=AutomationControlled"]
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            no_viewport=False
        )
        page = await ctx.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        print(f"Opening: {NC_UCC_URL}")
        await page.goto(NC_UCC_URL, wait_until="domcontentloaded", timeout=60000)

        # Wait for user to confirm the page loaded
        input("\n>>> Press ENTER once the NC SOS UCC search page is fully loaded...\n")

        # Save cookies
        cookies = await ctx.cookies()
        COOKIE_FILE.write_text(json.dumps(cookies, indent=2))
        print(f"✅ Saved {len(cookies)} cookies to {COOKIE_FILE}")

        # Also save local storage if any
        storage = await page.evaluate("() => Object.entries(localStorage)")
        print(f"   Local storage entries: {len(storage)}")

        # Print the current URL to confirm we're on the right page
        print(f"   Final URL: {page.url}")
        print(f"   Page title: {await page.title()}")

        await browser.close()
        print("\n✅ Session saved. Run nc_ucc_scraper.py to start scraping.")

if __name__ == "__main__":
    asyncio.run(main())
