from bs4 import BeautifulSoup
with open("/Users/robertle/tomcat_capex/idaho_results.html", "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f.read(), "html.parser")
    rows = soup.find_all("tr", class_="div-table-row")
    for row in rows:
        # Find elements with class exactly matching or containing div-table-cell
        cells = row.find_all(lambda tag: tag.has_attr('class') and 'div-table-cell' in tag['class'])
        print(f"Row has {len(cells)} div-table-cells")
        if len(cells) > 6:
            debtor = cells[1].text.strip()
            lender = cells[3].text.strip()
            print(f"Debtor: {debtor}, Lender: {lender}")
