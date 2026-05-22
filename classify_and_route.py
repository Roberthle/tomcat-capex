"""
Tomcat Lead Classifier & Router
================================
Reads every lead in the Capex DB, classifies it as:
  - EQUIPMENT  → stays in tomcat_capex.db, sets lien_type='equipment'
  - BLANKET    → migrated to tomcat_mca.db,  sets lien_type='blanket'

Classification uses three signals (any one match wins):
  1. collateral field keywords
  2. secured_party (known equipment lenders vs MCA/blanket funders)
  3. company_name / tech_category for domain hints

Run:
    python3 classify_and_route.py --dry-run   # preview counts only
    python3 classify_and_route.py             # execute migration
"""

import sqlite3
import json
import re
import argparse
from datetime import datetime

CAPEX_DB = '/Users/robertle/tomcat_capex/leads/tomcat_capex.db'
MCA_DB   = '/Users/robertle/tomcat_mca/leads/tomcat_mca.db'

# ── Classification keyword lists ──────────────────────────────────────────────

# Secured parties / lenders that are pure equipment finance
EQUIPMENT_LENDER_KEYWORDS = [
    'equipment finance', 'equipment leasing', 'equipment funding',
    'caterpillar financial', 'cat financial', 'komatsu financial',
    'john deere financial', 'deere financial', 'cnh industrial capital',
    'dell financial services', 'xerox financial', 'canon financial',
    'farm credit leasing', 'farm credit east',
    'key equipment finance', 'keystone equipment',
    'u.s. bank equipment', 'us bank equipment',
    'tcf equipment', 'western equipment finance',
    'de lage landen', 'dll finance',
    'leaf capital funding', 'vend lease',
    'nauticon', 'oakmont capital',
    'machinery finance', 'takeuchi financial',
    'yamaha motor finance', 'daimler',
    'construction equipment', 'forklift',
    'm2 equipment', 'stearns bank',
    'automotive finance corporation',
    'reyna capital', 'targeted lease capital',
    'geneva capital', 'blackriver business capital',
    'sumitomo mitsui finance and leasing',
    'adp commercial leasing', 'sachem capital',
    '1st source bank, construction',
    'wells fargo vendor financial',
]

# Secured parties that signal MCA / blanket receivables liens
BLANKET_LENDER_KEYWORDS = [
    'future receivables', 'all assets', 'all personal property',
    'all business assets', 'merchant cash', 'mca',
    'fundthrough', 'advanced flower capital',
    'star capital group', 'internal revenue service', 'irs',
    'idaho state tax commission', 'state tax commission',
    'department of revenue', 'department of taxation',
    'small business administration', 'sba',
    'united states of america acting',
    'u.s. department of agriculture', 'usda',
    'continental bank',           # general commercial bank — treat as blanket
    'texas capital bank',         # general commercial bank
    'bank of america',
    'wells fargo bank',
    'jpmorgan chase',
    'first interstate bank',
    'washington trust bank',
    'u.s. bank national association',  # general (not equipment division)
    'pacific western bank',
]

# Collateral field phrases that definitively signal equipment
EQUIPMENT_COLLATERAL_KEYWORDS = [
    'equipment financing', 'equipment/general', 'tech equipment',
    'equipment lease', 'leased equipment', 'titled equipment',
    'specific equipment', 'machinery', 'vehicle', 'fleet',
    'forklift', 'copier', 'printer', 'server', 'computer',
    'construction equipment', 'agricultural equipment',
    'medical equipment', 'dental equipment',
]

# Collateral field phrases that signal blanket
BLANKET_COLLATERAL_KEYWORDS = [
    'future receivables', 'all assets', 'all personal property',
    'all business assets', 'accounts receivable', 'inventory',
    'general intangibles', 'deposit accounts',
]

# tech_category values that confirm equipment
EQUIPMENT_TECH_CATS = {
    'GA_EQUIPMENT', 'PRINT_IMAGING', 'IT_CHANNEL', 'IT_OEM', 'CLOUD_SAAS'
}


def classify(row):
    """
    Returns 'equipment', 'blanket', or 'unknown'.
    Checks in order: collateral → secured_party → tech_category
    """
    col   = (row['collateral']    or '').lower()
    sp    = (row['secured_party'] or '').lower()
    cat   = (row['tech_category'] or '')
    cdesc = (row.get('collateral_desc') or '').lower()

    # 1. Explicit collateral field check
    for kw in BLANKET_COLLATERAL_KEYWORDS:
        if kw in col or kw in cdesc:
            return 'blanket'

    for kw in EQUIPMENT_COLLATERAL_KEYWORDS:
        if kw in col:
            return 'equipment'

    # 2. Secured party check (blanket first — more specific)
    for kw in BLANKET_LENDER_KEYWORDS:
        if kw in sp:
            return 'blanket'

    for kw in EQUIPMENT_LENDER_KEYWORDS:
        if kw in sp:
            return 'equipment'

    # 3. tech_category
    if cat in EQUIPMENT_TECH_CATS:
        return 'equipment'

    return 'unknown'


def ensure_lien_type_column(conn):
    """Add lien_type column to capex DB if not present."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ucc_leads)").fetchall()]
    if 'lien_type' not in cols:
        conn.execute("ALTER TABLE ucc_leads ADD COLUMN lien_type TEXT")
        conn.commit()
        print("[+] Added lien_type column to ucc_leads")


def get_mca_columns(mca_conn):
    return [r[1] for r in mca_conn.execute("PRAGMA table_info(mca_leads)").fetchall()]


def migrate_to_mca(row, mca_conn):
    """Insert a blanket-lien lead into the MCA DB."""
    try:
        mca_conn.execute("""
            INSERT OR IGNORE INTO mca_leads
            (company_name, address, city, state, zipcode,
             source_state, secured_party, collateral_desc,
             filing_date, lapse_date, days_to_lapse, file_id,
             created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            row['company_name'],
            row.get('address'),
            row.get('city'),
            row.get('state'),
            row.get('zipcode'),
            row.get('source_state'),
            row.get('secured_party'),
            row.get('collateral') or 'Blanket — All Assets / General',
            row.get('filing_date'),
            row.get('lapse_date'),
            row.get('days_to_lapse'),
            row.get('file_id'),
            datetime.now().isoformat(),
        ))
        return mca_conn.total_changes > 0
    except Exception as e:
        print(f"  [!] MCA insert error for {row['company_name']}: {e}")
        return False


def run(dry_run=False):
    capex = sqlite3.connect(CAPEX_DB)
    capex.row_factory = sqlite3.Row
    mca   = sqlite3.connect(MCA_DB)
    mca.row_factory = sqlite3.Row

    ensure_lien_type_column(capex)

    rows = capex.execute("SELECT * FROM ucc_leads").fetchall()
    print(f"\n[*] Classifying {len(rows):,} Capex leads...")

    counts    = {'equipment': 0, 'blanket': 0, 'unknown': 0}
    migrated  = 0
    by_state  = {}

    for row in rows:
        lien = classify(dict(row))
        counts[lien] += 1

        state = row['source_state'] or 'Unknown'
        by_state.setdefault(state, {'equipment': 0, 'blanket': 0, 'unknown': 0})
        by_state[state][lien] += 1

        if not dry_run:
            # Tag every record in Capex DB
            capex.execute(
                "UPDATE ucc_leads SET lien_type=? WHERE id=?",
                [lien, row['id']]
            )
            # Migrate blanket → MCA DB
            if lien == 'blanket':
                ok = migrate_to_mca(dict(row), mca)
                if ok:
                    migrated += 1

    if not dry_run:
        capex.commit()
        mca.commit()

    # ── Report ─────────────────────────────────────────────────────────────
    print(f"\n{'='*58}")
    print(f"  CLASSIFICATION RESULTS {'(DRY RUN)' if dry_run else '(APPLIED)'}")
    print(f"{'='*58}")
    print(f"  ✅ Equipment  : {counts['equipment']:>7,}  → stays in Capex DB")
    print(f"  ⚠️  Blanket    : {counts['blanket']:>7,}  → moved to MCA DB")
    print(f"  ❓ Unknown    : {counts['unknown']:>7,}  → tagged, stays in Capex DB")
    print(f"  {'Migrated' if not dry_run else 'Would migrate':<10}: {counts['blanket']:>7,} blanket leads")
    print(f"\n  By state:")
    for state, sc in sorted(by_state.items(), key=lambda x: -sum(x[1].values())):
        total = sum(sc.values())
        print(f"    {state:<15} total={total:>6,}  equip={sc['equipment']:>5,}  "
              f"blanket={sc['blanket']:>5,}  unknown={sc['unknown']:>4,}")

    # Sample unknown for manual review
    unknowns = [r for r in rows if classify(dict(r)) == 'unknown']
    if unknowns:
        print(f"\n  Sample 'unknown' leads (manual review needed):")
        for r in unknowns[:8]:
            print(f"    • {(r['company_name'] or '')[:40]}")
            print(f"      collateral : {(r['collateral'] or '')[:50]}")
            print(f"      secured_by : {(r['secured_party'] or '')[:50]}")

    capex.close()
    mca.close()
    print(f"\n{'='*58}\n")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview only — no DB changes')
    args = parser.parse_args()
    run(dry_run=args.dry_run)
