import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto("https://www.njportal.com/UCC")
        content = await page.content()
        with open("nj_ucc.html", "w") as f:
            f.write(content)
        await page.screenshot(path="nj_ucc.png")
        await browser.close()

asyncio.run(main())
