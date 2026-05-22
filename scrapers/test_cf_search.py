from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
import time

def main():
    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        page = browser.new_page()
        page.goto("https://ucc.michigan.gov/ucc-search", wait_until="networkidle")
        time.sleep(5) # wait for angular
        print(f"Title: {page.title()}")
        print("Inner text length:", len(page.inner_text("body")))
        with open("out_cf_search.html", "w") as f:
            f.write(page.content())
        browser.close()

if __name__ == "__main__":
    main()
