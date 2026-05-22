"""
capex_to_mca_crossfeed.py
Tomcat Cross-Engine Lead Router

Pulls high-signal leads from Tomcat Capex (ucc_leads) and pushes them into
Tomcat MCA (mca_leads) as cross-sell candidates. The logic:

  - Equipment financing = company has assets and makes payments → MCA eligible
  - Filter: days_to_lapse < 730 (filing not ancient), company_name not null
  - Dedup: skip if already in mca_leads by (company_name, source_state)
  - Enrich MCA fields with reasonable estimates from Capex context

USAGE:
  python3 capex_to_mca_crossfeed.py              # all states
  python3 capex_to_mca_crossfeed.py --state GA   # GA only
  python3 capex_to_mca_crossfeed.py --dry-run    # preview, no write
"""

import sqlite3, json, os, sys, argparse, logging
from datetime import datetime

CAPEX_DB = os.path.expanduser("~/tomcat_capex/leads/tomcat_capex.db")
MCA_DB   = os.path.expanduser("~/tomcat_mca/leads/tomcat_mca.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [CROSSFEED] %(message)s")
log = logging.getLogger("Crossfeed")

# ── Lender → MCA funder tier / industry mapping ────────────────────────────────
LENDER_META = {
    # IT / OEM
    "DELL FINANCIAL":        ("B",   "IT_SERVICES",     350_000),
    "HEWLETT PACKARD":       ("B",   "IT_SERVICES",     500_000),
    "HP FINANCIAL":          ("B",   "IT_SERVICES",     400_000),
    "IBM CREDIT":            ("A",   "IT_SERVICES",     750_000),
    "CISCO SYSTEMS":         ("A",   "IT_SERVICES",     600_000),
    # Print / Imaging
    "KONICA MINOLTA":        ("B",   "PROFESSIONAL",    250_000),
    "XEROX":                 ("B",   "PROFESSIONAL",    300_000),
    "CANON FINANCIAL":       ("B",   "PROFESSIONAL",    200_000),
    "RICOH":                 ("B",   "PROFESSIONAL",    250_000),
    "KYOCERA":               ("B",   "PROFESSIONAL",    180_000),
    # IT Channel
    "GREATAMERICA":          ("B",   "IT_SERVICES",     200_000),
    "MARLIN LEASING":        ("C",   "GENERAL",         150_000),
    "LEAF COMMERCIAL":       ("C",   "GENERAL",         150_000),
    "BALBOA CAPITAL":        ("C",   "GENERAL",         120_000),
    "PAWNEE LEASING":        ("C",   "GENERAL",         100_000),
    # Equipment Finance
    "DLL FINANCE":           ("B",   "MANUFACTURING",   400_000),
    "DE LAGE LANDEN":        ("B",   "MANUFACTURING",   400_000),
    "WELLS FARGO EQUIPMENT": ("A",   "GENERAL",         600_000),
    "US BANCORP EQUIPMENT":  ("A",   "GENERAL",         700_000),
    "KEY EQUIPMENT":         ("A",   "GENERAL",         500_000),
    "STEARNS BANK":          ("B",   "GENERAL",         300_000),
    "BANC OF AMERICA":       ("A",   "GENERAL",         800_000),
    "CIT BANK":              ("A",   "GENERAL",         700_000),
    # Heavy Equipment
    "CATERPILLAR":           ("A",   "CONSTRUCTION",  1_200_000),
    "JOHN DEERE":            ("A",   "AGRICULTURE",   1_000_000),
    "CNH INDUSTRIAL":        ("A",   "AGRICULTURE",     900_000),
    "TOYOTA INDUSTRIES":     ("B",   "DISTRIBUTION",    500_000),
}


def lender_meta(secured_party: str):
    sp = (secured_party or "").upper()
    for key, meta in LENDER_META.items():
        if key in sp:
            return meta
    return ("C", "GENERAL", 150_000)


def est_advance(annual_rev: float) -> float:
    """MCA advance is typically 10–15% of annual revenue."""
    return round(annual_rev * 0.12, -3)


def est_daily(advance: float) -> float:
    """Daily payment = advance × 1.35 factor rate ÷ 180 days."""
    return round(advance * 1.35 / 180, 2)


def already_in_mca(mca_conn, company_name: str, source_state: str) -> bool:
    row = mca_conn.execute(
        "SELECT id FROM mca_leads WHERE company_name=? AND source_state=? LIMIT 1",
        (company_name, source_state)
    ).fetchone()
    return row is not None


def crossfeed(state_filter=None, dry_run=False):
    capex = sqlite3.connect(CAPEX_DB)
    capex.row_factory = sqlite3.Row
    mca   = sqlite3.connect(MCA_DB)

    # Pull high-signal Capex leads
    query = """
        SELECT id, company_name, address, city, state, zipcode,
               source_state, secured_party, collateral,
               filing_date, lapse_date, days_to_lapse, file_id,
               phone, email, contact_name, company_website,
               signal_score, signal_tier, signals_json
        FROM ucc_leads
        WHERE company_name IS NOT NULL
          AND company_name != ''
          AND (days_to_lapse IS NULL OR days_to_lapse > -365)
    """
    params = []
    if state_filter:
        query += " AND source_state = ?"
        params.append(state_filter)

    rows = capex.execute(query, params).fetchall()
    log.info(f"Capex candidates: {len(rows):,} leads{f' ({state_filter})' if state_filter else ''}")

    inserted = 0
    skipped  = 0
    now      = datetime.now().isoformat()

    for r in rows:
        company  = r["company_name"]
        state    = r["source_state"]

        if already_in_mca(mca, company, state):
            skipped += 1
            continue

        tier, industry, annual_rev = lender_meta(r["secured_party"] or "")
        advance    = est_advance(annual_rev)
        daily      = est_daily(advance)

        signals = []
        try:
            signals = json.loads(r["signals_json"] or "[]")
        except Exception:
            pass
        signals.append("capex_crossfeed")

        if not dry_run:
            mca.execute("""
                INSERT INTO mca_leads (
                    company_name, address, city, state, zipcode,
                    source_state, secured_party, collateral_desc,
                    filing_date, lapse_date, days_to_lapse, file_id,
                    stack_depth, position_number,
                    est_advance_amount, est_daily_payment,
                    funder_tier, phone, email, contact_name,
                    company_website, industry,
                    est_annual_revenue, signals_json,
                    signal_score, signal_tier,
                    created_at, updated_at
                ) VALUES (
                    ?,?,?,?,?,
                    ?,?,?,
                    ?,?,?,?,
                    ?,?,
                    ?,?,
                    ?,?,?,?,
                    ?,?,
                    ?,?,
                    ?,?,
                    ?,?
                )
            """, (
                company,
                r["address"], r["city"], r["state"], r["zipcode"],
                state,
                r["secured_party"], r["collateral"],
                r["filing_date"], r["lapse_date"], r["days_to_lapse"], r["file_id"],
                1, 1,
                advance, daily,
                tier,
                r["phone"], r["email"], r["contact_name"],
                r["company_website"], industry,
                annual_rev, json.dumps(signals),
                r["signal_score"] or "0", r["signal_tier"] or "S1",
                now, now
            ))
        inserted += 1

    if not dry_run:
        mca.commit()

    capex.close()
    mca.close()

    action = "Would insert" if dry_run else "Inserted"
    log.info(f"{action} {inserted:,} new leads into MCA | Skipped {skipped:,} duplicates")
    return inserted


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Capex → MCA Cross-Feed Router")
    parser.add_argument("--state",   help="Filter by source_state (e.g. GA, CO)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no write")
    args = parser.parse_args()

    n = crossfeed(state_filter=args.state, dry_run=args.dry_run)
    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Cross-feed complete: {n:,} leads routed to MCA")
