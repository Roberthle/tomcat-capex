from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://biz.sosmt.gov/search/ucc")
        page.wait_for_selector("input", timeout=10000)
        print("Inputs:", page.locator("input").count())
        for i in range(page.locator("input").count()):
            print("Input", i, ":", page.locator("input").nth(i).get_attribute("id"), page.locator("input").nth(i).get_attribute("placeholder"))
        
        print(page.locator("body").text_content()[:2000])
        browser.close()

run()
