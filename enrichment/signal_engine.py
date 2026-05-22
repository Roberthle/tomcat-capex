"""
Tomcat Capex — Proxy Signal Engine
/Users/robertle/tomcat_capex/enrichment/signal_engine.py

For each UCC lead, stacks additional buying signals to score lead quality:

SIGNAL TIERS:
  S1 — UCC Confirmed (base — all leads have this)
  S2 — Recent News/PR  (company mentioned in news in last 90 days)
  S3 — Hiring Signal   (company is actively hiring equipment operators/drivers)
  S4 — Multi-Signal    (2+ signals confirmed = PRIME lead)

These signals are visible in the broker portal as signal badges on each lead.
Brokers see WHY a company needs financing, not just that they DO.

Run: python3 signal_engine.py [--limit N] [--hot-only]
"""

import os, sys, re, time, sqlite3, json, logging, argparse, random
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [SignalEngine] %(levelname)s - %(message)s')
logger = logging.getLogger("TomcatCapex.Signals")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# Equipment job titles — signals company is growing equipment fleet
EQUIPMENT_JOB_SIGNALS = [
    "equipment operator", "heavy equipment", "forklift operator",
    "crane operator", "truck driver", "cdl driver", "fleet driver",
    "equipment technician", "machine operator", "excavator",
    "backhoe", "bulldozer", "loader operator", "field technician",
    "hvac technician", "electrician", "plumber", "mechanic",
    "service technician", "construction worker", "laborer"
]

# ── DB setup ──────────────────────────────────────────────────────────────────

def init_signals_table():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("ALTER TABLE ucc_leads ADD COLUMN signal_score INTEGER DEFAULT 1")
        conn.execute("ALTER TABLE ucc_leads ADD COLUMN signals_json TEXT")
        conn.execute("ALTER TABLE ucc_leads ADD COLUMN signal_tier TEXT DEFAULT 'S1'")
        conn.execute("ALTER TABLE ucc_leads ADD COLUMN signals_checked_at TEXT")
    except:
        pass
    conn.commit()
    conn.close()


def save_signals(lead_id: str, signals: list, score: int, tier: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE ucc_leads
        SET signal_score = ?, signals_json = ?, signal_tier = ?,
            signals_checked_at = ?
        WHERE id = ?
    """, [score, json.dumps(signals), tier, datetime.now().isoformat(), lead_id])
    conn.commit()
    conn.close()


def get_leads_for_signals(limit=100, hot_only=False) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    where = "company_name IS NOT NULL AND signals_checked_at IS NULL"
    if hot_only:
        where += " AND days_to_lapse <= 30"
    rows = conn.execute(f"""
        SELECT id, company_name, city, state, zipcode, source_state,
               secured_party, collateral, days_to_lapse
        FROM ucc_leads WHERE {where}
        ORDER BY CASE WHEN days_to_lapse IS NULL THEN 9999 ELSE days_to_lapse END ASC
        LIMIT ?
    """, [limit]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Signal Checks ─────────────────────────────────────────────────────────────

def ddg_search(query: str, num=5) -> list[dict]:
    try:
        r = requests.post("https://html.duckduckgo.com/html/",
                          data={"q": query, "kl": "us-en"},
                          headers=HEADERS, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')
        results = []
        for res in soup.select('.result')[:num]:
            title   = res.select_one('.result__title')
            snippet = res.select_one('.result__snippet')
            results.append({
                'title':   (title.get_text(strip=True) if title else '').lower(),
                'snippet': (snippet.get_text(strip=True) if snippet else '').lower(),
            })
        return results
    except:
        return []


def check_news_signal(company: str, city: str, state: str):
    """
    S2: Company appears in recent news — expansion, contract win, growth.
    Returns signal dict or None.
    """
    q = f'"{company}" {city} {state} news 2025 OR 2026'
    results = ddg_search(q, num=5)
    time.sleep(random.uniform(1.0, 2.0))

    positive_kw = [
        'expand', 'growth', 'contract', 'award', 'open', 'launch',
        'hire', 'acqui', 'win', 'partner', 'new facility', 'new location',
        'revenue', 'million', 'project', 'development'
    ]
    for res in results:
        text = res['title'] + ' ' + res['snippet']
        # Must mention company AND a positive signal
        if any(company.lower()[:12] in text for _ in [1]):
            if any(kw in text for kw in positive_kw):
                # Find which trigger
                trigger = next((kw for kw in positive_kw if kw in text), 'activity')
                snippet_preview = (res['title'] or res['snippet'])[:100]
                return {
                    "type": "S2_NEWS",
                    "label": "📰 Recent News",
                    "detail": snippet_preview,
                    "trigger": trigger,
                    "weight": 20
                }
    return None


def check_hiring_signal(company: str, city: str, state: str):
    """
    S3: Company is actively hiring equipment-related roles.
    Signals: fleet expansion, new job openings, growth.
    """
    q = f'"{company}" {city} {state} jobs hiring 2026'
    results = ddg_search(q, num=5)
    time.sleep(random.uniform(1.0, 2.0))

    for res in results:
        text = res['title'] + ' ' + res['snippet']
        # Must have a job-related signal
        if any(kw in text for kw in ['job', 'career', 'hire', 'position', 'opening', 'indeed', 'ziprecruiter']):
            # Check if it's equipment-related
            if any(eq in text for eq in EQUIPMENT_JOB_SIGNALS):
                eq_kw = next((eq for eq in EQUIPMENT_JOB_SIGNALS if eq in text), 'equipment')
                return {
                    "type": "S3_HIRING",
                    "label": "📋 Actively Hiring",
                    "detail": f"Seeking {eq_kw} — fleet growth signal",
                    "trigger": eq_kw,
                    "weight": 25
                }
            # General hiring still a signal
            if any(kw in text for kw in ['hiring', 'now hiring', 'job opening']):
                return {
                    "type": "S3_HIRING",
                    "label": "📋 Hiring Activity",
                    "detail": "Active job postings detected",
                    "trigger": "hiring",
                    "weight": 15
                }
    return None


def check_growth_signal(company: str, city: str, state: str):
    """
    S3B: Company has recent social/web presence showing growth.
    """
    q = f'"{company}" {city} equipment financing loan lease 2025 OR 2026'
    results = ddg_search(q, num=3)
    time.sleep(random.uniform(0.8, 1.5))

    for res in results:
        text = res['title'] + ' ' + res['snippet']
        if any(kw in text for kw in ['equipment', 'fleet', 'machinery', 'vehicle', 'truck']):
            if any(kw in text for kw in ['financ', 'loan', 'lease', 'fund', 'capital']):
                return {
                    "type": "S3_FINANCE_ACTIVE",
                    "label": "💰 Finance Active",
                    "detail": "Company actively engaged in equipment financing",
                    "trigger": "equipment financing",
                    "weight": 20
                }
    return None


def compute_tier(signals: list):
    """Return (tier_label, score) based on confirmed signals."""
    base_score = 10  # S1 base — confirmed UCC filing
    signal_score = sum(s.get('weight', 0) for s in signals)
    total = base_score + signal_score

    confirmed = len(signals)
    if confirmed >= 2:
        tier = 'S4'  # PRIME — multi-signal
    elif confirmed == 1:
        t = signals[0]['type']
        if 'NEWS' in t:
            tier = 'S2'
        else:
            tier = 'S3'
    else:
        tier = 'S1'

    return tier, total


# ── Main run ─────────────────────────────────────────────────────────────────

def run(limit=100, hot_only=False):
    init_signals_table()
    leads = get_leads_for_signals(limit=limit, hot_only=hot_only)
    logger.info(f"Stacking signals for {len(leads)} leads")

    s1_count = s2_count = s3_count = s4_count = 0

    for i, lead in enumerate(leads, 1):
        company = lead['company_name']
        city    = lead.get('city', '')
        state   = lead.get('state', '')
        dtl     = lead.get('days_to_lapse', '?')

        logger.info(f"[{i}/{len(leads)}] {company} ({state}) — {dtl}d")

        signals = []

        # S2: News check
        news = check_news_signal(company, city, state)
        if news:
            signals.append(news)
            logger.info(f"  ✅ {news['label']}: {news['detail'][:60]}")

        # S3: Hiring check
        hiring = check_hiring_signal(company, city, state)
        if hiring:
            signals.append(hiring)
            logger.info(f"  ✅ {hiring['label']}: {hiring['detail'][:60]}")

        # S3B: Finance activity (only if no other signals yet)
        if not signals:
            growth = check_growth_signal(company, city, state)
            if growth:
                signals.append(growth)
                logger.info(f"  ✅ {growth['label']}: {growth['detail'][:60]}")

        tier, score = compute_tier(signals)

        if tier == 'S1': s1_count += 1
        elif tier == 'S2': s2_count += 1
        elif tier == 'S3': s3_count += 1
        elif tier == 'S4': s4_count += 1

        if not signals:
            logger.info(f"  ⚪ No additional signals (S1 — UCC confirmed only)")

        save_signals(lead['id'], signals, score, tier)
        time.sleep(random.uniform(1.5, 2.5))

    logger.info(f"\n{'='*55}")
    logger.info(f"  Signal Stack Complete — {len(leads)} leads")
    logger.info(f"  S1 (UCC only)     : {s1_count}")
    logger.info(f"  S2 (+News)        : {s2_count}")
    logger.info(f"  S3 (+Hiring)      : {s3_count}")
    logger.info(f"  S4 PRIME (Multi)  : {s4_count} ⭐")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--hot-only', action='store_true')
    args = parser.parse_args()
    run(limit=args.limit, hot_only=args.hot_only)
