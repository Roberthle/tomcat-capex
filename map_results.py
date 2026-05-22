from playwright.sync_api import sync_playwright
import time

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    print("Navigating to Idaho...")
    page.goto("https://sosbiz.idaho.gov/search/ucc", timeout=60000)
    page.wait_for_load_state("networkidle")
    
    # Advanced search might be needed to specify UCC vs Business entity?
    # Actually, the placeholder is "Search by name or file number", it's a global search.
    # Let's type 'excavator' and press enter.
    print("Typing 'excavator'...")
    page.fill('input[placeholder="Search by name or file number"]', 'excavator')
    page.click('.search-button')
    
    print("Waiting for results...")
    page.wait_for_load_state("networkidle")
    time.sleep(8)
    
    print("Saving Results HTML...")
    with open("/Users/robertle/tomcat_capex/idaho_results.html", "w", encoding="utf-8") as f:
        f.write(page.content())
    browser.close()
    print("Done")
