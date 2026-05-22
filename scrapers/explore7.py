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
    
    page.get_by_placeholder("Enter Organization Name").fill("CONSTRUCTION")
    page.keyboard.press("Enter")
    time.sleep(5)
    
    with open("/Users/robertle/floridaucc7.html", "w") as f:
        f.write(page.content())
        
    page.screenshot(path="/Users/robertle/floridaucc7.png")
    
    browser.close()

with sync_playwright() as playwright:
    run(playwright)
