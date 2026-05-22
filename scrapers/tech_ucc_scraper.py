"""
Tomcat Capex — Multi-State Tech Company UCC Scraper
/Users/robertle/tomcat_capex/scrapers/tech_ucc_scraper.py

Scrapes UCC filings from state Socrata open data portals, filtered by
tech equipment lenders (secured parties). This finds the highest-value
leads: companies that have financed IT/tech equipment.

Currently supported states (Socrata API — no auth needed):
  - Connecticut: data.ct.gov (UCC Lien Filings dataset)
  - Colorado:    data.colorado.gov (UCC Filing Info + Secured Party)
  - Oregon:      data.oregon.gov (UCC Filings + Secured Parties)

Strategy:
  Search by SECURED PARTY (lender) name matching known tech financiers.
  This yields leads for companies like law firms, hospitals, schools,
  and enterprises that lease Dell servers, Xerox copiers, HP laptops, etc.
  These are 10-50x more valuable than generic equipment leads.

Run: python3 tech_ucc_scraper.py [--state CT|CO|OR|ALL] [--limit N]
"""

import os, re, sys, time, json, sqlite3, logging, argparse
import requests
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOGS_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [TechUCC] %(levelname)s - %(message)s'
)
log = logging.getLogger("TomcatCapex.TechUCC")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept": "application/json"
}

# ── Tech Lenders to search for ──────────────────────────────────────────────

TECH_LENDERS = [
    # IT OEMs
    ("DELL FINANCIAL", "IT_OEM"),
    ("DELL TECHNOLOGIES", "IT_OEM"),
    ("HEWLETT-PACKARD", "IT_OEM"),
    ("HP FINANCIAL", "IT_OEM"),
    ("LENOVO", "IT_OEM"),
    ("IBM CREDIT", "IT_OEM"),
    ("IBM CORP", "IT_OEM"),
    ("CISCO", "IT_OEM"),
    ("ORACLE", "IT_OEM"),
    # Print/Imaging
    ("XEROX", "PRINT_IMAGING"),
    ("CANON FINANCIAL", "PRINT_IMAGING"),
    ("KONICA MINOLTA", "PRINT_IMAGING"),
    ("RICOH", "PRINT_IMAGING"),
    ("SHARP ELECTRONICS", "PRINT_IMAGING"),
    ("KYOCERA", "PRINT_IMAGING"),
    # IT Channel Finance
    ("GREATAMERICA", "IT_CHANNEL"),
    ("MARLIN BUSINESS", "IT_CHANNEL"),
    ("LEAF COMMERCIAL", "IT_CHANNEL"),
    ("ECS FINANCIAL", "IT_CHANNEL"),
    ("CIT TECHNOLOGY", "IT_CHANNEL"),
    # Cloud/SaaS
    ("AMAZON CAPITAL", "CLOUD_SAAS"),
    ("MICROSOFT", "CLOUD_SAAS"),
    ("SALESFORCE", "CLOUD_SAAS"),
]

# ── Database ─────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    # Ensure columns exist
    for col in ["tech_company TEXT", "tech_category TEXT", "tech_reason TEXT"]:
        try:
            conn.execute(f"ALTER TABLE ucc_leads ADD COLUMN {col}")
        except:
            pass
    conn.commit()
    conn.close()


def save_lead(lead: dict) -> bool:
    """Insert lead into DB. Returns True if new, False if duplicate."""
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


# ── Connecticut Scraper ──────────────────────────────────────────────────────

class CTTechScraper:
    """
    Connecticut UCC Lien Filings via Socrata.
    Dataset: data.ct.gov/resource/xfev-8smz
    Fields: debtor_nm_bus, sec_party_nm_bus, dt_lapse, lien_status, etc.
    """

    BASE_URL = "https://data.ct.gov/resource/xfev-8smz.json"
    PAGE_SIZE = 1000

    def scrape(self, lender_name: str, tech_category: str) -> list:
        """Search CT UCC for a specific tech lender."""
        leads = []
        offset = 0

        while True:
            where_clause = (
                f"sec_party_nm_bus like '%{lender_name}%' "
                f"AND lien_status='Active' "
                f"AND cd_flng_type='ORIG FIN STMT'"
            )

            params = {
                "$where": where_clause,
                "$limit": self.PAGE_SIZE,
                "$offset": offset,
                "$order": "dt_lapse ASC",
            }

            try:
                r = requests.get(self.BASE_URL, headers=HEADERS,
                                 params=params, timeout=15)
                r.raise_for_status()
                batch = r.json()
                if not batch:
                    break

                for rec in batch:
                    company = rec.get("debtor_nm_bus", "").strip()
                    if not company or len(company) < 3:
                        continue

                    lapse = (rec.get("dt_lapse") or "")[:10]
                    filing = (rec.get("dt_accept") or "")[:10]
                    file_id = rec.get("id_ucc_flng_nbr", "")

                    # Calculate days to lapse
                    dtl = None
                    if lapse:
                        try:
                            lapse_dt = datetime.strptime(lapse, "%Y-%m-%d")
                            dtl = (lapse_dt - datetime.now()).days
                        except:
                            pass

                    leads.append({
                        "id": f"CT-{file_id}",
                        "source_state": "Connecticut",
                        "file_id": file_id,
                        "company_name": company,
                        "address": rec.get("debtor_ad_str1", ""),
                        "city": rec.get("debtor_ad_city", ""),
                        "state": rec.get("debtor_ad_state", "CT"),
                        "zipcode": rec.get("debtor_ad_zip", "")[:10],
                        "secured_party": rec.get("sec_party_nm_bus", ""),
                        "collateral": f"Tech Equipment ({lender_name})",
                        "filing_date": filing,
                        "lapse_date": lapse,
                        "days_to_lapse": dtl,
                        "tech_company": "true",
                        "tech_category": tech_category,
                        "tech_reason": f"Tech lender: {lender_name}",
                    })

                if len(batch) < self.PAGE_SIZE:
                    break
                offset += self.PAGE_SIZE
                time.sleep(0.5)

            except Exception as e:
                log.error(f"CT API error ({lender_name}): {e}")
                break

        return leads


# ── Colorado Scraper ─────────────────────────────────────────────────────────

class COTechScraper:
    """
    Colorado UCC via Socrata (multi-dataset join).
    Secured Party: data.colorado.gov/resource/ap62-sav4
    Filing Info:   data.colorado.gov/resource/wffy-3uut
    Debtor Info:   data.colorado.gov/resource/8upq-58vz
    """

    SECURED_URL = "https://data.colorado.gov/resource/ap62-sav4.json"
    FILING_URL  = "https://data.colorado.gov/resource/wffy-3uut.json"
    DEBTOR_URL  = "https://data.colorado.gov/resource/8upq-58vz.json"
    PAGE_SIZE = 1000

    def _fetch_secured_party_fileids(self, lender_name: str) -> list:
        """Find file IDs where secured party matches the tech lender."""
        file_ids = []
        offset = 0

        while True:
            params = {
                "$where": f"upper(organizationname) like '%{lender_name}%' "
                          f"AND recordstatus='active'",
                "$select": "fileid",
                "$limit": self.PAGE_SIZE,
                "$offset": offset,
            }
            try:
                r = requests.get(self.SECURED_URL, headers=HEADERS,
                                 params=params, timeout=15)
                r.raise_for_status()
                batch = r.json()
                if not batch:
                    break
                file_ids.extend([b["fileid"] for b in batch if "fileid" in b])
                if len(batch) < self.PAGE_SIZE:
                    break
                offset += self.PAGE_SIZE
                time.sleep(0.5)
            except Exception as e:
                log.error(f"CO Secured Party API error: {e}")
                break

        return list(set(file_ids))

    def _fetch_filing_info(self, file_ids: list) -> dict:
        """Get filing date and lapse date for file IDs."""
        info = {}
        for chunk_start in range(0, len(file_ids), 50):
            chunk = file_ids[chunk_start:chunk_start + 50]
            ids_str = ",".join(f"'{fid}'" for fid in chunk)
            params = {
                "$where": f"fileid in ({ids_str}) AND terminationflag=false",
                "$select": "fileid,filingdate,lapsedate",
            }
            try:
                r = requests.get(self.FILING_URL, headers=HEADERS,
                                 params=params, timeout=15)
                r.raise_for_status()
                for rec in r.json():
                    fid = rec.get("fileid")
                    info[fid] = {
                        "filing_date": (rec.get("filingdate") or "")[:10],
                        "lapse_date": (rec.get("lapsedate") or "")[:10],
                    }
                time.sleep(0.3)
            except Exception as e:
                log.error(f"CO Filing API error: {e}")
        return info

    def _fetch_debtors(self, file_ids: list) -> dict:
        """Get debtor (company) info for file IDs."""
        debtors = {}
        for chunk_start in range(0, len(file_ids), 50):
            chunk = file_ids[chunk_start:chunk_start + 50]
            ids_str = ",".join(f"'{fid}'" for fid in chunk)
            params = {
                "$where": f"fileid in ({ids_str})",
                "$select": "fileid,organizationname,address1,city,state,zipcode",
            }
            try:
                r = requests.get(self.DEBTOR_URL, headers=HEADERS,
                                 params=params, timeout=15)
                r.raise_for_status()
                for rec in r.json():
                    fid = rec.get("fileid")
                    org = rec.get("organizationname", "").strip()
                    if org and len(org) >= 3:
                        debtors[fid] = {
                            "company_name": org,
                            "address": rec.get("address1", ""),
                            "city": rec.get("city", ""),
                            "state": rec.get("state", "CO"),
                            "zipcode": rec.get("zipcode", "")[:10],
                        }
                time.sleep(0.3)
            except Exception as e:
                log.error(f"CO Debtor API error: {e}")
        return debtors

    def scrape(self, lender_name: str, tech_category: str) -> list:
        """Full CO scrape pipeline for a single tech lender."""
        log.info(f"  CO: Searching secured parties for '{lender_name}'...")
        file_ids = self._fetch_secured_party_fileids(lender_name)
        log.info(f"  CO: Found {len(file_ids)} file IDs")

        if not file_ids:
            return []

        filing_info = self._fetch_filing_info(file_ids)
        debtors = self._fetch_debtors(file_ids)

        leads = []
        for fid in file_ids:
            if fid not in debtors:
                continue
            debtor = debtors[fid]
            info = filing_info.get(fid, {})
            lapse = info.get("lapse_date", "")
            filing = info.get("filing_date", "")

            dtl = None
            if lapse:
                try:
                    dtl = (datetime.strptime(lapse, "%Y-%m-%d") - datetime.now()).days
                except:
                    pass

            leads.append({
                "id": f"CO-{fid}",
                "source_state": "Colorado",
                "file_id": fid,
                "company_name": debtor["company_name"],
                "address": debtor.get("address", ""),
                "city": debtor.get("city", ""),
                "state": debtor.get("state", "CO"),
                "zipcode": debtor.get("zipcode", ""),
                "secured_party": lender_name,
                "collateral": f"Tech Equipment ({lender_name})",
                "filing_date": filing,
                "lapse_date": lapse,
                "days_to_lapse": dtl,
                "tech_company": "true",
                "tech_category": tech_category,
                "tech_reason": f"Tech lender: {lender_name}",
            })

        return leads


# ── Oregon Scraper ───────────────────────────────────────────────────────────

class ORTechScraper:
    """
    Oregon UCC via Socrata.
    Filings dataset: data.oregon.gov/resource/snfi-f79b (has both SP and DB rows)
    Secured Parties: data.oregon.gov/resource/2kf7-i54h

    Strategy: Search secured parties dataset for tech lenders, get file numbers,
    then look up debtor info from the filings dataset.
    """

    SP_URL     = "https://data.oregon.gov/resource/2kf7-i54h.json"
    FILING_URL = "https://data.oregon.gov/resource/snfi-f79b.json"
    PAGE_SIZE = 1000

    def scrape(self, lender_name: str, tech_category: str) -> list:
        """Search Oregon UCC for a specific tech lender."""
        # Step 1: Find file numbers where secured party matches
        file_numbers = []
        sp_info = {}  # filenumber -> secured_party_name
        offset = 0

        while True:
            params = {
                "$where": f"upper(secured_party) like '%{lender_name}%'",
                "$select": "filenumber,secured_party,filing_date",
                "$limit": self.PAGE_SIZE,
                "$offset": offset,
            }
            try:
                r = requests.get(self.SP_URL, headers=HEADERS,
                                 params=params, timeout=15)
                r.raise_for_status()
                batch = r.json()
                if not batch:
                    break
                for rec in batch:
                    fn = rec.get("filenumber", "")
                    if fn:
                        file_numbers.append(fn)
                        sp_info[fn] = rec.get("secured_party", lender_name)
                if len(batch) < self.PAGE_SIZE:
                    break
                offset += self.PAGE_SIZE
                time.sleep(0.5)
            except Exception as e:
                log.error(f"OR Secured Party API error: {e}")
                break

        file_numbers = list(set(file_numbers))
        log.info(f"  OR: Found {len(file_numbers)} file numbers for {lender_name}")

        if not file_numbers:
            return []

        # Step 2: Look up debtor info for those file numbers
        leads = []
        for chunk_start in range(0, len(file_numbers), 50):
            chunk = file_numbers[chunk_start:chunk_start + 50]
            fns_str = ",".join(f"'{fn}'" for fn in chunk)
            params = {
                "$where": f"original_file_number in ({fns_str}) "
                          f"AND party_type='DB'",
                "$limit": self.PAGE_SIZE,
            }
            try:
                r = requests.get(self.FILING_URL, headers=HEADERS,
                                 params=params, timeout=15)
                r.raise_for_status()
                for rec in r.json():
                    company = rec.get("entity", "").strip()
                    if not company or len(company) < 3:
                        continue

                    fn = rec.get("original_file_number", "")
                    lapse = (rec.get("lapse_date") or "")[:10]
                    filing = (rec.get("filing_date") or "")[:10]

                    dtl = None
                    if lapse:
                        try:
                            dtl = (datetime.strptime(lapse, "%Y-%m-%d") -
                                   datetime.now()).days
                        except:
                            pass

                    leads.append({
                        "id": f"OR-{fn}",
                        "source_state": "Oregon",
                        "file_id": fn,
                        "company_name": company,
                        "address": rec.get("mail_addr_1", ""),
                        "city": rec.get("city_descr", ""),
                        "state": rec.get("st_cd_txt", "OR"),
                        "zipcode": rec.get("zip_code_txt", "")[:10],
                        "secured_party": sp_info.get(fn, lender_name),
                        "collateral": f"Tech Equipment ({lender_name})",
                        "filing_date": filing,
                        "lapse_date": lapse,
                        "days_to_lapse": dtl,
                        "tech_company": "true",
                        "tech_category": tech_category,
                        "tech_reason": f"Tech lender: {lender_name}",
                    })
                time.sleep(0.3)
            except Exception as e:
                log.error(f"OR Filing API error: {e}")

        return leads


# ── Main Runner ──────────────────────────────────────────────────────────────

def run_tech_scrape(states: list = None, limit_per_lender: int = 0):
    """Run tech UCC scrape for specified states."""
    init_db()

    scrapers = {
        "CT": CTTechScraper(),
        "CO": COTechScraper(),
        "OR": ORTechScraper(),
    }

    if not states:
        states = list(scrapers.keys())

    total_new = 0
    total_found = 0

    for lender_name, tech_category in TECH_LENDERS:
        log.info(f"\n{'─'*55}")
        log.info(f"🔍 Searching for: {lender_name} ({tech_category})")

        for state_code in states:
            if state_code not in scrapers:
                log.warning(f"  No scraper for state: {state_code}")
                continue

            scraper = scrapers[state_code]
            leads = scraper.scrape(lender_name, tech_category)
            total_found += len(leads)
            log.info(f"  {state_code}: Found {len(leads)} leads for {lender_name}")

            new_count = 0
            for lead in leads:
                if limit_per_lender and new_count >= limit_per_lender:
                    break
                if save_lead(lead):
                    new_count += 1

            total_new += new_count
            log.info(f"  {state_code}: {new_count} NEW leads saved")
            time.sleep(1)  # Rate limit between lenders

    log.info(f"\n{'='*55}")
    log.info(f"  Tech UCC Scrape Complete")
    log.info(f"  States:     {', '.join(states)}")
    log.info(f"  Lenders:    {len(TECH_LENDERS)}")
    log.info(f"  Found:      {total_found:,}")
    log.info(f"  New leads:  {total_new:,}")
    log.info(f"{'='*55}")

    return total_new


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tech Company UCC Scraper")
    parser.add_argument("--state", choices=["CT", "CO", "OR", "ALL"], default="ALL",
                        help="State to scrape (default: ALL)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max new leads per lender (0=unlimited)")
    args = parser.parse_args()

    states = None if args.state == "ALL" else [args.state]
    run_tech_scrape(states=states, limit_per_lender=args.limit)
