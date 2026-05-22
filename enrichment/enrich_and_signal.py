"""
Tomcat Capex — Lead Enrichment + Signal Engine (Unified)
/Users/robertle/tomcat_capex/enrichment/enrich_and_signal.py

Hits real business directories to find phone, contact, and signals.
Sources: BBB, Yelp, Indeed, Google News
Run: python3 enrich_and_signal.py [--limit N] [--hot-only]
"""

import os, sys, re, time, sqlite3, json, logging, argparse, random
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [Enrich+Signal] %(levelname)s - %(message)s')
log = logging.getLogger("TomcatCapex")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.3; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15"
]

def get_random_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive"
    }
PHONE_RE = re.compile(r'(?<!\d)(\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4})(?!\d)')

# ── DB ────────────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    for col in ['phone','contact_name','company_website','enriched_at',
                'signal_score','signals_json','signal_tier','signals_checked_at']:
        try:
            conn.execute(f"ALTER TABLE ucc_leads ADD COLUMN {col} TEXT")
        except:
            pass
    conn.commit()
    conn.close()


def get_leads(limit, hot_only, unenriched_only=True) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    where = ["company_name IS NOT NULL"]
    if unenriched_only:
        where.append("enriched_at IS NULL")
    if hot_only:
        where.append("CAST(days_to_lapse AS INTEGER) BETWEEN 0 AND 180")
    rows = conn.execute(f"""
        SELECT id, company_name, city, state, zipcode, source_state,
               secured_party, collateral, days_to_lapse
        FROM ucc_leads WHERE {' AND '.join(where)}
        ORDER BY CASE WHEN days_to_lapse IS NULL THEN 9999 
                      ELSE CAST(days_to_lapse AS INTEGER) END ASC
        LIMIT ?
    """, [limit]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_lead(lead_id, data):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE ucc_leads SET
            phone=?, contact_name=?, company_website=?,
            enriched_at=?, signal_score=?, signals_json=?,
            signal_tier=?, signals_checked_at=?
        WHERE id=?
    """, [data.get('phone'), data.get('contact_name'), data.get('website'),
          datetime.now().isoformat(),
          data.get('signal_score', 10),
          json.dumps(data.get('signals', [])),
          data.get('signal_tier', 'S1'),
          datetime.now().isoformat(),
          lead_id])
    conn.commit()
    conn.close()


# ── Scrapers ──────────────────────────────────────────────────────────────────

def get_html(url, timeout=12, referer=None):
    h = get_random_headers()
    if referer:
        h['Referer'] = referer
    try:
        r = requests.get(url, headers=h, timeout=timeout,
                         allow_redirects=True)
        if r.ok:
            return r.text
    except:
        pass
    return ''


def extract_phone(text) -> str:
    for m in PHONE_RE.findall(text):
        d = re.sub(r'\D', '', m)
        if len(d) == 10 and d[0] not in ('0','1'):
            return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return ''


# ── BBB ───────────────────────────────────────────────────────────────────────

def scrape_bbb(company, city, state) -> dict:
    """BBB search — excellent phone + employee data for US businesses."""
    q = quote_plus(f"{company} {city} {state}")
    url = f"https://www.bbb.org/search?find_text={q}&find_country=USA"
    html = get_html(url)
    if not html:
        return {}
    soup = BeautifulSoup(html, 'html.parser')
    result = {}
    # First result card
    card = soup.select_one('[class*="SearchResults_searchResult"],'
                           '[class*="result-card"],[data-testid="search-result"]')
    if not card:
        # Try any card with the company name
        cards = soup.find_all(class_=lambda c: c and 'result' in c.lower())
        card = cards[0] if cards else None
    if card:
        text = card.get_text(' ', strip=True)
        phone = extract_phone(text)
        if phone:
            result['phone'] = phone
        # Look for contact/owner name
        name_match = re.search(
            r'(?:owner|president|ceo|principal|contact)[:\s]+([A-Z][a-z]+ [A-Z][a-z]+)',
            text, re.IGNORECASE
        )
        if name_match:
            result['contact_name'] = name_match.group(1)
    return result


# ── Yelp ──────────────────────────────────────────────────────────────────────

def scrape_yelp(company, city, state) -> dict:
    """Yelp for phone number + website."""
    q = quote_plus(company)
    loc = quote_plus(f"{city}, {state}")
    url = f"https://www.yelp.com/search?find_desc={q}&find_loc={loc}"
    html = get_html(url, referer="https://www.yelp.com/")
    if not html:
        return {}
    soup = BeautifulSoup(html, 'html.parser')
    result = {}
    # Grab phone from page text
    phone = extract_phone(soup.get_text())
    if phone:
        result['phone'] = phone
    # Find business website link
    for a in soup.find_all('a', href=True):
        href = a['href']
        if 'biz_website' in href or 'redirect_url' in href:
            m = re.search(r'url=([^&]+)', href)
            if m:
                from urllib.parse import unquote
                result['website'] = unquote(m.group(1))[:80]
                break
    return result


# ── Indeed ────────────────────────────────────────────────────────────────────

def scrape_indeed_hiring(company, city, state) -> dict:
    """Check if company is actively hiring equipment-related roles."""
    q = quote_plus(f"{company}")
    url = f"https://www.indeed.com/jobs?q={q}&l={quote_plus(f'{city}+{state}')}"
    html = get_html(url, referer="https://www.indeed.com/")
    if not html:
        return {}
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text(' ', strip=True).lower()
    
    EQ_KW = ['equipment', 'operator', 'driver', 'cdl', 'forklift',
              'crane', 'truck', 'mechanic', 'technician', 'hvac',
              'electrician', 'plumber', 'excavat', 'laborer', 'fleet']
    
    # Check if indeed returned any jobs for this company
    job_count_m = re.search(r'(\d[\d,]*)\s+jobs?', text)
    job_count = int(job_count_m.group(1).replace(',','')) if job_count_m else 0
    
    if job_count > 0:
        eq_match = next((kw for kw in EQ_KW if kw in text), None)
        return {
            'type': 'S3_HIRING',
            'label': '📋 Actively Hiring',
            'detail': f"{job_count} open position(s)" + (f" — {eq_match} role" if eq_match else ""),
            'weight': 25 if eq_match else 12
        }
    return {}


# ── Google News ───────────────────────────────────────────────────────────────

def scrape_news(company, city, state) -> dict:
    """Bing News RSS search for recent company mentions (Bypasses Google IP ban)."""
    q = quote_plus(f'"{company}" AND (contract OR awarded OR expansion OR facility OR opening OR hire)')
    url = f"https://www.bing.com/news/search?q={q}&format=rss"
    
    try:
        h = get_random_headers()
        r = requests.get(url, headers=h, timeout=10)
        if r.status_code != 200:
            return {}
        root = ET.fromstring(r.content)
        cutoff = datetime.now() - timedelta(days=365)
        
        for item in root.findall('.//item'):
            title = (item.findtext('title') or '').strip()
            desc = re.sub(r'<[^>]+>', '', item.findtext('description') or '')
            pub = (item.findtext('pubDate') or '').strip()
            
            # Check date
            try:
                from email.utils import parsedate_to_datetime
                if parsedate_to_datetime(pub).replace(tzinfo=None) < cutoff:
                    continue
            except Exception:
                pass
            
            # Basic noise filtering
            if 'killed' in title.lower() or 'murder' in title.lower() or 'crash' in title.lower():
                continue
                
            return {
                'type': 'S2_NEWS',
                'label': '📰 Expansion Signal',
                'detail': title[:120],
                'weight': 20
            }
    except Exception as e:
        log.error(f"News RSS Error for {company}: {str(e)}")
        
    return {}


# ── Main enrichment ───────────────────────────────────────────────────────────

def enrich_lead(lead) -> dict:
    company = lead['company_name']
    city    = lead.get('city', '') or ''
    state   = lead.get('state', '') or ''
    result  = {'signals': [], 'signal_score': 10, 'signal_tier': 'S1'}

    log.info(f"  Checking BBB...")
    bbb = scrape_bbb(company, city, state)
    time.sleep(random.uniform(2.5, 4.5))
    if bbb.get('phone'):
        result['phone'] = bbb['phone']
    if bbb.get('contact_name'):
        result['contact_name'] = bbb['contact_name']

    # Yelp for phone if BBB missed
    if not result.get('phone'):
        log.info(f"  Checking Yelp...")
        yelp = scrape_yelp(company, city, state)
        time.sleep(random.uniform(2.0, 3.5))
        if yelp.get('phone'):
            result['phone'] = yelp['phone']
        if yelp.get('website') and not result.get('website'):
            result['website'] = yelp['website']

    # Signal: News
    log.info(f"  Checking news...")
    news = scrape_news(company, city, state)
    time.sleep(random.uniform(2.0, 4.0))
    if news:
        result['signals'].append(news)
        result['signal_score'] += news['weight']

    # Signal: Hiring
    log.info(f"  Checking Indeed...")
    hiring = scrape_indeed_hiring(company, city, state)
    time.sleep(random.uniform(2.0, 4.0))
    if hiring:
        result['signals'].append(hiring)
        log.info(f"    ✅ {hiring['label']}: {hiring['detail'][:60]}")

    # Compute tier
    n = len(result['signals'])
    score = 10 + sum(s.get('weight', 0) for s in result['signals'])
    tier = 'S4' if n >= 2 else ('S3' if n == 1 and result['signals'][0]['type'].startswith('S3') else
                                 'S2' if n == 1 else 'S1')
    result['signal_score'] = score
    result['signal_tier']  = tier
    return result


# ── Run ───────────────────────────────────────────────────────────────────────

def run(limit=50, hot_only=False):
    init_db()
    leads = get_leads(limit, hot_only)
    log.info(f"Processing {len(leads)} leads")

    stats = {'phone': 0, 'contact': 0, 'S1': 0, 'S2': 0, 'S3': 0, 'S4': 0}

    for i, lead in enumerate(leads, 1):
        company = lead['company_name']
        dtl = lead.get('days_to_lapse', '?')
        log.info(f"\n[{i}/{len(leads)}] {company} ({lead.get('source_state')}) — {dtl}d")

        try:
            data = enrich_lead(lead)
            save_lead(lead['id'], data)

            if data.get('phone'):
                stats['phone'] += 1
                log.info(f"  📞 {data['phone']}")
            if data.get('contact_name'):
                stats['contact'] += 1
                log.info(f"  👤 {data['contact_name']}")

            tier = data.get('signal_tier', 'S1')
            stats[tier] = stats.get(tier, 0) + 1
            log.info(f"  🏷  Tier: {tier} | Score: {data.get('signal_score', 10)}")

            if not data.get('signals') and not data.get('phone'):
                log.info(f"  ⚪ Minimal data found")

        except Exception as e:
            log.error(f"  Error: {e}")

        time.sleep(random.uniform(2.0, 3.5))

    n = max(len(leads), 1)
    log.info(f"\n{'='*55}")
    log.info(f"  Complete — {len(leads)} leads")
    log.info(f"  Phones      : {stats['phone']} ({100*stats['phone']//n}%)")
    log.info(f"  Contacts    : {stats['contact']} ({100*stats['contact']//n}%)")
    log.info(f"  S1 (UCC)    : {stats.get('S1',0)}")
    log.info(f"  S2 (News)   : {stats.get('S2',0)}")
    log.info(f"  S3 (Hiring) : {stats.get('S3',0)}")
    log.info(f"  S4 PRIME    : {stats.get('S4',0)} ⭐")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--hot-only', action='store_true')
    args = parser.parse_args()
    run(limit=args.limit, hot_only=args.hot_only)
