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
    
    # Fill in organization name
    page.get_by_placeholder("Enter Organization Name").fill("CONSTRUCTION")
    page.locator("svg[data-testid='SearchIcon']").locator("..").click()
    # Or just hit enter
    # page.keyboard.press("Enter")
    
    page.wait_for_load_state("networkidle")
    time.sleep(3)
    
    page.screenshot(path="/Users/robertle/floridaucc4.png")
    
    # Print out results if table exists
    texts = page.locator("td").all_inner_texts()
    print(texts[:20])
    
    browser.close()

with sync_playwright() as playwright:
    run(playwright)
