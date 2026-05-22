"""
Tomcat Capex — SEC EDGAR 8-K Equipment Signal
/Users/robertle/tomcat_capex/enrichment/edgar_signal.py

UCC-FIRST approach:
  1. Take company name from the UCC DB
  2. Search SEC EDGAR full-text search API for recent 8-K filings
  3. Scan those filings for equipment/capex keywords
  4. If found → S3_EDGAR signal (institutional-grade intelligence)

Why this is powerful:
  A company that has a UCC-1 expiring AND just disclosed a capex event
  in an SEC filing = they are actively financing or expanding equipment.
  A broker calling with "I saw your recent SEC filing about fleet expansion"
  is a completely different conversation than a cold call.

EDGAR EFTS API: https://efts.sec.gov (free, no key required)
SEC rate limit: 10 requests/second max → we stay well under with sleep.

Run: python3 edgar_signal.py [--limit N] [--hot-only]
"""

import os, re, sys, time, json, sqlite3, logging, random, argparse
import requests
from datetime import datetime, timedelta
from urllib.parse import quote_plus

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')

log = logging.getLogger("TomcatCapex.EDGAR")

SEC_HEADERS = {
    "User-Agent": "TomcatCapex Equipment Finance Intelligence (tomcatengine@gmail.com)",
    "Accept":     "application/json",
}

# Equipment/capex keywords that confirm financing activity
CAPEX_KEYWORDS = re.compile(
    r'(fleet expansion|equipment lease|capital expenditure|machinery|'
    r'heavy equipment|material handling|warehouse expansion|capex|'
    r'credit facility|equipment financing|operating lease|finance lease|'
    r'equipment purchase|rolling stock|vehicle fleet|construction equipment|'
    r'excavat|bulldozer|crane|forklift|loader|paving|trucking fleet|'
    r'new facility|plant expansion|manufacturing expansion)',
    re.IGNORECASE
)

# Legal suffixes to strip for cleaner SEC search
LEGAL_SUFFIX = re.compile(
    r'\b(LLC|INC\.?|CORP\.?|LTD\.?|CO\.?|LP|LLP|PARTNERSHIP|ASSOCIATES|GROUP|COMPANY)\b\.?',
    re.IGNORECASE
)


def clean_company_name(name: str) -> str:
    """Strip legal suffixes for broader SEC search."""
    cleaned = LEGAL_SUFFIX.sub('', name).strip().strip(',').strip()
    # Remove extra spaces
    return re.sub(r'\s+', ' ', cleaned)


def edgar_company_search(company: str, days_back: int = 365) -> list:
    """
    Search SEC EDGAR company search for 8-K filings by company name.
    Returns list of filing dicts.
    """
    clean = clean_company_name(company)
    if len(clean) < 4:
        return []

    url = (f"https://www.sec.gov/cgi-bin/browse-edgar"
           f"?company={quote_plus(clean)}&CIK=&type=8-K"
           f"&dateb=&owner=include&count=10&search_text="
           f"&action=getcompany&output=atom")
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=12)
        r.raise_for_status()
        return parse_edgar_atom(r.text, days_back)
    except Exception as e:
        log.debug(f"EDGAR company search error: {e}")
        return []


def edgar_fulltext_search(company: str, days_back: int = 365) -> list:
    """
    Search SEC EDGAR full-text search for filings MENTIONING the company name.
    More powerful than company search — finds companies that appear in OTHER filings.
    """
    clean = clean_company_name(company)
    if len(clean) < 4:
        return []

    start_dt = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
    url = (f"https://efts.sec.gov/LATEST/search-index"
           f"?q=%22{quote_plus(clean)}%22"
           f"&forms=8-K"
           f"&dateRange=custom&startdt={start_dt}")
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=12)
        r.raise_for_status()
        data = r.json()
        hits = data.get('hits', {}).get('hits', [])
        filings = []
        for hit in hits[:5]:
            src = hit.get('_source', {})
            filings.append({
                'company':    src.get('entity_name', company),
                'filed':      src.get('file_date', ''),
                'form':       src.get('form_type', '8-K'),
                'url':        f"https://www.sec.gov/Archives/edgar/data/"
                              f"{src.get('entity_id','')}/{src.get('file_num','').replace('-','')}",
                'accession':  src.get('file_num', ''),
                'description': src.get('display_names', company)[:100],
            })
        return filings
    except Exception as e:
        log.debug(f"EDGAR full-text search error: {e}")
        return []


def parse_edgar_atom(xml_text: str, days_back: int) -> list:
    """Parse SEC EDGAR ATOM feed for 8-K entries."""
    import xml.etree.ElementTree as ET
    entries = []
    try:
        root = ET.fromstring(xml_text)
        ns = {'a': 'http://www.w3.org/2005/Atom'}
        cutoff = datetime.now() - timedelta(days=days_back)

        for entry in root.findall('a:entry', ns):
            title   = (entry.findtext('a:title', '', ns) or '').strip()
            link_el = entry.find('a:link', ns)
            link    = link_el.attrib.get('href', '') if link_el is not None else ''
            updated = (entry.findtext('a:updated', '', ns) or '').strip()

            try:
                upd_dt = datetime.fromisoformat(updated.replace('Z', '+00:00'))
                if upd_dt.replace(tzinfo=None) < cutoff:
                    continue
            except:
                pass

            entries.append({
                'company': title.split(' - ')[1].strip() if ' - ' in title else title,
                'filed':   updated[:10],
                'form':    '8-K',
                'url':     link,
            })
    except Exception as e:
        log.debug(f"ATOM parse error: {e}")
    return entries


def fetch_filing_text(url: str) -> str:
    """Fetch raw text of a filing document."""
    # Try .txt version first (fastest, full text)
    for suffix in ['.txt', '-index.htm', '']:
        try_url = url.replace('-index.htm', suffix) if suffix else url
        try:
            r = requests.get(try_url, headers=SEC_HEADERS, timeout=15)
            if r.ok and len(r.text) > 500:
                return r.text[:50000]  # First 50k chars is enough
        except:
            pass
    return ''


def check_edgar(company: str, city: str, state: str) -> dict:
    """
    Main entry point: check SEC EDGAR for equipment capex signals for a company.
    Returns signal dict or empty dict.
    """
    # Strategy 1: Company name search on EDGAR
    filings = edgar_company_search(company, days_back=365)
    time.sleep(random.uniform(0.8, 1.5))

    # Strategy 2: Full-text search (catches mentions in other companies' filings too)
    if not filings:
        filings = edgar_fulltext_search(company, days_back=365)
        time.sleep(random.uniform(0.8, 1.5))

    if not filings:
        return {}

    best = None
    best_score = 0

    for filing in filings[:5]:
        url = filing.get('url', '')
        if not url:
            continue

        # Fetch filing text and scan for capex keywords
        text = fetch_filing_text(url)
        time.sleep(random.uniform(1.0, 2.0))

        if not text:
            # Even without text, a matching 8-K is a weak signal
            if best_score == 0:
                best = filing
                best_score = 10
            continue

        matches = CAPEX_KEYWORDS.findall(text)
        if matches:
            unique = list(set(m.lower() for m in matches))
            score = len(unique) * 15
            # Bonus if company name appears in the actual text
            if clean_company_name(company).lower()[:8] in text.lower():
                score += 20
            if score > best_score:
                best_score = score
                best = {**filing, 'keywords': unique}

    if best and best_score >= 10:
        keywords = best.get('keywords', [])
        filed    = best.get('filed', '')
        url      = best.get('url', '')

        # Format pub date nicely
        try:
            filed_fmt = datetime.strptime(filed[:10], '%Y-%m-%d').strftime('%b %d, %Y')
        except:
            filed_fmt = filed

        keyword_str = ', '.join(k.title() for k in keywords[:3]) if keywords else 'Equipment Activity'
        detail = f"{keyword_str} — SEC 8-K filing dated {filed_fmt}"

        return {
            "type":     "S3_EDGAR",
            "label":    "📋 SEC Filing — Equipment Capex",
            "detail":   detail,
            "source":   "SEC EDGAR",
            "pub":      filed,
            "link":     url if url.startswith('http') else f"https://www.sec.gov{url}",
            "triggers": keywords[:4],
            "weight":   40,  # Highest weight — institutional-grade signal
        }

    return {}


def run_edgar_signals(limit=200, hot_only=False):
    """Run EDGAR signal check on UCC leads."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    where = [
        "company_name IS NOT NULL",
        "(signals_json IS NULL OR signals_json NOT LIKE '%S3_EDGAR%')",
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
    log.info(f"Running EDGAR signal check on {len(leads)} leads")
    hits = 0

    for i, lead in enumerate(leads, 1):
        company = lead['company_name']
        city    = lead.get('city', '') or ''
        state   = lead.get('state', '') or ''
        dtl     = lead.get('days_to_lapse', '?')
        log.info(f"[{i}/{len(leads)}] {company} ({state}) — {dtl}d")

        sig = check_edgar(company, city, state)

        if sig:
            hits += 1
            log.info(f"  ✅ EDGAR: {sig['detail'][:80]}")

            existing = []
            try:
                existing = json.loads(lead.get('signals_json') or '[]')
            except:
                pass
            existing = [s for s in existing if s.get('type') != 'S3_EDGAR']
            existing.append(sig)

            score = 10 + sum(s.get('weight', 0) for s in existing)
            n     = len(existing)
            tier  = ('S4' if n >= 2 else
                     'S3' if existing[0]['type'].startswith('S3') else
                     'S2' if n == 1 else 'S1')

            conn2 = sqlite3.connect(DB_PATH)
            conn2.execute("""
                UPDATE ucc_leads SET signals_json=?, signal_score=?, signal_tier=?,
                signals_checked_at=? WHERE id=?
            """, [json.dumps(existing), score, tier, datetime.now().isoformat(), lead['id']])
            conn2.commit()
            conn2.close()
        else:
            log.info(f"  ⚪ No EDGAR signal")

        time.sleep(random.uniform(1.5, 2.5))

    log.info(f"\n{'='*55}")
    log.info(f"  EDGAR Signal Run Complete")
    log.info(f"  Processed : {len(leads)}")
    log.info(f"  Hits      : {hits} ({100*hits//max(len(leads),1)}%)")
    return hits


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
        format='%(asctime)s [EDGAR] %(levelname)s - %(message)s')
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--hot-only', action='store_true')
    args = parser.parse_args()
    run_edgar_signals(limit=args.limit, hot_only=args.hot_only)
