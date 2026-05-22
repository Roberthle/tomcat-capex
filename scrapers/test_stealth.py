from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'
        )
        page = context.new_page()
        stealth = Stealth()
        stealth.apply_stealth_sync(page)
        
        page.goto('https://businesssearch.ohiosos.gov/', wait_until='networkidle')
        page.wait_for_timeout(3000)
        content = page.content()
        soup = BeautifulSoup(content, 'html.parser')
        for script in soup(['script', 'style']):
            script.extract()
        print(soup.get_text(separator=' ', strip=True)[:500])
        browser.close()

main()
