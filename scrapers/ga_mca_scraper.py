"""
ga_mca_scraper.py
Georgia GSCCCA UCC Scraper — MCA / Revenue-Based Finance Edition
Same HTTP engine as ga_ucc_scraper_v2.py.
Targets known MCA and alternative finance lenders that file UCC-1s in GA.
Writes to tomcat_mca.db (mca_leads table).

Run:
  python3 scrapers/ga_mca_scraper.py            # full run
  python3 scrapers/ga_mca_scraper.py --dry-run  # no DB write
"""

import requests, re, sqlite3, os, sys, time, logging, argparse
from datetime import datetime, timedelta

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCA_DB_PATH  = os.path.join(os.path.dirname(BASE_DIR), "tomcat_mca", "leads", "tomcat_mca.db")
LOG_DIR      = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

USERNAME  = "tomcatmca"
PASSWORD  = "Openclaw26"
STATE     = "Georgia"

SEARCH_URL  = "https://search.gsccca.org/UCC_Search/search.asp"
RESULTS_URL = "https://search.gsccca.org/UCC_Search/securedresults.asp"
OCCUR_URL   = "https://search.gsccca.org/UCC_Search/occurrences.asp"
LOGIN_URL   = "https://apps.gsccca.org/login.asp"

REQ_TIMEOUT  = 90
INTER_DELAY  = 1.5

# 5-year lookback, 6-month windows — MCA lenders filed heavily 2021-2023
def date_windows(years=5):
    end = datetime.now()
    windows = []
    for i in range(years * 2):
        win_end   = end - timedelta(days=i * 182)
        win_start = win_end - timedelta(days=182)
        windows.append((win_start.strftime("%m/%d/%Y"), win_end.strftime("%m/%d/%Y")))
    return windows

# ── MCA / Alt-Finance Lender List ─────────────────────────────────────────────
# These are the known MCA, revenue-based, and alternative finance companies
# that file UCC-1 financing statements in Georgia.
GA_MCA_LENDERS = [
    # Big MCA Players
    ("ONDECK CAPITAL",              "MCA"),
    ("ON DECK CAPITAL",             "MCA"),
    ("RAPID ADVANCE",               "MCA"),
    ("RAPID FINANCE",               "MCA"),
    ("LIBERTAS FUNDING",            "MCA"),
    ("FORWARD FINANCING",           "MCA"),
    ("KAPITUS",                     "MCA"),
    ("NATIONAL FUNDING",            "MCA"),
    ("YELLOWSTONE CAPITAL",         "MCA"),
    ("KALAMATA CAPITAL",            "MCA"),
    ("CREDIBLY",                    "MCA"),
    ("RETAIL CAPITAL",              "MCA"),
    ("CAN CAPITAL",                 "MCA"),
    ("IOU FINANCIAL",               "MCA"),
    ("RELIANT FUNDING",             "MCA"),
    ("SAMSON MCA",                  "MCA"),
    ("ITRIA VENTURES",              "MCA"),
    ("BUSINESS BACKER",             "MCA"),
    ("CURRENCY CAPITAL",            "MCA"),
    ("NEWTEK BUSINESS",             "MCA"),
    ("QUARTERSPOT",                 "MCA"),
    ("WELLEN CAPITAL",              "MCA"),

    # Fintech / Embedded Finance
    ("KABBAGE",                     "FINTECH"),
    ("FUNDBOX",                     "FINTECH"),
    ("BLUEVINE CAPITAL",            "FINTECH"),
    ("CLEARCO",                     "FINTECH"),
    ("PIPE TECHNOLOGIES",           "FINTECH"),
    ("YARDLINE",                    "FINTECH"),
    ("CAPCHASE",                    "FINTECH"),

    # Bank-backed Alt Finance
    ("AMERICAN EXPRESS BUSINESS",   "BANK_ALT"),
    ("SWIFT CAPITAL",               "BANK_ALT"),
    ("PAYPAL WORKING CAPITAL",      "BANK_ALT"),
    ("SHOPIFY CAPITAL",             "BANK_ALT"),
    ("AMAZON LENDING",              "BANK_ALT"),
    ("SQUARE CAPITAL",              "BANK_ALT"),
    ("STRIPE CAPITAL",              "BANK_ALT"),

    # Factoring / Invoice Finance
    ("RIVIERA FINANCE",             "FACTORING"),
    ("TRIUMPH BUSINESS CAPITAL",    "FACTORING"),
    ("ROSENTHAL",                   "FACTORING"),
    ("REPUBLIC BUSINESS CREDIT",    "FACTORING"),
    ("BREAKOUT CAPITAL",            "FACTORING"),
    ("LENDIO",                      "FACTORING"),

    # SBA / USDA adjacent
    ("CELTIC BANK",                 "SBA"),
    ("READYCAP",                    "SBA"),
    ("HARVEST SMALL BUSINESS",      "SBA"),
]

# ── GA County Code Map ─────────────────────────────────────────────────────────
GA_COUNTIES = {
    "001": "Appling County",    "021": "Bibb County",
    "051": "Chatham County",    "059": "Clarke County",
    "063": "Clayton County",    "067": "Cobb County",
    "073": "Columbia County",   "077": "Coweta County",
    "089": "DeKalb County",     "095": "Dougherty County",
    "097": "Douglas County",    "113": "Fayette County",
    "115": "Floyd County",      "117": "Forsyth County",
    "121": "Fulton County",     "127": "Glynn County",
    "135": "Gwinnett County",   "139": "Hall County",
    "151": "Henry County",      "153": "Houston County",
    "157": "Jackson County",    "175": "Laurens County",
    "185": "Lowndes County",    "215": "Muscogee County",
    "217": "Newton County",     "223": "Paulding County",
    "245": "Richmond County",   "247": "Rockdale County",
    "255": "Spalding County",   "275": "Thomas County",
    "277": "Tift County",       "285": "Troup County",
    "295": "Walker County",     "297": "Walton County",
    "313": "Whitfield County",
    "038": "DeKalb County",     "044": "Fulton County",
}

# ── Logging ────────────────────────────────────────────────────────────────────
log_file = os.path.join(LOG_DIR, f"ga_mca_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GA-MCA] %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("GA-MCA")


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    })
    return s


def login(session):
    try:
        session.get(LOGIN_URL, timeout=15)
        session.post(LOGIN_URL,
            data={"txtUserID": USERNAME, "txtPassword": PASSWORD, "submit": "Submit"},
            timeout=15, allow_redirects=True)
        session.get(SEARCH_URL + "?searchtype=SecuredParty", timeout=15)
        r = session.get(SEARCH_URL + "?searchtype=SecuredParty", timeout=15)
        return "logout.asp" in r.text
    except Exception as e:
        log.error(f"Login error: {e}")
        return False


def get_variations(session, lender_name, date_from, date_to):
    try:
        r = session.post(RESULTS_URL,
            headers={"Referer": SEARCH_URL + "?searchtype=SecuredParty"},
            data={"searchtype": "SecuredParty", "orderby": "2", "securedsearch": "0",
                  "SecuredPartyOrganizationName": lender_name, "SecuredPartyExact": "0",
                  "SecuredPartyLastName": "", "SecuredPartyFirstName": "",
                  "SecuredPartyMiddleName": "", "FromDate": date_from,
                  "ToDate": date_to, "maxrows": "100"},
            timeout=REQ_TIMEOUT, allow_redirects=True)
        time.sleep(INTER_DELAY)
        if "login.asp" in r.url.lower():
            return None
        if "securedresults.asp" not in r.url.lower():
            return []
        subnames = re.findall(
            r'<input[^>]+name=["\']subname0["\'][^>]+value=["\']([^"\']*)["\']',
            r.text, re.IGNORECASE)
        counts = [int(c) for c in re.findall(r'<td[^>]*>\s*(\d+)\s*</td>', r.text)]
        return [(sn, counts[i] if i < len(counts) else 1)
                for i, sn in enumerate(subnames)]
    except Exception as e:
        log.error(f"  Variation error: {e}")
        return []


def get_filings(session, lender_name, subname_value, j_count, date_from, date_to, page=1):
    try:
        r = session.post(OCCUR_URL,
            params={"NormType": "SecuredParty"},
            headers={"Referer": RESULTS_URL},
            data={"ActionType": "", "DebtorName": "", "SecuredPartyName": lender_name,
                  "DateFrom": date_from, "DateTo": date_to,
                  "Page": str(page) if page > 1 else "", "SearchOrder": "",
                  "searchtype": "SecuredParty", "securedsearch": "0",
                  "SecuredPartyOrganizationName": lender_name, "SecuredPartyExact": "0",
                  "SecuredPartyLastName": "", "SecuredPartyFirstName": "",
                  "SecuredPartyMiddleName": "", "ToDate": date_to, "FromDate": date_from,
                  "OrderBy": "2", "strResults": "", "subname0": subname_value,
                  "jCount": str(j_count), "maxrows": "100", "bFull": "Fullscreen View"},
            timeout=REQ_TIMEOUT, allow_redirects=True)
        time.sleep(INTER_DELAY)
        if "login.asp" in r.url.lower():
            return None, 1
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', r.text, re.DOTALL | re.IGNORECASE)
        filings = []
        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
            if len(cells) < 4:
                continue
            clean = [re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', c)).strip() for c in cells]
            if clean[1].lower() in ("file number", "file #", ""):
                continue
            if not clean[1] or not any(ch.isdigit() for ch in clean[1]):
                continue
            file_num    = re.sub(r'&[a-zA-Z]+;', '', clean[1]).strip()
            debtor_name = re.sub(r'&[a-zA-Z]+;', '', clean[3]).strip()
            date_raw    = re.sub(r'&[a-zA-Z]+;', '', clean[4]).strip()
            date_filed  = date_raw.split()[0] if date_raw else ""
            county_code = file_num.split("-")[0] if "-" in file_num else ""
            if not file_num or not debtor_name:
                continue
            if debtor_name.lower() in ("debtor name", "name", ""):
                continue
            if "debtor name not required" in debtor_name.lower():
                continue
            filings.append((file_num, debtor_name, date_filed, county_code))
        page_match  = re.search(r'Page\s+(\d+)\s+of\s+(\d+)', r.text, re.I)
        total_pages = int(page_match.group(2)) if page_match else 1
        return filings, total_pages
    except requests.exceptions.Timeout:
        log.warning(f"  Timeout (jCount={j_count})")
        return [], 1
    except Exception as e:
        log.error(f"  Filing error: {e}")
        return [], 1


def build_mca_lead(file_num, debtor_name, date_filed, county_code,
                   lender_name, product_type):
    lead_id    = f"GA-MCA-{file_num.strip()}"
    county     = GA_COUNTIES.get(county_code, f"County {county_code}")
    filing_iso = ""
    lapse_iso  = ""
    days_lapse = None
    est_revenue = None
    est_advance = None
    try:
        dt = datetime.strptime(date_filed, "%m/%d/%Y")
        filing_iso  = dt.strftime("%Y-%m-%d")
        lapse_dt    = dt + timedelta(days=5 * 365)
        lapse_iso   = lapse_dt.strftime("%Y-%m-%d")
        days_lapse  = (lapse_dt - datetime.now()).days
    except Exception:
        # Estimate from file number year
        parts = file_num.split("-")
        if len(parts) >= 2 and parts[1].isdigit():
            yr = int(parts[1])
            est = datetime(yr, 7, 1)
            lapse = est + timedelta(days=5 * 365)
            filing_iso = est.strftime("%Y-%m-%d") + " (est)"
            lapse_iso  = lapse.strftime("%Y-%m-%d")
            days_lapse = (lapse - datetime.now()).days

    return {
        "id":            lead_id,
        "source_state":  STATE,
        "file_id":       file_num.strip(),
        "company_name":  debtor_name,
        "city":          county,
        "state":         "GA",
        "secured_party": lender_name,
        "product_type":  product_type,
        "filing_date":   filing_iso,
        "lapse_date":    lapse_iso,
        "days_to_lapse": days_lapse,
        "signal_reason": f"UCC filed by {lender_name} ({product_type})",
    }


def ingest_mca(conn, leads, dry_run):
    found = len(leads)
    new   = 0
    for lead in leads:
        if dry_run:
            new += 1
            continue
        try:
            # Match actual mca_leads schema: AUTOINCREMENT id, specific columns
            conn.execute("""
                INSERT INTO mca_leads
                (company_name, city, state, source_state,
                 secured_party, file_id, filing_date, lapse_date,
                 days_to_lapse, funder_tier, created_at, updated_at)
                SELECT ?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now')
                WHERE NOT EXISTS (
                    SELECT 1 FROM mca_leads
                    WHERE file_id=? AND secured_party=?
                )
            """, (
                lead["company_name"], lead["city"], "GA", lead["source_state"],
                lead["secured_party"], lead["file_id"], lead["filing_date"],
                lead["lapse_date"], lead["days_to_lapse"], lead["product_type"],
                lead["file_id"], lead["secured_party"]
            ))
            if conn.execute("SELECT changes()").fetchone()[0]:
                new += 1
        except Exception as e:
            log.debug(f"  MCA DB error: {e}")
    if not dry_run:
        conn.commit()
    return found, new


def run(lenders=None, dry_run=False):
    if lenders is None:
        lenders = GA_MCA_LENDERS

    # Validate MCA DB path
    if not dry_run and not os.path.exists(MCA_DB_PATH):
        log.error(f"MCA DB not found at {MCA_DB_PATH}")
        log.error("Run from correct directory or update MCA_DB_PATH")
        return

    conn    = None if dry_run else sqlite3.connect(MCA_DB_PATH)
    session = make_session()

    log.info(f"{'DRY RUN — ' if dry_run else ''}GA MCA Scraper")
    log.info(f"MCA DB: {MCA_DB_PATH}")
    log.info("Logging in...")
    if not login(session):
        log.error("Login failed.")
        return
    log.info("✅ Authenticated")

    windows     = date_windows(years=5)
    total_found = 0
    total_new   = 0

    for lender_name, product_type in lenders:
        log.info(f"\n{'─'*60}")
        log.info(f"💰 {lender_name} ({product_type})")
        lender_found = 0
        lender_new   = 0

        for win_start, win_end in windows:
            log.info(f"  → {win_start} – {win_end}")
            variations = get_variations(session, lender_name, win_start, win_end)

            if variations is None:
                log.warning("  Session expired — re-logging...")
                session.cookies.clear()
                if not login(session):
                    log.error("  Re-login failed.")
                    return
                variations = get_variations(session, lender_name, win_start, win_end) or []

            if not variations:
                log.info("  No results")
                continue

            log.info(f"  {len(variations)} variation(s) — {sum(c for _,c in variations)} filings")

            window_leads = []
            for j_count, (subname, count) in enumerate(variations):
                exact_name = subname.split("\x03")[-1].rstrip("\x05") if "\x03" in subname else lender_name
                page = 1
                while True:
                    filings, total_pages = get_filings(
                        session, lender_name, subname, j_count,
                        win_start, win_end, page=page) or ([], 1)
                    log.info(f"    {exact_name[:50]}: pg {page}/{total_pages} → {len(filings)} records")
                    for (file_num, debtor, date_filed, county) in filings:
                        window_leads.append(build_mca_lead(
                            file_num, debtor, date_filed, county,
                            exact_name, product_type))
                    if page >= total_pages:
                        break
                    page += 1
                    time.sleep(INTER_DELAY)

            if window_leads:
                found, new = ingest_mca(conn, window_leads, dry_run)
                lender_found += found
                lender_new   += new
                log.info(f"  → {found} found, {new} new")

        total_found += lender_found
        total_new   += lender_new
        log.info(f"  Lender total: {lender_found} found, {lender_new} new")

    if conn:
        conn.close()

    log.info(f"\n{'='*60}")
    log.info(f"{'[DRY RUN] ' if dry_run else ''}GA MCA COMPLETE: {total_found} found, {total_new} new")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lenders", type=int, default=None)
    args = parser.parse_args()
    lenders = GA_MCA_LENDERS[:args.lenders] if args.lenders else GA_MCA_LENDERS
    run(lenders=lenders, dry_run=args.dry_run)
