"""
gsccca_full_session.py — Run from terminal, logs in and dumps filing link format.
Run: python3 scrapers/gsccca_full_session.py
"""
from playwright.sync_api import sync_playwright
import json, time, os

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COOKIE_FILE = os.path.join(BASE_DIR, "leads", "gsccca_cookies.json")


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--window-size=1200,800"])
        ctx = browser.new_context(viewport={"width": 1200, "height": 800})

        # Pre-load any existing cookies
        try:
            ctx.add_cookies(json.load(open(COOKIE_FILE)))
        except Exception:
            pass

        page = ctx.new_page()
        page.goto("https://apps.gsccca.org/login.asp", timeout=30000)
        time.sleep(2)
        print(f"\n→ At: {page.url}")
        print("  Log in now in the browser window.")
        print("  Watching for redirect away from login page...\n")

        # Poll until off login page (3 min max)
        for i in range(90):
            time.sleep(2)
            try:
                url = page.url
            except Exception:
                continue
            if "login.asp" not in url.lower():
                print(f"✅ Detected login success! URL: {url}")
                break
            if i % 10 == 0 and i > 0:
                print(f"  Still waiting ({i*2}s)...")
        else:
            print("⚠️  Timed out — saving whatever session we have")

        # Immediately run a test search to warm up the search session
        time.sleep(1)
        print("\n→ Running test search to confirm session and capture link format...")
        page.goto(
            "https://search.gsccca.org/UCC_Search/search.asp?searchtype=SecuredParty",
            timeout=30000, wait_until="domcontentloaded"
        )
        time.sleep(2)

        try:
            page.locator("input[name='securedsearch'][value='0']").check()
            page.locator("input[name='SecuredPartyOrganizationName']").fill("JOHN DEERE")
            page.locator("input[name='SecuredPartyExact'][value='0']").check()
            page.locator("select[name='maxrows']").select_option("100")
            page.locator("#btnSubmit").click()
            time.sleep(5)
        except Exception as e:
            print(f"  Search error: {e}")

        url = page.url
        all_links = page.locator("a").all()
        hrefs = []
        for l in all_links:
            try:
                hrefs.append((l.inner_text().strip()[:35], l.get_attribute("href") or ""))
            except Exception:
                pass

        print(f"\n  Search result URL: {url}")
        print(f"  Total links: {len(hrefs)}")

        nav_kw = ["gsccca.org", "forgotpassword", "SecuritySite", "javascript:", "#outage",
                  "logout.asp", "Alerts.asp", "sitemap", "glossary", "terms", "contact-us"]
        non_nav = [(t, h) for t, h in hrefs if h and not any(k in h for k in nav_kw)]
        print(f"\n  Non-nav links ({len(non_nav)}) — these are the filing links:")
        for t, h in non_nav[:25]:
            print(f"    [{t:35s}] -> {h}")

        # Save fresh cookies
        cookies = ctx.cookies()
        gsccca = [c for c in cookies if "gsccca.org" in c.get("domain", "")]
        os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
        json.dump(gsccca, open(COOKIE_FILE, "w"), indent=2)
        print(f"\n✅ Saved {len(gsccca)} cookies to {COOKIE_FILE}")
        print("   Now run: python3 scrapers/ga_ucc_scraper.py --lenders 5 --dry-run")

        browser.close()


if __name__ == "__main__":
    run()
