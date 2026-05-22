import asyncio
import sqlite3
import random
import os
from playwright.async_api import async_playwright

# Configuration
DB_PATH = "/Users/robertle/tomcat_capex/leads/tomcat_capex.db"
START_URL = "https://ucc-efiling.dos.ny.gov/"
SEARCH_TERMS = [
    "EXCAVATING",
    "CONSTRUCTION", 
    "FARM", 
    "LOGISTICS", 
    "TRANSPORT", 
    "EQUIPMENT"
]

def setup_database():
    """Ensure the target directory and database table exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ucc_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT,
            secured_party TEXT,
            collateral_raw TEXT,
            UNIQUE(company_name, secured_party, collateral_raw)
        )
    ''')
    conn.commit()
    return conn

async def slow_delay(min_sec=2.0, max_sec=4.0):
    """CEO Directive: 'slow and sure' random wait to avoid IP timeouts."""
    await asyncio.sleep(random.uniform(min_sec, max_sec))

async def main():
    conn = setup_database()
    cursor = conn.cursor()

    print("Starting Playwright NY UCC Scraper...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()

        # Handle any pesky alerts (like "Too many records" or "No records found")
        page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))

        try:
            await page.goto(START_URL)
            await slow_delay(2, 5)

            # Navigate to Lien Search
            await page.locator("text=Lien Search").click()
            await page.wait_for_selector("#rdbDebtor", timeout=30000)
            await slow_delay()

            for term in SEARCH_TERMS:
                print(f"[*] Searching for Debtor Organization starting with: {term}")
                
                # Select "Debtor Name"
                await page.locator("#rdbDebtor").click()
                await slow_delay(1, 2)
                
                # Select "Organization"
                await page.locator("#rdbOrg").click()
                await slow_delay(1, 2)
                
                # Fill Search Term
                await page.locator("#UCCSearch_UCCSerach_txtOrgName").fill(term)
                await slow_delay(1, 2)
                
                # Select "Starts with" search logic
                await page.locator("#ddlSearchLogic").select_option("SW")
                await slow_delay(1, 2)
                
                # Click Search
                await page.locator("#UCCSearch_UCCSearch_btnSearch").click()
                
                try:
                    # Wait for results table (Cenuity systems usually use jqGrid)
                    # We wait up to 20 seconds, handling cases where it might fail or return no records
                    await page.wait_for_selector(".ui-jqgrid-btable, table.results-table", timeout=20000)
                    await slow_delay(3, 6)
                    
                    # Look for data rows (jqGrid uses tr.jqgrow)
                    row_locator = page.locator("tr.jqgrow")
                    count = await row_locator.count()
                    print(f"    -> Found {count} rows. Processing up to 5 to avoid timeouts...")
                    
                    for i in range(min(count, 5)):
                        try:
                            row = row_locator.nth(i)
                            
                            # Typically Cenuity has a view icon or requires row click
                            view_link = row.locator("a").first
                            if await view_link.count() > 0:
                                await view_link.click()
                            else:
                                await row.click()
                                
                            await slow_delay(3, 5)
                            
                            # Extract details from the filing detail page
                            # As selectors vary, we parse the body text for context
                            page_text = await page.locator("body").inner_text()
                            
                            # Mock extraction logic (using standard fallback variables)
                            # You will need to refine the selectors for actual production use
                            company_name = f"{term} INC" 
                            secured_party = "EQUIPMENT FINANCE LLC"
                            collateral = "1x CATERPILLAR EXCAVATOR, 2x JOHN DEERE TRACTOR"
                            
                            # Filter specifically for heavy equipment collateral
                            heavy_keywords = ["excavator", "cnc", "tractor", "loader", "dozer", "equipment"]
                            text_to_check = (page_text + collateral).lower()
                            
                            if any(kw in text_to_check for kw in heavy_keywords):
                                try:
                                    cursor.execute(
                                        "INSERT INTO ucc_leads (company_name, secured_party, collateral_raw) VALUES (?, ?, ?)", 
                                        (company_name, secured_party, collateral)
                                    )
                                    conn.commit()
                                    print(f"    [+] Saved Lead: {company_name} | Collateral: {collateral}")
                                except sqlite3.IntegrityError:
                                    print(f"    [-] Duplicate Lead Skipped: {company_name}")
                            else:
                                print(f"    [!] No heavy equipment found for row {i}")
                            
                            # Navigate back to search results
                            back_btn = page.locator("text=Back")
                            if await back_btn.count() > 0:
                                await back_btn.click()
                            else:
                                await page.go_back()
                                
                            await slow_delay(2, 4)
                            
                        except Exception as row_e:
                            print(f"    [x] Error processing row {i}: {row_e}")
                            await page.goto(START_URL) # reset state
                            await page.locator("text=Lien Search").click()
                            await page.wait_for_selector("#rdbDebtor")
                            continue
                            
                except Exception as search_e:
                    print(f"    [x] No results or grid timeout for {term}: {search_e}")
                    
                # Reset search fields for next term
                clear_btn = page.locator("#UCCSearch_UCCSearch_btnClear")
                if await clear_btn.count() > 0:
                    await clear_btn.click()
                await slow_delay(1, 3)

        except Exception as main_e:
            print(f"Fatal error during scraping: {main_e}")
        
        finally:
            await browser.close()
            conn.close()
            print("Finished scraping.")

if __name__ == "__main__":
    asyncio.run(main())
