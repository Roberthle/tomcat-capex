"""
ga_ucc_fix.py
One-shot cleanup for GA leads ingested before the date-bug fix.
Applies 3 patches:
  1. Backfills filing_date / lapse_date / days_to_lapse from the file_id year
  2. Maps county code (city field) → county name
  3. Fixes tech_category from generic GA_EQUIPMENT → proper lender category
  4. Deletes junk rows (debtor name = "Debtor Name Not Required...")
Run: python3 scrapers/ga_ucc_fix.py
"""
import sqlite3, os, re
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, "leads", "tomcat_capex.db")

# ── Georgia county FIPS code → county name ────────────────────────────────────
# Format: file_id prefix (3-digit zero-padded code) → county name
GA_COUNTIES = {
    "001": "Appling County",       "003": "Atkinson County",
    "005": "Bacon County",         "007": "Baker County",
    "009": "Baldwin County",       "011": "Banks County",
    "013": "Barrow County",        "015": "Bartow County",
    "017": "Ben Hill County",      "019": "Berrien County",
    "021": "Bibb County",          "023": "Bleckley County",
    "025": "Brantley County",      "027": "Brooks County",
    "029": "Bryan County",         "031": "Bulloch County",
    "033": "Burke County",         "035": "Butts County",
    "037": "Calhoun County",       "039": "Camden County",
    "043": "Candler County",       "045": "Carroll County",
    "047": "Catoosa County",       "049": "Charlton County",
    "051": "Chatham County",       "053": "Chattahoochee County",
    "055": "Chattooga County",     "057": "Cherokee County",
    "059": "Clarke County",        "061": "Clay County",
    "063": "Clayton County",       "065": "Clinch County",
    "067": "Cobb County",          "069": "Coffee County",
    "071": "Colquitt County",      "073": "Columbia County",
    "075": "Cook County",          "077": "Coweta County",
    "079": "Crawford County",      "081": "Crisp County",
    "083": "Dade County",          "085": "Dawson County",
    "087": "Decatur County",       "089": "DeKalb County",
    "091": "Dodge County",         "093": "Dooly County",
    "095": "Dougherty County",     "097": "Douglas County",
    "099": "Early County",         "101": "Echols County",
    "103": "Effingham County",     "105": "Elbert County",
    "107": "Emanuel County",       "109": "Evans County",
    "111": "Fannin County",        "113": "Fayette County",
    "115": "Floyd County",         "117": "Forsyth County",
    "119": "Franklin County",      "121": "Fulton County",
    "123": "Gilmer County",        "125": "Glascock County",
    "127": "Glynn County",         "129": "Gordon County",
    "131": "Grady County",         "133": "Greene County",
    "135": "Gwinnett County",      "137": "Habersham County",
    "139": "Hall County",          "141": "Hancock County",
    "143": "Haralson County",      "145": "Harris County",
    "147": "Hart County",          "149": "Heard County",
    "151": "Henry County",         "153": "Houston County",
    "155": "Irwin County",         "157": "Jackson County",
    "159": "Jasper County",        "161": "Jeff Davis County",
    "163": "Jefferson County",     "165": "Jenkins County",
    "167": "Johnson County",       "169": "Jones County",
    "171": "Lamar County",         "173": "Lanier County",
    "175": "Laurens County",       "177": "Lee County",
    "179": "Liberty County",       "181": "Lincoln County",
    "183": "Long County",          "185": "Lowndes County",
    "187": "Lumpkin County",       "189": "McDuffie County",
    "191": "McIntosh County",      "193": "Macon County",
    "195": "Madison County",       "197": "Marion County",
    "199": "Meriwether County",    "201": "Miller County",
    "205": "Mitchell County",      "207": "Monroe County",
    "209": "Montgomery County",    "211": "Morgan County",
    "213": "Murray County",        "215": "Muscogee County",
    "217": "Newton County",        "219": "Oconee County",
    "221": "Oglethorpe County",    "223": "Paulding County",
    "225": "Peach County",         "227": "Pickens County",
    "229": "Pierce County",        "231": "Pike County",
    "233": "Polk County",          "235": "Pulaski County",
    "237": "Putnam County",        "239": "Quitman County",
    "241": "Rabun County",         "243": "Randolph County",
    "245": "Richmond County",      "247": "Rockdale County",
    "249": "Schley County",        "251": "Screven County",
    "253": "Seminole County",      "255": "Spalding County",
    "257": "Stephens County",      "259": "Stewart County",
    "261": "Sumter County",        "263": "Talbot County",
    "265": "Taliaferro County",    "267": "Tattnall County",
    "269": "Taylor County",        "271": "Telfair County",
    "273": "Terrell County",       "275": "Thomas County",
    "277": "Tift County",          "279": "Toombs County",
    "281": "Towns County",         "283": "Treutlen County",
    "285": "Troup County",         "287": "Turner County",
    "289": "Twiggs County",        "291": "Union County",
    "293": "Upson County",         "295": "Walker County",
    "297": "Walton County",        "299": "Ware County",
    "301": "Warren County",        "303": "Washington County",
    "305": "Wayne County",         "307": "Webster County",
    "309": "Wheeler County",       "311": "White County",
    "313": "Whitfield County",     "315": "Wilcox County",
    "317": "Wilkes County",        "319": "Wilkinson County",
    "321": "Worth County",
    # Larger metro shorthands used in practice
    "044": "Fulton County",        "038": "DeKalb County",
    "067": "Cobb County",          "135": "Gwinnett County",
    "089": "DeKalb County",
}

# ── Lender → category map (from GA_LENDERS list) ─────────────────────────────
LENDER_CATEGORY = {
    "DELL FINANCIAL":         "IT_OEM",
    "HEWLETT PACKARD":        "IT_OEM",
    "HP FINANCIAL":           "IT_OEM",
    "IBM CREDIT":             "IT_OEM",
    "CISCO SYSTEMS CAPITAL":  "IT_OEM",
    "KONICA MINOLTA":         "PRINT_IMAGING",
    "XEROX FINANCIAL":        "PRINT_IMAGING",
    "CANON FINANCIAL":        "PRINT_IMAGING",
    "RICOH":                  "PRINT_IMAGING",
    "KYOCERA":                "PRINT_IMAGING",
    "GREATAMERICA":           "IT_CHANNEL",
    "MARLIN":                 "IT_CHANNEL",
    "PAWNEE":                 "IT_CHANNEL",
    "BALBOA":                 "IT_CHANNEL",
    "LEAF COMMERCIAL":        "IT_CHANNEL",
    "DLL FINANCE":            "EQUIP_FINANCE",
    "DE LAGE LANDEN":         "EQUIP_FINANCE",
    "WELLS FARGO EQUIPMENT":  "EQUIP_FINANCE",
    "US BANCORP":             "EQUIP_FINANCE",
    "KEY EQUIPMENT":          "EQUIP_FINANCE",
    "STEARNS BANK":           "EQUIP_FINANCE",
    "BANC OF AMERICA":        "EQUIP_FINANCE",
    "CIT BANK":               "EQUIP_FINANCE",
    "CATERPILLAR":            "HEAVY_EQUIP",
    "JOHN DEERE":             "HEAVY_EQUIP",
    "CNH INDUSTRIAL":         "HEAVY_EQUIP",
    "TOYOTA INDUSTRIES":      "HEAVY_EQUIP",
}

def lender_to_category(secured_party: str) -> str:
    sp_upper = secured_party.upper()
    for keyword, cat in LENDER_CATEGORY.items():
        if keyword in sp_upper:
            return cat
    return "EQUIP_FINANCE"


def run():
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # ── Step 1: Delete junk rows ──────────────────────────────────────────────
    junk_patterns = [
        "Debtor Name Not Required%",
        "DEBTOR NAME NOT REQUIRED%",
        "%REVISED ARTICLE 9%",
    ]
    total_deleted = 0
    for pat in junk_patterns:
        cur.execute("DELETE FROM ucc_leads WHERE source_state='Georgia' AND company_name LIKE ?", (pat,))
        total_deleted += cur.rowcount
    print(f"Step 1 — Deleted {total_deleted} junk rows")

    # ── Step 2: Fetch all GA rows needing updates ─────────────────────────────
    cur.execute("""
        SELECT id, file_id, city, secured_party, filing_date
        FROM ucc_leads WHERE source_state='Georgia'
    """)
    rows = cur.fetchall()
    print(f"Step 2 — {len(rows)} GA rows to patch")

    date_fixed    = 0
    county_fixed  = 0
    category_fixed = 0

    for (row_id, file_id, city, secured_party, filing_date) in rows:
        updates = {}

        # County code → county name
        county_code = file_id.split("-")[0] if "-" in file_id else city
        county_name = GA_COUNTIES.get(county_code, f"County {county_code}")
        if city != county_name:
            updates["city"]  = county_name
            updates["state"] = "GA"
            county_fixed += 1

        # Fix tech_category from generic GA_EQUIPMENT
        cat = lender_to_category(secured_party or "")
        updates["tech_category"] = cat
        updates["tech_reason"]   = f"Equipment Financing | {secured_party}"
        category_fixed += 1

        # Backfill filing date if missing — use file_id year to estimate
        if not filing_date or "est" in filing_date:
            # File ID format: 121-2024-006161 → year=2024
            parts = file_id.split("-")
            if len(parts) >= 2 and parts[1].isdigit() and len(parts[1]) == 4:
                year = int(parts[1])
                # Estimate mid-year (Jul 1) — best we can do without re-scraping
                est_date   = datetime(year, 7, 1)
                
                lapse_date = est_date + timedelta(days=5 * 365)
                
                days_lapse = (lapse_date - now).days
                updates["filing_date"]   = est_date.strftime("%Y-%m-%d") + " (est)"
                updates["lapse_date"]    = lapse_date.strftime("%Y-%m-%d")
                updates["days_to_lapse"] = days_lapse
                date_fixed += 1

        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            vals = list(updates.values()) + [row_id]
            cur.execute(f"UPDATE ucc_leads SET {set_clause} WHERE id=?", vals)

    conn.commit()
    conn.close()

    print(f"\n✅ Patch complete:")
    print(f"   Junk rows deleted  : {total_deleted}")
    print(f"   County names fixed : {county_fixed}")
    print(f"   Categories fixed   : {category_fixed}")
    print(f"   Dates backfilled   : {date_fixed} (estimated from file year)")
    print(f"\nSample query:")
    print(f"  python3 -c \"import sqlite3; [print(r) for r in sqlite3.connect('{DB_PATH}').execute(\\\"SELECT company_name, city, secured_party, tech_category, filing_date, days_to_lapse FROM ucc_leads WHERE source_state='Georgia' LIMIT 5\\\")]\"")


if __name__ == "__main__":
    run()
