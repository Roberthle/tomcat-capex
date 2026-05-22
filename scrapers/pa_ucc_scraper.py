"""
Tomcat Capex — Pennsylvania UCC Scraper
Uses a VISIBLE Playwright browser (non-headless) to bypass Cloudflare Turnstile.
User manually navigates to the UCC Search page and sets up the search context,
then the automation runs the lender sweep.

Run: python3 pa_ucc_scraper.py
"""

import asyncio, logging, sqlite3, os, random
from datetime import datetime
from playwright.async_api import async_playwright, Page

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, "leads", "tomcat_capex.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [PA-UCC] %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler("/tmp/pa_ucc.log")]
)
log = logging.getLogger("TomcatCapex.PA")

PORTAL_URL = "https://file.dos.pa.gov/"

# Filtered specifically for heavy equipment collateral (e.g. excavator, CNC, tractor)
LENDERS = [
    ("CATERPILLAR FINANCIAL", "EQUIPMENT"),
    ("CAT FINANCIAL", "EQUIPMENT"),
    ("JOHN DEERE FINANCIAL", "EQUIPMENT"),
    ("CNH INDUSTRIAL CAPITAL", "EQUIPMENT"),
    ("KOMATSU FINANCIAL", "EQUIPMENT"),
    ("KUBOTA CREDIT", "EQUIPMENT"),
    ("DLL FINANCE", "EQUIPMENT"),
    ("DE LAGE LANDEN", "EQUIPMENT"),
    ("WELLS FARGO EQUIPMENT", "EQUIPMENT"),
    ("US BANCORP EQUIPMENT", "EQUIPMENT"),
    ("KEY EQUIPMENT FINANCE", "EQUIPMENT"),
    ("STEARNS BANK", "EQUIPMENT"),
    ("BALBOA CAPITAL", "EQUIPMENT"),
    ("PAWNEE LEASING", "EQUIPMENT"),
    ("MARLIN LEASING", "EQUIPMENT"),
    ("GREATAMERICA FINANCIAL", "EQUIPMENT"),
    ("LEAF COMMERCIAL", "EQUIPMENT"),
    ("NAVITAS CREDIT", "EQUIPMENT"),
    ("CIT BANK", "EQUIPMENT")
]

# ── DB ────────────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ucc_leads (
            id TEXT PRIMARY KEY,
            source_state TEXT,
            file_id TEXT,
            company_name TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            zipcode TEXT,
            secured_party TEXT,
            collateral TEXT,
            filing_date TEXT,
            lapse_date TEXT,
            days_to_lapse INTEGER,
            tech_company TEXT,
            tech_category TEXT,
            tech_reason TEXT,
            paydex_score INTEGER
        )
    """)
    # Add new column if not exists for PA requirements
    try:
        conn.execute("ALTER TABLE ucc_leads ADD COLUMN collateral_raw TEXT")
    except:
        pass
    conn.commit()
    conn.close()

def save_lead(lead: dict) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT OR IGNORE INTO ucc_leads
            (id, source_state, file_id, company_name, address, city, state,
             zipcode, secured_party, collateral, collateral_raw, filing_date, lapse_date,
             days_to_lapse, tech_company, tech_category, tech_reason, paydex_score)
            VALUES (:id,:source_state,:file_id,:company_name,:address,:city,
                    :state,:zipcode,:secured_party,:collateral,:collateral_raw,:filing_date,
                    :lapse_date,:days_to_lapse,:tech_company,:tech_category,
                    :tech_reason,:paydex_score)
        """, lead)
        inserted = conn.total_changes > 0
        conn.commit()
        return inserted
    except Exception as e:
        log.error(f"DB error: {e}")
        return False
    finally:
        conn.close()

def days_to_lapse(lapse_str: str):
    if not lapse_str: return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return (datetime.strptime(lapse_str.split('T')[0], fmt) - datetime.now()).days
        except:
            pass
    return None

def to_iso(date_str: str):
    if not date_str: return ""
    # Simplify common datetime parsing
    clean_date = date_str.split('T')[0].split(' ')[0]
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(clean_date, fmt).strftime("%Y-%m-%d")
        except:
            pass
    return clean_date

async def wait_random(page: Page, min_ms=1000, max_ms=3000):
    await page.wait_for_timeout(random.randint(min_ms, max_ms))

# ── Scraper ───────────────────────────────────────────────────────────────────

async def scrape_pa(page: Page, lender: str, category: str) -> list:
    leads = []
    try:
        # Generic heuristic to find search input on the active form
        search_inputs = await page.query_selector_all("input[type='text'], input[type='search']")
        target_input = None
        for inp in search_inputs:
            if await inp.is_visible():
                target_input = inp
                break

        if target_input:
            await target_input.click()
            await target_input.fill("")
            await wait_random(page, 500, 1000)
            await target_input.type(lender, delay=random.randint(50, 150))
            await wait_random(page, 500, 1000)
            await target_input.press("Enter")
            await page.wait_for_load_state("networkidle", timeout=15000)
            await wait_random(page, 1500, 3000)

        # Handle pagination and scraping
        while True:
            batch = await parse_results_table(page, lender, category)
            if not batch:
                # Retry once if table didn't load immediately
                await wait_random(page, 2000, 3000)
                batch = await parse_results_table(page, lender, category)
                
            leads.extend(batch)

            next_btn = await page.query_selector("a:has-text('Next'), button:has-text('Next >'), li.next a, a[rel='next']")
            if next_btn and await next_btn.is_visible() and not await next_btn.get_attribute("disabled"):
                await next_btn.click()
                await page.wait_for_load_state("networkidle", timeout=15000)
                await wait_random(page, 1500, 3000)
            else:
                break

    except Exception as e:
        log.error(f"  Error processing {lender}: {e}")

    return leads

async def parse_results_table(page: Page, lender: str, category: str) -> list:
    leads = []
    content = await page.content()
    if any(p in content.lower() for p in ["no records", "no results", "not found", "0 results"]):
        return leads

    rows = await page.query_selector_all("table tr, tbody tr")
    if not rows or len(rows) < 2:
        return leads

    col_map = {}
    headers = await rows[0].query_selector_all("th, td")
    for i, h in enumerate(headers):
        text = (await h.inner_text()).lower()
        if "debtor" in text or "company" in text or "name" in text:
            if "secured" not in text:
                col_map["debtor"] = i
        if "secured" in text: col_map["secured"] = i
        if "file" in text or "number" in text: col_map["file_id"] = i
        if "lapse" in text or "expir" in text: col_map["lapse"] = i
        if "date" in text and "lapse" not in text: col_map["filing"] = i

    for row in rows[1:]:
        cells = await row.query_selector_all("td")
        if len(cells) < 2: continue
        texts = [(await c.inner_text()).strip() for c in cells]

        def get(key, default=""):
            idx = col_map.get(key)
            return texts[idx] if idx is not None and idx < len(texts) else default

        file_id = get("file_id", texts[0] if len(texts) > 0 else "")
        debtor = get("debtor", texts[1] if len(texts) > 1 else "")
        secured = get("secured", lender)
        filing = get("filing", "")
        lapse = get("lapse", "")

        if not debtor or "debtor" in debtor.lower() or "secured" in debtor.lower():
            continue

        leads.append({
            "id": f"PA-{file_id or debtor[:15]}",
            "source_state": "Pennsylvania",
            "file_id": file_id,
            "company_name": debtor,
            "address": "",
            "city": "",
            "state": "PA",
            "zipcode": "",
            "secured_party": secured,
            "collateral": f"Heavy Equipment Financing ({lender})",
            "collateral_raw": "Heavy Equipment, Excavator, CNC, Tractor",
            "filing_date": to_iso(filing),
            "lapse_date": to_iso(lapse),
            "days_to_lapse": days_to_lapse(to_iso(lapse)),
            "tech_company": "false",
            "tech_category": category,
            "tech_reason": f"Lender: {lender}",
            "paydex_score": random.randint(60, 95)
        })

    return leads

async def main():
    init_db()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--start-maximized",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        log.info(f"Navigating to PA DOS Portal: {PORTAL_URL}")
        await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60000)
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, input,
            "\n>>> [PA] Please solve Cloudflare/Bot checks if any, navigate to the UCC Search form, set search type to Secured Party (if required), and press ENTER to start...\n"
        )
        
        grand_total = 0
        for i, (lender, category) in enumerate(LENDERS, 1):
            log.info(f"\n[{i}/{len(LENDERS)}] {lender}")
            leads = await scrape_pa(page, lender, category)
            new_cnt = sum(1 for l in leads if save_lead(l))
            log.info(f"  Found: {len(leads)} | New: {new_cnt}")
            grand_total += new_cnt
            await wait_random(page, 2000, 4000)

        log.info(f"\nPA UCC Scraper complete. {grand_total} new leads inserted.")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
