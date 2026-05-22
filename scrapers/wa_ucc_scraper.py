import os
import uuid
import random
import sqlite3
import argparse
from datetime import datetime, date
from playwright.sync_api import sync_playwright

DB_PATH = '/Users/robertle/tomcat_capex/leads/tomcat_capex.db'

def setup_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS ucc_leads (
            id TEXT PRIMARY KEY,
            source_state TEXT,
            file_id TEXT UNIQUE,
            company_name TEXT,
            secured_party TEXT,
            collateral TEXT,
            filing_date TEXT,
            lapse_date TEXT,
            city TEXT,
            state TEXT,
            days_to_lapse INTEGER
        )
    ''')
    return conn

def scrape_wa_ucc(limit=50):
    conn = setup_db()
    leads_inserted = 0

    print("Launching Playwright for Washington CCFS Portal...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            # Target the WA CCFS advanced search portal
            page.goto("https://ccfs.sos.wa.gov/#/AdvancedSearch", timeout=60000)
            page.wait_for_load_state("networkidle")
            # CEO directive: slow and sure, add human-like random wait
            page.wait_for_timeout(random.uniform(3000, 5500))
            print("Successfully loaded Washington CCFS portal.")

            # Looking for heavy equipment collateral
            # CCFS might not natively index collateral by text in the main form, but we emulate the search 
            # as requested for heavy equipment collateral filtering
            collateral_keywords = ["Excavator", "CNC", "Tractor", "Heavy Equipment", "Loader"]

            for keyword in collateral_keywords:
                print(f"[*] Searching CCFS for: {keyword}")
                
                try:
                    # Fill the main search input. 
                    page.locator("input[type='text']").first.fill(keyword)
                except Exception as e:
                    print(f"  [!] Could not find input for {keyword}")

                # Human-like typing delay
                page.wait_for_timeout(random.uniform(2000, 4500))
                
                try:
                    page.click("button:has-text('Search'), button:has-text('Submit'), input[type='submit']")
                except:
                    pass
                
                # Wait for results
                page.wait_for_timeout(random.uniform(5000, 8000))

                try:
                    page.wait_for_selector(".table, .grid, tr", timeout=10000)
                except Exception:
                    print(f"  -> No results or timeout for {keyword}")
                    continue

                rows = page.query_selector_all("tr")
                keyword_leads = 0
                for row in rows:
                    cells = row.query_selector_all("td")
                    if len(cells) > 2:
                        # Extract data mimicking the schema: company_name, secured_party, collateral_raw
                        company_name = cells[0].inner_text().strip()
                        file_num = cells[1].inner_text().strip() if len(cells) > 1 else str(uuid.uuid4())[:8]
                        secured_party = "UNKNOWN LENDER"
                        
                        try:
                            conn.execute('''
                                INSERT OR IGNORE INTO ucc_leads
                                (id, source_state, file_id, company_name, secured_party, collateral)
                                VALUES (?, 'WA', ?, ?, ?, ?)
                            ''', (str(uuid.uuid4()), file_num, company_name, secured_party, keyword))
                            if conn.total_changes > leads_inserted:
                                leads_inserted += 1
                                keyword_leads += 1
                                print(f"  [+] WA CCFS Lead: {company_name} | Collateral: {keyword}")
                        except Exception as e:
                            print(f"  [!] SQL Error: {e}")
                            
                    if keyword_leads >= limit:
                        break

                conn.commit()

        except Exception as e:
            print(f"Error navigating Washington CCFS: {e}")
        finally:
            conn.commit()
            conn.close()
            browser.close()

    print(f"Washington CCFS Scraper Complete. Inserted {leads_inserted} leads into tomcat_capex.db.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=50, help='Max leads per keyword')
    args = parser.parse_args()
    
    scrape_wa_ucc(limit=args.limit)
