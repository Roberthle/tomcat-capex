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
    
    # Wait for the input to appear
    page.wait_for_selector('input[placeholder="Enter Organization Name"]')
    page.get_by_placeholder("Enter Organization Name").fill("FLORIDA CONSTRUCTION")
    
    # Click search button (the magnifying glass)
    page.locator('button[aria-label="search"]').click()
    # If aria-label doesn't work, let's try the SVG
    try:
        page.locator("svg[data-testid='SearchIcon']").locator("..").click()
    except:
        pass
        
    time.sleep(5)
    page.screenshot(path="/Users/robertle/floridaucc5.png")
    
    with open("/Users/robertle/floridaucc5.html", "w") as f:
        f.write(page.content())
    
    browser.close()

with sync_playwright() as playwright:
    run(playwright)
