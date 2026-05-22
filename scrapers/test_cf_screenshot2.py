from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
import time

def main():
    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        page = browser.new_page()
        page.goto("https://ucc.michigan.gov/ucc-search", wait_until="networkidle")
        time.sleep(3)
        page.locator('label[for="personInd2"]').click()
        time.sleep(1)
        
        # Click the input, type, and press Tab
        page.locator('input#organizationName').click()
        page.keyboard.type("CONSTRUCTION", delay=100)
        time.sleep(1)
        
        page.get_by_role("button", name="Search").click()
        time.sleep(5)
        
        # Check if there's any API call during wait
        print("Done clicking search")
        page.screenshot(path="search_after_click2.png")
        
        # Save HTML too
        with open("out_search_results2.html", "w") as f:
            f.write(page.content())
            
        browser.close()

if __name__ == "__main__":
    main()
