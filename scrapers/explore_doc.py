import time
from playwright.sync_api import sync_playwright

def run(playwright):
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("https://www.floridaucc.com/")
    time.sleep(2)
    
    page.get_by_role("button", name="UCC Search").click()
    time.sleep(2)
    
    page.get_by_text("I accept the terms in the agreement").click()
    page.get_by_role("button", name="Next").click()
    time.sleep(2)
    
    # Select Document Number
    dropdowns = page.locator(".MuiSelect-select")
    dropdowns.nth(0).click()
    time.sleep(1)
    
    page.locator("li[role='option']").filter(has_text="Document Number").click()
    time.sleep(1)
    
    # Enter Document Number
    page.get_by_placeholder("Enter Document Number").fill("202400000010")
    
    # Click search button
    page.locator("svg[data-testid='SearchIcon']").locator("..").click()
    time.sleep(3)
    
    page.screenshot(path="/Users/robertle/floridaucc_doc.png")
    print("Texts:")
    print(page.locator("body").inner_text()[:2000])
    
    browser.close()

with sync_playwright() as playwright:
    run(playwright)
