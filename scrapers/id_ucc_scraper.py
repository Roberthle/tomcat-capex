import os
import re
import time
import uuid
import random
import sqlite3
import argparse
from datetime import datetime, date
from playwright.sync_api import sync_playwright

DB_PATH = '/Users/robertle/tomcat_capex/leads/tomcat_capex.db'

def setup_db():
    conn = sqlite3.connect(DB_PATH)
    return conn


def _parse_location(raw_name):
    """Split 'COMPANY - CITY, ST' into (clean_name, city, state)."""
    m = re.match(r'^(.+?)\s+-\s+([A-Z][A-Za-z\s\.]+),\s+([A-Z]{2})\s*$', (raw_name or '').strip())
    if m:
        return m.group(1).strip(), m.group(2).strip().title(), m.group(3).strip()
    return raw_name, None, None


def _days_to_lapse(lapse_str):
    """Convert MM/DD/YYYY to integer days from today."""
    for fmt in ('%m/%d/%Y', '%Y-%m-%d'):
        try:
            return (datetime.strptime((lapse_str or '').strip(), fmt).date() - date.today()).days
        except ValueError:
            continue
    return None

def scrape_idaho_ucc(limit=50):
    """
    Playwright scraper for Idaho UCC filings.
    Target: sosbiz.idaho.gov
    """
    conn = setup_db()
    leads_inserted = 0

    print("Launching Playwright for Idaho UCC (sosbiz.idaho.gov)...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            # Navigate to the Idaho SOS UCC Search page
            page.goto("https://sosbiz.idaho.gov/search/ucc", timeout=60000)
            page.wait_for_load_state("networkidle")
            print("Successfully loaded Idaho SOS portal.")

            # Note: This is a structural template. Actual execution requires specific DOM interaction
            # depending on whether Idaho implements captchas or dynamic iframe loading.
            
            # ID economy: agriculture (potatoes/dairy), tech (Boise),
            # mining, logging, construction, trucking, healthcare, credit unions
            lender_keywords = [
                # Original
                "Financial", "Capital", "Funding", "Bank",
                # Equipment-intensive industries
                "Leasing", "Equipment", "Trucking", "Transport",
                "Construction", "Excavating", "Contracting",
                # ID agriculture (biggest in state)
                "Potato", "Dairy", "Grain", "Livestock", "Agriculture",
                "Irrigation", "Seed", "Crop", "Farm", "Ranch",
                # ID natural resources
                "Logging", "Timber", "Lumber",
                "Mining", "Silver", "Energy", "Solar",
                # Tech (Boise corridor)
                "Technology", "Tech", "Systems",
                # Healthcare
                "Medical", "Dental", "Clinic", "Health",
                # Regional lender types
                "Credit Union", "Holdings", "Services", "Enterprises",
            ]
            
            for keyword in lender_keywords:
                print(f"[*] Hunting Lender Keyword: {keyword}")
                page.fill("input[placeholder='Search by name or file number']", keyword)
                page.click(".search-button")
                
                # Hard delay to allow Tyler Tech AJAX to fetch and render the new payload
                page.wait_for_timeout(6000)
                
                try:
                    page.wait_for_selector(".div-table-row", timeout=10000)
                except Exception:
                    print(f"  -> No results or timeout for {keyword}")
                    continue
                
                keyword_leads = 0
                while keyword_leads < limit:
                    rows = page.query_selector_all(".div-table-row")
                    
                    for row in rows:
                        cells = row.query_selector_all(".div-table-cell")
                        if len(cells) > 6:
                            debtor = cells[1].inner_text().strip()
                            file_num = cells[2].inner_text().strip()
                            lender = cells[3].inner_text().strip()
                            filing_date = cells[5].inner_text().strip()
                            lapse_date = cells[6].inner_text().strip() if len(cells) > 6 else ""
                            
                            clean_name, city, state = _parse_location(debtor)
                            days                    = _days_to_lapse(lapse_date)

                            try:
                                conn.execute('''
                                    INSERT OR IGNORE INTO ucc_leads
                                    (id, source_state, file_id, company_name, secured_party,
                                     collateral, filing_date, lapse_date, city, state, days_to_lapse)
                                    VALUES (?, 'ID', ?, ?, ?, 'Equipment/General', ?, ?, ?, ?, ?)
                                ''', (str(uuid.uuid4()), file_num, clean_name, lender,
                                      filing_date, lapse_date, city, state, days))
                                if conn.total_changes > leads_inserted:
                                    leads_inserted += 1
                                    keyword_leads  += 1
                                    print(f"  [+] Idaho: {clean_name} | {city}, {state} | {days}d | {lender}")
                            except Exception as e:
                                print(f"  [!] SQL Error: {e}")
                                
                        if keyword_leads >= limit:
                            break
                            
                    conn.commit()
                            
                    if keyword_leads >= limit:
                        break
                        
                    # Human Delay
                    delay = random.uniform(5.5, 11.2)
                    print(f"  [~] Human delay: resting for {delay:.1f}s...")
                    time.sleep(delay)
                    
                    # Pagination attempt
                    next_button = page.locator("button:has-text('Next'), .icon-caret-right, a:has-text('Next')").first
                    if next_button.is_visible():
                        print("  [>] Clicking Next Page...")
                        next_button.click()
                        page.wait_for_timeout(6000)
                        try:
                            page.wait_for_selector(".div-table-row", timeout=10000)
                        except:
                            break
                    else:
                        # Attempt infinite scroll
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(4000)
                        new_rows = page.query_selector_all(".div-table-row")
                        if len(new_rows) <= len(rows):
                            print(f"  [x] End of results for {keyword}")
                            break
            
            print(f"Scraped {leads_inserted} live leads from the DOM.")
            
        except Exception as e:
            print(f"Error navigating Idaho SOS: {e}")
        finally:
            conn.commit()
            conn.close()
            browser.close()
            
    print(f"Idaho Scraper Complete. Inserted {leads_inserted} new leads into tomcat_capex.db.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=200, help='Max leads per keyword')
    args = parser.parse_args()
    
    scrape_idaho_ucc(limit=args.limit)
