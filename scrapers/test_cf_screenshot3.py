from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
import time

def main():
    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        page = browser.new_page()
        page.goto("https://ucc.michigan.gov/ucc-search", wait_until="networkidle")
        time.sleep(3)
        
        # Select Organization
        page.locator('label[for="personInd2"]').click()
        time.sleep(1)
        
        # Fill organization name
        page.evaluate('''
            const input = document.getElementById('organizationName');
            input.value = 'CONSTRUCTION';
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new Event('blur', { bubbles: true }));
        ''')
        time.sleep(1)
        
        # Click search
        page.evaluate('''
            const btn = document.querySelector('button[aria-label="Click to serach"]');
            if(btn) btn.click();
        ''')
        time.sleep(5)
        
        print("Done clicking search")
        page.screenshot(path="search_after_click3.png")
        
        with open("out_search_results3.html", "w") as f:
            f.write(page.content())
            
        browser.close()

if __name__ == "__main__":
    main()
