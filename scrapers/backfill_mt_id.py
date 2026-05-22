"""
Backfill script: parse city, state, and days_to_lapse for all MT and ID
leads that were inserted without those fields.

Pattern: company_name = "COMPANY NAME - CITY, ST"
         lapse_date   = "MM/DD/YYYY"

Safe to re-run — only touches rows where city IS NULL and source_state IN ('MT','ID').
"""

import sqlite3
import re
from datetime import datetime, date

DB_PATH = '/Users/robertle/tomcat_capex/leads/tomcat_capex.db'

def parse_city_state(company_name):
    """
    Extract city and state from strings like:
      'PARK SIDE FINANCIAL CREDIT UNION - WHITEFISH, MT'
      'TRIAD LEASING & FINANCIAL, INC. - BOISE, ID'
    Returns (clean_company, city, state) or (company_name, None, None)
    """
    if not company_name:
        return company_name, None, None

    # Pattern: everything before " - CITY, ST" at the end
    m = re.match(r'^(.+?)\s+-\s+([A-Z][A-Za-z\s\.]+),\s+([A-Z]{2})\s*$', company_name.strip())
    if m:
        clean_co = m.group(1).strip()
        city     = m.group(2).strip().title()
        state    = m.group(3).strip()
        return clean_co, city, state

    return company_name, None, None


def parse_days_to_lapse(lapse_date_str):
    """
    Convert 'MM/DD/YYYY' to integer days from today.
    Returns None if unparseable.
    """
    if not lapse_date_str:
        return None
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m-%d-%Y'):
        try:
            ld = datetime.strptime(lapse_date_str.strip(), fmt).date()
            return (ld - date.today()).days
        except ValueError:
            continue
    return None


def run_backfill():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, company_name, lapse_date
        FROM ucc_leads
        WHERE source_state IN ('MT', 'ID')
          AND (city IS NULL OR city = 'None' OR city = '')
    """).fetchall()

    print(f"[*] Found {len(rows)} MT/ID records to backfill")

    updated      = 0
    city_fixed   = 0
    lapse_fixed  = 0
    sample_fixes = []

    for row in rows:
        rid          = row['id']
        company_name = row['company_name'] or ''
        lapse_str    = row['lapse_date']

        clean_co, city, state = parse_city_state(company_name)
        days                  = parse_days_to_lapse(lapse_str)

        if city or days is not None:
            conn.execute("""
                UPDATE ucc_leads
                SET city          = ?,
                    state         = COALESCE(state, ?),
                    days_to_lapse = ?,
                    company_name  = ?
                WHERE id = ?
            """, (city, state, days, clean_co, rid))
            updated += 1
            if city:   city_fixed  += 1
            if days is not None: lapse_fixed += 1

            if len(sample_fixes) < 6:
                sample_fixes.append(
                    f"  {clean_co[:40]} | {city}, {state} | {days}d"
                )

    conn.commit()

    print(f"\n[+] Backfill complete:")
    print(f"    Rows updated     : {updated}")
    print(f"    City parsed      : {city_fixed}")
    print(f"    Lapse days calc  : {lapse_fixed}")
    print(f"\n    Sample fixes:")
    for s in sample_fixes:
        print(s)

    # Final count breakdown
    print(f"\n=== Post-backfill counts ===")
    for st in ['MT', 'ID']:
        total  = conn.execute("SELECT COUNT(*) FROM ucc_leads WHERE source_state=?", [st]).fetchone()[0]
        urgent = conn.execute("SELECT COUNT(*) FROM ucc_leads WHERE source_state=? AND CAST(days_to_lapse AS INTEGER) BETWEEN 0 AND 7",   [st]).fetchone()[0]
        hot    = conn.execute("SELECT COUNT(*) FROM ucc_leads WHERE source_state=? AND CAST(days_to_lapse AS INTEGER) BETWEEN 0 AND 30",  [st]).fetchone()[0]
        warm   = conn.execute("SELECT COUNT(*) FROM ucc_leads WHERE source_state=? AND CAST(days_to_lapse AS INTEGER) BETWEEN 31 AND 180",[st]).fetchone()[0]
        cities = conn.execute("""
            SELECT city, COUNT(*) c FROM ucc_leads
            WHERE source_state=? AND city IS NOT NULL
            GROUP BY city ORDER BY c DESC LIMIT 5
        """, [st]).fetchall()
        city_str = ", ".join(f"{r['city']} ({r['c']})" for r in cities) or "—"

        print(f"\n  [{st}]  Total={total:,}  Urgent={urgent}  Hot={hot}  Warm={warm}")
        print(f"         Top cities: {city_str}")

    conn.close()


if __name__ == '__main__':
    run_backfill()
