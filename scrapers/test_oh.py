import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        print("Navigating to https://businesssearch.ohiosos.gov/")
        await page.goto("https://businesssearch.ohiosos.gov/")
        await page.screenshot(path="/Users/robertle/tomcat_capex/scrapers/screenshot.png")
        print("Navigating to https://bizsearch.sos.state.oh.us/")
        try:
            await page.goto("https://bizsearch.sos.state.oh.us/")
            await page.screenshot(path="/Users/robertle/tomcat_capex/scrapers/screenshot_bizsearch.png")
        except Exception as e:
            print("Failed bizsearch:", e)
        await browser.close()

asyncio.run(main())
