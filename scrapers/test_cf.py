import asyncio
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        page = await browser.new_page()
        await stealth_async(page)
        await page.goto("https://ucc.michigan.gov/")
        await page.wait_for_timeout(10000)
        title = await page.title()
        print(f"Title: {title}")
        content = await page.content()
        with open("out_cf.html", "w") as f:
            f.write(content)
        await browser.close()

asyncio.run(main())
