"""
Tomcat Capex — Georgia UCC Scraper (Playwright + GSCCCA)
/Users/robertle/tomcat_capex/scrapers/ga_ucc_scraper.py

Data source: Georgia Superior Court Clerks' Cooperative Authority (GSCCCA)
Search URL:  https://search.gsccca.org/UCC_Search/search.asp?searchtype=SecuredParty
Auth:        Cookie-based (export from Chrome via export_gsccca_cookies.py)

SETUP:
  1. Log into https://www.gsccca.org in Chrome
  2. python3 scrapers/export_gsccca_cookies.py   ← run ONCE to save session
  3. python3 scrapers/ga_ucc_scraper.py --lenders 1 --dry-run
  4. python3 scrapers/ga_ucc_scraper.py           ← full run

Key design decisions:
  - Cookie injection: avoids CAPTCHA on apps.gsccca.org bot-detection login
  - page.set_default_timeout(4000): prevents 30s hangs on missing elements
  - Navigate fresh per window: clean state each search
  - Filing links as discriminator: a[href*='SecuredResults'] only in real results
  - 2yr lookback × 6mo windows = 4 searches per lender
"""

import os, sys, time, json, sqlite3, logging, argparse
from datetime import datetime, timedelta

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH     = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')
COOKIE_FILE = os.path.join(BASE_DIR, 'leads', 'gsccca_cookies.json')
LOG_DIR     = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [GA-UCC] %(levelname)s - %(message)s'
)
log = logging.getLogger("TomcatCapex.GA_UCC")

GSCCCA_SEARCH_URL = "https://search.gsccca.org/UCC_Search/search.asp?searchtype=SecuredParty"
PAGE_TIMEOUT_MS   = 4000    # 4s max per selector operation — no 30s hangs
NAV_TIMEOUT_MS    = 30000   # 30s for page navigation only
LOOKBACK_YEARS    = 6       # 6yr × 6mo windows = 12 searches per lender

# ── Lender list ────────────────────────────────────────────────────────────────
GA_LENDERS = [
    ("DELL FINANCIAL SERVICES",          "IT_OEM"),
    ("HEWLETT PACKARD",                  "IT_OEM"),
    ("HP FINANCIAL SERVICES",            "IT_OEM"),
    ("IBM CREDIT",                       "IT_OEM"),
    ("CISCO SYSTEMS CAPITAL",            "IT_OEM"),
    ("KONICA MINOLTA",                   "PRINT_IMAGING"),
    ("XEROX FINANCIAL",                  "PRINT_IMAGING"),
    ("CANON FINANCIAL SERVICES",         "PRINT_IMAGING"),
    ("RICOH USA",                        "PRINT_IMAGING"),
    ("KYOCERA DOCUMENT SOLUTIONS",       "PRINT_IMAGING"),
    ("GREATAMERICA FINANCIAL",           "IT_CHANNEL"),
    ("MARLIN LEASING",                   "IT_CHANNEL"),
    ("LEAF COMMERCIAL CAPITAL",          "IT_CHANNEL"),
    ("BALBOA CAPITAL",                   "IT_CHANNEL"),
    ("PAWNEE LEASING",                   "IT_CHANNEL"),
    ("DLL FINANCE",                      "EQUIP_FINANCE"),
    ("DE LAGE LANDEN",                   "EQUIP_FINANCE"),
    ("WELLS FARGO EQUIPMENT FINANCE",    "EQUIP_FINANCE"),
    ("US BANCORP EQUIPMENT FINANCE",     "EQUIP_FINANCE"),
    ("KEY EQUIPMENT FINANCE",            "EQUIP_FINANCE"),
    ("STEARNS BANK",                     "EQUIP_FINANCE"),
    ("BANC OF AMERICA LEASING",          "EQUIP_FINANCE"),
    ("CIT BANK",                         "EQUIP_FINANCE"),
    ("CATERPILLAR FINANCIAL",            "HEAVY_EQUIP"),
    ("JOHN DEERE FINANCIAL",             "HEAVY_EQUIP"),
    ("CNH INDUSTRIAL CAPITAL",           "HEAVY_EQUIP"),
    ("TOYOTA INDUSTRIES COMMERCIAL",     "HEAVY_EQUIP"),
]


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
    q = "SELECT COUNT(*) FROM ucc_leads WHERE source_state=?" if state else "SELECT COUNT(*) FROM ucc_leads"
    c = conn.execute(q, [state] if state else []).fetchone()[0]
    conn.close()
    return c


def parse_date(s: str) -> tuple:
    if not s or s.strip() in ('', 'N/A', '--', 'None'):
        return '', None
    s = s.strip()
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m-%d-%Y', '%m/%d/%y'):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime('%Y-%m-%d'), (dt - datetime.now()).days
        except ValueError:
            continue
    return s[:10], None


# ── Session ────────────────────────────────────────────────────────────────────

def load_session_cookies(context) -> bool:
    """
    Inject Chrome-exported GSCCCA cookies into Playwright context.
    Run export_gsccca_cookies.py once while logged into Chrome.
    Cookie injection bypasses the CAPTCHA on apps.gsccca.org.
    """
    if not os.path.exists(COOKIE_FILE):
        log.error(f"❌ Cookie file not found: {COOKIE_FILE}")
        log.error("   Run: python3 scrapers/export_gsccca_cookies.py")
        return False

    try:
        with open(COOKIE_FILE) as f:
            cookies = json.load(f)

        gsccca_cookies = [c for c in cookies if 'gsccca.org' in c.get('domain', '')]
        if not gsccca_cookies:
            log.error("Cookie file has no gsccca.org cookies — re-export from Chrome")
            return False

        context.add_cookies(gsccca_cookies)
        log.info(f"✅ Loaded {len(gsccca_cookies)} GSCCCA session cookies")
        return True

    except Exception as e:
        log.error(f"Cookie load failed: {e}")
        return False


def verify_session(page) -> bool:
    """Navigate to search page and confirm we're authenticated (not on login)."""
    try:
        page.goto(GSCCCA_SEARCH_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        time.sleep(2)
        content = page.content()
        if "SecuredPartyOrganizationName" in content or "Organization Name" in content:
            log.info("✅ Session verified — search form accessible")
            return True
        elif "txtUserID" in content or "frmLogin" in content:
            log.error("❌ Session invalid — hit login wall. Re-export cookies from Chrome.")
            return False
        else:
            log.warning(f"  Session unclear (URL: {page.url}) — proceeding")
            return True
    except Exception as e:
        log.error(f"Session verify error: {e}")
        return False


# ── Parse individual filing rows from a variation detail page ──────────────────

GSCCCA_BASE = "https://search.gsccca.org/UCC_Search/"

# Known non-data keywords that appear in GSCCCA nav/header cells
_SKIP_CELL_FRAGMENTS = {
    "display", "results per page", "select", "secured party name",
    "instruments", "variation", "page 1 of", "filing #", "filing date",
    "debtor", "type", "county", "secured party", "date filed",
}

def _is_junk_text(t: str) -> bool:
    tl = t.lower()
    return any(frag in tl for frag in _SKIP_CELL_FRAGMENTS)


def _parse_filing_rows(page, lender_name: str) -> list:
    """
    Parse individual filing rows from a variation detail page.
    GSCCCA filing list columns: [0]Filing# [1]Date [2]Debtor Name [3]Type [4]County
    Returns list of lead dicts.
    """
    leads = []
    try:
        rows = page.locator("tr").all()
    except Exception:
        return leads

    for row in rows:
        try:
            cells = row.locator("td").all()
            if len(cells) < 4:
                continue

            texts = [c.inner_text().strip() for c in cells]

            # Filing # is first cell — must look like a number or alphanumeric ID
            file_num = texts[0]
            try:
                lnk = cells[0].locator("a").first
                lt = lnk.inner_text().strip()
                if lt:
                    file_num = lt
            except Exception:
                pass

            filing_date_s = texts[1] if len(texts) > 1 else ""
            company_name  = texts[2] if len(texts) > 2 else ""
            county        = texts[4] if len(texts) > 4 else "GA"

            # Skip non-data rows
            if not file_num or not company_name:
                continue
            if _is_junk_text(file_num) or _is_junk_text(company_name):
                continue
            # Filing numbers on GSCCCA are typically all-digits
            if not any(c.isdigit() for c in file_num):
                continue
            # Company name must have letters
            if not any(c.isalpha() for c in company_name):
                continue

            filing_iso, _ = parse_date(filing_date_s.split()[0] if filing_date_s else "")

            lapse_iso, days_to_lapse = "", None
            if filing_iso:
                try:
                    lapse_dt      = datetime.strptime(filing_iso, "%Y-%m-%d") + timedelta(days=5 * 365)
                    lapse_iso     = lapse_dt.strftime("%Y-%m-%d")
                    days_to_lapse = (lapse_dt - datetime.now()).days
                except Exception:
                    pass

            leads.append({
                "id":            f"GA-{file_num}",
                "source_state":  "Georgia",
                "file_id":       file_num,
                "company_name":  company_name,
                "address":       "",
                "city":          county,
                "state":         "GA",
                "zipcode":       "",
                "secured_party": lender_name,
                "collateral":    f"Equipment Financing ({lender_name})",
                "filing_date":   filing_iso,
                "lapse_date":    lapse_iso,
                "days_to_lapse": days_to_lapse,
                "tech_company":  "true",
                "tech_category": "GA_EQUIPMENT",
                "tech_reason":   f"Lender: {lender_name}",
            })

        except Exception as e:
            log.debug(f"      Filing row error: {e}")
            continue

    return leads


# ── Search one window ──────────────────────────────────────────────────────────

def search_window(page, lender_name: str, win_start: str, win_end: str) -> list:
    """
    2-level GSCCCA scrape for one date window:
      Level 1: Submit secured-party search → securedresults.asp
               This is a SUMMARY page showing lender name *variations* with counts.
               Collect all variation links (non-nav relative hrefs).
      Level 2: Navigate to each variation's filing list → parse individual debtors.

    Returns [] on no results, None on session expiry.
    """
    leads = []
    try:
        log.info(f"    → {win_start}–{win_end}")
        page.goto(GSCCCA_SEARCH_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        time.sleep(2)

        if "apps.gsccca.org/login.asp" in page.url.lower():
            log.warning("    Session expired — redirected to login")
            return None

        content = page.content()
        if "SecuredPartyOrganizationName" not in content and "Organization Name" not in content:
            log.warning(f"    Unexpected page ({page.url})")
            return []

        log.info("    Form ✓")

        try:
            page.locator("input[name='securedsearch'][value='0']").check()
            time.sleep(0.3)
        except Exception:
            pass
        try:
            page.locator("input[name='SecuredPartyExact'][value='0']").check()
        except Exception:
            pass
        try:
            page.locator("input[name='SecuredPartyOrganizationName']").fill(lender_name)
        except Exception as e:
            log.error(f"    Fill failed: {e}")
            return []
        try:
            page.locator("input[name='FromDate']").fill(win_start)
            page.locator("input[name='ToDate']").fill(win_end)
        except Exception:
            pass
        try:
            page.locator("select[name='maxrows']").select_option("100")
        except Exception:
            pass
        try:
            page.locator("#btnSubmit").click()
        except Exception:
            page.keyboard.press("Enter")

        time.sleep(4)

        post_url = page.url
        log.info(f"    Summary URL: {post_url}")

        if "apps.gsccca.org/login.asp" in post_url.lower():
            log.warning("    Session expired after submit")
            return None

        # No results = stays at search.asp
        if "securedresults.asp" not in post_url.lower():
            log.info("    No results")
            return []

        # ── Level 1: Collect variation links from summary page ─────────────────
        # Variation links are relative hrefs like:
        #   securedresults.asp?SecuredPartyName=CANON+FINANCIAL...
        # They do NOT contain "UCC_Search" — that's only in sidebar nav paths.
        #
        # Exclude: sidebar nav (/CarbonRegistry/, /Lien/, /plat/, etc.)
        #          UCC nav (certified.asp, default.asp, search.asp forms)
        #          auth/account links (logout, Alerts, login, forgotpassword)
        #          javascript/hash links
        _EXCLUDE = {
            "CarbonRegistry", "Lien/", "liensearch", "notary/", "plat/",
            "pt61/", "PT61Premium", "RealEstate", "certified.asp",
            "certifiedHistory", "default.asp", "search.asp", "Alerts.asp",
            "logout", "forgotpassword", "SecuritySite", "javascript:",
            "sitemap", "glossary", "terms", "contact-us", "login.asp", "#",
        }
        variation_links = []
        try:
            all_links = page.locator("a").all()
            for lnk in all_links:
                try:
                    href = lnk.get_attribute("href") or ""
                    if not href or "?" not in href:
                        continue
                    # Skip known non-data patterns
                    if any(ex in href for ex in _EXCLUDE):
                        continue
                    # Skip external absolute URLs (not search.gsccca.org)
                    if href.startswith("http") and "search.gsccca.org" not in href:
                        continue
                    variation_links.append(href)
                except Exception:
                    continue
        except Exception:
            pass

        # Deduplicate while preserving order
        seen = set()
        unique_variations = []
        for h in variation_links:
            if h not in seen:
                seen.add(h)
                unique_variations.append(h)

        log.info(f"    {len(unique_variations)} variation links found")
        if unique_variations:
            log.debug(f"      Sample: {unique_variations[0]}")

        if not unique_variations:
            log.info("    No variation links — summary only, skipping")
            return []

        # ── Level 2: Navigate to each variation and parse individual filings ───
        for i, href in enumerate(unique_variations):
            try:
                if href.startswith("http"):
                    full_url = href
                else:
                    full_url = GSCCCA_BASE + href.lstrip("./")

                page.goto(full_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                time.sleep(2)

                if "apps.gsccca.org/login.asp" in page.url.lower():
                    log.warning("    Session expired on variation drilldown")
                    return None

                variation_leads = _parse_filing_rows(page, lender_name)
                log.debug(f"      Variation {i+1}: {len(variation_leads)} filings")
                leads.extend(variation_leads)

            except Exception as e:
                log.debug(f"      Variation {i+1} error: {e}")
                continue

        log.info(f"    Parsed {len(leads)} leads ({len(unique_variations)} variations)")

    except Exception as e:
        log.error(f"    Window error: {e}")

    return leads


    """
    Navigate to search page, fill form, submit, return list of lead dicts.
    Returns [] on no results, None on session expiry.
    Fast-fails via PAGE_TIMEOUT_MS (4s) — no 30s hangs.

    Confirmed field names (from live form inspection 2026-05-07):
      securedsearch[value='0']        = Organization radio
      SecuredPartyExact[value='0']    = Stem search
      SecuredPartyOrganizationName    = org name text input
      FromDate / ToDate               = date range mm/dd/yyyy
      maxrows                         = results per page select
      #btnSubmit                      = search button
    """
    leads = []
    try:
        log.info(f"    → {win_start}–{win_end}")
        page.goto(GSCCCA_SEARCH_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        time.sleep(2)

        # Check session still valid
        # Login wall = redirected to apps.gsccca.org login page
        # NOTE: frmLogin appears on ALL gsccca pages (nav element) — NOT a reliable signal
        if "apps.gsccca.org/login.asp" in page.url.lower():
            log.warning("    Session expired — redirected to login")
            return None  # Signal to re-authenticate

        content = page.content()
        if "Organization Name" not in content and "SecuredPartyOrganizationName" not in content:
            log.warning(f"    Unexpected page (URL: {page.url})")
            return []

        log.info("    Form ✓")

        # Organization radio (value '0' = Organization, confirmed)
        try:
            page.locator("input[name='securedsearch'][value='0']").check()
            time.sleep(0.3)
        except Exception:
            pass

        # Stem search (value '0' = Stem/partial, confirmed)
        try:
            page.locator("input[name='SecuredPartyExact'][value='0']").check()
        except Exception:
            pass

        # Org name (confirmed field name)
        try:
            page.locator("input[name='SecuredPartyOrganizationName']").fill(lender_name)
        except Exception as e:
            log.error(f"    Org name fill failed: {e}")
            return []

        # Date range (confirmed field names)
        try:
            page.locator("input[name='FromDate']").fill(win_start)
            page.locator("input[name='ToDate']").fill(win_end)
        except Exception:
            pass

        # Results per page (confirmed: select name='maxrows')
        try:
            page.locator("select[name='maxrows']").select_option("100")
        except Exception:
            pass

        # Submit (confirmed: id='btnSubmit')
        try:
            page.locator("#btnSubmit").click()
        except Exception:
            try:
                page.locator("input[value='Search']").first.click()
            except Exception:
                page.keyboard.press("Enter")

        time.sleep(4)

        post_url = page.url
        log.info(f"    Post-submit URL: {post_url}")

        # Session expired = redirected to apps.gsccca.org/login.asp
        if "apps.gsccca.org/login.asp" in post_url.lower():
            log.warning("    Session hit login wall after submit")
            return None

        # No results = URL stays at search.asp (GSCCCA only redirects to securedresults.asp when results exist)
        if "securedresults.asp" not in post_url.lower():
            log.info("    No results (URL stayed at search.asp)")
            return []

        # ── Parse results from table rows ─────────────────────────────────────
        # GSCCCA results table columns (confirmed):
        #   [0] Filing#   [1] Filing Date   [2] Debtor Name
        #   [3] Secured Party   [4] Type   [5] County
        # Parse every TR with ≥4 TD cells — no href pattern needed.
        try:
            all_rows = page.locator("tr").all()
            log.info(f"    {len(all_rows)} table rows on results page")
        except Exception:
            all_rows = []

        for row in all_rows:
            try:
                cells = row.locator("td").all()
                if len(cells) < 4:
                    continue  # skip header/nav rows

                texts = [c.inner_text().strip() for c in cells]

                # Get filing number — prefer link text in first cell
                file_num = texts[0]
                try:
                    lnk = cells[0].locator("a").first
                    lt = lnk.inner_text().strip()
                    if lt:
                        file_num = lt
                except Exception:
                    pass

                filing_date_s = texts[1] if len(texts) > 1 else ""
                company_name  = texts[2] if len(texts) > 2 else ""
                secured_party = texts[3] if len(texts) > 3 else lender_name
                county        = texts[5] if len(texts) > 5 else "GA"

                # Skip header rows
                skip_vals = {"filing #", "filing#", "number", "file #", "debtor", "name",
                             "debtor name", "secured party", "type", "county", ""}
                if file_num.lower() in skip_vals or company_name.lower() in skip_vals:
                    continue

                filing_iso, _ = parse_date(filing_date_s.split()[0] if filing_date_s else "")

                lapse_iso, days_to_lapse = "", None
                if filing_iso:
                    try:
                        lapse_dt      = datetime.strptime(filing_iso, "%Y-%m-%d") + timedelta(days=5 * 365)
                        lapse_iso     = lapse_dt.strftime("%Y-%m-%d")
                        days_to_lapse = (lapse_dt - datetime.now()).days
                    except Exception:
                        pass

                leads.append({
                    "id":            f"GA-{file_num}",
                    "source_state":  "Georgia",
                    "file_id":       file_num,
                    "company_name":  company_name,
                    "address":       "",
                    "city":          county,
                    "state":         "GA",
                    "zipcode":       "",
                    "secured_party": secured_party or lender_name,
                    "collateral":    f"Equipment Financing ({lender_name})",
                    "filing_date":   filing_iso,
                    "lapse_date":    lapse_iso,
                    "days_to_lapse": days_to_lapse,
                    "tech_company":  "true",
                    "tech_category": "GA_EQUIPMENT",
                    "tech_reason":   f"Lender: {lender_name}",
                })

            except Exception as e:
                log.debug(f"    Row error: {e}")
                continue

        log.info(f"    Parsed {len(leads)} leads from {len(all_rows)} rows")

    except Exception as e:
        log.error(f"    Window error: {e}")

    return leads



# ── Main ───────────────────────────────────────────────────────────────────────

def scrape_ga_uccs(lenders=None, dry_run=False):
    from playwright.sync_api import sync_playwright

    if lenders is None:
        lenders = GA_LENDERS

    # Chrome Profile 2 is where GSCCCA session lives (confirmed from cookie search)
    CHROME_PROFILE = os.path.expanduser(
        "~/Library/Application Support/Google/Chrome/Profile 2"
    )

    total_found = 0
    total_new   = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        # Inject live session cookies (captured from Chrome DevTools)
        if not load_session_cookies(context):
            browser.close()
            return 0

        # Verify session
        if not verify_session(page):
            log.error("Session not valid — re-copy cookies from Chrome DevTools.")
            browser.close()
            return 0

        # ── Build date windows (2yr lookback, 6mo chunks) ─────────────────────
        now = datetime.now()
        windows = []
        for i in range(0, LOOKBACK_YEARS * 12, 6):
            start = now - timedelta(days=30 * (i + 6))
            end   = now - timedelta(days=30 * i)
            windows.append((start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y")))
        log.info(f"Date windows: {len(windows)} × 6mo = {LOOKBACK_YEARS}yr lookback")

        # ── Per-lender loop ───────────────────────────────────────────────────
        for lender_name, tech_category in lenders:
            log.info(f"\n{'─'*60}")
            log.info(f"🔍 {lender_name} ({tech_category})")

            lender_total = 0
            lender_new   = 0

            for win_start, win_end in windows:
                result = search_window(page, lender_name, win_start, win_end)

                if result is None:
                    log.error("  Session expired — log back into GSCCCA in Chrome Profile 2 and re-run")
                    browser.close()
                    return total_new

                lender_total += len(result)
                if not dry_run:
                    for lead in result:
                        if save_lead(lead):
                            lender_new += 1

                time.sleep(1.5)

            total_found += lender_total
            total_new   += lender_new
            log.info(f"  → {lender_total} found, {lender_new} new")
            time.sleep(2)

        browser.close()

    log.info(f"\n{'='*60}")
    log.info(f"GA Scrape Complete: {total_found:,} found | {total_new:,} new | {get_lead_count('Georgia'):,} total in DB")
    log.info(f"{'='*60}")
    return total_new


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Georgia UCC Scraper (GSCCCA)")
    parser.add_argument("--lenders", type=int, default=0, help="Max lenders (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    args = parser.parse_args()

    init_db()
    lenders = GA_LENDERS[:args.lenders] if args.lenders else None
    scrape_ga_uccs(lenders=lenders, dry_run=args.dry_run)
