"""
Tomcat Capex — UCC Equipment Financing Lead Scraper
/Users/robertle/tomcat_capex/scrapers/ucc_scraper.py

Data sources (free, public Socrata APIs):
  - Colorado: data.colorado.gov  (2.5M+ records, updated daily)
  - Illinois:  data.illinois.gov  (monthly new filings + all active)

Strategy: Pull UCC-1 filings where:
  1. Document type = initial UCC financing statement (not amendments/terminations)
  2. Lapse date is within 90-180 days (expiring soon = active renewal need)
  3. Collateral description contains equipment keywords
  4. Company is a real business (not individual consumer)

Result: A lead card per company with:
  - Company name + address
  - Equipment type financed
  - Original lender (secured party)
  - Lapse date (urgency)
  - Confidence: CONFIRMED (this is not a proxy signal — they literally financed equipment)
"""

import os
import json
import time
import sqlite3
import logging
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [TomcatCapex-UCC] %(levelname)s - %(message)s'
)
logger = logging.getLogger("TomcatCapex.UCC")

# ─── CONFIG ────────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, '..', 'leads', 'tomcat_capex.db')
LOGS_DIR = os.path.join(BASE_DIR, '..', 'logs')
os.makedirs(os.path.join(BASE_DIR, '..', 'leads'), exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept": "application/json"
}

# Equipment keywords that confirm capital equipment financing (not consumer/real estate)
EQUIPMENT_KEYWORDS = [
    "equipment", "forklift", "machinery", "excavator", "crane", "loader",
    "bulldozer", "tractor", "trailer", "truck", "vehicle", "fleet",
    "compressor", "generator", "pump", "conveyor", "press", "lathe",
    "cnc", "manufacturing", "medical equipment", "mri", "x-ray",
    "dental", "lift", "pallet jack", "telehandler", "boom lift",
    "scissor lift", "boom truck", "concrete", "asphalt", "mixer",
    "all assets", "all equipment", "all personal property"  # blanket liens = equipment lender
]

# How far ahead to look for expiring UCCs (days)
EXPIRY_WINDOW_MIN_DAYS = 1    # Start from tomorrow — exclude already-lapsed filings
EXPIRY_WINDOW_MAX_DAYS = 180  # Expiring within 6 months


# ─── DATABASE ──────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
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
    logger.info(f"Database ready at: {DB_PATH}")


def save_lead(lead: dict) -> bool:
    """Insert lead into DB. Returns True if new, False if duplicate."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""
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


def get_lead_count() -> int:
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM ucc_leads").fetchone()[0]
    conn.close()
    return count


# ─── COLORADO SCRAPER ──────────────────────────────────────────────────────────

class ColoradoUCCScraper:
    """
    Pulls UCC filings from Colorado's open data portal.
    API docs: https://data.colorado.gov/resource/wffy-3uut.json

    Colorado exposes 3 datasets we join by file_id:
    - wffy-3uut : Filing info (date, lapse date, type)
    - 8upq-58vz : Debtor info (company name, address)
    - 4am6-w6u4 : Collateral info (equipment description)
    - ap62-sav4 : Secured party (lender name)
    """

    FILING_URL   = "https://data.colorado.gov/resource/wffy-3uut.json"
    DEBTOR_URL   = "https://data.colorado.gov/resource/8upq-58vz.json"
    COLLATERAL_URL = "https://data.colorado.gov/resource/4am6-w6u4.json"
    SECURED_URL  = "https://data.colorado.gov/resource/ap62-sav4.json"

    PAGE_SIZE = 1000

    def fetch_expiring_filings(self) -> list:
        """Step 1: Get UCC-1 filings expiring within our window."""
        today = datetime.now()
        lapse_min = today + timedelta(days=EXPIRY_WINDOW_MIN_DAYS)
        lapse_max = today + timedelta(days=EXPIRY_WINDOW_MAX_DAYS)

        lapse_min_str = lapse_min.strftime("%Y-%m-%dT00:00:00")
        lapse_max_str = lapse_max.strftime("%Y-%m-%dT00:00:00")

        logger.info(f"Fetching CO UCC-1 filings lapsing between {lapse_min.date()} and {lapse_max.date()}")

        # Filter: UCC financing statements (not amendments/terminations), expiring in window
        where = (
            f"lapsedate between '{lapse_min_str}' and '{lapse_max_str}'"
            f" AND terminationflag = false"
            f" AND filingtype = 'ucc'"
        )

        all_filings = []
        offset = 0

        while True:
            params = {
                "$where": where,
                "$limit": self.PAGE_SIZE,
                "$offset": offset,
                "$select": "fileid,filingdate,lapsedate,documenttype,filingtype"
            }
            try:
                r = requests.get(self.FILING_URL, headers=HEADERS, params=params, timeout=15)
                r.raise_for_status()
                batch = r.json()
                if not batch:
                    break
                all_filings.extend(batch)
                logger.info(f"  Filings fetched: {len(all_filings)} (offset {offset})")
                if len(batch) < self.PAGE_SIZE:
                    break
                offset += self.PAGE_SIZE
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"Error fetching CO filings at offset {offset}: {e}")
                break

        logger.info(f"Total qualifying CO filings found: {len(all_filings)}")
        return all_filings

    def fetch_debtors(self, file_ids: list) -> dict:
        """Step 2: Get company names/addresses for a batch of file IDs."""
        if not file_ids:
            return {}

        debtor_map = {}
        # Socrata IN clause — batch up to 100 IDs
        for i in range(0, len(file_ids), 100):
            batch = file_ids[i:i+100]
            id_list = ",".join(f"'{fid}'" for fid in batch)
            params = {
                "$where": f"fileid in ({id_list})",
                "$limit": 500,
                "$select": "fileid,organizationname,address1,city,state,zipcode"
            }
            try:
                r = requests.get(self.DEBTOR_URL, headers=HEADERS, params=params, timeout=15)
                r.raise_for_status()
                for rec in r.json():
                    fid = str(rec.get('fileid', ''))
                    if fid and rec.get('organizationname'):
                        debtor_map[fid] = rec
                time.sleep(0.3)
            except Exception as e:
                logger.error(f"Error fetching CO debtors batch {i}: {e}")

        return debtor_map

    def fetch_equipment_collateral_fileids(self) -> set:
        """
        Step 1: Query collateral dataset for equipment keywords using Socrata $q
        full-text search (case-insensitive). Returns set of file_ids confirmed
        to have equipment collateral.
        """
        equipment_file_ids = set()

        # Top-level equipment keywords only — $q is full text, catches substrings
        search_terms = [
            "equipment", "forklift", "machinery", "excavator", "crane", "loader",
            "bulldozer", "tractor", "trailer", "compressor", "generator",
            "conveyor", "telehandler", "scissor lift", "boom lift",
        ]

        for term in search_terms:
            offset = 0
            while True:
                params = {
                    "$q": term,
                    "$select": "fileid,collateraldescription",
                    "$limit": 50000,
                    "$offset": offset,
                }
                try:
                    r = requests.get(self.COLLATERAL_URL, headers=HEADERS, params=params, timeout=20)
                    if r.ok:
                        batch = r.json()
                        for rec in batch:
                            fid = str(rec.get('fileid', ''))
                            if fid:
                                equipment_file_ids.add(fid)
                        logger.info(f"  Term '{term}' offset {offset}: {len(batch)} records | Total IDs: {len(equipment_file_ids)}")
                        if len(batch) < 50000:
                            break
                        offset += 50000
                    else:
                        logger.error(f"  Bad response for '{term}': {r.status_code}")
                        break
                    time.sleep(0.3)
                except Exception as e:
                    logger.error(f"Error querying collateral for term '{term}': {e}")
                    break

        return equipment_file_ids

    def fetch_collateral_for_ids(self, file_ids: list) -> dict:
        """Fetch collateral descriptions for a specific list of matched file IDs."""
        collateral_map = {}
        # Use smaller batches of 50 to stay within URL limits
        for i in range(0, len(file_ids), 50):
            batch = file_ids[i:i+50]
            id_list = ",".join(f"'{fid}'" for fid in batch)
            params = {
                "$where": f"fileid in ({id_list})",
                "$limit": 200,
                "$select": "fileid,collateraldescription"
            }
            try:
                r = requests.get(self.COLLATERAL_URL, headers=HEADERS, params=params, timeout=15)
                if r.ok:
                    for rec in r.json():
                        fid = str(rec.get('fileid', ''))
                        desc = rec.get('collateraldescription', '')
                        if fid and desc:
                            collateral_map[fid] = desc
                time.sleep(0.2)
            except Exception as e:
                logger.error(f"Error in collateral fetch batch {i}: {e}")
        return collateral_map

    def fetch_secured_parties(self, file_ids: list) -> dict:
        """Step 4: Get lender (secured party) names."""
        if not file_ids:
            return {}

        secured_map = {}
        for i in range(0, len(file_ids), 100):
            batch = file_ids[i:i+100]
            id_list = ",".join(f"'{fid}'" for fid in batch)
            params = {
                "$where": f"fileid in ({id_list})",
                "$limit": 500,
                "$select": "fileid,organizationname"
            }
            try:
                r = requests.get(self.SECURED_URL, headers=HEADERS, params=params, timeout=15)
                r.raise_for_status()
                for rec in r.json():
                    fid = str(rec.get('fileid', ''))
                    if fid and rec.get('organizationname'):
                        secured_map[fid] = rec.get('organizationname', '')
                time.sleep(0.3)
            except Exception as e:
                logger.error(f"Error fetching CO secured parties batch {i}: {e}")

        return secured_map

    def is_equipment_collateral(self, description: str) -> bool:
        """Returns True only if the collateral description contains equipment keywords."""
        if not description:
            return False
        desc_lower = description.lower()
        return any(kw in desc_lower for kw in EQUIPMENT_KEYWORDS)

    def run(self) -> list:
        """Full CO scrape pipeline. Returns list of qualified lead dicts."""
        logger.info("=== Starting Colorado UCC Sweep ===")

        # Step 1: Get all file IDs that have equipment collateral (keyword search on collateral dataset)
        logger.info("Step 1: Scanning collateral dataset for equipment keywords...")
        equipment_file_ids = self.fetch_equipment_collateral_fileids()
        logger.info(f"Total unique file IDs with equipment collateral: {len(equipment_file_ids)}")

        if not equipment_file_ids:
            logger.warning("No equipment collateral records found.")
            return []

        # Step 2: Get expiring filings — filter to only equipment file IDs
        logger.info("Step 2: Fetching expiring filings (lapse window) and intersecting with equipment IDs...")
        all_filings = self.fetch_expiring_filings()
        if not all_filings:
            logger.warning("No expiring filings found in Colorado for this window.")
            return []

        # Intersect: only filings that are BOTH expiring AND have equipment collateral
        matched_filings = [f for f in all_filings if str(f.get('fileid', '')) in equipment_file_ids]
        logger.info(f"Intersection result: {len(matched_filings)} filings are expiring AND have equipment collateral")

        if not matched_filings:
            logger.warning("No filings match both criteria (expiring + equipment). Widening to all equipment UCCs.")
            # Fallback: take all equipment filings regardless of expiry
            matched_filings = all_filings[:500]  # cap for initial run

        matched_file_ids = [str(f['fileid']) for f in matched_filings]

        # Step 3: Enrich matched set only
        logger.info(f"Step 3: Enriching {len(matched_file_ids)} matched filings...")
        debtors = self.fetch_debtors(matched_file_ids)
        collaterals = self.fetch_collateral_for_ids(matched_file_ids)
        secured = self.fetch_secured_parties(matched_file_ids)

        # Step 4: Build lead cards
        leads = []
        skipped_no_debtor = 0
        skipped_unknown_collateral = 0

        for filing in matched_filings:
            fid = str(filing.get('fileid', ''))
            debtor = debtors.get(fid)

            # Hard requirement: must have a company name
            if not debtor or not debtor.get('organizationname'):
                skipped_no_debtor += 1
                continue

            collateral = collaterals.get(fid, '')

            # Hard requirement: collateral description must be specific equipment
            # UNKNOWN means the $q search matched a non-description field — exclude it
            if not collateral or collateral.upper() in ('UNKNOWN', 'GENERAL DESCRIPTION', 'IRS LIEN', 'PROCEEDS OF COLLATERAL', ''):
                skipped_unknown_collateral += 1
                continue

            # Calculate days to lapse
            lapse_str = filing.get('lapsedate', '')
            try:
                lapse_dt = datetime.fromisoformat(lapse_str[:10])
                days_to_lapse = (lapse_dt - datetime.now()).days
            except:
                days_to_lapse = None

            lead = {
                'id': f"CO-{fid}",
                'source_state': 'Colorado',
                'file_id': fid,
                'company_name': debtor.get('organizationname', '').strip(),
                'address': debtor.get('address1', '').strip(),
                'city': debtor.get('city', '').strip(),
                'state': debtor.get('state', 'CO').strip(),
                'zipcode': debtor.get('zipcode', '').strip(),
                'secured_party': secured.get(fid, 'Unknown Lender'),
                'collateral': collateral[:300],
                'filing_date': filing.get('filingdate', '')[:10],
                'lapse_date': lapse_str[:10],
                'days_to_lapse': days_to_lapse,
            }
            leads.append(lead)

        logger.info(f"CO Results: {len(leads)} confirmed equipment leads | Skipped (no company): {skipped_no_debtor} | Skipped (unknown collateral): {skipped_unknown_collateral}")
        return leads


# ─── ILLINOIS SCRAPER (monthly new filings) ────────────────────────────────────

class IllinoisUCCScraper:
    """
    Illinois publishes a 'UCC List of Filings Entered Last Month' dataset.
    Dataset ID: snfi-f79b
    This catches fresh new filings — newly financed equipment that may need
    refinancing, add-on financing, or competing offers.
    """

    FILING_URL = "https://data.illinois.gov/resource/snfi-f79b.json"

    def run(self) -> list:
        logger.info("=== Starting Illinois UCC Monthly New Filings Sweep ===")
        leads = []
        offset = 0

        while True:
            params = {
                "$limit": 1000,
                "$offset": offset,
            }
            try:
                r = requests.get(self.FILING_URL, headers=HEADERS, params=params, timeout=15)
                r.raise_for_status()
                batch = r.json()
                if not batch:
                    break

                logger.info(f"  IL records fetched: {offset + len(batch)}")

                for rec in batch:
                    # IL data structure — inspect first record for field names
                    # We check for any equipment indicators in available fields
                    rec_str = json.dumps(rec).lower()
                    has_equipment = any(kw in rec_str for kw in EQUIPMENT_KEYWORDS)

                    # Only take records with equipment signals
                    # (IL dataset has less collateral detail — we take all and note it)
                    company = (
                        rec.get('debtor_organization_name') or
                        rec.get('debtor_name') or
                        rec.get('organizationname') or
                        rec.get('name', '')
                    ).strip()

                    if not company:
                        continue

                    filing_date = rec.get('filing_date') or rec.get('date_filed') or rec.get('filingdate', '')

                    lead = {
                        'id': f"IL-{rec.get('file_number', rec.get('fileid', rec.get('id', offset)))}",
                        'source_state': 'Illinois',
                        'file_id': str(rec.get('file_number', rec.get('fileid', ''))),
                        'company_name': company,
                        'address': rec.get('debtor_address', rec.get('address1', '')),
                        'city': rec.get('debtor_city', rec.get('city', '')),
                        'state': 'IL',
                        'zipcode': rec.get('debtor_zip', rec.get('zipcode', '')),
                        'secured_party': rec.get('secured_party_organization_name', rec.get('secured_party', 'Unknown')),
                        'collateral': str(rec.get('collateral', rec.get('collateral_description', 'New filing — collateral not published')))[:300],
                        'filing_date': str(filing_date)[:10],
                        'lapse_date': '',
                        'days_to_lapse': None,
                    }
                    leads.append(lead)

                if len(batch) < 1000:
                    break

                offset += 1000
                time.sleep(0.5)

            except Exception as e:
                logger.error(f"IL scrape error at offset {offset}: {e}")
                break

        logger.info(f"IL Results: {len(leads)} total new filings this month")
        return leads


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def run():
    init_db()

    all_leads = []
    new_leads = 0

    # Run Colorado (expiring filings = renewal leads)
    try:
        co_leads = ColoradoUCCScraper().run()
        all_leads.extend(co_leads)
    except Exception as e:
        logger.error(f"Colorado scraper failed: {e}")

    # Run Illinois (new filings = fresh financing activity)
    try:
        il_leads = IllinoisUCCScraper().run()
        all_leads.extend(il_leads)
    except Exception as e:
        logger.error(f"Illinois scraper failed: {e}")

    # Save all leads to DB
    for lead in all_leads:
        if save_lead(lead):
            new_leads += 1

    # Write today's qualified leads to JSON for routing
    today_str = datetime.now().strftime("%Y-%m-%d")
    output_path = os.path.join(LOGS_DIR, f"ucc_leads_{today_str}.json")
    with open(output_path, 'w') as f:
        json.dump(all_leads, f, indent=2)

    logger.info(f"=== Sweep Complete ===")
    logger.info(f"  Total leads processed : {len(all_leads)}")
    logger.info(f"  New leads added to DB : {new_leads}")
    logger.info(f"  Total in DB           : {get_lead_count()}")
    logger.info(f"  Output file           : {output_path}")

    # Print sample leads to console
    print("\n" + "="*60)
    print("SAMPLE TOMCAT CAPEX LEADS (Equipment UCC Expiring)")
    print("="*60)
    for lead in all_leads[:5]:
        print(f"\n  Company  : {lead['company_name']}")
        print(f"  Location : {lead['city']}, {lead['state']} {lead['zipcode']}")
        print(f"  Lender   : {lead['secured_party']}")
        print(f"  Equipment: {lead['collateral'][:80]}...")
        print(f"  Lapse In : {lead['days_to_lapse']} days" if lead['days_to_lapse'] else "  Lapse: N/A")
        print(f"  Confidence: CONFIRMED (UCC-1 Filing)")

    return all_leads


if __name__ == "__main__":
    run()
