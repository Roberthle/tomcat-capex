import json
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
import time

def main():
    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        page = browser.new_page()
        
        reqs = []
        def handle_response(response):
            if "ucc.michigan.gov" in response.url:
                reqs.append(response.url)
                    
        page.on("response", handle_response)
        
        page.goto("https://ucc.michigan.gov/ucc-search", wait_until="networkidle")
        time.sleep(3)
        page.locator('label[for="personInd2"]').click()
        page.evaluate('''
            const input = document.getElementById('organizationName');
            input.value = 'CONSTRUCTION';
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new Event('blur', { bubbles: true }));
        ''')
        time.sleep(1)
        reqs.clear()
        
        page.evaluate('''
            const btn = document.querySelector('button[aria-label="Click to serach"]');
            if(btn) btn.click();
        ''')
        time.sleep(5)
        
        with open("all_reqs.txt", "w") as f:
            for r in reqs:
                f.write(r + "\n")
            
        browser.close()

if __name__ == "__main__":
    main()
