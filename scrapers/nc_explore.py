import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        print("Navigating to URL...")
        await page.goto("https://www.sosnc.gov/online_services/search/by_title/_UCC")
        await page.wait_for_timeout(2000)
        content = await page.content()
        with open("nc_ucc_initial.html", "w") as f:
            f.write(content)
        print("Initial page saved.")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
