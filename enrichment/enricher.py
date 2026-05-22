"""
Tomcat Capex — Lead Enrichment Engine
/Users/robertle/tomcat_capex/enrichment/enricher.py

Enriches raw UCC leads with:
  - Company phone number
  - Decision-maker name
  - Company website
  - Industry classification
  - Google News mentions (proxy signal)
  - Job postings signal (hiring = growing = needs equipment)

Uses DuckDuckGo HTML search — no API key required.
Run: python3 enricher.py [--limit N] [--state STATE]
"""

import os, sys, re, time, sqlite3, logging, argparse, random
import requests
from bs4 import BeautifulSoup
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [Enricher] %(levelname)s - %(message)s')
logger = logging.getLogger("TomcatCapex.Enricher")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# Phone number pattern
PHONE_RE = re.compile(
    r'(?<!\d)(\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4})(?!\d)'
)

# Email pattern
EMAIL_RE = re.compile(
    r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
)

# Common name prefixes that signal a person
TITLE_KEYWORDS = [
    'owner', 'president', 'ceo', 'founder', 'principal',
    'manager', 'director', 'partner', 'vice president', 'vp',
    'controller', 'cfo', 'operator'
]

def ddg_search(query: str, num=5) -> list[dict]:
    """Search DuckDuckGo HTML, return list of {title, url, snippet}."""
    try:
        url = "https://html.duckduckgo.com/html/"
        r = requests.post(url, data={"q": query, "kl": "us-en"},
                          headers=HEADERS, timeout=12)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        results = []
        for res in soup.select('.result')[:num]:
            title   = res.select_one('.result__title')
            snippet = res.select_one('.result__snippet')
            link    = res.select_one('.result__url')
            results.append({
                'title':   title.get_text(strip=True)   if title   else '',
                'snippet': snippet.get_text(strip=True) if snippet else '',
                'url':     link.get_text(strip=True)    if link    else '',
            })
        return results
    except Exception as e:
        logger.debug(f"DDG search error: {e}")
        return []


def extract_phone(text: str):
    """Extract first US phone number from text."""
    matches = PHONE_RE.findall(text)
    for m in matches:
        digits = re.sub(r'\D', '', m)
        if len(digits) == 10 and digits[0] not in ('0','1'):
            return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return None


def extract_contact_name(results):
    """Try to extract a person's name from search snippets."""
    full_text = ' '.join(r['title'] + ' ' + r['snippet'] for r in results)
    # Look for patterns like "John Smith, Owner" or "Owner: Jane Doe"
    for kw in TITLE_KEYWORDS:
        # Pattern: Name, Title
        m = re.search(
            rf'([A-Z][a-z]+ [A-Z][a-z]+(?:\s[A-Z][a-z]+)?),?\s*{kw}',
            full_text, re.IGNORECASE
        )
        if m:
            return m.group(1).strip()
        # Pattern: Title: Name or Title Name
        m2 = re.search(
            rf'{kw}[:\s]+([A-Z][a-z]+ [A-Z][a-z]+)',
            full_text, re.IGNORECASE
        )
        if m2:
            return m2.group(1).strip()
    return None


def extract_website(results, company: str):
    """Find company's own website from search results."""
    skip = {'yelp.com', 'yellowpages.com', 'bbb.org', 'linkedin.com',
            'facebook.com', 'duckduckgo.com', 'google.com', 'bloomberg.com',
            'indeed.com', 'glassdoor.com', 'bizapedia.com', 'opencorporates.com'}
    for r in results:
        url = r['url'].lower().strip()
        if url and not any(s in url for s in skip):
            # Prefer if company name word appears in domain
            words = [w for w in company.lower().split() if len(w) > 3]
            if any(w in url for w in words):
                return r['url']
    # Fallback: first non-directory result
    for r in results:
        url = r['url'].lower().strip()
        if url and not any(s in url for s in skip):
            return r['url']
    return None


def fetch_page_contacts(url: str) -> dict:
    """Visit a URL and extract phone, email from the page HTML."""
    result = {'phone': None, 'email': None}
    skip_domains = {'yelp.com', 'yellowpages.com', 'facebook.com', 'linkedin.com',
                    'instagram.com', 'twitter.com', 'x.com', 'bbb.org',
                    'indeed.com', 'glassdoor.com', 'google.com'}
    if any(d in url.lower() for d in skip_domains):
        return result
    try:
        # Normalize URL
        if not url.startswith('http'):
            url = 'https://' + url
        r = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=True)
        if r.status_code != 200:
            return result
        text = r.text[:50000]  # Cap at 50KB

        # Extract phone
        phones = PHONE_RE.findall(text)
        for p in phones:
            digits = re.sub(r'\D', '', p)
            if len(digits) == 10 and digits[0] not in ('0', '1'):
                result['phone'] = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
                break

        # Extract email (skip generic ones)
        skip_emails = {'info@example', 'noreply@', 'no-reply@', 'support@google',
                       'wix.com', 'wordpress.com', 'sentry.io'}
        emails = EMAIL_RE.findall(text)
        for em in emails:
            em_lower = em.lower()
            if not any(s in em_lower for s in skip_emails):
                result['email'] = em
                break

    except Exception:
        pass
    return result


def enrich_lead(lead: dict) -> dict:
    """
    Run enrichment for a single lead.
    Returns dict with: phone, contact_name, company_website, email, enriched_at
    """
    company = lead['company_name']
    city    = lead.get('city', '')
    state   = lead.get('state', '')
    location = f"{city}, {state}".strip(', ')

    result = {
        'phone': None,
        'contact_name': None,
        'company_website': None,
        'email': None,
        'enriched_at': datetime.now().isoformat()
    }

    # ── Search 1: Direct company info ────────────────────────────────────
    q1 = f'"{company}" {location} phone contact'
    r1 = ddg_search(q1, num=5)
    time.sleep(random.uniform(1.2, 2.2))

    # Try phone from snippets
    for res in r1:
        text = res['title'] + ' ' + res['snippet']
        phone = extract_phone(text)
        if phone:
            result['phone'] = phone
            break

    # Try contact name
    result['contact_name'] = extract_contact_name(r1)

    # Try website
    result['company_website'] = extract_website(r1, company)

    # ── Visit the website to scrape phone/email directly ────────────────
    if result['company_website']:
        page_data = fetch_page_contacts(result['company_website'])
        if page_data['phone'] and not result['phone']:
            result['phone'] = page_data['phone']
        if page_data['email']:
            result['email'] = page_data['email']

    # ── If still no phone, try visiting top 2 search result URLs ────────
    if not result['phone']:
        for res in r1[:2]:
            url = res.get('url', '')
            if url:
                page_data = fetch_page_contacts(url)
                if page_data['phone']:
                    result['phone'] = page_data['phone']
                    if not result['company_website']:
                        result['company_website'] = url
                    break
                if page_data['email'] and not result['email']:
                    result['email'] = page_data['email']
                time.sleep(0.5)

    # ── Search 2: Owner/contact name if not found ─────────────────────
    if not result['contact_name']:
        q2 = f'"{company}" {location} owner president CEO'
        r2 = ddg_search(q2, num=3)
        time.sleep(random.uniform(0.8, 1.5))
        result['contact_name'] = extract_contact_name(r2)
        # Also try phone from this search
        if not result['phone']:
            for res in r2:
                phone = extract_phone(res['title'] + ' ' + res['snippet'])
                if phone:
                    result['phone'] = phone
                    break

    return result


def save_enrichment(lead_id: str, data: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE ucc_leads
        SET phone = ?, contact_name = ?, company_website = ?,
            email = ?, enriched_at = ?
        WHERE id = ?
    """, [data['phone'], data['contact_name'],
          data['company_website'], data.get('email'),
          data['enriched_at'], lead_id])
    conn.commit()
    conn.close()


def get_unenriched_leads(limit=50, state=None, hot_only=False) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    where = "enriched_at IS NULL AND company_name IS NOT NULL"
    params = []
    if state:
        where += " AND source_state = ?"
        params.append(state)
    if hot_only:
        where += " AND lapse_date >= date('now') AND lapse_date <= date('now', '+30 days')"
    rows = conn.execute(f"""
        SELECT id, company_name, city, state, zipcode, source_state,
               secured_party, days_to_lapse
        FROM ucc_leads
        WHERE {where}
        ORDER BY
            CASE WHEN days_to_lapse IS NULL THEN 9999 ELSE days_to_lapse END ASC
        LIMIT ?
    """, params + [limit]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def run(limit=100, state=None, hot_only=False):
    leads = get_unenriched_leads(limit=limit, state=state, hot_only=hot_only)
    mode = "HOT ONLY (≤30d)" if hot_only else "ALL"
    logger.info(f"Enriching {len(leads)} leads — Mode: {mode}")

    success = 0
    phone_found = 0
    contact_found = 0
    web_found = 0

    for i, lead in enumerate(leads, 1):
        company = lead['company_name']
        dtl = lead.get('days_to_lapse', '?')
        logger.info(f"[{i}/{len(leads)}] {company} ({lead['source_state']}) — {dtl}d to lapse")

        try:
            data = enrich_lead(lead)
            save_enrichment(lead['id'], data)
            success += 1
            if data['phone']:
                phone_found += 1
                logger.info(f"  ✅ Phone: {data['phone']}")
            if data['contact_name']:
                contact_found += 1
                logger.info(f"  ✅ Contact: {data['contact_name']}")
            if data['company_website']:
                web_found += 1
                logger.info(f"  ✅ Web: {data['company_website']}")
            if not data['phone'] and not data['contact_name']:
                logger.info(f"  ⚪ No contact data found")
        except Exception as e:
            logger.error(f"  ❌ Error: {e}")

        # Polite delay between leads
        time.sleep(random.uniform(2.0, 3.5))

    logger.info(f"\n{'='*50}")
    logger.info(f"  Enrichment Complete — {mode}")
    logger.info(f"  Processed : {success}/{len(leads)}")
    logger.info(f"  Phones    : {phone_found} ({100*phone_found//max(success,1)}%)")
    logger.info(f"  Contacts  : {contact_found} ({100*contact_found//max(success,1)}%)")
    logger.info(f"  Websites  : {web_found} ({100*web_found//max(success,1)}%)")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--state', type=str, default=None)
    parser.add_argument('--hot-only', action='store_true',
                        help='Only enrich leads with lapse date ≤30 days')
    args = parser.parse_args()
    run(limit=args.limit, state=args.state, hot_only=args.hot_only)
