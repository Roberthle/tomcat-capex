from bs4 import BeautifulSoup

with open("/Users/robertle/tomcat_capex/idaho_results.html", "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f.read(), "html.parser")
    rows = soup.find_all("tr", class_="div-table-row")
    if rows:
        cells = rows[0].find_all(["td", "div"])
        for i, cell in enumerate(cells):
            print(f"Cell {i}: class='{cell.get('class')}' text='{cell.text.strip()}'")
