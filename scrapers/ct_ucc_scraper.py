"""
Tomcat Capex — Connecticut UCC Scraper
/Users/robertle/tomcat_capex/scrapers/ct_ucc_scraper.py

Data source: Connecticut Secretary of State — data.ct.gov
Dataset: xfev-8smz (Uniform Commercial Code UCC Lien Filings)
Updated: Daily. 833,554 active records. Zero join required.

CT dataset is a single table containing:
  - Debtor company name + full address
  - Secured party (lender) name + address
  - Lien status (Active/Released/etc)
  - Lapse date (expiry)
  - Filing type code

CT does NOT store collateral text descriptions.
We identify equipment leads using:
  1. Lender name contains equipment finance keywords (e.g. "equipment finance", "leasing", "capital")
  2. Filing type = ORIG FIN STMT (original financing statement)
  3. Status = Active
  4. Lapse date in target window
"""

import os
import sys
import json
import time
import sqlite3
import logging
import requests
from datetime import datetime, timedelta

# ─── PATH SETUP ────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [TomcatCapex-CT] %(levelname)s - %(message)s'
)
logger = logging.getLogger("TomcatCapex.CT")

DB_PATH  = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')
LOG_DIR  = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
CT_URL  = "https://data.ct.gov/resource/xfev-8smz.json"

# Equipment lender keyword signals
# CT doesn't store collateral text — we confirm equipment via lender name patterns
EQUIPMENT_LENDER_KEYWORDS = [
    "equipment", "leasing", "capital", "machinery", "finance corp",
    "financial services", "asset finance", "bancorp", "bank equipment",
    "fleet", "vehicle finance", "truck finance", "farm credit",
    "equipment finance", "equipment capital", "equipment leasing",
    "stearns bank", "macquarie", "cit group", "us bancorp",
    "key equipment", "de lage landen", "dll", "great elm",
    "western technology", "first western", "byline"
]

EXPIRY_WINDOW_MIN_DAYS = 1
EXPIRY_WINDOW_MAX_DAYS = 180

# ─── DB HELPERS ────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ucc_leads (
            id              TEXT PRIMARY KEY,
            source_state    TEXT NOT NULL,
            file_id         TEXT NOT NULL,
            company_name    TEXT,
            address         TEXT,
            city            TEXT,
            state           TEXT,
            zipcode         TEXT,
            secured_party   TEXT,
            collateral      TEXT,
            filing_date     TEXT,
            lapse_date      TEXT,
            days_to_lapse   INTEGER,
            status          TEXT DEFAULT 'new',
            routed_to       TEXT,
            routed_at       TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
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


def get_lead_count(state: str = None) -> int:
    conn = sqlite3.connect(DB_PATH)
    if state:
        count = conn.execute("SELECT COUNT(*) FROM ucc_leads WHERE source_state=?", [state]).fetchone()[0]
    else:
        count = conn.execute("SELECT COUNT(*) FROM ucc_leads").fetchone()[0]
    conn.close()
    return count


# ─── CONNECTICUT SCRAPER ───────────────────────────────────────────────────────

class ConnecticutUCCScraper:
    """
    Pulls active UCC-1 filings from Connecticut's open data portal.
    Uses lender-name keyword matching to identify equipment financing.
    
    CT dataset is one table — no joins needed.
    41,044 filings expiring in the next 180 days.
    """

    PAGE_SIZE = 5000  # CT allows large pages

    def is_equipment_lender(self, lender_name: str) -> bool:
        """True if lender name suggests equipment financing."""
        if not lender_name:
            return False
        ln = lender_name.lower()
        return any(kw in ln for kw in EQUIPMENT_LENDER_KEYWORDS)

    def run(self) -> list:
        logger.info("=== Starting Connecticut UCC Sweep ===")

        today     = datetime.now()
        lapse_min = (today + timedelta(days=EXPIRY_WINDOW_MIN_DAYS)).strftime("%Y-%m-%dT00:00:00")
        lapse_max = (today + timedelta(days=EXPIRY_WINDOW_MAX_DAYS)).strftime("%Y-%m-%dT00:00:00")

        logger.info(f"Window: {today.date() + timedelta(1)} → {today.date() + timedelta(EXPIRY_WINDOW_MAX_DAYS)}")

        all_leads    = []
        offset       = 0
        total_fetched = 0
        skipped_lender = 0
        skipped_no_company = 0

        while True:
            params = {
                "$where": (
                    f"lien_status='Active'"
                    f" AND cd_flng_type='ORIG FIN STMT'"
                    f" AND dt_lapse between '{lapse_min}' and '{lapse_max}'"
                ),
                "$select": (
                    "id_lien_flng_nbr,debtor_nm_bus,debtor_ad_str1,"
                    "debtor_ad_city,debtor_ad_state,debtor_ad_zip,"
                    "sec_party_nm_bus,dt_lapse,dt_accept"
                ),
                "$limit": self.PAGE_SIZE,
                "$offset": offset,
                "$order": "dt_lapse ASC",  # Sort by urgency
            }

            try:
                r = requests.get(CT_URL, headers=HEADERS, params=params, timeout=20)
                r.raise_for_status()
                batch = r.json()

                if not batch:
                    break

                total_fetched += len(batch)
                logger.info(f"  Fetched {total_fetched} records (offset {offset})")

                for rec in batch:
                    company = (rec.get('debtor_nm_bus') or '').strip()
                    lender  = (rec.get('sec_party_nm_bus') or '').strip()
                    file_id = rec.get('id_lien_flng_nbr', '')

                    # Hard requirement: must be a business (not individual)
                    if not company:
                        skipped_no_company += 1
                        continue

                    # Confirm equipment financing via lender name
                    if not self.is_equipment_lender(lender):
                        skipped_lender += 1
                        continue

                    # Calculate days to lapse
                    lapse_str = rec.get('dt_lapse', '')
                    try:
                        lapse_dt = datetime.fromisoformat(lapse_str[:10])
                        days_to_lapse = (lapse_dt - today).days
                    except:
                        days_to_lapse = None

                    lead = {
                        'id': f"CT-{file_id}",
                        'source_state': 'Connecticut',
                        'file_id': file_id,
                        'company_name': company,
                        'address': (rec.get('debtor_ad_str1') or '').strip(),
                        'city': (rec.get('debtor_ad_city') or '').strip(),
                        'state': (rec.get('debtor_ad_state') or 'CT').strip(),
                        'zipcode': (rec.get('debtor_ad_zip') or '').strip(),
                        'secured_party': lender,
                        'collateral': f"Equipment Financing (lender: {lender[:60]})",
                        'filing_date': str(rec.get('dt_accept', ''))[:10],
                        'lapse_date': lapse_str[:10],
                        'days_to_lapse': days_to_lapse,
                    }
                    all_leads.append(lead)

                if len(batch) < self.PAGE_SIZE:
                    break

                offset += self.PAGE_SIZE
                time.sleep(0.3)

            except Exception as e:
                logger.error(f"CT fetch error at offset {offset}: {e}")
                break

        logger.info(f"CT Results: {len(all_leads)} equipment leads | Skipped (no company): {skipped_no_company} | Skipped (non-equipment lender): {skipped_lender}")
        return all_leads


def run():
    init_db()
    scraper = ConnecticutUCCScraper()
    leads   = scraper.run()

    new_leads = 0
    for lead in leads:
        if save_lead(lead):
            new_leads += 1

    today_str  = datetime.now().strftime("%Y-%m-%d")
    output_path = os.path.join(LOG_DIR, f"ct_ucc_leads_{today_str}.json")
    with open(output_path, 'w') as f:
        json.dump(leads, f, indent=2)

    logger.info(f"=== CT Sweep Complete ===")
    logger.info(f"  Total CT leads found  : {len(leads)}")
    logger.info(f"  New leads added to DB : {new_leads}")
    logger.info(f"  Total CT in DB        : {get_lead_count('Connecticut')}")
    logger.info(f"  Total all states in DB: {get_lead_count()}")
    logger.info(f"  Output file           : {output_path}")

    print(f"\n{'='*60}")
    print(f"SAMPLE TOMCAT CAPEX LEADS — Connecticut")
    print(f"{'='*60}")
    for lead in leads[:8]:
        lapse = f"{lead['days_to_lapse']}d" if lead.get('days_to_lapse') else '?'
        urgency = "🔴" if (lead.get('days_to_lapse') or 999) <= 30 else "🟡" if (lead.get('days_to_lapse') or 999) <= 90 else "🟢"
        print(f"\n  {urgency} {lead['company_name']}")
        print(f"     Location : {lead['city']}, {lead['state']} {lead['zipcode']}")
        print(f"     Lender   : {lead['secured_party']}")
        print(f"     Expires  : {lead['lapse_date']} ({lapse} from today)")

    return leads


if __name__ == "__main__":
    run()
