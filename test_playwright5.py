import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://www.njportal.com/UCC/SearchBulk/Search.aspx")
        
        # Fill dates (Assuming format MM/DD/YYYY)
        await page.fill('input[name="ctl00$mainContent$txtStartDate"]', '05/20/2026')
        await page.fill('input[name="ctl00$mainContent$txtEndDate"]', '05/22/2026')
        
        # Click search
        await page.click('input[value="Search"]')
        
        await page.wait_for_timeout(3000)
        content = await page.content()
        with open("nj_ucc_bulk_results.html", "w") as f:
            f.write(content)
        await page.screenshot(path="nj_ucc_bulk_results.png")
        await browser.close()

asyncio.run(main())
