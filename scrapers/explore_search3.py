import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://floridaucc.com/search")
        await page.locator('input[type="checkbox"]').check()
        await page.locator('button:has-text("Next")').click()
        await page.wait_for_load_state("networkidle")
        
        # open the Search Type dropdown
        await page.locator('div:has-text("Organization Debtor Name")').nth(0).click()
        await page.wait_for_timeout(1000)
        opts = await page.evaluate("Array.from(document.querySelectorAll('li')).map(e => e.innerText)")
        print("Search Type Options:", opts)
        
        await browser.close()

asyncio.run(main())
