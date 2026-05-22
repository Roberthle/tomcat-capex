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
        
        await page.screenshot(path="search_page.png", full_page=True)
        
        # print all visible text that looks like a label or option
        texts = await page.evaluate("Array.from(document.querySelectorAll('label, .MuiSelect-select, button')).map(e => e.innerText).filter(t => t)")
        print("Labels/Selects:", texts)
        
        await browser.close()

asyncio.run(main())
