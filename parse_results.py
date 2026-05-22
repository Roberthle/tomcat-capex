from bs4 import BeautifulSoup
import re

with open("/Users/robertle/tomcat_capex/idaho_results.html", "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f.read(), "html.parser")
    
    # Tyler Tech SOS portals usually use a specific class for results like .item, .row, .search-result
    for element in soup.find_all(['tr', 'div']):
        if 'excavator' in element.text.lower() and len(element.text) < 500:
            print(f"Tag: {element.name}, Class: {element.get('class')}")
            print(f"Text: {element.text.strip()}")
            print("-" * 40)
            break
