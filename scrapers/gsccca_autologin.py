"""
gsccca_autologin.py  — fills credentials AND auto-submits.
If CAPTCHA appears the window stays open for you to solve it.
Run: python3 scrapers/gsccca_autologin.py
"""
from playwright.sync_api import sync_playwright
import json, os, time

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COOKIE_FILE = os.path.join(BASE_DIR, "leads", "gsccca_cookies.json")

USERNAME = "tomcatmca"
PASSWORD = "Openclaw26"

# Nav sidebar paths — not variation links
_SIDEBAR = {"/CarbonRegistry/", "/Lien/", "/liensearch/", "/notary/",
            "/plat/", "/pt61/", "/PT61Premium/", "/RealEstate/",
            "/RealEstatePremium/", "/UCC_Search/default", "/UCC_Search/search.asp"}

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--window-size=1100,800", "--window-position=150,80",
                  "--disable-blink-features=AutomationControlled"]
        )
        ctx  = browser.new_context(viewport={"width": 1100, "height": 800})
        page = ctx.new_page()

        print("\n=== GSCCCA Auto-Login ===")
        print("Navigating and filling credentials automatically...\n")

        page.goto("https://apps.gsccca.org/login.asp", timeout=30000)
        time.sleep(2)

        # Fill credentials
        try:
            page.locator("input[name='txtUserID']").fill(USERNAME)
            time.sleep(0.4)
            page.locator("input[name='txtPassword']").fill(PASSWORD)
            time.sleep(0.4)
            print("✓ Credentials filled")
        except Exception as e:
            print(f"Fill error: {e}")

        # Auto-submit the form
        try:
            page.evaluate("document.frmLogin.submit()")
            print("✓ Form submitted automatically")
        except Exception:
            try:
                page.locator("input[type='submit']").first.click()
                print("✓ Submit button clicked")
            except Exception:
                try:
                    page.locator("a:has-text('Login')").first.click()
                    print("✓ Login link clicked")
                except Exception:
                    print("⚠  Could not auto-submit — please click Login in the window")

        print("\nIf a CAPTCHA appeared, solve it in the browser window.")
        print("Waiting for login to complete...\n")

        # Poll for success (3 min)
        for i in range(90):
            time.sleep(2)
            try:
                url = page.url
            except Exception:
                continue
            if "login.asp" not in url.lower():
                print(f"✅ Login detected! URL: {url}")
                break
            if i % 15 == 0 and i > 0:
                print(f"  ({i*2}s) still waiting...")
        else:
            print("⚠  Timed out")

        # Validate via test search
        time.sleep(1)
        print("\nValidating session with a quick search...")
        try:
            page.goto(
                "https://search.gsccca.org/UCC_Search/search.asp?searchtype=SecuredParty",
                timeout=30000, wait_until="domcontentloaded"
            )
            time.sleep(2)
            page.locator("input[name='securedsearch'][value='0']").check()
            page.locator("input[name='SecuredPartyOrganizationName']").fill("DELL FINANCIAL")
            page.locator("input[name='SecuredPartyExact'][value='0']").check()
            page.locator("select[name='maxrows']").select_option("100")
            page.locator("#btnSubmit").click()
            time.sleep(5)
        except Exception as e:
            print(f"Search error: {e}")

        result_url = page.url
        all_links  = page.locator("a").all()
        link_hrefs = []
        for l in all_links:
            try:
                link_hrefs.append(l.get_attribute("href") or "")
            except Exception:
                pass

        # Auth check — apps.gsccca.org logout link only present when authenticated
        authenticated = any(
            "apps.gsccca.org" in h and "logout" in h.lower()
            for h in link_hrefs
        )

        # Variation links = UCC_Search relative links WITH query params, not sidebar
        variation_links = [
            h for h in link_hrefs
            if h and "?" in h and "UCC_Search" in h
            and not any(s in h for s in _SIDEBAR)
            and "search.asp" not in h
        ]

        print(f"\nResult URL   : {result_url}")
        print(f"Total links  : {len(link_hrefs)}")
        print(f"Auth status  : {'✅ AUTHENTICATED' if authenticated else '❌ NOT authenticated'}")
        print(f"Variation links: {len(variation_links)}")
        if variation_links:
            for vl in variation_links[:5]:
                print(f"  → {vl}")

        # Save cookies
        cookies = ctx.cookies()
        gsccca  = [c for c in cookies if "gsccca.org" in c.get("domain", "")]
        os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
        json.dump(gsccca, open(COOKIE_FILE, "w"), indent=2)
        print(f"\nSaved {len(gsccca)} cookies")
        for c in gsccca:
            v = c.get("value", "")[:25]
            print(f"  {c['domain']:35s}  {c['name']:30s}  {v}")

        browser.close()

        if authenticated:
            print("\n✅ Ready! Run:")
            print("   python3 scrapers/ga_ucc_scraper.py --lenders 2 --dry-run")
        else:
            print("\n❌ Session not valid. Try again.")


if __name__ == "__main__":
    run()
