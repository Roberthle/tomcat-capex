import json
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
import time

def main():
    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        page = browser.new_page()
        
        responses = []
        def handle_response(response):
            if response.request.method != "OPTIONS" and ("search" in response.url.lower() or "ucc" in response.url.lower()):
                try:
                    data = response.json()
                    responses.append({
                        "url": response.url,
                        "data": data
                    })
                except:
                    pass
                    
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
        page.evaluate('''
            const btn = document.querySelector('button[aria-label="Click to serach"]');
            if(btn) btn.click();
        ''')
        time.sleep(10)
        
        with open("intercepted_responses2.json", "w") as f:
            json.dump(responses, f, indent=2)
            
        browser.close()

if __name__ == "__main__":
    main()
