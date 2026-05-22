import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        print("Navigating to search...")
        await page.goto("https://floridaucc.com/search")
        
        # wait for checkbox
        print("Clicking checkbox...")
        await page.locator('input[type="checkbox"]').check()
        
        # click next
        print("Clicking next...")
        await page.locator('button:has-text("Next")').click()
        
        print("Waiting for network idle...")
        await page.wait_for_load_state("networkidle")
        
        with open("search_main.html", "w") as f:
            f.write(await page.content())
            
        print("Search options?")
        inputs = await page.evaluate("""Array.from(document.querySelectorAll('input, select, button')).map(el => {
            return { tag: el.tagName, type: el.type, id: el.id, name: el.name, placeholder: el.placeholder, text: el.textContent };
        })""")
        for el in inputs:
            print(el)
        await browser.close()

asyncio.run(main())
