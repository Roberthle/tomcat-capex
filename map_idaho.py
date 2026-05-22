from playwright.sync_api import sync_playwright
import time

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    print("Navigating to Idaho...")
    page.goto("https://sosbiz.idaho.gov/search/ucc", timeout=60000)
    page.wait_for_load_state("networkidle")
    time.sleep(5)
    print("Saving HTML...")
    with open("/Users/robertle/tomcat_capex/idaho_ucc.html", "w", encoding="utf-8") as f:
        f.write(page.content())
    browser.close()
    print("Done")
