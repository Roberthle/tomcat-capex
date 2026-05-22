import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://www.njportal.com/UCC/Search/NonCertifiedSearch.aspx")
        
        await page.check('input[value="FilingNumber"]')
        await page.check('input[value="StatusReport"]')
        await page.click('input[value="Continue"]')
        
        await page.wait_for_timeout(2000)
        
        await page.fill('input[name="ctl00$mainContent$DebtorSearch1$Wizard1$txtFilingNum"]', '1804194')
        await page.click('input[value="Search"]')
        
        await page.wait_for_timeout(4000)
        
        content = await page.content()
        with open("nj_ucc_filing_results.html", "w") as f:
            f.write(content)
        await browser.close()

asyncio.run(main())
