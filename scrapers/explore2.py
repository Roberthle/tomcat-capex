import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://floridaucc.com/search")
        with open("search.html", "w") as f:
            f.write(await page.content())
        print("Disclaimer buttons?")
        buttons = await page.evaluate("Array.from(document.querySelectorAll('button, a')).map(el => el.textContent.trim())")
        print(buttons)
        await browser.close()

asyncio.run(main())
