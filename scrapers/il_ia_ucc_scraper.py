"""
Tomcat Capex — Illinois & Iowa UCC Scraper
Uses a VISIBLE Playwright browser (non-headless) to bypass server-side bot detection.
User confirms the page is loaded once per state, then automation runs the lender sweep.

Run:  python3 il_ia_ucc_scraper.py --state IL
      python3 il_ia_ucc_scraper.py --state IA
      python3 il_ia_ucc_scraper.py --state ALL
"""

import asyncio, argparse, logging, re, sqlite3, os, json
from datetime import datetime, timedelta
from playwright.async_api import async_playwright, Page

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, "leads", "tomcat_capex.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [IL-IA] %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler("/tmp/il_ia_ucc.log")]
)
log = logging.getLogger("TomcatCapex.ILIA")

# ── State Portal Config ───────────────────────────────────────────────────────
PORTALS = {
    "IL": {
        "name":  "Illinois",
        "url":   "https://apps.ilsos.gov/uccsearch/",
        "state": "IL",
    },
    "IA": {
        "name":  "Iowa",
        "url":   "https://sos.iowa.gov/search/ucc/search.aspx",
        "state": "IA",
    },
}

# ── Lenders ───────────────────────────────────────────────────────────────────
LENDERS = [
    # Equipment captives
    ("CATERPILLAR FINANCIAL", "EQUIPMENT"),
    ("CAT FINANCIAL", "EQUIPMENT"),
    ("JOHN DEERE FINANCIAL", "EQUIPMENT"),
    ("CNH INDUSTRIAL CAPITAL", "EQUIPMENT"),
    ("KOMATSU FINANCIAL", "EQUIPMENT"),
    ("KUBOTA CREDIT", "EQUIPMENT"),
    # Equipment finance
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
    ("CIT BANK", "EQUIPMENT"),
    # Tech OEMs
    ("DELL FINANCIAL", "IT_OEM"),
    ("HEWLETT PACKARD", "IT_OEM"),
    ("HP FINANCIAL", "IT_OEM"),
    ("IBM CREDIT", "IT_OEM"),
    ("CISCO SYSTEMS CAPITAL", "IT_OEM"),
    ("XEROX FINANCIAL", "PRINT_IMAGING"),
    ("KONICA MINOLTA", "PRINT_IMAGING"),
    ("RICOH USA", "PRINT_IMAGING"),
    ("CANON FINANCIAL", "PRINT_IMAGING"),
    ("KYOCERA DOCUMENT", "PRINT_IMAGING"),
]

# ── DB ────────────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    for col in ["tech_company TEXT", "tech_category TEXT", "tech_reason TEXT",
                "paydex_score INTEGER"]:
        try:
            conn.execute(f"ALTER TABLE ucc_leads ADD COLUMN {col}")
        except:
            pass
    conn.commit(); conn.close()

def save_lead(lead: dict) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT OR IGNORE INTO ucc_leads
            (id, source_state, file_id, company_name, address, city, state,
             zipcode, secured_party, collateral, filing_date, lapse_date,
             days_to_lapse, tech_company, tech_category, tech_reason, paydex_score)
            VALUES (:id,:source_state,:file_id,:company_name,:address,:city,
                    :state,:zipcode,:secured_party,:collateral,:filing_date,
                    :lapse_date,:days_to_lapse,:tech_company,:tech_category,
                    :tech_reason,:paydex_score)
        """, lead)
        inserted = conn.total_changes > 0
        conn.commit()
        return inserted
    except Exception as e:
        log.error(f"DB: {e}")
        return False
    finally:
        conn.close()

def days_to_lapse(lapse_str: str):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return (datetime.strptime(lapse_str, fmt) - datetime.now()).days
        except:
            pass
    return None

def to_iso(date_str: str):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except:
            pass
    return date_str

def estimate_paydex(lender: str, dtl):
    score = 55
    lender_up = lender.upper()
    BIG  = ["WELLS FARGO","US BANCORP","KEY EQUIPMENT","CIT BANK"]
    CAP  = ["CATERPILLAR","CAT FIN","JOHN DEERE","CNH","KOMATSU","KUBOTA"]
    ATIER= ["DLL","DE LAGE","GREATAMERICA","STEARNS","MARLIN","LEAF","NAVITAS",
            "PAWNEE","BALBOA","DELL","HP","IBM","CISCO","XEROX","KONICA",
            "RICOH","CANON","KYOCERA","HEWLETT"]
    if any(b in lender_up for b in BIG):   score += 12
    elif any(c in lender_up for c in CAP): score += 8
    elif any(a in lender_up for a in ATIER): score += 4
    if dtl is not None:
        tenure_years = abs(dtl - 1825) / 365.25  # rough estimate from lapse
        bump = min(10, int(max(0, tenure_years) * 2))
        score += bump
    return min(100, max(1, score))

# ── Form Detector ─────────────────────────────────────────────────────────────
async def detect_form(page: Page) -> dict:
    """Auto-detect UCC search form fields on any SOS page."""
    info = {"selects": [], "inputs": [], "submit": None, "sp_value": None,
            "name_field": None, "search_type_field": None}

    selects = await page.query_selector_all("select")
    for sel in selects:
        sel_id   = await sel.get_attribute("id")   or ""
        sel_name = await sel.get_attribute("name") or ""
        options  = await sel.query_selector_all("option")
        opt_data = []
        for opt in options:
            txt = (await opt.inner_text()).strip()
            val = await opt.get_attribute("value") or txt
            opt_data.append({"text": txt, "value": val})
            if any(k in txt.lower() for k in ["secured", "sec party", "sp name"]):
                info["sp_value"]          = val
                info["search_type_field"] = sel_name or sel_id
        info["selects"].append({"id": sel_id, "name": sel_name, "options": opt_data})

    inputs = await page.query_selector_all("input")
    for inp in inputs:
        inp_type = (await inp.get_attribute("type") or "text").lower()
        inp_id   = await inp.get_attribute("id")   or ""
        inp_name = await inp.get_attribute("name") or ""
        inp_ph   = await inp.get_attribute("placeholder") or ""
        if inp_type in ("text", "search", ""):
            info["inputs"].append({"id": inp_id, "name": inp_name, "placeholder": inp_ph})
            if not info["name_field"]:
                info["name_field"] = inp_name or inp_id

    # Find submit button
    for sel in ["input[type='submit']", "button[type='submit']",
                "button:has-text('Search')", "input[value='Search']"]:
        btn = await page.query_selector(sel)
        if btn:
            info["submit"] = sel
            break

    return info

# ── IL Scraper ────────────────────────────────────────────────────────────────
async def _il_load_search_form(page: Page):
    """
    IL UCC has a 2-step landing:
      Step 1: Choose 'UCC Search' vs 'Federal Tax Lien'
      Step 2: The real name/search-type form appears
    This function lands on the UCC Search form and returns True if ready.
    """
    IL_URL = "https://apps.ilsos.gov/uccsearch/"
    await page.goto(IL_URL, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(2)

    # Click "UCC Search" option if still on the landing chooser
    for selector in [
        "input[type='radio'][value='UCC']",
        "input[type='radio'][value='ucc']",
        "label:has-text('UCC Search')",
        "a:has-text('UCC Search')",
        "input[type='submit'][value='UCC Search']",
        "button:has-text('UCC Search')",
    ]:
        try:
            el = await page.query_selector(selector)
            if el:
                await el.click()
                await asyncio.sleep(2)
                log.info("  IL: Clicked 'UCC Search' — now on name form")
                break
        except:
            continue

    # After clicking, detect radio buttons for Secured Party
    radios = await page.query_selector_all("input[type='radio']")
    sp_radio = None
    for r in radios:
        val   = (await r.get_attribute("value") or "").upper()
        label = ""
        rid   = await r.get_attribute("id") or ""
        # Try to find associated label text
        if rid:
            lbl = await page.query_selector(f"label[for='{rid}']")
            if lbl:
                label = (await lbl.inner_text()).upper()
        if any(k in val+label for k in ["SECURED","SP","SEC"]):
            sp_radio = r
            break

    if sp_radio:
        await sp_radio.click()
        await asyncio.sleep(0.5)
        log.info("  IL: Selected 'Secured Party' radio button")
    else:
        log.warning("  IL: Could not find Secured Party radio — will try name search anyway")

    return True


async def scrape_il(page: Page, lender: str, category: str) -> list:
    """Scrape Illinois apps.ilsos.gov UCC for one lender using 6-month date windows."""
    leads = []
    IL_URL = "https://apps.ilsos.gov/uccsearch/"

    # Illinois typically limits to 200 results per search — use 6-month windows
    now = datetime.now()
    lookback_years = 6
    windows = []
    for i in range(0, lookback_years * 12, 6):
        end   = now - timedelta(days=30 * i)
        start = now - timedelta(days=30 * (i + 6))
        windows.append((start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y")))

    for start_date, end_date in windows:
        try:
            # Use the 2-step loader: lands on chooser → clicks UCC Search → selects SP
            await _il_load_search_form(page)

            # Fill name field — form is now on the Secured Party search page
            filled = False
            for sel in ["input[name='name']", "input[name='search']",
                        "input[name='SearchName']", "input[type='text']"]:
                name_input = await page.query_selector(sel)
                if name_input:
                    try:
                        await name_input.wait_for_element_state("visible", timeout=5000)
                        await name_input.fill("")
                        await name_input.fill(lender)
                        filled = True
                        break
                    except:
                        continue
            if not filled:
                log.warning(f"  IL: Could not fill name field for {lender}")
                continue

            # Set date range if fields exist
            date_inputs = await page.query_selector_all("input[placeholder*='MM'],input[type='text'][placeholder*='date']")
            if len(date_inputs) >= 3:  # name + 2 date fields
                await date_inputs[1].fill(start_date)
                await date_inputs[2].fill(end_date)

            # Submit
            submit = await page.query_selector(
                "input[type='submit'],button[type='submit'],button:has-text('Search')"
            )
            if submit:
                await submit.click()
                await page.wait_for_load_state("networkidle", timeout=20000)
                await asyncio.sleep(2)

            batch = await parse_results_table(page, "Illinois", lender, category)
            leads.extend(batch)
            log.info(f"  IL [{start_date}–{end_date}] {lender}: {len(batch)} results")

            if len(batch) == 0:
                break  # No results in this window

        except Exception as e:
            log.error(f"  IL window error: {e}")
            continue

    return leads

# ── IA Scraper ────────────────────────────────────────────────────────────────
async def scrape_ia(page: Page, lender: str, category: str) -> list:
    """Scrape Iowa SOS UCC for one lender."""
    leads = []
    IA_URL = "https://sos.iowa.gov/search/ucc/search.aspx"

    try:
        await page.goto(IA_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        form = await detect_form(page)
        log.debug(f"  IA form: {form}")

        # Select Secured Party
        if form["search_type_field"] and form["sp_value"]:
            sel_elem = await page.query_selector(
                f"select[name='{form['search_type_field']}'],"
                f"select[id='{form['search_type_field']}']"
            )
            if sel_elem:
                await sel_elem.select_option(value=form["sp_value"])
                await asyncio.sleep(0.5)
        else:
            # Try common IA patterns
            for sel_id in ["SearchType","searchType","ddlSearchType","Type"]:
                try:
                    sel_elem = await page.query_selector(
                        f"select[name='{sel_id}'],select[id='{sel_id}']"
                    )
                    if sel_elem:
                        await sel_elem.select_option(label="Secured Party")
                        break
                except:
                    pass

        # Fill name
        name_input = await page.query_selector("input[type='text']")
        if name_input:
            await name_input.fill(lender)

        # Submit
        submit = await page.query_selector(
            "input[type='submit'],button[type='submit'],button:has-text('Search')"
        )
        if submit:
            await submit.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            await asyncio.sleep(2)

        # Handle pagination
        while True:
            batch = await parse_results_table(page, "Iowa", lender, category)
            leads.extend(batch)

            # Check for next page
            next_btn = await page.query_selector(
                "a:has-text('Next'),input[value='Next'],button:has-text('Next >'),a[rel='next']"
            )
            if next_btn and len(batch) > 0:
                await next_btn.click()
                await page.wait_for_load_state("networkidle", timeout=10000)
                await asyncio.sleep(1.5)
            else:
                break

        log.info(f"  IA {lender}: {len(leads)} total results")

    except Exception as e:
        log.error(f"  IA error: {e}")

    return leads

# ── Results Parser ────────────────────────────────────────────────────────────
async def parse_results_table(page: Page, state_name: str, lender: str, category: str) -> list:
    """Generic results table parser — handles different column orders."""
    leads = []

    # Check for "no results" message
    content = await page.content()
    if any(p in content.lower() for p in ["no records","no results","0 record","not found"]):
        return leads

    rows = await page.query_selector_all("table tr, tbody tr")
    if not rows:
        return leads

    # Get header to map columns
    col_map = {}
    header_row = rows[0]
    headers = [await h.inner_text() for h in await header_row.query_selector_all("th,td")]
    for i, h in enumerate(headers):
        h_lower = h.lower()
        if any(k in h_lower for k in ["debtor","company","name"]) and "secured" not in h_lower:
            col_map["debtor"] = i
        elif "secured" in h_lower:
            col_map["secured"] = i
        elif "file" in h_lower and "number" in h_lower or "file #" in h_lower:
            col_map["file_id"] = i
        elif "lapse" in h_lower or "expir" in h_lower:
            col_map["lapse"] = i
        elif "filing" in h_lower or "file date" in h_lower or "filed" in h_lower:
            col_map["filing"] = i
        elif "status" in h_lower:
            col_map["status"] = i
        elif "city" in h_lower:
            col_map["city"] = i

    state_abbr = "IL" if state_name == "Illinois" else "IA"
    prefix     = f"{state_abbr}-"

    for row in rows[1:]:
        cells = await row.query_selector_all("td")
        if len(cells) < 2:
            continue
        texts = [await c.inner_text() for c in cells]
        if not texts or not texts[0].strip():
            continue

        # Skip header-like rows
        if any(h in texts[0].lower() for h in ["debtor","secured","name","file"]):
            continue

        def get(key, default=""):
            idx = col_map.get(key)
            return texts[idx].strip() if idx is not None and idx < len(texts) else default

        # If no column map built, fall back to positional
        if not col_map:
            file_id  = texts[0].strip() if len(texts) > 0 else ""
            company  = texts[1].strip() if len(texts) > 1 else ""
            sp       = texts[2].strip() if len(texts) > 2 else lender
            filing   = texts[3].strip() if len(texts) > 3 else ""
            lapse    = texts[4].strip() if len(texts) > 4 else ""
            city_val = ""
        else:
            file_id  = get("file_id")
            company  = get("debtor")
            sp       = get("secured", lender)
            filing   = get("filing")
            lapse    = get("lapse")
            city_val = get("city")

        if not company or len(company) < 2:
            continue

        filing_iso = to_iso(filing)
        lapse_iso  = to_iso(lapse)
        dtl        = days_to_lapse(lapse_iso)
        px         = estimate_paydex(lender, dtl)

        leads.append({
            "id":           f"{prefix}{file_id or company[:20]}",
            "source_state": state_name,
            "file_id":      file_id,
            "company_name": company,
            "address":      "",
            "city":         city_val,
            "state":        state_abbr,
            "zipcode":      "",
            "secured_party": sp or lender,
            "collateral":   f"Equipment Financing ({lender[:40]})",
            "filing_date":  filing_iso,
            "lapse_date":   lapse_iso,
            "days_to_lapse": dtl,
            "tech_company":  "true" if category in ("IT_OEM","PRINT_IMAGING") else "false",
            "tech_category": category,
            "tech_reason":  f"Lender: {lender}",
            "paydex_score":  px,
        })

    return leads

# ── Main ──────────────────────────────────────────────────────────────────────
async def main(states: list):
    init_db()
    grand_total = 0

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
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        for state_code in states:
            portal  = PORTALS[state_code]
            log.info(f"\n{'='*60}")
            log.info(f"Starting {portal['name']} UCC sweep")
            log.info(f"{'='*60}")

            log.info(f"Opening {portal['url']} in browser...")
            await page.goto(portal["url"], wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(4)

            # Print detected form fields
            form = await detect_form(page)
            log.info(f"\nDetected form fields:")
            log.info(f"  Search type select: {form['search_type_field']} (SP value: {form['sp_value']})")
            log.info(f"  Name input: {form['name_field']}")
            log.info(f"  Submit: {form['submit']}")
            log.info(f"  Select options: {[o for s in form['selects'] for o in s['options'][:5]]}")

            # Pause for user to confirm
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, input,
                f"\n>>> [{portal['name']}] Confirm the search form is visible and "
                f"ready, then press ENTER to begin the lender sweep...\n"
            )

            state_total = 0
            state_new   = 0

            for i, (lender, category) in enumerate(LENDERS, 1):
                log.info(f"\n[{i}/{len(LENDERS)}] {lender}")

                if state_code == "IL":
                    leads = await scrape_il(page, lender, category)
                else:
                    leads = await scrape_ia(page, lender, category)

                new_cnt = sum(1 for l in leads if save_lead(l))
                state_total += len(leads)
                state_new   += new_cnt
                log.info(f"  Found: {len(leads)} | New: {new_cnt} | State total: {state_new}")
                await asyncio.sleep(1.5)

            log.info(f"\n{portal['name']} complete: {state_total} found, {state_new} new")
            grand_total += state_new

        log.info(f"\n{'='*60}")
        log.info(f"ALL COMPLETE — {grand_total} new leads added")
        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IL + IA UCC Scraper")
    parser.add_argument("--state", choices=["IL","IA","ALL"], default="ALL",
                        help="State to scrape (default: ALL)")
    args   = parser.parse_args()
    states = ["IL","IA"] if args.state == "ALL" else [args.state]
    asyncio.run(main(states))
