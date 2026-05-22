import asyncio
import sqlite3
import random
import re
import os
from playwright.async_api import async_playwright, Page, TimeoutError

DB_PATH = "/Users/robertle/tomcat_capex/leads/tomcat_capex.db"

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ucc_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT,
            secured_party TEXT,
            collateral_raw TEXT,
            scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def save_lead(company_name: str, secured_party: str, collateral_raw: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO ucc_leads (company_name, secured_party, collateral_raw)
        VALUES (?, ?, ?)
    ''', (company_name, secured_party, collateral_raw))
    conn.commit()
    conn.close()
    print(f"[+] Saved lead to DB: {company_name}")

async def random_delay(page: Page, min_ms: int = 1500, max_ms: int = 4000):
    """Wait for a random amount of time to act human-like and avoid IP timeouts."""
    delay = random.randint(min_ms, max_ms)
    await page.wait_for_timeout(delay)

async def slow_type(page: Page, selector: str, text: str):
    """Type slowly into an input field to simulate human interaction."""
    locator = page.locator(selector)
    await locator.click()
    await locator.fill("")  # Clear existing text
    for char in text:
        await page.keyboard.press(char)
        await page.wait_for_timeout(random.randint(50, 200))

async def handle_disclaimer(page: Page):
    """Handle the Florida UCC disclaimer popup if it appears."""
    try:
        checkbox = page.locator('input[type="checkbox"]')
        if await checkbox.count() > 0 and await checkbox.is_visible(timeout=5000):
            print("Accepting terms of use disclaimer...")
            await checkbox.check()
            await random_delay(page, 1000, 2000)
            await page.locator('button:has-text("Next")').click()
            await page.wait_for_load_state("networkidle")
            await random_delay(page)
    except TimeoutError:
        pass  # Disclaimer didn't appear or already accepted

async def extract_and_filter_results(page: Page, query: str):
    """
    Parses the results page, clicks into filings, checks collateral for heavy equipment.
    This is a structural blueprint based on the Florida UCC search logic.
    """
    target_keywords = re.compile(r'(?i)(excavator|cnc|tractor|loader|backhoe|dozer|heavy equipment|forklift|skid steer)')
    
    print(f"Analyzing results for query: {query}")
    
    # Wait for results table to load.
    try:
        await page.wait_for_selector('table tbody tr, .MuiDataGrid-row', timeout=10000)
    except TimeoutError:
        print("No results found or page took too long to load.")
        return

    row_count = await page.locator('table tbody tr, .MuiDataGrid-row').count()
    print(f"Found {row_count} potential business rows.")

    for i in range(min(row_count, 10)):  # Limit to first 10 for safety/anti-ban
        try:
            row = page.locator('table tbody tr, .MuiDataGrid-row').nth(i)
            text_content = await row.inner_text()
            
            if not text_content.strip():
                continue
                
            print(f"Inspecting row {i+1}...")
            
            await row.click()
            await page.wait_for_load_state("networkidle")
            await random_delay(page, 2000, 4000)
            
            company_name_element = page.locator('h2, .MuiTypography-h6, h3').nth(0)
            company_name = await company_name_element.inner_text() if await company_name_element.count() > 0 else f"Unknown_Company_{query}"
            
            filing_rows = page.locator('table tbody tr, .MuiDataGrid-row')
            filing_count = await filing_rows.count()
            
            for j in range(min(filing_count, 5)):
                filing = page.locator('table tbody tr, .MuiDataGrid-row').nth(j)
                await filing.click()
                await page.wait_for_load_state("networkidle")
                await random_delay(page, 1500, 3000)
                
                page_text = await page.evaluate("document.body.innerText")
                
                if target_keywords.search(page_text):
                    secured_party_match = re.search(r'(?i)Secured Party:?\s*(.*?)\n', page_text)
                    secured_party = secured_party_match.group(1).strip() if secured_party_match else "Unknown Secured Party"
                    
                    collateral_match = re.search(r'(?i)Collateral:?\s*(.*?)(?:\n\n|$)', page_text)
                    collateral_raw = collateral_match.group(1).strip() if collateral_match else "Matched keywords in document"
                    
                    save_lead(company_name, secured_party, collateral_raw)
                
                await page.go_back()
                await page.wait_for_load_state("networkidle")
                await random_delay(page, 1000, 2000)
                
            await page.go_back()
            await page.wait_for_load_state("networkidle")
            await random_delay(page, 2000, 3500)
            
        except Exception as e:
            print(f"Error processing row {i}: {e}")
            await page.goto("https://floridaucc.com/search", wait_until="networkidle")
            await random_delay(page)
            await handle_disclaimer(page)

async def main():
    init_db()
    
    search_queries = ["CONSTRUCTION", "EXCAVATING", "FARM", "LOGISTICS"]
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()

        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        for query in search_queries:
            try:
                print(f"\n--- Starting Search for '{query}' ---")
                await page.goto("https://floridaucc.com/search", wait_until="networkidle")
                await random_delay(page, 2000, 4000)
                
                await handle_disclaimer(page)
                
                keyword_selector = 'input[name="keyword"]'
                await page.wait_for_selector(keyword_selector, state="visible", timeout=10000)
                
                print(f"Entering search term '{query}'")
                await slow_type(page, keyword_selector, query)
                
                await random_delay(page, 500, 1500)
                
                await page.keyboard.press("Enter")
                await page.wait_for_load_state("networkidle")
                await random_delay(page, 3000, 6000)
                
                await extract_and_filter_results(page, query)
                
            except Exception as e:
                print(f"Critical error on query '{query}': {e}")
                
        await browser.close()
        print("\nScraping session completed.")

if __name__ == "__main__":
    asyncio.run(main())
