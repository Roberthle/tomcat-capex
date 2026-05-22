from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://sosbiz.idaho.gov/search/ucc")
        page.wait_for_selector("text=Advanced Search Options", timeout=10000)
        page.click("text=Advanced Search Options")
        page.wait_for_timeout(1000)
        
        # Fill date
        page.fill("#field-date-FILING_DATEs", "05/01/2026")
        page.fill("#field-date-FILING_DATEe", "05/19/2026")
        page.click("button:has-text('Search')")
        page.wait_for_timeout(4000)
        
        print("UCC rows count:", page.locator("tr:has-text('UCC')").count())
        browser.close()

run()
