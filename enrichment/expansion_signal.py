"""
Tomcat Capex — Business Expansion Signal
/Users/robertle/tomcat_capex/enrichment/expansion_signal.py

Scans Google News RSS for articles about businesses:
  - Opening new locations
  - Winning contracts
  - Expanding fleet/facility
  - Breaking ground
  - Revenue/growth milestones

These are the highest-intent signals: a company that has an expiring
UCC-1 AND is publicly expanding = they NEED equipment financing right now.

Signal tier: S2 (news alone) or upgrades existing lead to S4 PRIME
"""

import os, sys, re, time, sqlite3, json, logging, random
import requests
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import quote_plus

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')

log = logging.getLogger("TomcatCapex.Expansion")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# Keywords that confirm expansion/growth requiring equipment
EXPANSION_TRIGGERS = [
    # Physical expansion
    "new location", "new facility", "new branch", "new office",
    "grand opening", "opening soon", "opens doors", "ribbon cutting",
    "breaking ground", "groundbreaking", "new warehouse", "new plant",
    "new headquarters", "relocating to", "expanded to", "expands into",
    # Contract wins
    "awarded contract", "wins contract", "secures contract",
    "awarded bid", "wins bid", "selected for", "chosen for",
    "awarded project", "new project", "major project",
    # Fleet/equipment growth
    "fleet expansion", "new equipment", "new vehicles", "new trucks",
    "additional equipment", "fleet upgrade", "fleet growth",
    # Financial growth
    "revenue growth", "record revenue", "raises funding", "secures funding",
    "million dollar", "million contract", "new investment",
    # Hiring = growth
    "hiring", "new employees", "creating jobs", "adding jobs", "workforce",
]

# Keywords that indicate this is NOT what we want
NOISE_TRIGGERS = [
    "closes", "bankrupt", "shutdown", "laid off", "layoffs",
    "sold to", "acquired by", "ceases", "discontinue",
]


def gnews_search(query: str, days_back: int = 365) -> list:
    """Search Google News RSS for articles matching query."""
    url = (f"https://news.google.com/rss/search"
           f"?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en")
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = []
        cutoff = datetime.now() - timedelta(days=days_back)

        for item in root.findall('.//item'):
            title  = (item.findtext('title') or '').strip()
            desc   = (item.findtext('description') or '').strip()
            pub    = (item.findtext('pubDate') or '').strip()
            link   = (item.findtext('link') or '').strip()
            source = (item.findtext('source') or '').strip()

            # Try to parse date
            try:
                from email.utils import parsedate_to_datetime
                pub_dt = parsedate_to_datetime(pub)
                if pub_dt.replace(tzinfo=None) < cutoff:
                    continue
            except:
                pass  # If can't parse, include it

            items.append({
                'title': title,
                'desc': re.sub(r'<[^>]+>', '', desc)[:200],
                'pub': pub,
                'link': link,
                'source': source,
            })
        return items
    except Exception as e:
        log.debug(f"GNews error: {e}")
        return []


def check_expansion(company: str, city: str, state: str) -> dict:
    """
    Check Google News for business expansion signals.
    Returns signal dict or empty dict.
    """
    # Strip legal suffixes for cleaner search
    clean = re.sub(
        r'\b(LLC|INC|CORP|LTD|CO|LP|LLP|PARTNERSHIP|ASSOCIATES|GROUP)\.?\b',
        '', company, flags=re.IGNORECASE
    ).strip().strip(',').strip()

    # Try multiple search strategies
    queries = [
        f'"{clean}" {city} "new location" OR "expanding" OR "new facility"',
        f'"{clean}" {state} contract OR opening OR expansion OR awarded',
        f'"{clean}" {city} {state}',
    ]

    best_match = None
    best_score = 0

    for query in queries:
        articles = gnews_search(query, days_back=365)
        time.sleep(random.uniform(0.8, 1.5))

        for art in articles[:5]:
            full_text = (art['title'] + ' ' + art['desc']).lower()

            # Skip noise
            if any(noise in full_text for noise in NOISE_TRIGGERS):
                continue

            # Score expansion signals
            triggers_found = [t for t in EXPANSION_TRIGGERS if t in full_text]
            score = len(triggers_found) * 10

            # Bonus: company name appears in article title
            if clean.lower()[:8] in art['title'].lower():
                score += 20

            # Bonus: city/state mentioned
            if city.lower() in full_text or state.lower() in full_text:
                score += 10

            if score > best_score:
                best_score = score
                best_match = {
                    'article': art,
                    'triggers': triggers_found,
                    'score': score,
                }

    if best_match and best_score >= 20:
        art = best_match['article']
        triggers = best_match['triggers']

        # Build human-readable detail
        primary_trigger = triggers[0].title() if triggers else 'Business Activity'
        snippet = art['title'][:120] if art['title'] else art['desc'][:120]
        source  = art.get('source', 'News')

        # Build a clickable search link from the article title
        # (Google News RSS links are obfuscated and don't resolve for end users)
        title_for_search = art['title'].rsplit(' - ', 1)[0].strip() if ' - ' in art['title'] else art['title']
        search_link = f"https://www.google.com/search?q={quote_plus(title_for_search)}"

        return {
            "type":    "S2_EXPANSION",
            "label":   "📰 Business Expansion",
            "detail":  f"{primary_trigger} — {snippet}",
            "source":  source,
            "pub":     art.get('pub', ''),
            "link":    search_link,
            "triggers": triggers[:3],
            "weight":  30,  # High weight — confirms active growth
        }

    return {}


def run_expansion_signals(limit=200, hot_only=False, reset=False):
    """Run expansion signal check on leads without news signals."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    where = ["company_name IS NOT NULL"]
    if not reset:
        # Only leads that haven't been checked yet, or were checked without expansion signal
        where.append("""(
            signals_checked_at IS NULL OR
            (signals_json IS NOT NULL AND signals_json NOT LIKE '%S2_EXPANSION%')
        )""")
    if hot_only:
        where.append("CAST(days_to_lapse AS INTEGER) BETWEEN 0 AND 90")

    rows = conn.execute(f"""
        SELECT id, company_name, city, state, zipcode, source_state,
               secured_party, collateral, days_to_lapse,
               signals_json, signal_score, signal_tier
        FROM ucc_leads WHERE {' AND '.join(where)}
        ORDER BY CASE WHEN days_to_lapse IS NULL THEN 9999
                      ELSE CAST(days_to_lapse AS INTEGER) END ASC
        LIMIT ?
    """, [limit]).fetchall()
    conn.close()

    leads = [dict(r) for r in rows]
    log.info(f"Checking expansion signals for {len(leads)} leads")

    hits = 0
    for i, lead in enumerate(leads, 1):
        company = lead['company_name']
        city    = lead.get('city', '') or ''
        state   = lead.get('state', '') or ''
        dtl     = lead.get('days_to_lapse', '?')

        log.info(f"[{i}/{len(leads)}] {company} ({lead.get('source_state')}) — {dtl}d")

        expansion = check_expansion(company, city, state)

        if expansion:
            hits += 1
            log.info(f"  ✅ EXPANSION: {expansion['detail'][:80]}")

            # Merge with existing signals
            existing = []
            try:
                existing = json.loads(lead.get('signals_json') or '[]')
            except:
                pass

            # Remove any old expansion signal, add new one
            existing = [s for s in existing if s.get('type') != 'S2_EXPANSION']
            existing.append(expansion)

            # Recompute tier
            base = 10
            total_weight = base + sum(s.get('weight', 0) for s in existing)
            n = len(existing)
            tier = 'S4' if n >= 2 else \
                   'S3' if n == 1 and existing[0]['type'].startswith('S3') else \
                   'S2' if n == 1 else 'S1'

            conn2 = sqlite3.connect(DB_PATH)
            conn2.execute("""
                UPDATE ucc_leads
                SET signals_json = ?, signal_score = ?, signal_tier = ?,
                    signals_checked_at = ?
                WHERE id = ?
            """, [json.dumps(existing), total_weight, tier,
                  datetime.now().isoformat(), lead['id']])
            conn2.commit()
            conn2.close()
        else:
            log.info(f"  ⚪ No expansion news found")

        time.sleep(random.uniform(1.5, 2.5))

    log.info(f"\n{'='*55}")
    log.info(f"  Expansion Signal Run Complete")
    log.info(f"  Processed : {len(leads)}")
    log.info(f"  Hits      : {hits} ({100*hits//max(len(leads),1)}%)")
    log.info(f"  Miss      : {len(leads)-hits}")
    return hits


if __name__ == '__main__':
    import argparse
    logging.basicConfig(level=logging.INFO,
        format='%(asctime)s [Expansion] %(levelname)s - %(message)s')
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=200)
    parser.add_argument('--hot-only', action='store_true')
    parser.add_argument('--reset', action='store_true',
                        help='Re-check all leads even if already checked')
    args = parser.parse_args()
    run_expansion_signals(limit=args.limit,
                          hot_only=args.hot_only,
                          reset=args.reset)
