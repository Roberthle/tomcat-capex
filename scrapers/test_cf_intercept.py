import json
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
import time

def main():
    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        page = browser.new_page()

        api_urls = []
        def handle_request(request):
            if "api" in request.url or "search" in request.url:
                api_urls.append(request.url)
        
        page.on("request", handle_request)
        
        page.goto("https://ucc.michigan.gov/ucc-search", wait_until="networkidle")
        time.sleep(3)
        
        # Click on Organization radio button
        page.locator('label[for="personInd2"]').click()
        time.sleep(1)
        
        # Fill organization name
        page.locator('input#organizationName').fill("CONSTRUCTION")
        time.sleep(1)
        
        # Click search button
        page.get_by_role("button", name="Search").click()
        
        time.sleep(5) # wait for results
        
        with open("api_urls.txt", "w") as f:
            for url in set(api_urls):
                f.write(url + "\n")
        
        browser.close()

if __name__ == "__main__":
    main()
