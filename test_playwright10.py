import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://www.njportal.com/UCC/Search/NonCertifiedSearch.aspx")
        
        await page.check('input[value="Organization"]')
        await page.check('input[value="StatusReport"]')
        await page.click('input[value="Continue"]')
        
        await page.wait_for_timeout(2000)
        
        # Now fill in "A" for Organization Name
        await page.fill('input[name="ctl00$mainContent$DebtorSearch1$Wizard1$txtOrganizationName"]', 'A')
        await page.click('input[value="Search"]')
        
        await page.wait_for_timeout(4000)
        
        content = await page.content()
        with open("nj_ucc_org_results.html", "w") as f:
            f.write(content)
        await browser.close()

asyncio.run(main())
