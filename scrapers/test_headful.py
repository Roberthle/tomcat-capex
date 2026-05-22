from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto('https://businesssearch.ohiosos.gov/', wait_until='networkidle')
        page.wait_for_timeout(3000)
        content = page.content()
        soup = BeautifulSoup(content, 'html.parser')
        for script in soup(['script', 'style']):
            script.extract()
        print(soup.get_text(separator=' ', strip=True)[:500])
        browser.close()

main()
