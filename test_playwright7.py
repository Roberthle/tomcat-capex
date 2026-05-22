import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://www.njportal.com/UCC/SearchBulk/Search.aspx")
        
        await page.fill('input[name="ctl00$mainContent$startDate$txtFilingDate1"]', '05/20/2026')
        await page.keyboard.press('Escape')
        await page.fill('input[name="ctl00$mainContent$endDate$txtFilingDate1"]', '05/22/2026')
        await page.keyboard.press('Escape')
        
        await page.click('input[value="Search"]', force=True)
        
        await page.wait_for_timeout(3000)
        content = await page.content()
        with open("nj_ucc_bulk_results.html", "w") as f:
            f.write(content)
        await page.screenshot(path="nj_ucc_bulk_results.png")
        await browser.close()

asyncio.run(main())
