import time
from playwright.sync_api import sync_playwright

def run(playwright):
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://www.floridaucc.com/", wait_until="networkidle")
    time.sleep(2)
    
    # Click UCC Search
    page.get_by_role("button", name="UCC Search").click()
    page.wait_for_load_state("networkidle")
    time.sleep(2)
    
    # Click I accept
    page.get_by_text("I accept the terms in the agreement").click()
    time.sleep(1)
    
    # Click Next
    page.get_by_role("button", name="Next").click()
    page.wait_for_load_state("networkidle")
    time.sleep(2)
    
    page.screenshot(path="/Users/robertle/floridaucc3.png")
    print("Screenshot saved")
    browser.close()

with sync_playwright() as playwright:
    run(playwright)
