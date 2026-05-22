from playwright.sync_api import sync_playwright
from playwright_stealth import stealth

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        page = browser.new_page()
        stealth(page)
        page.goto("https://ucc.michigan.gov/")
        page.wait_for_timeout(10000)
        print(f"Title: {page.title()}")
        with open("out_cf.html", "w") as f:
            f.write(page.content())
        browser.close()

if __name__ == "__main__":
    main()
