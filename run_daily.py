#!/usr/bin/env python3
"""
Tomcat Capex — Daily Pipeline Runner
/Users/robertle/tomcat_capex/run_daily.py

Runs every morning at 6am via launchd.
Steps:
  1. Scrape Colorado UCC (new leads expiring in next 180 days)
  2. Scrape Connecticut UCC (new leads)
  3. Run expansion signal check on all unchecked leads
  4. Log summary to logs/daily_YYYY-MM-DD.log

Usage: python3 run_daily.py
"""

import os, sys, json, logging, sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR  = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

log_file = os.path.join(LOG_DIR, f"daily_{datetime.now().strftime('%Y-%m-%d')}.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [DailyPipeline] %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("TomcatCapex.Daily")


def count_db_leads():
    db = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')
    conn = sqlite3.connect(db)
    total = conn.execute("SELECT COUNT(*) FROM ucc_leads").fetchone()[0]
    hot   = conn.execute("SELECT COUNT(*) FROM ucc_leads WHERE CAST(days_to_lapse AS INTEGER) <= 30 AND CAST(days_to_lapse AS INTEGER) >= 0").fetchone()[0]
    warm  = conn.execute("SELECT COUNT(*) FROM ucc_leads WHERE CAST(days_to_lapse AS INTEGER) > 30 AND CAST(days_to_lapse AS INTEGER) <= 90").fetchone()[0]
    s4    = conn.execute("SELECT COUNT(*) FROM ucc_leads WHERE signal_tier = 'S4'").fetchone()[0]
    news  = conn.execute("SELECT COUNT(*) FROM ucc_leads WHERE signals_json LIKE '%S2_EXPANSION%'").fetchone()[0]
    conn.close()
    return {"total": total, "hot": hot, "warm": warm, "s4_prime": s4, "expansion_hits": news}


def run_scraper(scraper_path: str, name: str) -> bool:
    import subprocess
    log.info(f"Running {name} scraper...")
    result = subprocess.run(
        [sys.executable, scraper_path],
        cwd=BASE_DIR,
        capture_output=True, text=True,
        timeout=3600,
    )
    if result.returncode == 0:
        # Extract key stats from output
        for line in result.stdout.splitlines()[-10:]:
            if any(k in line for k in ['leads found', 'Total', 'Complete', 'New leads']):
                log.info(f"  {name}: {line.strip()}")
        return True
    else:
        log.error(f"  {name} failed: {result.stderr[-300:]}")
        return False


def run_expansion_signals(limit=300):
    log.info(f"Running expansion signal check (limit={limit})...")
    sys.path.insert(0, os.path.join(BASE_DIR, 'enrichment'))
    try:
        from expansion_signal import run_expansion_signals as _run
        hits = _run(limit=limit)
        log.info(f"  Expansion signals: {hits} new hits")
    except Exception as e:
        log.error(f"  Expansion signal error: {e}")


def run_hiring_signals(limit=200):
    log.info(f"Running hiring signal check (limit={limit}, hot leads only)...")
    sys.path.insert(0, os.path.join(BASE_DIR, 'enrichment'))
    try:
        from hiring_signal import run_hiring_signals as _run
        hits = _run(limit=limit, hot_only=True)
        log.info(f"  Hiring signals: {hits} new hits")
    except Exception as e:
        log.error(f"  Hiring signal error: {e}")


def run_edgar_signals(limit=150):
    log.info(f"Running EDGAR signal check (limit={limit}, hot leads only)...")
    sys.path.insert(0, os.path.join(BASE_DIR, 'enrichment'))
    try:
        from edgar_signal import run_edgar_signals as _run
        hits = _run(limit=limit, hot_only=True)
        log.info(f"  EDGAR signals: {hits} new hits")
    except Exception as e:
        log.error(f"  EDGAR signal error: {e}")


def main():
    log.info("=" * 60)
    log.info("  TOMCAT CAPEX DAILY PIPELINE")
    log.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    before = count_db_leads()
    log.info(f"DB before: {before}")

    # Step 1: Colorado
    co_ok = run_scraper(
        os.path.join(BASE_DIR, 'scrapers', 'ucc_scraper.py'), 'Colorado'
    )

    # Step 2: Connecticut
    ct_ok = run_scraper(
        os.path.join(BASE_DIR, 'scrapers', 'ct_ucc_scraper.py'), 'Connecticut'
    )

    # Step 3: Texas
    tx_ok = run_scraper(
        os.path.join(BASE_DIR, 'scrapers', 'tx_ucc_scraper.py'), 'Texas'
    )

    # Step 4: Georgia (requires GSCCCA_USER env var — skips if not set)
    ga_ok = False
    if os.environ.get('GSCCCA_USER'):
        ga_ok = run_scraper(
            os.path.join(BASE_DIR, 'scrapers', 'ga_ucc_scraper.py'), 'Georgia'
        )
    else:
        log.info('  Georgia scraper skipped — GSCCCA_USER not set')

    run_expansion_signals(limit=300)
    run_hiring_signals(limit=200)
    run_edgar_signals(limit=150)

    after = count_db_leads()
    new_leads = after['total'] - before['total']

    log.info("=" * 60)
    log.info(f"  DAILY SUMMARY")
    log.info(f"  New leads added : {new_leads}")
    log.info(f"  Total in DB     : {after['total']}")
    log.info(f"  HOT (≤30d)      : {after['hot']}")
    log.info(f"  WARM (≤90d)     : {after['warm']}")
    log.info(f"  S4 PRIME        : {after['s4_prime']} ⭐")
    log.info(f"  Expansion hits  : {after['expansion_hits']} 📰")
    log.info(f"  Scrapers OK     : CO={co_ok} CT={ct_ok} TX={tx_ok} GA={ga_ok}")
    log.info("=" * 60)

    # Write summary JSON for portal
    summary_path = os.path.join(LOG_DIR, 'last_run.json')
    with open(summary_path, 'w') as f:
        json.dump({
            "ran_at": datetime.now().isoformat(),
            "new_leads": new_leads,
            "stats": after,
            "scrapers": {"co": co_ok, "ct": ct_ok, "tx": tx_ok, "ga": ga_ok},
        }, f, indent=2)
    log.info(f"  Summary written: {summary_path}")


if __name__ == '__main__':
    main()
