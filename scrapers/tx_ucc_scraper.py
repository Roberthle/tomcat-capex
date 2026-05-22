"""
Tomcat Capex — Texas UCC Scraper (Playwright + SOSDirect)
/Users/robertle/tomcat_capex/scrapers/tx_ucc_scraper.py

Data source: Texas Secretary of State — SOSDirect
URL:  https://direct.sos.state.tx.us
Cost: $1.00 per search (statutory fee — NOT free)

Strategy:
  Texas does not publish UCC data via open API.
  The SOSDirect portal charges $1.00 per search query.
  We search by Secured Party Organization name with date windows.
  At 31 lenders × weekly runs = ~$31/week = ~$124/month.
  For daily runs = ~$930/month — use weekly cron instead.

  Requires a SOSDirect account:
  https://direct.sos.state.tx.us → Create Account
  Set env vars: SOS_TX_USER and SOS_TX_PASS

Run: python3 tx_ucc_scraper.py [--lenders N] [--dry-run]
"""

import os, re, sys, time, json, sqlite3, logging, argparse
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')
LOG_DIR  = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [TX-UCC] %(levelname)s - %(message)s'
)
log = logging.getLogger("TomcatCapex.TX_UCC")

TX_UCC_URL    = "https://direct.sos.state.tx.us"
TX_LOGIN_URL  = "https://direct.sos.state.tx.us/account/login"

# ── Lender list ────────────────────────────────────────────────────────────────
# Tech + Equipment finance lenders with active TX presence
TX_LENDERS = [
    # Tech / Print OEM
    ("DELL FINANCIAL SERVICES",          "IT_OEM"),
    ("HEWLETT PACKARD",                  "IT_OEM"),
    ("HP FINANCIAL SERVICES",            "IT_OEM"),
    ("LENOVO FINANCIAL",                 "IT_OEM"),
    ("IBM CREDIT",                       "IT_OEM"),
    ("CISCO SYSTEMS CAPITAL",            "IT_OEM"),
    ("KONICA MINOLTA",                   "PRINT_IMAGING"),
    ("XEROX FINANCIAL",                  "PRINT_IMAGING"),
    ("CANON FINANCIAL SERVICES",         "PRINT_IMAGING"),
    ("RICOH USA",                        "PRINT_IMAGING"),
    ("KYOCERA DOCUMENT SOLUTIONS",       "PRINT_IMAGING"),
    # IT Channel / Finance
    ("GREATAMERICA FINANCIAL",           "IT_CHANNEL"),
    ("MARLIN LEASING",                   "IT_CHANNEL"),
    ("MARLIN BUSINESS SERVICES",         "IT_CHANNEL"),
    ("LEAF COMMERCIAL CAPITAL",          "IT_CHANNEL"),
    ("BALBOA CAPITAL",                   "IT_CHANNEL"),
    ("PAWNEE LEASING",                   "IT_CHANNEL"),
    # Equipment Finance (broad)
    ("DLL FINANCE",                      "EQUIP_FINANCE"),
    ("DE LAGE LANDEN",                   "EQUIP_FINANCE"),
    ("WELLS FARGO EQUIPMENT FINANCE",    "EQUIP_FINANCE"),
    ("US BANCORP EQUIPMENT FINANCE",     "EQUIP_FINANCE"),
    ("KEY EQUIPMENT FINANCE",            "EQUIP_FINANCE"),
    ("STEARNS BANK",                     "EQUIP_FINANCE"),
    ("BANC OF AMERICA LEASING",          "EQUIP_FINANCE"),
    ("CIT BANK",                         "EQUIP_FINANCE"),
    # Heavy Equipment OEM
    ("CATERPILLAR FINANCIAL",            "HEAVY_EQUIP"),
    ("JOHN DEERE FINANCIAL",             "HEAVY_EQUIP"),
    ("CNH INDUSTRIAL CAPITAL",           "HEAVY_EQUIP"),
    ("KOMATSU FINANCIAL",                "HEAVY_EQUIP"),
    ("TOYOTA INDUSTRIES COMMERCIAL",     "HEAVY_EQUIP"),
    ("HYSTER CREDIT",                    "HEAVY_EQUIP"),
    ("CROWN CREDIT",                     "HEAVY_EQUIP"),
]

EXPIRY_WINDOW_MAX_DAYS = 365  # TX: pull up to 1yr out, filter in portal


# ── DB ─────────────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ucc_leads (
            id TEXT PRIMARY KEY, source_state TEXT NOT NULL,
            file_id TEXT NOT NULL, company_name TEXT, address TEXT,
            city TEXT, state TEXT, zipcode TEXT, secured_party TEXT,
            collateral TEXT, filing_date TEXT, lapse_date TEXT,
            days_to_lapse INTEGER, status TEXT DEFAULT 'new',
            routed_to TEXT, routed_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(source_state, file_id)
        )
    """)
    # tech columns (for CA/TX tech-lender scrapers)
    for col in ["tech_company TEXT", "tech_category TEXT", "tech_reason TEXT"]:
        try:
            conn.execute(f"ALTER TABLE ucc_leads ADD COLUMN {col}")
        except Exception:
            pass
    conn.commit()
    conn.close()


def save_lead(lead: dict) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT OR IGNORE INTO ucc_leads
            (id, source_state, file_id, company_name, address, city, state,
             zipcode, secured_party, collateral, filing_date, lapse_date,
             days_to_lapse, tech_company, tech_category, tech_reason)
            VALUES (:id, :source_state, :file_id, :company_name, :address,
                    :city, :state, :zipcode, :secured_party, :collateral,
                    :filing_date, :lapse_date, :days_to_lapse,
                    :tech_company, :tech_category, :tech_reason)
        """, lead)
        inserted = conn.total_changes > 0
        conn.commit()
        return inserted
    except Exception as e:
        log.error(f"DB error: {e}")
        return False
    finally:
        conn.close()


def get_lead_count(state=None):
    conn = sqlite3.connect(DB_PATH)
    if state:
        c = conn.execute("SELECT COUNT(*) FROM ucc_leads WHERE source_state=?", [state]).fetchone()[0]
    else:
        c = conn.execute("SELECT COUNT(*) FROM ucc_leads").fetchone()[0]
    conn.close()
    return c


# ── Date parsing ───────────────────────────────────────────────────────────────

def parse_date(s: str) -> tuple:
    """Returns (iso_str, days_to_lapse). Handles MM/DD/YYYY."""
    if not s or s.strip() in ('', 'N/A', '--'):
        return '', None
    s = s.strip()
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m-%d-%Y'):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime('%Y-%m-%d'), (dt - datetime.now()).days
        except ValueError:
            continue
    return s[:10], None


def tx_login(page) -> bool:
    """
    Log into SOSDirect. Returns True on success.
    Reads credentials from SOS_TX_USER / SOS_TX_PASS env vars.
    NOTE: Texas charges $1.00 per UCC search query.
    """
    username = os.environ.get("SOS_TX_USER", "")
    password = os.environ.get("SOS_TX_PASS", "")

    if not username or not password:
        log.error("❌ TX SOSDirect credentials not set.")
        log.error("   1. Create account at: https://direct.sos.state.tx.us")
        log.error("   2. export SOS_TX_USER=your@email.com")
        log.error("   3. export SOS_TX_PASS=yourpassword")
        log.error("   NOTE: Texas charges $1.00 per search query.")
        return False

    log.info(f"Logging in to SOSDirect as {username} ...")
    try:
        page.goto(TX_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        for sel in ["input[name='email']", "input[id*='email']", "input[type='email']", "input[type='text']"]:
            try:
                u = page.locator(sel).first
                if u.count() > 0 and u.is_visible():
                    u.fill(username)
                    break
            except Exception:
                continue

        for sel in ["input[name='password']", "input[type='password']"]:
            try:
                pw = page.locator(sel).first
                if pw.count() > 0 and pw.is_visible():
                    pw.fill(password)
                    break
            except Exception:
                continue

        for sel in ["button[type='submit']", "input[type='submit']",
                    "button:has-text('Sign In')", "button:has-text('Login')"]:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0:
                    btn.click()
                    break
            except Exception:
                continue

        time.sleep(3)
        page_text = page.content().lower()
        if "logout" in page_text or "sign out" in page_text or "dashboard" in page_text:
            log.info("✅ SOSDirect login successful")
            return True
        elif "invalid" in page_text or "incorrect" in page_text:
            log.error("❌ SOSDirect login failed — check credentials")
            return False
        else:
            log.info("✅ SOSDirect login assumed successful")
            return True
    except Exception as e:
        log.error(f"❌ Login error: {e}")
        return False


def scrape_tx_uccs(lenders=None, date_range_months=6, dry_run=False):
    """
    Scrape Texas UCC filings for target lenders via SOSDirect.
    NOTE: Texas charges $1.00 per search. Use --lenders N to limit cost.
    Requires SOS_TX_USER and SOS_TX_PASS env vars.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    if lenders is None:
        lenders = TX_LENDERS

    total_new   = 0
    total_found = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        # ── Login ─────────────────────────────────────────────────────────────
        if not tx_login(page):
            browser.close()
            return 0

        # ── Navigate to UCC search ────────────────────────────────────────────
        try:
            # SOSDirect dashboard — navigate to UCC search section
            page.goto(TX_UCC_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)

            # Find and click UCC search link on dashboard
            for selector in [
                "a:has-text('Uniform Commercial Code')",
                "a:has-text('UCC')",
                "a[href*='ucc']",
            ]:
                try:
                    link = page.locator(selector).first
                    if link.count() > 0 and link.is_visible():
                        link.click()
                        time.sleep(2)
                        break
                except Exception:
                    continue

        except PWTimeout:
            log.error("❌ SOSDirect portal timed out")
            browser.close()
            return 0

        for lender_name, tech_category in lenders:
            log.info(f"\n{'─'*60}")
            log.info(f"🔍 Searching TX for: {lender_name} ({tech_category})")

            # Build 6-month date windows, 5 years back
            now = datetime.now()
            windows = []
            for i in range(0, 60, date_range_months):
                start = now - timedelta(days=30 * (i + date_range_months))
                end   = now - timedelta(days=30 * i)
                windows.append((
                    start.strftime("%m/%d/%Y"),
                    end.strftime("%m/%d/%Y")
                ))

            lender_total = 0
            lender_new   = 0

            for win_start, win_end in windows:
                try:
                    # Navigate fresh for each window to reset VIEWSTATE
                    page.goto(TX_UCC_URL, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(2)

                    # ── Select "Secured Party Organization" ───────────────────
                    # TX uses radio buttons — try multiple selector patterns
                    sp_org_selected = False
                    for selector in [
                        "input[value*='SecPartyOrg']",
                        "input[value*='SPOrg']",
                        "input[id*='SecuredParty'][value*='Org']",
                        "input[id*='rbSPOrg']",
                        "input[id*='SP'][type='radio']",
                    ]:
                        try:
                            btn = page.locator(selector).first
                            if btn.count() > 0:
                                btn.click()
                                sp_org_selected = True
                                time.sleep(0.3)
                                break
                        except Exception:
                            continue

                    if not sp_org_selected:
                        # Try clicking any radio near "Secured Party" text
                        try:
                            sp_label = page.locator("label:has-text('Secured Party')").first
                            if sp_label.count() > 0:
                                # Click its associated radio via for= attribute
                                for_id = sp_label.get_attribute("for")
                                if for_id:
                                    page.locator(f"#{for_id}").click()
                                    sp_org_selected = True
                                    time.sleep(0.3)
                        except Exception:
                            pass

                    if not sp_org_selected:
                        log.warning("  ⚠️  Could not select Secured Party radio — trying plain search")

                    # ── Fill organization name ────────────────────────────────
                    name_filled = False
                    for selector in [
                        "input[id*='OrgName']",
                        "input[id*='orgName']",
                        "input[name*='OrgName']",
                        "input[id*='Name'][type='text']",
                        "input[type='text']:visible",
                    ]:
                        try:
                            inp = page.locator(selector).first
                            if inp.count() > 0 and inp.is_visible():
                                inp.fill(lender_name)
                                name_filled = True
                                break
                        except Exception:
                            continue

                    if not name_filled:
                        log.error(f"  ❌ Could not fill name field for {lender_name}")
                        break

                    # ── Date range (if available) ─────────────────────────────
                    try:
                        date_inputs = page.locator("input[placeholder*='MM/DD']")
                        if date_inputs.count() >= 2:
                            date_inputs.nth(0).fill(win_start)
                            date_inputs.nth(1).fill(win_end)
                    except Exception:
                        pass  # Date filter not available on this portal version

                    # ── Submit ────────────────────────────────────────────────
                    search_clicked = False
                    for selector in [
                        "input[type='submit'][value*='Search']",
                        "button:has-text('Search')",
                        "input[id*='btnSearch']",
                        "input[value='Search']",
                    ]:
                        try:
                            btn = page.locator(selector).first
                            if btn.count() > 0:
                                btn.click()
                                search_clicked = True
                                break
                        except Exception:
                            continue

                    if not search_clicked:
                        log.warning("  ⚠️  Could not find Search button — trying Enter key")
                        page.keyboard.press("Enter")

                    time.sleep(4)

                    # ── Wait for results ──────────────────────────────────────
                    try:
                        page.wait_for_selector("table", timeout=12000)
                    except PWTimeout:
                        log.debug(f"  No results table ({win_start} – {win_end})")
                        continue

                    # ── Parse results table ───────────────────────────────────
                    rows = page.locator("table tbody tr").all()
                    if not rows:
                        rows = page.locator("table tr").all()[1:]  # skip header

                    batch_count = len(rows)
                    if batch_count == 0:
                        continue

                    log.info(f"  {win_start}–{win_end}: {batch_count} rows")

                    for row in rows:
                        cells = row.locator("td").all()
                        if len(cells) < 4:
                            continue
                        try:
                            texts = [c.inner_text().strip() for c in cells]

                            # TX table columns (typical order):
                            # [0] File Number  [1] Debtor Name  [2] Secured Party
                            # [3] Filing Date  [4] Lapse Date   [5] Type
                            # Some portals have slightly different ordering — be resilient
                            file_num      = texts[0] if len(texts) > 0 else ''
                            company_name  = texts[1] if len(texts) > 1 else ''
                            secured_party = texts[2] if len(texts) > 2 else lender_name
                            filing_date_s = texts[3] if len(texts) > 3 else ''
                            lapse_date_s  = texts[4] if len(texts) > 4 else ''

                            # Skip header rows or blank rows
                            if not file_num or file_num.lower() in ('file number', '#', 'no.', ''):
                                continue
                            if not company_name or company_name.lower() in ('debtor', 'name', ''):
                                continue

                            filing_iso, _        = parse_date(filing_date_s)
                            lapse_iso, days_left = parse_date(lapse_date_s)

                            lead = {
                                "id":            f"TX-{file_num}",
                                "source_state":  "Texas",
                                "file_id":       file_num,
                                "company_name":  company_name,
                                "address":       "",
                                "city":          "",
                                "state":         "TX",
                                "zipcode":       "",
                                "secured_party": secured_party or lender_name,
                                "collateral":    f"Tech Equipment ({lender_name})",
                                "filing_date":   filing_iso,
                                "lapse_date":    lapse_iso,
                                "days_to_lapse": days_left,
                                "tech_company":  "true",
                                "tech_category": tech_category,
                                "tech_reason":   f"Tech lender: {lender_name}",
                            }

                            lender_total += 1
                            if not dry_run and save_lead(lead):
                                lender_new += 1

                        except Exception as e:
                            log.debug(f"  Row parse error: {e}")
                            continue

                    if batch_count >= 500:
                        log.warning(f"  ⚠️  May have hit result cap — consider smaller windows")

                except Exception as e:
                    log.error(f"  Search error ({win_start}–{win_end}): {e}")
                    continue

                time.sleep(2)  # Polite delay between windows

            total_found += lender_total
            total_new   += lender_new
            log.info(f"  TX {lender_name}: {lender_total} found, {lender_new} new")

            # Brief pause between lenders
            time.sleep(3)

        browser.close()

    log.info(f"\n{'='*60}")
    log.info(f"  Texas UCC Scrape Complete")
    log.info(f"  Lenders searched : {len(lenders)}")
    log.info(f"  Total found      : {total_found:,}")
    log.info(f"  New leads saved  : {total_new:,}")
    log.info(f"  Total TX in DB   : {get_lead_count('Texas'):,}")
    log.info(f"{'='*60}")

    return total_new


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Texas UCC Scraper (SOSDirect — $1.00/search)"
    )
    parser.add_argument("--lenders", type=int, default=0,
                        help="Max lenders to process (0 = all). Each lender = $1+ in TX search fees.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scrape but don't write to DB")
    args = parser.parse_args()

    if not os.environ.get("SOS_TX_USER"):
        print("\n⚠️  Texas SOSDirect credentials required.")
        print("   Texas charges $1.00 per UCC search query.")
        print("   1. Create account at: https://direct.sos.state.tx.us")
        print("   2. export SOS_TX_USER=your@email.com")
        print("   3. export SOS_TX_PASS=yourpassword")
        print("   4. Run again\n")
        import sys; sys.exit(1)

    init_db()
    lenders = TX_LENDERS[:args.lenders] if args.lenders else None
    scrape_tx_uccs(lenders=lenders, dry_run=args.dry_run)
