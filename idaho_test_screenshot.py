from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://sosbiz.idaho.gov/search/ucc")
        page.wait_for_selector("input", timeout=10000)
        page.fill("input", "TRACTOR")
        page.keyboard.press("Enter")
        page.wait_for_timeout(4000)
        
        cards = page.locator("tr:has-text('UCC')")
        if cards.count() > 0:
            row = cards.nth(0)
            row.click()
            page.wait_for_timeout(2000)
            page.screenshot(path="idaho_expanded.png")
            
        browser.close()

run()
