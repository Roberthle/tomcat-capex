from curl_cffi import requests

r = requests.get("https://businesssearch.ohiosos.gov/", impersonate="chrome110")
print(r.status_code)
print(r.text[:500])
