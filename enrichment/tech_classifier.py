"""
Tomcat Capex — Tech Company Classifier
/Users/robertle/tomcat_capex/enrichment/tech_classifier.py

Tags UCC leads as tech companies based on:
  1. Secured party (lender) matches known tech equipment financiers
  2. Collateral description mentions tech equipment
  3. Company name contains tech industry keywords

Tech company UCC leads are 10-50x more valuable to brokers because:
  - Higher deal sizes ($500K-$50M vs $50K for a landscaper)
  - Repeat financing cycles (3-5 year refresh)
  - SEC-matchable (public tech co = EDGAR signals fire)
  - Better broker commissions

Run: python3 tech_classifier.py
"""

import os, sqlite3, re, logging, json
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')

log = logging.getLogger("TomcatCapex.TechClassifier")

# ── Tech Lenders (Secured Parties) ──────────────────────────────────────────

TECH_LENDERS = [
    # IT OEM financing arms
    "DELL FINANCIAL", "DELL TECHNOLOGIES",
    "HEWLETT-PACKARD", "HP FINANCIAL", "HP INC",
    "LENOVO FINANCIAL", "LENOVO",
    "IBM CREDIT", "IBM CORP",
    "CISCO SYSTEMS", "CISCO CAPITAL",
    "ORACLE CREDIT", "ORACLE FINANCIAL",
    "MICROSOFT", "GOOGLE", "APPLE FINANCIAL",
    "NETAPP", "NVIDIA",
    # Print/imaging OEMs
    "XEROX FINANCIAL", "XEROX CORP",
    "CANON FINANCIAL", "CANON U.S.A",
    "KONICA MINOLTA", "RICOH", "SHARP ELECTRONICS",
    "KYOCERA", "TOSHIBA FINANCIAL", "BROTHER",
    # IT channel finance
    "GREATAMERICA FINANCIAL",
    "CIT TECHNOLOGY", "CIT GROUP",
    "TIAA COMMERCIAL", "TIAA BANK",
    "MARLIN BUSINESS", "MARLIN CAPITAL",
    "LEAF COMMERCIAL", "LEAF CAPITAL",
    "ECS FINANCIAL",
    # Cloud/SaaS lenders
    "AMAZON CAPITAL", "AWS",
    "SALESFORCE",
    # Telecom
    "AT&T CAPITAL", "VERIZON CREDIT",
    "T-MOBILE",
]

# ── Tech Collateral Keywords ────────────────────────────────────────────────

TECH_COLLATERAL = [
    "server", "computer", "laptop", "workstation", "desktop",
    "software", "saas", "cloud", "network", "router", "switch",
    "firewall", "data center", "storage", "backup",
    "printer", "copier", "mfp", "multifunct", "scanner",
    "telecom", "telephone", "voip", "pbx", "unified comm",
    "it equipment", "technology", "tech equipment",
    "monitor", "display", "projector",
    "point of sale", "pos system", "kiosk",
    "security camera", "surveillance", "access control",
]

# ── Tech Company Name Keywords ──────────────────────────────────────────────

TECH_COMPANY_NAMES = [
    "technology", "technologies", "tech ",
    "software", "digital", "data ",
    "cyber", "cloud", "computing",
    "it services", "information tech", "infotech",
    "systems inc", "systems llc", "systems corp",
    "telecom", "communications",
    "network", "hosting", "solutions inc",
    "saas", "platform",
]


def classify_tech(secured_party: str, collateral: str, company_name: str) -> dict:
    """
    Returns classification dict:
      is_tech: bool
      tech_reason: str (why it was classified as tech)
      tech_category: str (IT_OEM, IT_CHANNEL, PRINT, TELECOM, CLOUD, GENERAL)
    """
    sp = (secured_party or "").upper()
    coll = (collateral or "").lower()
    name = (company_name or "").lower()

    reasons = []
    category = "GENERAL"

    # Check lender
    for lender in TECH_LENDERS:
        if lender.upper() in sp:
            reasons.append(f"Tech lender: {lender}")
            # Categorize
            if any(x in lender.upper() for x in ["DELL", "HP", "LENOVO", "IBM", "CISCO", "ORACLE", "NETAPP"]):
                category = "IT_OEM"
            elif any(x in lender.upper() for x in ["XEROX", "CANON", "KONICA", "RICOH", "SHARP", "KYOCERA"]):
                category = "PRINT_IMAGING"
            elif any(x in lender.upper() for x in ["AMAZON", "SALESFORCE", "MICROSOFT", "GOOGLE"]):
                category = "CLOUD_SAAS"
            elif any(x in lender.upper() for x in ["AT&T", "VERIZON", "T-MOBILE"]):
                category = "TELECOM"
            elif any(x in lender.upper() for x in ["GREATAMERICA", "CIT", "TIAA", "MARLIN", "LEAF", "ECS"]):
                category = "IT_CHANNEL"
            break

    # Check collateral
    for kw in TECH_COLLATERAL:
        if kw in coll:
            reasons.append(f"Tech collateral: {kw}")
            if not category or category == "GENERAL":
                if any(x in kw for x in ["printer", "copier", "mfp", "scanner"]):
                    category = "PRINT_IMAGING"
                elif any(x in kw for x in ["telecom", "telephone", "voip"]):
                    category = "TELECOM"
                elif any(x in kw for x in ["server", "network", "data center", "cloud"]):
                    category = "IT_INFRASTRUCTURE"
                else:
                    category = "IT_GENERAL"
            break

    # Check company name
    for kw in TECH_COMPANY_NAMES:
        if kw in name:
            reasons.append(f"Tech company name: {kw}")
            if not category or category == "GENERAL":
                category = "TECH_COMPANY"
            break

    is_tech = len(reasons) > 0

    return {
        "is_tech": is_tech,
        "tech_reason": " | ".join(reasons[:3]) if reasons else "",
        "tech_category": category if is_tech else "",
    }


def add_tech_columns():
    """Add tech classification columns to the DB if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    for col in ["tech_company", "tech_category", "tech_reason"]:
        try:
            conn.execute(f"ALTER TABLE ucc_leads ADD COLUMN {col} TEXT")
        except:
            pass  # column exists
    conn.commit()
    conn.close()


def run_classification():
    """Classify all leads in the database."""
    add_tech_columns()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, company_name, secured_party, collateral
        FROM ucc_leads
    """).fetchall()

    tech_count = 0
    categories = {}

    for row in rows:
        result = classify_tech(row["secured_party"], row["collateral"], row["company_name"])

        if result["is_tech"]:
            tech_count += 1
            cat = result["tech_category"]
            categories[cat] = categories.get(cat, 0) + 1

            conn.execute("""
                UPDATE ucc_leads SET tech_company=?, tech_category=?, tech_reason=?
                WHERE id=?
            """, ["true", result["tech_category"], result["tech_reason"], row["id"]])

    conn.commit()
    conn.close()

    log.info(f"\n{'='*55}")
    log.info(f"  Tech Classification Complete")
    log.info(f"  Total leads:  {len(rows)}")
    log.info(f"  Tech leads:   {tech_count} ({100*tech_count//max(len(rows),1)}%)")
    log.info(f"  Categories:")
    for cat, cnt in sorted(categories.items(), key=lambda x: -x[1]):
        log.info(f"    {cat:20} {cnt}")
    log.info(f"{'='*55}")

    return tech_count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
        format="%(asctime)s [TechClassifier] %(levelname)s - %(message)s")
    run_classification()
""
