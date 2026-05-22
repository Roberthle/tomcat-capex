import requests, json, re

cookie_file = '/Users/robertle/tomcat_capex/leads/gsccca_cookies.json'
with open(cookie_file) as f:
    cookies = json.load(f)

s = requests.Session()
s.headers.update({"User-Agent": "Mozilla/5.0"})
for c in cookies:
    if 'gsccca.org' in c.get('domain', ''):
        s.cookies.set(c['name'], c['value'], domain=c['domain'], path=c['path'])

# Initialize session
s.get("https://search.gsccca.org/UCC_Search/search.asp?searchtype=SecuredParty")

r = s.post("https://search.gsccca.org/UCC_Search/securedresults.asp", data={
    "searchtype": "SecuredParty",
    "SecuredPartyOrganizationName": "DELL FINANCIAL SERVICES L.L.C.",
    "FromDate": "01/01/2021",
    "ToDate": "01/01/2026",
    "orderby": "2",
    "securedsearch": "0",
    "maxrows": "100"
})

subnames = re.findall(r'<input[^>]+name=["\']subname0["\'][^>]+value=["\']([^"\']*)["\']', r.text, re.I)
if subnames:
    sub = subnames[0]
    r2 = s.post("https://search.gsccca.org/UCC_Search/occurrences.asp", data={
        "SecuredPartyName": "DELL FINANCIAL SERVICES L.L.C.",
        "searchtype": "SecuredParty",
        "subname0": sub,
        "jCount": "0",
        "bFull": "Fullscreen View",
        "maxrows": "100"
    })
    
    with open("occurrences.html", "w") as f:
        f.write(r2.text)
    print("Saved occurrences.html")
else:
    print("No subnames found")
