"""
ga_ucc_scraper_v2.py
Georgia GSCCCA UCC Scraper — Pure HTTP (requests) Edition
No browser/Playwright required. Uses direct form POST flow:
  1. Login → apps.gsccca.org
  2. Search → securedresults.asp  (gets variation summary + subname fields)
  3. Drill  → occurrences.asp     (gets individual debtor filing rows)
  4. Parse  → File#, Debtor Name, Date Filed, County
  5. Ingest → tomcat_capex.db

Account: tomcatmca / Openclaw26  (paid — Statewide UCC Index access)
Run:
  python3 scrapers/ga_ucc_scraper_v2.py               # full run
  python3 scrapers/ga_ucc_scraper_v2.py --lenders 3   # first 3 lenders
  python3 scrapers/ga_ucc_scraper_v2.py --dry-run      # no DB write
"""

import requests, re, sqlite3, os, sys, time, logging, argparse, hashlib, json
from datetime import datetime, timedelta

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH    = os.path.join(BASE_DIR, "leads", "tomcat_capex.db")
LOG_DIR    = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

USERNAME   = "roberthle@gmail.com"
PASSWORD   = "Openclaw26"
STATE      = "Georgia"
SOURCE     = "GA"

SEARCH_URL    = "https://search.gsccca.org/UCC_Search/search.asp"
RESULTS_URL   = "https://search.gsccca.org/UCC_Search/securedresults.asp"
OCCUR_URL     = "https://search.gsccca.org/UCC_Search/occurrences.asp"
LOGIN_URL     = "https://apps.gsccca.org/login.asp"

REQ_TIMEOUT   = 90   # seconds — big result sets take time
INTER_DELAY   = 1.5  # seconds between requests — be polite

# 2-year lookback in 6-month windows
def date_windows(years=6):
    end = datetime.now()
    windows = []
    for i in range(years * 2):
        win_end   = end - timedelta(days=i * 182)
        win_start = win_end - timedelta(days=182)
        windows.append((win_start.strftime("%m/%d/%Y"), win_end.strftime("%m/%d/%Y")))
    return windows

GA_LENDERS = [
    ("DELL FINANCIAL SERVICES",        "IT_OEM"),
    ("HEWLETT PACKARD",                "IT_OEM"),
    ("HP FINANCIAL",                   "IT_OEM"),
    ("IBM CREDIT",                     "IT_OEM"),
    ("CISCO SYSTEMS CAPITAL",          "IT_OEM"),
    ("KONICA MINOLTA",                 "PRINT_IMAGING"),
    ("XEROX FINANCIAL",                "PRINT_IMAGING"),
    ("CANON FINANCIAL SERVICES",       "PRINT_IMAGING"),
    ("RICOH USA",                      "PRINT_IMAGING"),
    ("KYOCERA DOCUMENT SOLUTIONS",     "PRINT_IMAGING"),
    ("GREATAMERICA FINANCIAL",         "IT_CHANNEL"),
    ("MARLIN LEASING",                 "IT_CHANNEL"),
    ("PAWNEE LEASING",                 "IT_CHANNEL"),
    ("BALBOA CAPITAL",                 "IT_CHANNEL"),
    ("DLL FINANCE",                    "EQUIP_FINANCE"),
    ("DE LAGE LANDEN",                 "EQUIP_FINANCE"),
    ("WELLS FARGO EQUIPMENT FINANCE",  "EQUIP_FINANCE"),
    ("US BANCORP EQUIPMENT",           "EQUIP_FINANCE"),
    ("KEY EQUIPMENT FINANCE",          "EQUIP_FINANCE"),
    ("STEARNS BANK",                   "EQUIP_FINANCE"),
    ("BANC OF AMERICA",          "EQUIP_FINANCE"),
    ("CIT BANK",                  "EQUIP_FINANCE"),
    ("CATERPILLAR FINANCIAL",          "HEAVY_EQUIP"),
    ("JOHN DEERE FINANCIAL",           "HEAVY_EQUIP"),
    ("CNH INDUSTRIAL CAPITAL",         "HEAVY_EQUIP"),
    ("TOYOTA INDUSTRIES",              "HEAVY_EQUIP"),
    ("LEAF COMMERCIAL CAPITAL",        "IT_CHANNEL"),
]

# ── Logging ────────────────────────────────────────────────────────────────────
log_file = os.path.join(LOG_DIR, f"ga_ucc_v2_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GA-UCC-v2] %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("GA-UCC-v2")

# ── HTTP Session ───────────────────────────────────────────────────────────────
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def login(session: requests.Session) -> bool:
    try:
        session.get(LOGIN_URL, timeout=15)
        r = session.post(LOGIN_URL,
            data={"txtUserID": USERNAME, "txtPassword": PASSWORD, "submit": "Submit"},
            timeout=15, allow_redirects=True)
        # Establish search.gsccca.org cross-domain session
        session.get(SEARCH_URL + "?searchtype=SecuredParty", timeout=15)
        # Check auth by looking for logout link
        r2 = session.get(SEARCH_URL + "?searchtype=SecuredParty", timeout=15)
        return "logout.asp" in r2.text
    except Exception as e:
        log.error(f"Login failed: {e}")
        return False


def relogin(session: requests.Session) -> bool:
    """Re-authenticate when session expires mid-run."""
    log.info("Re-authenticating...")
    # Clear cookies
    session.cookies.clear()
    return login(session)

# ── Level 1: Get variation subnames from securedresults.asp ───────────────────

def get_variations(session, lender_name, date_from, date_to):
    """
    Returns list of (subname_value, count) tuples.
    subname_value encodes both the stem and the exact match:
      b"STEM\x04\x03EXACT_NAME\x05"
    """
    try:
        r = session.post(
            RESULTS_URL,
            headers={"Referer": SEARCH_URL + "?searchtype=SecuredParty"},
            data={
                "searchtype":                    "SecuredParty",
                "orderby":                       "2",
                "securedsearch":                 "0",
                "SecuredPartyOrganizationName":  lender_name,
                "SecuredPartyExact":             "0",
                "SecuredPartyLastName":          "",
                "SecuredPartyFirstName":         "",
                "SecuredPartyMiddleName":        "",
                "FromDate":                      date_from,
                "ToDate":                        date_to,
                "maxrows":                       "100",
            },
            timeout=REQ_TIMEOUT, allow_redirects=True
        )
        time.sleep(INTER_DELAY)

        if "login.asp" in r.url.lower():
            return None  # Session expired

        if "securedresults.asp" not in r.url.lower():
            return []  # No results

        # Extract subname values — these are hidden <input name="subname0" value="...">
        subnames_raw = re.findall(
            r'<input[^>]+name=["\']subname0["\'][^>]+value=["\']([^"\']*)["\']',
            r.text, re.IGNORECASE
        )
        if not subnames_raw:
            # Try alternate attribute order
            subnames_raw = re.findall(
                r'<input[^>]+value=["\']([^"\']*)["\'][^>]+name=["\']subname0["\']',
                r.text, re.IGNORECASE
            )

        # Extract counts (the number shown next to each variation name)
        counts_raw = re.findall(r'<td[^>]*>\s*(\d+)\s*</td>', r.text, re.IGNORECASE)
        counts = [int(c) for c in counts_raw]

        variations = []
        for i, sn in enumerate(subnames_raw):
            cnt = counts[i] if i < len(counts) else 1
            variations.append((sn, cnt))

        return variations

    except requests.exceptions.Timeout:
        log.warning(f"  Timeout getting variations for {lender_name}")
        return []
    except Exception as e:
        log.error(f"  Variation error: {e}")
        return []


# ── Level 2: Get individual filing rows from occurrences.asp ──────────────────

def get_filings(session, lender_name, subname_value, j_count,
                date_from, date_to, page=1) -> list:
    """
    POST to occurrences.asp with the specific subname.
    Returns list of (file_num, debtor_name, date_filed, county_code) tuples.
    """
    try:
        r = session.post(
            OCCUR_URL,
            params={"NormType": "SecuredParty"},
            headers={"Referer": RESULTS_URL},
            data={
                "ActionType":               "",
                "DebtorName":               "",
                "SecuredPartyName":         lender_name,
                "DateFrom":                 date_from,
                "DateTo":                   date_to,
                "Page":                     str(page) if page > 1 else "",
                "SearchOrder":              "",
                "searchtype":               "SecuredParty",
                "securedsearch":            "0",
                "SecuredPartyOrganizationName": lender_name,
                "SecuredPartyExact":        "0",
                "SecuredPartyLastName":     "",
                "SecuredPartyFirstName":    "",
                "SecuredPartyMiddleName":   "",
                "ToDate":                   date_to,
                "FromDate":                 date_from,
                "OrderBy":                  "2",
                "strResults":               "",
                "subname0":                 subname_value,
                "jCount":                   str(j_count),
                "maxrows":                  "100",
                "bFull":                    "Fullscreen View",
            },
            timeout=REQ_TIMEOUT, allow_redirects=True
        )
        time.sleep(INTER_DELAY)

        if "login.asp" in r.url.lower():
            return None  # Session expired

        # Parse filing rows: [Select][File Number][Doc Type][Debtor Name][Date Filed][Original File#]
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', r.text, re.DOTALL | re.IGNORECASE)
        filings = []

        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
            if len(cells) < 4:
                continue
            clean = [re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', c)).strip() for c in cells]
            # Skip header rows
            if clean[1].lower() in ("file number", "file #", "filing #", ""):
                continue
            if not clean[1] or not any(ch.isdigit() for ch in clean[1]):
                continue
            # Extract just the file number from formatting like "&nbsp;044-2025-006161&nbsp;"
            file_num    = re.sub(r'&[a-zA-Z]+;', '', clean[1]).strip()
            debtor_name = re.sub(r'&[a-zA-Z]+;', '', clean[3]).strip()
            date_raw    = re.sub(r'&[a-zA-Z]+;', '', clean[4]).strip()
            date_filed  = date_raw.split()[0] if date_raw else ""
            county_code = file_num.split("-")[0] if "-" in file_num else ""

            if not file_num or not debtor_name:
                continue
            if debtor_name.lower() in ("debtor name", "name", ""):
                continue

            filings.append((file_num, debtor_name, date_filed, county_code))

        # Check for pagination — "Page X of Y"
        page_match = re.search(r'Page\s+(\d+)\s+of\s+(\d+)', r.text, re.I)
        total_pages = int(page_match.group(2)) if page_match else 1

        return filings, total_pages

    except requests.exceptions.Timeout:
        log.warning(f"  Timeout fetching filings (jCount={j_count})")
        return [], 1
    except Exception as e:
        log.error(f"  Filing fetch error: {e}")
        return [], 1


# ── DB Ingest ─────────────────────────────────────────────────────────────────

def ingest(conn, leads: list, dry_run: bool) -> tuple:
    found = len(leads)
    new   = 0
    for lead in leads:
        if dry_run:
            new += 1
            continue
        try:
            conn.execute("""
                INSERT OR IGNORE INTO ucc_leads
                (id, source_state, file_id, company_name, city, state,
                 secured_party, collateral, filing_date, lapse_date,
                 days_to_lapse, tech_company, tech_category, tech_reason, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            """, (
                lead["id"], lead["source_state"], lead["file_id"],
                lead["company_name"], lead["city"], "GA",
                lead["secured_party"], lead["collateral"],
                lead["filing_date"], lead["lapse_date"],
                lead["days_to_lapse"],
                "true", "GA_EQUIPMENT", lead["tech_reason"],
            ))
            if conn.execute("SELECT changes()").fetchone()[0]:
                new += 1
        except Exception as e:
            log.debug(f"  DB error: {e}")
    if not dry_run:
        conn.commit()
    return found, new


def build_lead(file_num, debtor_name, date_filed, county_code, lender_name) -> dict:
    file_id     = file_num.strip()
    lead_id     = f"GA-{file_id}"
    filing_iso  = ""
    lapse_iso   = ""
    days_lapse  = None
    try:
        dt = datetime.strptime(date_filed, "%m/%d/%Y")
        filing_iso = dt.strftime("%Y-%m-%d")
        lapse_dt   = dt + timedelta(days=5 * 365)
        lapse_iso  = lapse_dt.strftime("%Y-%m-%d")
        days_lapse = (lapse_dt - datetime.now()).days
    except Exception:
        pass
    return {
        "id":            lead_id,
        "source_state":  STATE,
        "file_id":       file_id,
        "company_name":  debtor_name,
        "city":          county_code,
        "secured_party": lender_name,
        "collateral":    f"Equipment Financing ({lender_name})",
        "filing_date":   filing_iso,
        "lapse_date":    lapse_iso,
        "days_to_lapse": days_lapse,
        "tech_reason":   f"Lender: {lender_name}",
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def run(lenders=None, dry_run=False):
    if lenders is None:
        lenders = GA_LENDERS

    conn = None if dry_run else sqlite3.connect(DB_PATH)
    session = make_session()

    log.info(f"{'DRY RUN — ' if dry_run else ''}Logging to {log_file}")
    log.info("Logging in to GSCCCA (tomcatmca)...")
    if not login(session):
        log.error("Login failed. Check credentials.")
        return

    log.info("✅ Authenticated")

    windows      = date_windows(years=6)
    total_found  = 0
    total_new    = 0

    for lender_name, category in lenders:
        log.info(f"\n{'─'*60}")
        log.info(f"🔍 {lender_name} ({category})")
        lender_found = 0
        lender_new   = 0

        for win_start, win_end in windows:
            log.info(f"  → {win_start} – {win_end}")

            # Level 1: Get variations
            variations = get_variations(session, lender_name, win_start, win_end)

            if variations is None:
                log.warning("  Session expired — re-authenticating...")
                if not relogin(session):
                    log.error("  Re-login failed. Stopping.")
                    return
                variations = get_variations(session, lender_name, win_start, win_end) or []

            if not variations:
                log.info("  No results")
                continue

            total_filings = sum(cnt for _, cnt in variations)
            log.info(f"  {len(variations)} variation(s) — ~{total_filings} total filings")

            # Level 2: Drill into each variation
            window_leads = []
            for j_count, (subname, count) in enumerate(variations):
                exact_name = subname.split("\x03")[-1].rstrip("\x05") if "\x03" in subname else lender_name
                log.info(f"    Variation {j_count}: '{exact_name}' ({count} filing{'s' if count != 1 else ''})")

                page = 1
                while True:
                    result = get_filings(session, lender_name, subname, j_count,
                                         win_start, win_end, page=page)
                    if result is None:
                        log.warning("    Session expired mid-variation — re-authenticating...")
                        if not relogin(session):
                            log.error("    Re-login failed.")
                            break
                        result = get_filings(session, lender_name, subname, j_count,
                                              win_start, win_end, page=page) or ([], 1)

                    filings, total_pages = result
                    log.info(f"    Page {page}/{total_pages}: {len(filings)} records")

                    for (file_num, debtor_name, date_filed, county_code) in filings:
                        lead = build_lead(file_num, debtor_name, date_filed,
                                          county_code, exact_name)
                        window_leads.append(lead)

                    if page >= total_pages:
                        break
                    page += 1
                    time.sleep(INTER_DELAY)

            if window_leads:
                found, new = ingest(conn, window_leads, dry_run)
                lender_found += found
                lender_new   += new
                log.info(f"  → {found} found, {new} new")

        total_found += lender_found
        total_new   += lender_new
        log.info(f"  Lender total: {lender_found} found, {lender_new} new")
        time.sleep(INTER_DELAY)

    if conn:
        conn.close()

    log.info(f"\n{'='*60}")
    log.info(f"{'[DRY RUN] ' if dry_run else ''}COMPLETE: {total_found} found, {total_new} new")
    log.info(f"{'='*60}")
    return total_new


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Georgia GSCCCA UCC Scraper v2")
    parser.add_argument("--lenders", type=int, default=None, help="Limit to first N lenders")
    parser.add_argument("--dry-run", action="store_true", help="Parse but don't write to DB")
    args = parser.parse_args()

    lenders = GA_LENDERS[:args.lenders] if args.lenders else GA_LENDERS
    run(lenders=lenders, dry_run=args.dry_run)
