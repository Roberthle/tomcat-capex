from playwright.sync_api import sync_playwright
import time

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://sosbiz.idaho.gov/search/ucc")
        page.wait_for_selector("input", timeout=10000)
        page.fill("input", "TRACTOR")
        page.keyboard.press("Enter")
        page.wait_for_timeout(4000)
        
        cards = page.locator("tr:has-text('UCC')")
        if cards.count() > 0:
            row = cards.nth(0)
            print("Before click:", row.text_content())
            
            # Click the actual row to expand
            row.click()
            page.wait_for_timeout(2000)
            
            # Print text of element containing "Collateral" or "Legacy"
            try:
                collateral_locator = page.locator("text=Collateral")
                if collateral_locator.count() > 0:
                    print("Collateral section found! Text:", collateral_locator.nth(0).evaluate("el => el.parentElement.parentElement.textContent"))
            except Exception as e:
                print("No collateral:", e)
                
            try:
                # The expanded details usually appear in a div right below the row or inside a drawer.
                # Let's just find the drawer or the active expanded row.
                print("Drawer text:", page.locator(".MuiDrawer-paper, .drawer").text_content())
            except Exception as e:
                print("No drawer")

            # Try to grab the exact HTML to find the details
            print(page.evaluate("document.body.innerHTML").find("Legacy"))
            
        browser.close()

run()
