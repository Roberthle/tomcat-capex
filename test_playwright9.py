import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://www.njportal.com/UCC/Search/NonCertifiedSearch.aspx")
        
        # Select FilingNumber
        await page.check('input[value="FilingNumber"]')
        await page.check('input[value="StatusReport"]')
        await page.click('input[value="Continue"]')
        
        await page.wait_for_timeout(2000)
        
        content = await page.content()
        with open("nj_ucc_step3.html", "w") as f:
            f.write(content)
        await browser.close()

asyncio.run(main())
