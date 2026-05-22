import requests
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

q = quote_plus(f'"Caterpillar" AND (contract OR awarded OR expansion OR facility OR opening OR hire)')
url = f"https://www.bing.com/news/search?q={q}&format=rss"

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

r = requests.get(url, headers=headers, timeout=10)
print("Status:", r.status_code)
if r.status_code == 200:
    root = ET.fromstring(r.content)
    items = root.findall('.//item')
    print(f"Found {len(items)} items")
    for item in items[:2]:
        print("-", item.findtext('title'))
else:
    print(r.text)
