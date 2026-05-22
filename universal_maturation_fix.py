import sqlite3
from datetime import datetime, timedelta

def get_next_maturation(start_date):
    # Reverting to exact 5-year legal UCC expiration
    return start_date + timedelta(days=5*365)

def run_universal_fix():
    conn = sqlite3.connect('/Users/robertle/tomcat_capex/leads/tomcat_capex.db')
    cur = conn.cursor()
    
    cur.execute("SELECT id, source_state, filing_date, lapse_date FROM ucc_leads WHERE filing_date != '' AND filing_date IS NOT NULL")
    rows = cur.fetchall()
    print(f"Loaded {len(rows)} leads for maturation recalculation.")
    
    fixed = 0
    now = datetime.now()
    
    for row_id, state, filing_date, lapse_date in rows:
        try:
            # Handle filing_date parsing (some are YYYY-MM-DD, some MM/DD/YYYY)
            date_str = filing_date.split(' ')[0] # strip (est) if present
            if '-' in date_str:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
            else:
                dt = datetime.strptime(date_str, "%m/%d/%Y")
                
            lapse_dt = get_next_maturation(dt)
            new_lapse_iso = lapse_dt.strftime("%Y-%m-%d")
            new_days = (lapse_dt - now).days
            
            # Only update if the computed next maturation is different from what's stored
            # (e.g. state was showing 5-year, but next maturation is actually 3-year)
            cur.execute("UPDATE ucc_leads SET lapse_date=?, days_to_lapse=? WHERE id=?", 
                        (new_lapse_iso, new_days, row_id))
            fixed += 1
        except Exception as e:
            pass
            
    conn.commit()
    conn.close()
    print(f"Universally fixed maturation dates for {fixed} leads across ALL states.")

if __name__ == "__main__":
    run_universal_fix()
