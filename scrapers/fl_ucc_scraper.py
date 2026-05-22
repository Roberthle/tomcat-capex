"""
Tomcat Capex — Florida UCC Scraper
/Users/robertle/tomcat_capex/scrapers/fl_ucc_scraper.py

Data source: Florida Secured Transaction Registry (public REST API)
API base: https://publicsearchapi.floridaucc.com/
No login required. Updated daily.

Strategy:
  Florida's API searches by DEBTOR NAME — no date range search available.
  We search using equipment industry keywords + company type terms to
  find companies with active and recently-lapsed equipment filings.

  Sub-options used:
    - FiledCompactDebtorNameList   → Active filings (renewal candidates)
    - LapsedCompactDebtorNameList  → Recently lapsed (urgent: already expired)

  For each debtor match, we fetch the full filing detail to get:
    - Secured party (lender)
    - Collateral description
    - Lapse date
    - File number
"""

import os, sys, json, time, sqlite3, logging, requests
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [TomcatCapex-FL] %(levelname)s - %(message)s')
logger = logging.getLogger("TomcatCapex.FL")

DB_PATH = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

API_BASE = "https://publicsearchapi.floridaucc.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin": "https://floridaucc.com",
    "Referer": "https://floridaucc.com/search",
    "Accept": "application/json, text/plain, */*",
}

# Equipment industry search terms → cast a wide net, filter on results
SEARCH_TERMS = [
    "EQUIPMENT", "FORKLIFT", "EXCAVATION", "CRANE SERVICE", "LOADER",
    "TRUCKING", "TRANSPORT", "FLEET", "CONSTRUCTION", "ASPHALT",
    "CONCRETE", "PAVING", "MACHINERY", "MECHANICAL", "INDUSTRIAL",
    "HVAC", "ELECTRIC", "PLUMBING", "ROOFING", "LANDSCAPING",
    "AGRICULTURE", "FARMING", "MANUFACTURING", "WELDING", "DRILLING",
    "MINING", "FOREST", "LUMBER", "SEPTIC", "EXCAVATING",
]

# Equipment keywords to filter collateral descriptions
EQUIPMENT_COLLATERAL_KEYWORDS = [
    "equipment", "forklift", "truck", "trailer", "vehicle", "machinery",
    "excavator", "crane", "loader", "bulldozer", "tractor", "generator",
    "compressor", "lift", "boom", "fleet", "all assets", "all personal",
    "rolling stock", "titled", "motor vehicle", "commercial vehicle",
]

EXPIRY_WINDOW_MAX_DAYS = 180


# ─── DATABASE ──────────────────────────────────────────────────────────────────

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
    conn.commit()
    conn.close()


def save_lead(lead: dict) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.execute("""
            INSERT OR IGNORE INTO ucc_leads
            (id, source_state, file_id, company_name, address, city, state,
             zipcode, secured_party, collateral, filing_date, lapse_date, days_to_lapse)
            VALUES (:id, :source_state, :file_id, :company_name, :address, :city,
                    :state, :zipcode, :secured_party, :collateral, :filing_date,
                    :lapse_date, :days_to_lapse)
        """, lead)
        inserted = c.rowcount > 0
        conn.commit()
        return inserted
    finally:
        conn.close()


def get_lead_count(state=None):
    conn = sqlite3.connect(DB_PATH)
    if state:
        count = conn.execute("SELECT COUNT(*) FROM ucc_leads WHERE source_state=?", [state]).fetchone()[0]
    else:
        count = conn.execute("SELECT COUNT(*) FROM ucc_leads").fetchone()[0]
    conn.close()
    return count


# ─── FLORIDA API CLIENT ────────────────────────────────────────────────────────

def search_debtors(term: str, sub_option: str, page: int = 1) -> dict:
    """Search Florida UCC API for debtors matching a term."""
    params = {
        "text": term,
        "searchOptionType": "OrganizationDebtorName",
        "searchOptionSubOption": sub_option,
        "searchCategory": "Standard",
        "pageNumber": page,
        "pageSize": 100,
    }
    try:
        r = requests.get(f"{API_BASE}/search", headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        return r.json().get('payload', {})
    except Exception as e:
        logger.error(f"Search error for '{term}': {e}")
        return {}


def get_filing_detail(debtor_id: str) -> list:
    """Fetch full filing details for a debtor including collateral and secured party."""
    try:
        r = requests.get(f"{API_BASE}/debtors/{debtor_id}/filings", headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json().get('payload', [])
    except Exception as e:
        logger.error(f"Filing detail error for debtor {debtor_id}: {e}")
        return []


def is_equipment_collateral(text: str) -> bool:
    """Returns True if the collateral text confirms equipment (not real estate/IP/etc)."""
    if not text:
        return False
    t = text.lower()
    # Exclude clearly non-equipment collateral
    exclusions = ["real property", "real estate", "mortgage", "receivable", "account", 
                  "intellectual", "patent", "trademark", "software", "cash", "deposit", 
                  "inventory only", "crops", "farm products"]
    if any(ex in t for ex in exclusions):
        return False
    return any(kw in t for kw in EQUIPMENT_COLLATERAL_KEYWORDS)


# ─── MAIN SCRAPER ──────────────────────────────────────────────────────────────

class FloridaUCCScraper:

    def run(self) -> list:
        logger.info("=== Starting Florida UCC Sweep ===")
        today     = datetime.now()
        lapse_deadline = today + timedelta(days=EXPIRY_WINDOW_MAX_DAYS)
        all_leads = []
        seen_file_ids = set()

        # We search both FILED (active, expiring soon) and LAPSED (recently expired)
        sub_options = [
            ("FiledCompactDebtorNameList",   "ACTIVE — expiring soon"),
            ("LapsedCompactDebtorNameList",  "LAPSED — recently expired"),
        ]

        for term in SEARCH_TERMS:
            for sub_option, label in sub_options:
                page_num = 1
                while True:
                    payload = search_debtors(term, sub_option, page=page_num)
                    debtors = payload.get('debtors', [])

                    if not debtors:
                        break

                    logger.info(f"  [{label}] '{term}' p{page_num}: {len(debtors)} debtors")

                    for debtor in debtors:
                        debtor_id   = debtor.get('debtorId') or debtor.get('id', '')
                        company     = (debtor.get('organizationName') or debtor.get('name') or '').strip()
                        address     = (debtor.get('address1') or debtor.get('address') or '').strip()
                        city        = (debtor.get('city') or '').strip()
                        state_code  = (debtor.get('state') or 'FL').strip()
                        zipcode     = (debtor.get('postalCode') or debtor.get('zip') or '').strip()

                        if not company:
                            continue

                        # Fetch filing details for this debtor
                        filings = get_filing_detail(debtor_id) if debtor_id else []
                        time.sleep(0.15)

                        if not filings:
                            # Use debtor-level data even without filing detail
                            filings = [{}]

                        for filing in filings:
                            file_num    = filing.get('fileNumber') or filing.get('documentNumber') or debtor_id
                            lapse_str   = filing.get('lapseDate') or filing.get('expirationDate') or ''
                            filing_date = filing.get('filedDate') or filing.get('filingDate') or ''
                            collateral  = filing.get('collateralDescription') or filing.get('collateral') or ''
                            lender_raw  = filing.get('securedParties', [])
                            lender      = ', '.join(
                                sp.get('organizationName') or sp.get('name') or ''
                                for sp in (lender_raw if isinstance(lender_raw, list) else [])
                            ).strip() or 'Unknown Lender'

                            lead_id = f"FL-{file_num or debtor_id}"
                            if lead_id in seen_file_ids:
                                continue
                            seen_file_ids.add(lead_id)

                            # Calculate lapse urgency
                            days_to_lapse = None
                            if lapse_str:
                                try:
                                    lapse_dt = datetime.fromisoformat(lapse_str[:10])
                                    days_to_lapse = (lapse_dt - today).days
                                    # Skip if too far out
                                    if days_to_lapse > EXPIRY_WINDOW_MAX_DAYS:
                                        continue
                                except:
                                    pass

                            # Confirm equipment via collateral description if available
                            if collateral and not is_equipment_collateral(collateral):
                                continue

                            lead = {
                                'id': lead_id,
                                'source_state': 'Florida',
                                'file_id': str(file_num or debtor_id),
                                'company_name': company,
                                'address': address,
                                'city': city,
                                'state': state_code,
                                'zipcode': zipcode,
                                'secured_party': lender,
                                'collateral': collateral[:300] if collateral else f"Equipment (matched via search term: {term})",
                                'filing_date': str(filing_date)[:10],
                                'lapse_date': str(lapse_str)[:10],
                                'days_to_lapse': days_to_lapse,
                            }
                            all_leads.append(lead)

                    # Pagination
                    next_row = payload.get('nextRowNumber')
                    if not next_row or len(debtors) < 100:
                        break
                    page_num += 1
                    time.sleep(0.3)

            time.sleep(0.5)  # Polite delay between terms

        logger.info(f"FL Results: {len(all_leads)} leads found across {len(SEARCH_TERMS)} search terms")
        return all_leads


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def run():
    init_db()
    scraper = FloridaUCCScraper()
    leads   = scraper.run()

    new_leads = 0
    for lead in leads:
        if save_lead(lead):
            new_leads += 1

    today_str   = datetime.now().strftime("%Y-%m-%d")
    output_path = os.path.join(LOG_DIR, f"fl_ucc_leads_{today_str}.json")
    with open(output_path, 'w') as f:
        json.dump(leads, f, indent=2)

    logger.info(f"=== FL Sweep Complete ===")
    logger.info(f"  FL leads found        : {len(leads)}")
    logger.info(f"  New leads added to DB : {new_leads}")
    logger.info(f"  Total FL in DB        : {get_lead_count('Florida')}")
    logger.info(f"  Total all states in DB: {get_lead_count()}")
    logger.info(f"  Output file           : {output_path}")

    print(f"\n{'='*60}")
    print(f"SAMPLE TOMCAT CAPEX LEADS — Florida")
    print(f"{'='*60}")
    for lead in leads[:8]:
        lapse = f"{lead['days_to_lapse']}d" if lead.get('days_to_lapse') is not None else "?"
        urgency = "🔴" if (lead.get('days_to_lapse') or 999) <= 30 else "🟡" if (lead.get('days_to_lapse') or 999) <= 90 else "🟢"
        print(f"\n  {urgency} {lead['company_name']}")
        print(f"     Location : {lead['city']}, {lead['state']} {lead['zipcode']}")
        print(f"     Lender   : {lead['secured_party']}")
        print(f"     Collateral: {lead['collateral'][:70]}")
        print(f"     Expires  : {lead['lapse_date']} ({lapse})")

    return leads


if __name__ == "__main__":
    run()
