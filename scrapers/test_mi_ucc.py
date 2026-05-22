import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://ucc.michigan.gov/")
        await page.wait_for_timeout(5000)
        content = await page.content()
        print(content)
        await browser.close()

asyncio.run(main())
