from playwright.sync_api import sync_playwright
import time

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    print("Navigating to Montana...")
    page.goto("https://biz.sosmt.gov/search/ucc", timeout=60000)
    page.wait_for_load_state("networkidle")
    time.sleep(5)
    print("Saving HTML...")
    with open("/Users/robertle/tomcat_capex/montana_ucc.html", "w", encoding="utf-8") as f:
        f.write(page.content())
    browser.close()
    print("Done")
