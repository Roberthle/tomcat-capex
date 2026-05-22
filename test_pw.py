from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("file:///Users/robertle/tomcat_capex/idaho_results.html")
    
    rows = page.query_selector_all(".div-table-row")
    print(f"Found {len(rows)} rows.")
    for row in rows:
        cells = row.query_selector_all(".div-table-cell")
        print(f"Row has {len(cells)} cells")
    browser.close()
