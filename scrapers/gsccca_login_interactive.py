"""
gsccca_login_interactive.py
Run from terminal: python3 scrapers/gsccca_login_interactive.py

Opens a VISIBLE Chrome browser pointed at the GSCCCA login page.
You log in normally (no automation), it waits, then saves the session cookies.
"""

import json, os, time, sys
from pathlib import Path

OUT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "leads", "gsccca_cookies.json")

LOGIN_URL  = "https://search.gsccca.org/UCC_Search/securedresults.asp"
VERIFY_URL = "https://search.gsccca.org/UCC_Search/search.asp?searchtype=SecuredParty"


def run():
    from playwright.sync_api import sync_playwright

    print("\n" + "="*60)
    print("  GSCCCA Interactive Login")
    print("="*60)
    print("\nA Chrome window will open pointed at the GSCCCA login page.")
    print("Log in with your credentials, then come back here.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,          # VISIBLE browser — you interact with it
            slow_mo=0,
            args=["--window-size=1200,800"]
        )
        context = browser.new_context(
            viewport={"width": 1200, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        print(f"Opening: {LOGIN_URL}")
        page.goto(LOGIN_URL, timeout=30000)

        print("\n→ Log in with your GSCCCA credentials in the browser window.")
        print("  (Username: roberthle@gmail.com  Password: Openclaw26)")
        print("  Once you see the search form or results, come back here.\n")

        # Poll every 2 seconds until no longer on login page
        max_wait = 180  # 3 minutes
        waited = 0
        while waited < max_wait:
            time.sleep(2)
            waited += 2
            try:
                current_url = page.url
                content = page.content()

                # Success signals
                if "txtUserID" not in content and "frmLogin" not in content:
                    if "search" in current_url.lower() or "secured" in current_url.lower():
                        print(f"✅ Login detected! URL: {current_url}")
                        break
            except Exception as e:
                # Page is probably navigating, ignore
                current_url = "<navigating>"

            if waited % 10 == 0:
                print(f"  Waiting... ({waited}s) URL: {current_url}")
        else:
            print("⚠️  Timed out waiting for login. Saving whatever cookies we have.")

        # Save all gsccca.org cookies
        all_cookies = context.cookies()
        gsccca = [c for c in all_cookies if "gsccca.org" in c.get("domain", "")]

        browser.close()

    if not gsccca:
        print("❌ No GSCCCA cookies captured. Login may not have completed.")
        sys.exit(1)

    # Save
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    json.dump(gsccca, open(OUT_FILE, "w"), indent=2)

    print(f"\n✅ Saved {len(gsccca)} cookies:")
    for c in gsccca:
        val = c.get("value", "")
        print(f"   {c['domain']:35s} {c['name']:30s} {val[:20] if val else '<empty>'}")

    print(f"\n→ {OUT_FILE}")
    print("\nNow run: python3 scrapers/ga_ucc_scraper.py\n")


if __name__ == "__main__":
    run()
