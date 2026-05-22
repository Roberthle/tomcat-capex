import os
import json
import sqlite3
import glob

CAPEX_DB = "/Users/robertle/tomcat_capex/leads/tomcat_capex.db"
MCA_DB = "/Users/robertle/tomcat_mca/leads/tomcat_mca.db"
LOGS_DIR = "/Users/robertle/tomcat_capex/logs/"

def recover_capex():
    conn = sqlite3.connect(CAPEX_DB)
    json_files = glob.glob(os.path.join(LOGS_DIR, "*.json"))
    
    total_recovered = 0
    for file in json_files:
        try:
            with open(file, 'r') as f:
                data = json.load(f)
                if not isinstance(data, list):
                    continue
                
                for lead in data:
                    try:
                        conn.execute("""
                            INSERT OR IGNORE INTO ucc_leads
                            (id, source_state, file_id, company_name, address, city, state, zipcode, secured_party, collateral, filing_date, lapse_date, days_to_lapse)
                            VALUES (:id, :source_state, :file_id, :company_name, :address, :city, :state, :zipcode, :secured_party, :collateral, :filing_date, :lapse_date, :days_to_lapse)
                        """, lead)
                        if conn.total_changes > 0:
                            total_recovered += 1
                    except Exception as e:
                        pass
        except Exception as e:
            print(f"Error reading {file}: {e}")
            
    conn.commit()
    conn.close()
    print(f"Recovered {total_recovered} Capex leads from JSON logs.")

if __name__ == "__main__":
    recover_capex()
