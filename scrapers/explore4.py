import time
from playwright.sync_api import sync_playwright

def run(playwright):
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://www.floridaucc.com/", wait_until="networkidle")
    time.sleep(1)
    
    page.get_by_role("button", name="UCC Search").click()
    page.wait_for_load_state("networkidle")
    time.sleep(1)
    
    page.get_by_text("I accept the terms in the agreement").click()
    page.get_by_role("button", name="Next").click()
    page.wait_for_load_state("networkidle")
    time.sleep(2)
    
    print("Search Types:")
    dropdowns = page.locator(".MuiSelect-select")
    dropdowns.nth(0).click()
    time.sleep(1)
    
    options = page.locator("li[role='option']").all_inner_texts()
    print(options)
    
    browser.close()

with sync_playwright() as playwright:
    run(playwright)
