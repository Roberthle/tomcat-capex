"""
Tomcat Contact Enricher v2
Finds phone, email, contact name + LinkedIn for leads.

Data sources (in order):
  1. Yelp Fusion API  → phone + website (best for small biz, FREE 500/day)
  2. Hunter.io         → email + contact name from domain (FREE 2,000/month)
  3. LinkedIn via Proxycurl → contact name / title (OPTIONAL, $0.01/call)

Setup:
  export YELP_API_KEY="your_key_here"          # get free at yelp.com/developers
  export HUNTER_API_KEY="..."                  # already configured
  export PROXYCURL_API_KEY="..."               # optional, for LinkedIn

Run:
  python3 contact_enricher.py --db mca   --limit 200
  python3 contact_enricher.py --db capex --limit 200
  python3 contact_enricher.py --db both  --limit 100
"""

import os, sys, re, time, json, sqlite3, argparse, urllib.parse
import requests
from bs4 import BeautifulSoup

HUNTER_KEY    = os.environ.get('HUNTER_API_KEY',    '1887e8635a67226297cd838788a997569a7113ed')
YELP_KEY      = os.environ.get('YELP_API_KEY',      '')
PROXYCURL_KEY = os.environ.get('PROXYCURL_API_KEY', '')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MCA_DB   = os.path.join(BASE_DIR, '..', '..', 'tomcat_mca',   'leads', 'tomcat_mca.db')
CAPEX_DB = os.path.join(BASE_DIR, '..', 'leads', 'tomcat_capex.db')


# ── Yelp Fusion ───────────────────────────────────────────────────────────────

def yelp_search(company_name, city, state):
    """Search Yelp for a business. Returns (phone, website, yelp_url) or (None,None,None)."""
    if not YELP_KEY:
        return None, None, None
    try:
        r = requests.get(
            'https://api.yelp.com/v3/businesses/search',
            headers={'Authorization': f'Bearer {YELP_KEY}'},
            params={
                'term':     company_name,
                'location': f'{city}, {state}',
                'limit':    3,
                'sort_by':  'best_match',
            },
            timeout=8
        )
        data = r.json()
        businesses = data.get('businesses', [])
        if not businesses:
            return None, None, None

        # Pick closest match by name similarity
        name_lower = company_name.lower()
        best = None
        for b in businesses:
            bname = (b.get('name') or '').lower()
            if any(w in bname for w in name_lower.split() if len(w) > 3):
                best = b
                break
        if not best:
            best = businesses[0]

        # Yelp sometimes has phone in display_phone or phone field
        phone   = best.get('display_phone') or best.get('phone')
        website = best.get('url')           # Yelp URL as fallback; we'll also get real website
        # Get business detail for actual website URL
        biz_id = best.get('id')
        if biz_id:
            det = requests.get(
                f'https://api.yelp.com/v3/businesses/{biz_id}',
                headers={'Authorization': f'Bearer {YELP_KEY}'},
                timeout=8
            ).json()
            website = det.get('website') or website
        return phone, website, best.get('url')
    except Exception as e:
        print(f"    Yelp error: {e}")
        return None, None, None


# ── Hunter.io ────────────────────────────────────────────────────────────────

def _domain_from_url(url):
    if not url:
        return None
    m = re.match(r'https?://(?:www\.)?([^/\s?]+)', url)
    if m:
        d = m.group(1).lower()
        SKIP = ['yelp.','facebook.','linkedin.','yellowpages.','bbb.',
                'google.','bloomberg.','whitepages.','duckduck.','yelp.com']
        if '.' in d and not any(s in d for s in SKIP):
            return d
    return None


def hunter_domain_search(domain):
    """Return (contact_name, email) from Hunter.io domain search."""
    if not domain or not HUNTER_KEY:
        return None, None
    try:
        r = requests.get(
            'https://api.hunter.io/v2/domain-search',
            params={'domain': domain, 'limit': 10, 'api_key': HUNTER_KEY},
            timeout=10
        )
        data = r.json().get('data', {})
        emails = data.get('emails', [])
        if not emails:
            return None, None

        PRIORITY = ['ceo', 'owner', 'president', 'founder', 'principal',
                    'partner', 'director', 'manager', 'gm', 'general manager']
        best = None
        for e in emails:
            pos = (e.get('position') or '').lower()
            for p in PRIORITY:
                if p in pos:
                    best = e
                    break
            if best:
                break
        if not best:
            best = max(emails, key=lambda x: x.get('confidence', 0))

        name  = f"{best.get('first_name','') or ''} {best.get('last_name','') or ''}".strip() or None
        email = best.get('value')
        phone = best.get('phone_number')
        return name, email, phone
    except Exception:
        return None, None, None


# ── Proxycurl (optional LinkedIn) ─────────────────────────────────────────────

def proxycurl_company(li_url):
    if not PROXYCURL_KEY or not li_url:
        return None, None
    try:
        r = requests.get(
            'https://nubela.co/proxycurl/api/linkedin/company',
            params={'url': li_url, 'extra': 'include', 'funding_data': 'exclude'},
            headers={'Authorization': f'Bearer {PROXYCURL_KEY}'},
            timeout=12
        )
        d = r.json()
        for e in (d.get('executives') or []):
            title = (e.get('title') or '').lower()
            if any(t in title for t in ['ceo', 'owner', 'president', 'founder', 'principal']):
                return e.get('name'), e.get('phone')
        return d.get('name'), None
    except Exception as e:
        print(f"    Proxycurl error: {e}")
        return None, None

def duckduckgo_linkedin_search(company_name, city, state):
    """Searches DuckDuckGo HTML for the company's LinkedIn profile."""
    query = urllib.parse.quote_plus(f"{company_name} {city} {state} linkedin")
    url = f"https://html.duckduckgo.com/html/?q={query}"
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        for a in soup.find_all('a', class_='result__url'):
            link = a.get('href', '')
            if 'linkedin.com/company/' in link:
                return f"https://{link.split('//')[-1]}"
    except Exception as e:
        print(f"    DuckDuckGo search error: {e}")
    return None


# ── Main enrichment ───────────────────────────────────────────────────────────

def enrich_lead(company_name, city, state):
    """Return dict of enriched fields for one lead."""
    result = {
        'phone': None, 'email': None,
        'contact_name': None, 'company_website': None,
    }

    # Step 1: Yelp → phone + website
    phone, website, yelp_url = yelp_search(company_name, city, state)
    if phone:   result['phone']           = phone
    if website: result['company_website'] = website

    # Step 2: Proxycurl LinkedIn (Premium Decision-Maker Extractor)
    if PROXYCURL_KEY:
        li_url = duckduckgo_linkedin_search(company_name, city, state)
        if li_url:
            pc_name, pc_phone = proxycurl_company(li_url)
            if pc_name: result['contact_name'] = pc_name
            if pc_phone and not result['phone']: result['phone'] = pc_phone

    # Step 3: Hunter.io → email + contact fallback
    domain = _domain_from_url(website)
    if domain:
        h_name, h_email, h_phone = hunter_domain_search(domain)
        if h_email:   result['email']        = h_email
        if h_name and not result['contact_name']: result['contact_name'] = h_name
        if h_phone and not result['phone']: result['phone'] = h_phone

    return result


def get_unenriched(conn, table, limit):
    return conn.execute(f"""
        SELECT id, company_name, city, state
        FROM {table}
        WHERE (enriched_at IS NULL)
        ORDER BY
            CASE WHEN days_to_lapse BETWEEN -90 AND 30 THEN 0  -- hot leads first
                 WHEN days_to_lapse BETWEEN 31 AND 180  THEN 1
                 ELSE 2 END,
            days_to_lapse ASC
        LIMIT ?
    """, [limit]).fetchall()


def save_result(conn, table, lead_id, result):
    sets, vals = [], []
    for col in ('phone', 'email', 'contact_name', 'company_website'):
        if result.get(col):
            sets.append(f"{col} = ?")
            vals.append(result[col])
    sets.append("enriched_at = datetime('now')")
    sql = f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?"
    conn.execute(sql, vals + [lead_id])
    conn.commit()


def run(db_path, table, limit, delay=0.8):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    leads = get_unenriched(conn, table, limit)
    print(f"\n{'='*60}")
    print(f"Enriching {len(leads)} leads from [{table}]")
    if not YELP_KEY:
        print("⚠  YELP_API_KEY not set — phone enrichment will be skipped")
        print("   Get free key at: https://www.yelp.com/developers/v3/manage_app")
    print(f"{'='*60}")

    stats = {'phone': 0, 'email': 0, 'contact': 0}

    for i, row in enumerate(leads):
        lead_id      = row['id']
        company_name = row['company_name'] or ''
        city         = row['city'] or ''
        state        = row['state'] or ''

        print(f"\n  [{i+1}/{len(leads)}] {company_name[:50]}, {city} {state}")

        result = enrich_lead(company_name, city, state)
        save_result(conn, table, lead_id, result)

        if result['phone']:   stats['phone']   += 1; print(f"    ✓ phone:   {result['phone']}")
        if result['email']:   stats['email']   += 1; print(f"    ✓ email:   {result['email']}")
        if result['contact_name']: stats['contact'] += 1; print(f"    ✓ contact: {result['contact_name']}")
        if result['company_website']:            print(f"    ✓ website: {result['company_website'][:60]}")

        time.sleep(delay)

    conn.close()
    total = len(leads)
    print(f"\n{'='*60}")
    print(f"Results: phone={stats['phone']}/{total}  email={stats['email']}/{total}  contact={stats['contact']}/{total}")
    print(f"Hunter usage: {stats['email']} / 2000 monthly budget")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db',    choices=['mca', 'capex', 'both'], default='mca')
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--delay', type=float, default=0.8)
    args = parser.parse_args()

    if args.db in ('mca', 'both'):
        run(MCA_DB,   'mca_leads', args.limit, args.delay)
    if args.db in ('capex', 'both'):
        run(CAPEX_DB, 'ucc_leads', args.limit, args.delay)
