import os
import shutil
from datetime import datetime
import sqlite3

def backup_databases():
    capex_db = "/Users/robertle/tomcat_capex/leads/tomcat_capex.db"
    mca_db = "/Users/robertle/tomcat_mca/leads/tomcat_mca.db"
    backup_dir = "/Users/robertle/tomcat_capex/backups"
    
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)
        
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Backup Capex DB
    if os.path.exists(capex_db):
        capex_backup = os.path.join(backup_dir, f"tomcat_capex_{timestamp}.db")
        # Use sqlite3 backup API for safe atomic copy while DB is active
        with sqlite3.connect(capex_db) as src, sqlite3.connect(capex_backup) as dst:
            src.backup(dst)
        print(f"✅ Capex DB backed up to {capex_backup}")
        
    # Backup MCA DB
    if os.path.exists(mca_db):
        mca_backup = os.path.join(backup_dir, f"tomcat_mca_{timestamp}.db")
        with sqlite3.connect(mca_db) as src, sqlite3.connect(mca_backup) as dst:
            src.backup(dst)
        print(f"✅ MCA DB backed up to {mca_backup}")

    # Rotate old backups (keep last 7 days)
    print("🧹 Cleaning up old backups (>7 days)...")
    for file in os.listdir(backup_dir):
        if file.endswith(".db"):
            path = os.path.join(backup_dir, file)
            # if older than 7 days
            if os.path.getmtime(path) < (datetime.now().timestamp() - 7 * 86400):
                os.remove(path)
                print(f"Removed old backup: {file}")

if __name__ == "__main__":
    print("Starting automated Tomcat Vault backup...")
    backup_databases()
    print("Backup complete.")
