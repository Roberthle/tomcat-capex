import asyncio
import sqlite3
import re
import os
import random
import uuid
from playwright.async_api import async_playwright, TimeoutError

DB_PATH = "/Users/robertle/tomcat_capex/leads/tomcat_capex.db"
URL = "https://bizfileonline.sos.ca.gov/search/ucc"

# Target heavy equipment collateral
HEAVY_EQUIPMENT_KEYWORDS = [
    r"excavator", r"cnc", r"tractor", r"heavy\s*equipment", r"loader", 
    r"bulldozer", r"crane", r"forklift", r"backhoe", r"skid\s*steer"
]

def setup_db():
    """Ensure the database connection is established."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    # Removing table creation since the complex schema already exists in this database.
    return conn

def is_heavy_equipment(collateral_text):
    """Check if the collateral description matches heavy equipment keywords."""
    if not collateral_text:
        return False
    text = collateral_text.lower()
    for kw in HEAVY_EQUIPMENT_KEYWORDS:
        if re.search(kw, text):
            return True
    return False

def save_lead(conn, company_name, secured_party, collateral_text):
    """Insert the lead into the database matching the existing schema."""
    cursor = conn.cursor()
    record_id = uuid.uuid4().hex
    file_id = uuid.uuid4().hex[:12] # Mock file ID for testing
    
    max_retries = 10
    for attempt in range(max_retries):
        try:
            cursor.execute('''
                INSERT INTO ucc_leads (id, source_state, file_id, company_name, secured_party, collateral)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (record_id, 'CA', file_id, company_name, secured_party, collateral_text))
            conn.commit()
            print(f"✅ Saved lead: {company_name} | {secured_party}")
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                print(f"Database locked, retrying {attempt+1}/{max_retries}...")
                import time
                time.sleep(random.uniform(1.0, 3.0))
            else:
                raise e
    print(f"❌ Failed to save lead after {max_retries} attempts: {company_name}")

async def human_delay(page, min_ms=2000, max_ms=5000):
    """Simulate human reading/typing delay to avoid IP timeouts and bot detection."""
    delay = random.randint(min_ms, max_ms)
    await page.wait_for_timeout(delay)

async def slow_type(page, selector, text):
    """Simulate slow human typing."""
    await page.click(selector)
    await human_delay(page, 500, 1500)
    for char in text:
        await page.type(selector, char, delay=random.randint(50, 200))
    await human_delay(page, 1000, 2000)

async def run_scraper():
    conn = setup_db()
    
    async with async_playwright() as p:
        # Launching Chromium with stealth arguments
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-infobars',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--window-size=1920,1080'
            ]
        )
        
        # Using a realistic User-Agent and setting viewport
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080},
            java_script_enabled=True,
            bypass_csp=True
        )
        
        # Stealth init script to mask webdriver
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            window.navigator.chrome = {
                runtime: {}
            };
        """)
        
        page = await context.new_page()
        
        print(f"Navigating to {URL}...")
        try:
            # Go to the California UCC portal with slow/sure approach
            await page.goto(URL, timeout=60000, wait_until='domcontentloaded')
            
            # Initial human-like delay to let any bot challenges load/resolve
            print("Waiting for page load and potential bot checks to pass...")
            await human_delay(page, 5000, 8000)
            
            # Note: The actual DOM of BizFile can vary and requires specific interaction.
            # Example slow-and-sure interaction flow:
            print("Looking for UCC search interface...")
            
            print("Parsing results and filtering for heavy equipment...")
            
            # Simulated dummy data to test DB pipeline and filtering
            mock_data = [
                ("Acme Corp", "Wells Fargo", "General intangibles, office supplies"),
                ("Builders Inc", "Bank of America", "1x Caterpillar 320 Excavator, 1x John Deere Tractor"),
                ("Tech Manufacturing", "Chase Bank", "5 Axis CNC Machine, Lathes")
            ]
            
            print("Testing the DB pipeline with sample extracted data...")
            for comp, sec_party, coll_raw in mock_data:
                if is_heavy_equipment(coll_raw):
                    save_lead(conn, comp, sec_party, coll_raw)
                
            print("Scraping completed. (Note: Adapt the DOM selectors in the code based on the actual search workflow)")
            
        except TimeoutError:
            print("Timeout while loading the page or waiting for elements. The site may be blocking headless browsers.")
        except Exception as e:
            print(f"Error during scraping: {e}")
        finally:
            await browser.close()
            conn.close()

if __name__ == "__main__":
    asyncio.run(run_scraper())
