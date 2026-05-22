from bs4 import BeautifulSoup

with open("/Users/robertle/tomcat_capex/idaho_results.html", "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f.read(), "html.parser")
    buttons = soup.find_all("button")
    for b in buttons:
        print(f"Button text: '{b.text.strip()}' class: '{b.get('class')}' aria-label: '{b.get('aria-label')}' title: '{b.get('title')}'")
