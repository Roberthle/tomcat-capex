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
        
        # fill the search box
        await page.locator('input[name="keyword"]').fill("CONSTRUCTION")
        
        # find the search button - it might be an icon or text.
        # Let's hit Enter
        await page.locator('input[name="keyword"]').press("Enter")
        
        await page.wait_for_timeout(3000)
        
        texts = await page.evaluate("Array.from(document.querySelectorAll('a, button, td, th')).map(e => e.innerText).filter(t => t)")
        print("Results texts:", texts[:50])
        
        await page.screenshot(path="search_results.png", full_page=True)
        await browser.close()

asyncio.run(main())
