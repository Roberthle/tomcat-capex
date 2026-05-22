"""
Tomcat Capex — Company-Name-First Hiring Signal
/Users/robertle/tomcat_capex/enrichment/hiring_signal.py

COMPANY-NAME-FIRST approach (not keyword-first like the old LeadBot):
  1. Take company name from UCC DB
  2. Search LinkedIn guest jobs endpoint for that specific company
  3. Search Indeed for that specific company
  4. If they are hiring equipment-related roles → S3_HIRING signal

Why this works better than the old approach:
  Old LeadBot searched "equipment operator jobs San Diego" → generic results,
  no reliable company name match back to UCC records.

  This approach searches "THE LANE CONSTRUCTION CORPORATION" jobs → finds
  their specific open positions → "Heavy Equipment Operator (3 openings)" is
  a verified S3 signal tied to an expiring UCC-1. Broker calls with:
  "I saw you're hiring 3 Equipment Operators — your current lien with Wells
  Fargo is expiring in 3 days. Let me help you finance the additional fleet."

LinkedIn guest endpoint: unauthenticated, no API key, returns structured HTML.
Indeed: open URL, returns structured results.

Run: python3 hiring_signal.py [--limit N] [--hot-only]
"""

import os, re, sys, time, json, sqlite3, logging, random, argparse
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import quote_plus

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')

log = logging.getLogger("TomcatCapex.Hiring")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

# Equipment-related job titles that confirm fleet/equipment growth
EQUIPMENT_ROLES = [
    # Operators
    'equipment operator', 'heavy equipment', 'crane operator', 'forklift operator',
    'excavator operator', 'bulldozer operator', 'loader operator', 'backhoe',
    # Drivers
    'cdl driver', 'truck driver', 'fleet driver', 'commercial driver',
    'delivery driver', 'transport driver',
    # Mechanics
    'equipment mechanic', 'fleet mechanic', 'diesel mechanic', 'heavy truck',
    'equipment technician', 'fleet technician',
    # Construction
    'construction worker', 'laborer', 'site supervisor', 'field supervisor',
    'project manager construction', 'general contractor',
    # Industrial
    'warehouse operator', 'material handler', 'machine operator',
    'production operator', 'plant operator',
    # HVAC/Electrical/Plumbing (equipment-intensive)
    'hvac technician', 'electrician', 'plumber', 'pipefitter',
]

LEGAL_SUFFIX = re.compile(
    r'\b(LLC|INC\.?|CORP\.?|LTD\.?|CO\.?|LP|LLP|PARTNERSHIP|ASSOCIATES|GROUP|COMPANY)\b\.?',
    re.IGNORECASE
)


def clean_name(name: str) -> str:
    return re.sub(r'\s+', ' ', LEGAL_SUFFIX.sub('', name)).strip().strip(',')


def get_html(url: str) -> str:
    try:
        r = requests.get(url, headers={
            'User-Agent': UA,
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml',
        }, timeout=12, allow_redirects=True)
        if r.ok:
            return r.text
    except Exception as e:
        log.debug(f"HTTP error {url}: {e}")
    return ''


# ── LinkedIn Guest Jobs ────────────────────────────────────────────────────────

def search_linkedin_jobs(company: str, city: str, state: str) -> list:
    """
    Hit LinkedIn's unauthenticated guest jobs endpoint with company-name-first query.
    Returns list of job dicts.
    """
    clean = clean_name(company)

    # Query just the company name — LinkedIn's own geo handles location
    queries = [
        clean,
        # Fallback: first 3 significant words of company name
        ' '.join([w for w in clean.split() if len(w) >= 4][:3]),
    ]

    # Name words for matching (4+ chars, lowercased)
    name_words = [w.lower() for w in clean.split() if len(w) >= 4]

    for query in queries:
        if not query.strip():
            continue
        url = (f"https://www.linkedin.com/jobs/search"
               f"?keywords={quote_plus(query)}"
               f"&f_TPR=r2592000")  # Last 30 days

        html = get_html(url)
        if not html:
            continue

        soup = BeautifulSoup(html, 'html.parser')
        cards = soup.find_all('div', class_='base-search-card__info')

        jobs = []
        for card in cards[:15]:
            title_el   = card.find('h3')
            company_el = card.find('h4')
            loc_el     = card.find('span', class_=lambda c: c and 'location' in (c or ''))

            title   = title_el.get_text(strip=True) if title_el else ''
            co_name = company_el.get_text(strip=True) if company_el else ''
            loc     = loc_el.get_text(strip=True) if loc_el else ''

            if not title or not co_name:
                continue

            # Case-insensitive match: any significant word from our company name
            # appears in the returned company name
            co_lower = co_name.lower()
            if not any(w in co_lower for w in name_words):
                continue

            jobs.append({'title': title, 'company': co_name, 'location': loc, 'source': 'LinkedIn'})

        if jobs:
            return jobs
        time.sleep(random.uniform(1.0, 2.0))

    return []


# ── Indeed Jobs ────────────────────────────────────────────────────────────────

def search_indeed_jobs(company: str, city: str, state: str) -> list:
    """
    Hit Indeed's open job search with company-name-first query.
    """
    clean = clean_name(company)
    url = (f"https://www.indeed.com/jobs"
           f"?q={quote_plus(clean)}"
           f"&l={quote_plus(f'{city}, {state}')}"
           f"&fromage=30")  # Last 30 days

    html = get_html(url)
    if not html:
        return []

    soup = BeautifulSoup(html, 'html.parser')
    jobs = []

    # Indeed job cards
    for card in soup.find_all('div', class_=lambda c: c and 'job_seen_beacon' in (c or ''))[:10]:
        title_el = card.find('h2', class_=lambda c: c and 'jobTitle' in (c or ''))
        co_el    = card.find('span', class_=lambda c: c and 'companyName' in (c or ''))
        loc_el   = card.find('div', class_=lambda c: c and 'companyLocation' in (c or ''))

        title   = title_el.get_text(strip=True) if title_el else ''
        co_name = co_el.get_text(strip=True) if co_el else ''
        loc     = loc_el.get_text(strip=True) if loc_el else ''

        if not title or not co_name:
            continue

        # Verify company match
        name_words = [w for w in clean.lower().split() if len(w) >= 3]
        if not any(w in co_name.lower() for w in name_words):
            continue

        jobs.append({'title': title, 'company': co_name, 'location': loc, 'source': 'Indeed'})

    return jobs


# ── Signal Scoring ─────────────────────────────────────────────────────────────

def score_jobs(jobs: list) -> tuple:
    """
    Score jobs list for equipment relevance.
    Returns (score, matched_roles, total_count, best_source).
    """
    matched = []
    for job in jobs:
        title_lower = job['title'].lower()
        for role in EQUIPMENT_ROLES:
            if role in title_lower:
                matched.append({'title': job['title'], 'role': role,
                                 'company': job['company'], 'source': job['source']})
                break
    return len(matched), matched, len(jobs)


# ── Main Check ─────────────────────────────────────────────────────────────────

def check_hiring(company: str, city: str, state: str) -> dict:
    """
    Company-name-first hiring signal check.
    Returns signal dict or empty dict.
    """
    # LinkedIn first (stateless — no session)
    li_jobs = search_linkedin_jobs(company, city, state)
    time.sleep(random.uniform(2.5, 4.0))

    # Indeed if LinkedIn didn't hit
    indeed_jobs = []
    if not li_jobs:
        indeed_jobs = search_indeed_jobs(company, city, state)
        time.sleep(random.uniform(2.0, 3.0))

    all_jobs = li_jobs + indeed_jobs
    if not all_jobs:
        return {}

    eq_count, matched_roles, total = score_jobs(all_jobs)

    if total == 0:
        return {}

    # Build detail string
    sources = list(set(j['source'] for j in all_jobs))
    source_str = ' + '.join(sources)

    if eq_count > 0:
        # Equipment-specific hiring — high value
        role_titles = list(set(m['title'] for m in matched_roles[:3]))
        detail = (f"{eq_count} equipment role{'s' if eq_count > 1 else ''} open "
                  f"({', '.join(role_titles[:2])})")
        weight = 35
        label  = "👷 Actively Hiring Equipment Roles"
    else:
        # General hiring — still indicates growth
        all_titles = list(set(j['title'] for j in all_jobs[:3]))
        detail = f"{total} open position{'s' if total > 1 else ''} ({', '.join(all_titles[:2])})"
        weight = 15
        label  = "📋 Actively Hiring"

    return {
        "type":     "S3_HIRING",
        "label":    label,
        "detail":   detail,
        "source":   source_str,
        "triggers": [m['title'] for m in matched_roles[:3]],
        "count":    total,
        "eq_count": eq_count,
        "weight":   weight,
    }


# ── DB Runner ──────────────────────────────────────────────────────────────────

def run_hiring_signals(limit=200, hot_only=False):
    """Run company-name-first hiring signal check on UCC leads."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    where = [
        "company_name IS NOT NULL",
        "(signals_json IS NULL OR signals_json NOT LIKE '%S3_HIRING%')",
    ]
    if hot_only:
        where.append("CAST(days_to_lapse AS INTEGER) BETWEEN 0 AND 90")

    rows = conn.execute(f"""
        SELECT id, company_name, city, state, days_to_lapse, signals_json, signal_score, signal_tier
        FROM ucc_leads WHERE {' AND '.join(where)}
        ORDER BY CASE WHEN days_to_lapse IS NULL THEN 9999
                      ELSE CAST(days_to_lapse AS INTEGER) END ASC
        LIMIT ?
    """, [limit]).fetchall()
    conn.close()

    leads = [dict(r) for r in rows]
    log.info(f"Running hiring signal check on {len(leads)} leads")
    hits = 0
    eq_hits = 0

    for i, lead in enumerate(leads, 1):
        company = lead['company_name']
        city    = lead.get('city', '') or ''
        state   = lead.get('state', '') or ''
        dtl     = lead.get('days_to_lapse', '?')
        log.info(f"[{i}/{len(leads)}] {company} ({state}) — {dtl}d")

        sig = check_hiring(company, city, state)

        if sig:
            hits += 1
            if sig.get('eq_count', 0) > 0:
                eq_hits += 1
            log.info(f"  ✅ HIRING: {sig['detail'][:80]} | Weight: {sig['weight']}")

            existing = []
            try:
                existing = json.loads(lead.get('signals_json') or '[]')
            except:
                pass
            existing = [s for s in existing if s.get('type') != 'S3_HIRING']
            existing.append(sig)

            score = 10 + sum(s.get('weight', 0) for s in existing)
            n     = len(existing)
            tier  = ('S4' if n >= 2 else
                     'S3' if n == 1 and existing[0]['type'].startswith('S3') else
                     'S2' if n == 1 else 'S1')

            conn2 = sqlite3.connect(DB_PATH)
            conn2.execute("""
                UPDATE ucc_leads SET signals_json=?, signal_score=?, signal_tier=?,
                signals_checked_at=? WHERE id=?
            """, [json.dumps(existing), score, tier, datetime.now().isoformat(), lead['id']])
            conn2.commit()
            conn2.close()
        else:
            log.info(f"  ⚪ Not actively hiring (for this company)")

        time.sleep(random.uniform(2.0, 3.5))

    log.info(f"\n{'='*55}")
    log.info(f"  Hiring Signal Run Complete")
    log.info(f"  Processed     : {len(leads)}")
    log.info(f"  Any hiring    : {hits} ({100*hits//max(len(leads),1)}%)")
    log.info(f"  Equipment roles: {eq_hits} ({100*eq_hits//max(len(leads),1)}%) 👷")
    return hits


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
        format='%(asctime)s [Hiring] %(levelname)s - %(message)s')
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=200)
    parser.add_argument('--hot-only', action='store_true')
    args = parser.parse_args()
    run_hiring_signals(limit=args.limit, hot_only=args.hot_only)
