import asyncio
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
        
        print("Navigating to URL...")
        await page.goto("https://www.sosnc.gov/online_services/search/by_title/_UCC")
        await page.wait_for_timeout(10000)
        content = await page.content()
        with open("nc_ucc_stealth.html", "w") as f:
            f.write(content)
        print("Stealth page saved.")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
