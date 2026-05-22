from bs4 import BeautifulSoup

def parse_inputs(file_path):
    print(f"\n--- {file_path} ---")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
            inputs = soup.find_all("input")
            for i in inputs:
                print(f"Input: name='{i.get('name')}', id='{i.get('id')}', type='{i.get('type')}', placeholder='{i.get('placeholder')}'")
            buttons = soup.find_all("button")
            for b in buttons:
                print(f"Button: text='{b.text.strip()[:30]}', type='{b.get('type')}', class='{b.get('class')}'")
    except Exception as e:
        print(f"Error: {e}")

parse_inputs("/Users/robertle/tomcat_capex/idaho_ucc.html")
parse_inputs("/Users/robertle/tomcat_capex/montana_ucc.html")
