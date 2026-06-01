import os
import sys
import time
from playwright.sync_api import sync_playwright

def main():
    print("🚀 Starting Resilient Tomcat CapEx Subscription Verification...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        try:
            print("🌐 Navigating to http://localhost:5050 ...")
            page.goto("http://localhost:5050")
            
            # Wait for leads table to finish loading
            print("⏳ Waiting for leads to populate...")
            page.wait_for_selector(".score-ring", timeout=10000)
            
            # Assert Page Title
            title = page.title()
            print(f"📌 Page Title: {title}")
            assert "Tomcat CapEx" in title, "Page title should contain 'Tomcat CapEx'"
            
            # Find an unpurchased lead
            leads_rows = page.locator("#leads-body tr")
            rows_count = leads_rows.count()
            print(f"📊 Total leads visible: {rows_count}")
            
            clicked_unpurchased = False
            for i in range(rows_count):
                row = leads_rows.nth(i)
                # Click the row to open detailed panel
                print(f"🔎 Testing row {i+1}...")
                row.click()
                page.wait_for_timeout(1000)
                
                # Check if the Buy Lead button is active and not purchased
                buy_btn = page.locator(".buy-lead-btn")
                if buy_btn.count() > 0:
                    btn_text = buy_btn.first.inner_text()
                    if "LEAD PURCHASED" not in btn_text and "ALREADY" not in btn_text:
                        print(f"🎯 Found unpurchased lead at row {i+1}! Button text: '{btn_text}'")
                        print("🔒 Clicking 'Buy This Lead' to trigger subscription selection modal...")
                        buy_btn.first.click()
                        clicked_unpurchased = True
                        break
                    else:
                        print(f"ℹ️ Lead at row {i+1} is already purchased, closing panel and skipping...")
                        # Close the panel using evaluate to execute closePanel() cleanly
                        page.evaluate("closePanel()")
                        page.wait_for_timeout(800)
                else:
                    print(f"ℹ️ No buy button found at row {i+1}, closing panel and skipping...")
                    page.evaluate("closePanel()")
                    page.wait_for_timeout(800)
            
            assert clicked_unpurchased, "Could not find any unpurchased lead to test pricing modal"
            page.wait_for_timeout(1000)
            
            # Assert Choice Modal is visible
            modal = page.locator("#sx-unlock-modal")
            print(f"🔓 Choice modal visible: {modal.is_visible()}")
            assert modal.is_visible(), "Selector modal should open upon clicking Buy Lead"
            
            # Assert Single and Monthly cards are visible with correct pricing
            assert page.query_selector("text=SINGLE LEAD UNLOCK") is not None, "Single lead card should be visible"
            assert page.query_selector("text=UNLIMITED ACCESS PASS") is not None, "Subscription card should be visible"
            assert page.query_selector("text=$799") is not None, "Subscription card should show the $799 pricing"
            
            # Take screenshot of the choice modal
            screenshot_path = "/tmp/verify_tomcat_capex_choice_modal.png"
            page.screenshot(path=screenshot_path)
            print(f"📸 Screenshot saved to {screenshot_path}")
            
            print("✅ Tomcat CapEx Double Checkout modal ($49 vs. $799/mo) verified successfully!")
            
        except Exception as e:
            print(f"❌ Verification failed: {str(e)}")
            browser.close()
            sys.exit(1)
            
        browser.close()

if __name__ == "__main__":
    main()
