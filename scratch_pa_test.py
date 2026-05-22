import time
import random
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

def main():
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/115.0'
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)
        
        print("Navigating to https://file.dos.pa.gov/...")
        page.goto("https://file.dos.pa.gov/")
        page.wait_for_timeout(10000)
        content = page.content()
        with open("pa_dos_homepage.html", "w") as f:
            f.write(content)
        page.screenshot(path="pa_dos_homepage.png")
        print("Saved screenshot and HTML.")
        browser.close()

if __name__ == "__main__":
    main()
