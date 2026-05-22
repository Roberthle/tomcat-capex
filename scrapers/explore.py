import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        print("Navigating...")
        await page.goto("https://www.floridaucc.com/")
        print("Saving screenshot and HTML")
        await page.screenshot(path="screenshot_home.png")
        with open("home.html", "w") as f:
            f.write(await page.content())
            
        print("Looking for search...")
        try:
            # Let's see if there is an explicit disclaimer link or search link
            links = await page.evaluate("Array.from(document.querySelectorAll('a')).map(a => a.href)")
            print("Links:", links)
        except Exception as e:
            print("Error:", e)
        await browser.close()

asyncio.run(main())
