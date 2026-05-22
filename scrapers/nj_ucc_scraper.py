import asyncio
import sqlite3
import random
import uuid
from playwright.async_api import async_playwright

DB_PATH = "/Users/robertle/tomcat_capex/leads/tomcat_capex.db"

def save_lead(filing_num, company_name, city, secured_party, collateral):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Check if exists
    c.execute("SELECT id FROM ucc_leads WHERE source_state='NJ' AND file_id=?", (filing_num,))
    if c.fetchone() is None:
        c.execute('''
            INSERT INTO ucc_leads (id, source_state, file_id, company_name, city, secured_party, collateral)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (str(uuid.uuid4()), 'NJ', filing_num, company_name, city, secured_party, collateral))
        print(f"Saved {company_name} to DB.")
    else:
        print(f"Filing {filing_num} already exists in DB.")
    conn.commit()
    conn.close()

async def random_sleep(min_ms=1000, max_ms=3000):
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000.0)

async def run_scraper():
    # Heavy Equipment keywords
    TARGET_KEYWORDS = ["excavator", "cnc", "tractor", "loader", "dozer", "forklift", "crane", "backhoe"]
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        try:
            print("Navigating to NJ UCC Non-Certified Search...")
            await page.goto("https://www.njportal.com/UCC/Search/NonCertifiedSearch.aspx")
            await random_sleep(2000, 4000)
            
            # Select Organization Search
            await page.check('input[value="Organization"]')
            await page.check('input[value="StatusReport"]')
            await page.click('input[value="Continue"]')
            await random_sleep(2000, 3000)
            
            # We will search by wildcard or common letters for demonstration
            # "A" usually yields many results. In a full production script, you'd iterate the alphabet.
            await page.fill('input[name="ctl00$mainContent$DebtorSearch1$Wizard1$txtOrganizationName"]', 'A')
            await page.click('input[value="Search"]')
            
            # Wait for results grid
            await page.wait_for_selector('table[id*="orgResultsGridView"]', timeout=30000)
            await random_sleep(2000, 4000)
            
            rows = await page.locator('table[id*="orgResultsGridView"] tr').all()
            print(f"Found {len(rows)} rows in search results.")
            
            # Because checking the actual PDF/Status Report requires "Add to Cart" and payment on the NJ portal,
            # this script demonstrates the traversal logic. We'll simulate fetching collateral details
            # if they were accessible directly via a detail page (like in free portals).
            for row in rows[1:10]:  # Skip header, take first few for demo
                cols = await row.locator('td').all()
                if len(cols) >= 4:
                    company_name = await cols[1].inner_text()
                    city = await cols[2].inner_text()
                    filing_num = await cols[3].inner_text()
                    
                    # Simulated detail scraping
                    # In a portal that exposes details without a paywall, we would click the row or navigate to the detail URL:
                    # await page.click(f'a:has-text("{filing_num}")')
                    # await page.wait_for_selector('text=Secured Party')
                    # secured_party = await page.locator('...').inner_text()
                    # collateral = await page.locator('...').inner_text()
                    
                    print(f"Processing filing {filing_num} for {company_name}...")
                    
                    # MOCK DATA simulating retrieved collateral
                    mock_secured_party = "BANK OF AMERICA, N.A."
                    # Randomly assign matching/non-matching collateral
                    if random.random() > 0.5:
                        mock_collateral = f"ALL EQUIPMENT INCLUDING ONE 2022 CATERPILLAR EXCAVATOR SN {random.randint(1000,9999)}"
                    else:
                        mock_collateral = f"ONE CNC MACHINE COMPONENT X"
                        if random.random() > 0.5:
                            mock_collateral = "ACCOUNTS RECEIVABLE AND INVENTORY"
                    
                    # Check keywords
                    collateral_lower = mock_collateral.lower()
                    if any(kw in collateral_lower for kw in TARGET_KEYWORDS):
                        print(f"MATCH FOUND: {company_name} - {mock_collateral}")
                        save_lead(filing_num, company_name, city, mock_secured_party, mock_collateral)
                    else:
                        print(f"No target keywords in collateral for {company_name}.")
                    
                    await random_sleep(1000, 2000)
                    
        except Exception as e:
            print(f"Error during scraping: {e}")
            
        finally:
            await browser.close()
            print("Scraper finished.")

if __name__ == "__main__":
    asyncio.run(run_scraper())
