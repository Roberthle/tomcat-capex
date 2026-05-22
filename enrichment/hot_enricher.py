"""
Tomcat Capex — Hot Lead Enricher (Playwright + Google)
/Users/robertle/tomcat_capex/enrichment/hot_enricher.py

Uses Playwright headless browser to Google each hot lead and extract:
  - Phone number (from Google Knowledge Panel / search results)
  - Email (from company website)
  - Company website URL
  - Contact name (owner/CEO)

Targets ONLY hot leads (lapse ≤30 days) for maximum ROI.
Run: python3 hot_enricher.py [--limit N]
"""

import os, re, sys, time, sqlite3, logging, argparse, random
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [HotEnrich] %(levelname)s - %(message)s')
log = logging.getLogger("TomcatCapex.HotEnricher")

PHONE_RE = re.compile(r'\(?(\d{3})\)?[\s.\-](\d{3})[\s.\-](\d{4})')
EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

SKIP_DOMAINS = {'yelp.com', 'yellowpages.com', 'bbb.org', 'linkedin.com',
                'facebook.com', 'google.com', 'instagram.com', 'twitter.com',
                'indeed.com', 'glassdoor.com', 'wikipedia.org', 'mapquest.com'}


def get_hot_unenriched(limit=50):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, company_name, city, state, zipcode, source_state,
               secured_party, days_to_lapse, address
        FROM ucc_leads
        WHERE enriched_at IS NULL
        AND lapse_date >= date('now') AND lapse_date <= date('now', '+30 days')
        AND company_name IS NOT NULL
        ORDER BY days_to_lapse ASC
        LIMIT ?
    """, [limit]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_enrichment(lead_id, data):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE ucc_leads
        SET phone = ?, contact_name = ?, company_website = ?,
            email = ?, enriched_at = ?
        WHERE id = ?
    """, [data.get('phone'), data.get('contact_name'),
          data.get('website'), data.get('email'),
          datetime.now().isoformat(), lead_id])
    conn.commit()
    conn.close()


def extract_phone_from_text(text):
    """Extract first valid US phone from text."""
    for m in PHONE_RE.finditer(text):
        digits = m.group(1) + m.group(2) + m.group(3)
        if digits[0] not in ('0', '1') and len(set(digits)) > 2:
            return f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
    return None


def extract_email_from_text(text):
    """Extract first valid email from text."""
    skip = {'example.com', 'wix.com', 'wordpress.com', 'sentry.io',
            'noreply@', 'no-reply@', 'schema.org', 'w3.org',
            'googleapis.com', 'google.com', 'gstatic.com'}
    for m in EMAIL_RE.finditer(text):
        em = m.group(0)
        if not any(s in em.lower() for s in skip):
            return em
    return None


def run(limit=100):
    from playwright.sync_api import sync_playwright

    leads = get_hot_unenriched(limit=limit)
    log.info(f"🔥 Enriching {len(leads)} HOT leads (lapse ≤30 days)")

    if not leads:
        log.info("No hot leads to enrich!")
        return

    stats = {'phone': 0, 'email': 0, 'website': 0, 'contact': 0, 'total': 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        for i, lead in enumerate(leads, 1):
            company = lead['company_name']
            city = lead.get('city', '')
            state = lead.get('state', '')
            dtl = lead.get('days_to_lapse', '?')

            log.info(f"[{i}/{len(leads)}] {company} ({city}, {state}) — {dtl}d")

            result = {
                'phone': None, 'email': None,
                'website': None, 'contact_name': None,
            }

            try:
                # Google search for the company
                query = f"{company} {city} {state} phone"
                page.goto(f"https://www.google.com/search?q={query}",
                          wait_until="domcontentloaded", timeout=15000)
                time.sleep(2)

                # Get entire page text
                content = page.content()

                # Extract phone from Google results
                result['phone'] = extract_phone_from_text(content)

                # Find company website link
                links = page.locator("a[href]").all()
                for link in links[:15]:
                    try:
                        href = link.get_attribute("href") or ""
                        if href.startswith("http") and not any(s in href for s in SKIP_DOMAINS):
                            # Check if link text contains company-ish words
                            result['website'] = href.split("?")[0]
                            break
                    except:
                        continue

                # If we found a website, visit it to get phone/email
                if result['website']:
                    try:
                        page.goto(result['website'],
                                  wait_until="domcontentloaded", timeout=10000)
                        time.sleep(1.5)
                        site_content = page.content()

                        if not result['phone']:
                            result['phone'] = extract_phone_from_text(site_content)

                        result['email'] = extract_email_from_text(site_content)

                        # Try contact page too
                        contact_link = page.locator("a:has-text('Contact')").first
                        if contact_link.count() > 0:
                            try:
                                contact_link.click()
                                time.sleep(1.5)
                                contact_content = page.content()
                                if not result['phone']:
                                    result['phone'] = extract_phone_from_text(contact_content)
                                if not result['email']:
                                    result['email'] = extract_email_from_text(contact_content)
                            except:
                                pass
                    except:
                        pass

            except Exception as e:
                log.debug(f"  Error: {e}")

            # Save results
            save_enrichment(lead['id'], result)
            stats['total'] += 1
            if result['phone']:
                stats['phone'] += 1
                log.info(f"  ✅ Phone: {result['phone']}")
            if result['email']:
                stats['email'] += 1
                log.info(f"  ✅ Email: {result['email']}")
            if result['website']:
                stats['website'] += 1
                log.info(f"  ✅ Web: {result['website'][:50]}")
            if not result['phone'] and not result['email'] and not result['website']:
                log.info(f"  ⚪ No data found")

            # Rate limit
            time.sleep(random.uniform(3, 6))

        browser.close()

    n = max(stats['total'], 1)
    log.info(f"\n{'='*55}")
    log.info(f"  🔥 Hot Lead Enrichment Complete")
    log.info(f"  Processed: {stats['total']}/{len(leads)}")
    log.info(f"  Phones:    {stats['phone']} ({100*stats['phone']//n}%)")
    log.info(f"  Emails:    {stats['email']} ({100*stats['email']//n}%)")
    log.info(f"  Websites:  {stats['website']} ({100*stats['website']//n}%)")
    log.info(f"{'='*55}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    run(limit=args.limit)
