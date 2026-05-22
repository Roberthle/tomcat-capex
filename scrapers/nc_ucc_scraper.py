"""
NC SOS UCC Scraper v1
Uses a persistent Playwright browser session to bypass Cloudflare.
User solves the challenge once, then all lender searches run automatically.
"""
import asyncio, logging, re, sqlite3, sys, pathlib
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [NC-UCC] %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/tmp/nc_ucc_scraper.log"),
    ],
)
log = logging.getLogger("NC-UCC")

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH      = pathlib.Path(__file__).parent.parent / "leads" / "tomcat_capex.db"
NC_UCC_URL   = "https://www.sosnc.gov/online_services/search/by_title/_UCC"
LOOKBACK_YRS = 6
STATE        = "North Carolina"

# ── Lenders ────────────────────────────────────────────────────────────────────
LENDERS = [
    "DELL FINANCIAL SERVICES",
    "HEWLETT PACKARD FINANCIAL",
    "HP FINANCIAL SERVICES",
    "IBM CREDIT",
    "CISCO SYSTEMS CAPITAL",
    "KONICA MINOLTA",
    "XEROX FINANCIAL SERVICES",
    "CANON FINANCIAL SERVICES",
    "RICOH USA",
    "KYOCERA DOCUMENT SOLUTIONS",
    "GREATAMERICA FINANCIAL SERVICES",
    "MARLIN LEASING",
    "PAWNEE LEASING",
    "BALBOA CAPITAL",
    "DLL FINANCE",
    "DE LAGE LANDEN FINANCIAL",
    "WELLS FARGO EQUIPMENT FINANCE",
    "US BANCORP EQUIPMENT FINANCE",
    "KEY EQUIPMENT FINANCE",
    "STEARNS BANK",
    "BANC OF AMERICA",
    "CIT BANK",
    "CATERPILLAR FINANCIAL",
    "JOHN DEERE FINANCIAL",
    "CNH INDUSTRIAL CAPITAL",
    "TOYOTA INDUSTRIES COMMERCIAL FINANCE",
    "LEAF COMMERCIAL CAPITAL",
]

# ── DB setup ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ucc_leads (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id         TEXT UNIQUE,
            company_name    TEXT,
            city            TEXT,
            state           TEXT,
            county          TEXT,
            secured_party   TEXT,
            collateral      TEXT,
            tech_category   TEXT,
            filing_date     TEXT,
            lapse_date      TEXT,
            days_to_lapse   INTEGER,
            source_state    TEXT DEFAULT 'North Carolina',
            source_url      TEXT,
            scraped_at      TEXT,
            enriched_at     TEXT,
            phone           TEXT,
            email           TEXT,
            website         TEXT,
            deal_score      INTEGER DEFAULT 0,
            signals_json    TEXT,
            urgency_tier    TEXT,
            claim_status    INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def save_lead(row: dict) -> bool:
    """Returns True if new record inserted."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT OR IGNORE INTO ucc_leads
            (file_id, company_name, city, state, county, secured_party,
             collateral, tech_category, filing_date, lapse_date, days_to_lapse,
             source_state, source_url, scraped_at, deal_score, urgency_tier)
            VALUES (:file_id,:company_name,:city,:state,:county,:secured_party,
                    :collateral,:tech_category,:filing_date,:lapse_date,:days_to_lapse,
                    :source_state,:source_url,:scraped_at,:deal_score,:urgency_tier)
        """, row)
        inserted = conn.total_changes
        conn.commit()
        return inserted > 0
    finally:
        conn.close()

def calc_urgency(lapse_date_str: str):
    """Returns (days_to_lapse, urgency_tier)."""
    if not lapse_date_str:
        return None, "cold"
    try:
        lapse = datetime.strptime(lapse_date_str, "%Y-%m-%d")
        dtl   = (lapse - datetime.now()).days
        tier  = "hot" if dtl <= 90 else "warm" if dtl <= 365 else "cold"
        return dtl, tier
    except:
        return None, "cold"

def parse_filing_date(date_str: str):
    """Parse MM/DD/YYYY → YYYY-MM-DD."""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except:
            pass
    return None

def estimate_lapse(filing_date_str: str, years=5) -> str:
    """Add 5 years to filing date for UCC lapse estimate."""
    if not filing_date_str:
        return None
    try:
        fd = datetime.strptime(filing_date_str, "%Y-%m-%d")
        return (fd + timedelta(days=years * 365)).strftime("%Y-%m-%d")
    except:
        return None

# ── NC SOS Search ─────────────────────────────────────────────────────────────
async def search_secured_party(page, lender_name: str) -> list[dict]:
    """
    Search NC SOS UCC for a given secured party name.
    Returns list of lead dicts.
    """
    leads = []
    
    try:
        # Navigate to search page
        await page.goto(NC_UCC_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)
        
        # Wait for the search form
        await page.wait_for_selector("select, input[type='text']", timeout=10000)
        
        # Look for search type dropdown and select "Secured Party"
        # NC SOS UCC typically has a dropdown with search type options
        selects = await page.query_selector_all("select")
        for sel in selects:
            options = await sel.query_selector_all("option")
            for opt in options:
                text = (await opt.inner_text()).lower()
                val  = await opt.get_attribute("value") or ""
                if "secured" in text or "secured" in val.lower():
                    await sel.select_option(value=val)
                    log.info(f"  Selected search type: {await opt.inner_text()}")
                    break
        
        # Find the name input field and fill it
        inputs = await page.query_selector_all("input[type='text'], input:not([type])")
        if not inputs:
            log.warning("  No text inputs found on page")
            return leads
        
        # Clear and fill the first text input (usually the name field)
        await inputs[0].fill("")
        await inputs[0].type(lender_name, delay=50)
        
        # Submit the form
        submit = await page.query_selector("input[type='submit'], button[type='submit'], button:has-text('Search')")
        if submit:
            await submit.click()
        else:
            await inputs[0].press("Enter")
        
        # Wait for results
        await page.wait_for_load_state("networkidle", timeout=15000)
        await asyncio.sleep(1)
        
        # Parse results table
        leads = await parse_results(page, lender_name)
        
    except Exception as e:
        log.error(f"  Error searching {lender_name}: {e}")
    
    return leads

async def parse_results(page, lender_name: str) -> list[dict]:
    """Extract leads from the NC SOS results table."""
    leads = []
    
    # Check for "no results" message
    content = await page.content()
    if any(phrase in content.lower() for phrase in ["no records", "no results", "0 records"]):
        log.info("  No results")
        return leads
    
    # Find all table rows in results
    rows = await page.query_selector_all("table tr")
    if not rows:
        # Try other result containers
        rows = await page.query_selector_all(".result-row, .filing-row, li.result")
    
    for row in rows[1:]:  # Skip header row
        cells = await row.query_selector_all("td")
        if len(cells) < 3:
            continue
        
        try:
            texts = [await c.inner_text() for c in cells]
            
            # NC SOS UCC typically shows: File Number | Debtor | Secured Party | Filing Date | Lapse Date | Status
            file_id      = texts[0].strip() if len(texts) > 0 else ""
            company_name = texts[1].strip() if len(texts) > 1 else ""
            secured_pty  = texts[2].strip() if len(texts) > 2 else lender_name
            filing_raw   = texts[3].strip() if len(texts) > 3 else ""
            lapse_raw    = texts[4].strip() if len(texts) > 4 else ""
            
            if not company_name or not file_id:
                continue
            
            # Parse dates
            filing_date  = parse_filing_date(filing_raw)
            lapse_date   = parse_filing_date(lapse_raw) if lapse_raw else estimate_lapse(filing_date)
            dtl, tier    = calc_urgency(lapse_date)
            
            # Score: base 60, boost for tech lenders
            score = 60
            if any(t in lender_name.upper() for t in ["DELL", "HP", "IBM", "CISCO", "XEROX"]):
                score += 5
            if tier == "hot":
                score += 10
            
            lead = {
                "file_id":      f"NC-{file_id}",
                "company_name": company_name,
                "city":         "",
                "state":        "NC",
                "county":       "",
                "secured_party": secured_pty or lender_name,
                "collateral":   f"Equipment Financing ({lender_name[:40]})",
                "tech_category": "Equipment Finance",
                "filing_date":  filing_date,
                "lapse_date":   lapse_date,
                "days_to_lapse": dtl,
                "source_state": "North Carolina",
                "source_url":   page.url,
                "scraped_at":   datetime.now().isoformat(),
                "deal_score":   score,
                "urgency_tier": tier,
            }
            leads.append(lead)
            
        except Exception as e:
            log.debug(f"  Row parse error: {e}")
            continue
    
    return leads

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    init_db()
    
    total_found = 0
    total_new   = 0
    
    log.info("=" * 60)
    log.info("NC SOS UCC SCRAPER — 6-Year Lookback")
    log.info("=" * 60)
    
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
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        
        # ── Step 1: Let user solve Cloudflare ────────────────────────────────
        log.info("\nOpening NC SOS UCC search page...")
        log.info("A browser window will appear. If Cloudflare challenge shows, click 'Verify'.")
        log.info("Once the UCC SEARCH FORM is visible, press ENTER here to begin scraping.\n")
        
        await page.goto(NC_UCC_URL, wait_until="domcontentloaded", timeout=60000)
        
        # Inspect the page to understand form structure
        await asyncio.sleep(5)
        content = await page.content()
        
        # Auto-detect form structure
        selects = await page.query_selector_all("select")
        inputs  = await page.query_selector_all("input")
        log.info(f"Form detected: {len(selects)} selects, {len(inputs)} inputs")
        
        for sel in selects:
            sel_name = await sel.get_attribute("name") or await sel.get_attribute("id") or "?"
            options  = await sel.query_selector_all("option")
            opt_texts = [await o.inner_text() for o in options]
            log.info(f"  Select '{sel_name}': {opt_texts}")
        
        for inp in inputs:
            inp_name = await inp.get_attribute("name") or await inp.get_attribute("id") or "?"
            inp_type = await inp.get_attribute("type") or "text"
            inp_ph   = await inp.get_attribute("placeholder") or ""
            log.info(f"  Input '{inp_name}' type={inp_type} placeholder='{inp_ph}'")
        
        # Pause for user
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input, "\n>>> Press ENTER once UCC search form is visible and ready...\n")
        
        # ── Step 2: Auto-search all lenders ──────────────────────────────────
        log.info(f"\nStarting automated search for {len(LENDERS)} lenders...")
        
        for i, lender in enumerate(LENDERS, 1):
            log.info(f"\n[{i}/{len(LENDERS)}] Searching: {lender}")
            
            leads = await search_secured_party(page, lender)
            log.info(f"  Found: {len(leads)} leads")
            
            new_count = 0
            for lead in leads:
                if save_lead(lead):
                    new_count += 1
            
            total_found += len(leads)
            total_new   += new_count
            log.info(f"  New: {new_count} | Running total: {total_new}")
            
            await asyncio.sleep(1.5)  # Be polite
        
        log.info("\n" + "=" * 60)
        log.info(f"COMPLETE: {total_found} found, {total_new} new NC leads")
        log.info("=" * 60)
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
