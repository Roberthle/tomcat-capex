import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://www.njportal.com/UCC/SearchBulk/Search.aspx")
        await page.wait_for_timeout(2000)
        content = await page.content()
        with open("nj_ucc_bulk.html", "w") as f:
            f.write(content)
        await browser.close()

asyncio.run(main())
