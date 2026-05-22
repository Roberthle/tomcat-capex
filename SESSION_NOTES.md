# Tomcat Capex — Session Knowledge
## What Was Built (April 16-17, 2026)

### Summary
Tomcat Capex is a fully autonomous equipment financing lead engine. It is completely separate from SalesAgentic/LeadBot. It produces CONFIRMED leads — not inferred signals.

### Directory
```
/Users/robertle/tomcat_capex/
├── scrapers/ucc_scraper.py   ← Core data engine (Colorado live)
├── auto_router.py            ← Broker delivery (email/webhook)
├── config/partners.json      ← Add broker partners here
├── leads/tomcat_capex.db     ← SQLite lead database
└── logs/ucc_leads_*.json     ← Daily lead exports
```

### How It Works
1. Queries Colorado SOS Socrata API (data.colorado.gov) for equipment UCC-1 filings
2. Intersects with leads expiring in next 1-180 days (lapse window)
3. Filters: company must have a name + collateral must be specific equipment
4. Saves leads to SQLite with company, address, lender, equipment type, lapse date
5. auto_router.py delivers batches to broker partners via email or webhook

### Live Results (First Run)
- Colorado: 84 confirmed equipment leads in DB
- Several expiring within 1-10 days (hot renewal window)
- Sample: SPOKES INC (Windsor CO), FRANK BROTHERS (Milliken CO), AAA RUTHER CONSTRUCTION (Bethune CO)
- Cost: $0.00

### Socrata API Details (Colorado)
- Filing dataset: https://data.colorado.gov/resource/wffy-3uut.json
- Debtor dataset: https://data.colorado.gov/resource/8upq-58vz.json
- Collateral dataset: https://data.colorado.gov/resource/4am6-w6u4.json (field: collateraldescription UPPERCASE)
- Secured party: https://data.colorado.gov/resource/ap62-sav4.json
- Key field: lapsedate (expiry), terminationflag, filingtype='ucc'
- Search: use $q (full text, case-insensitive) NOT $where LIKE (data is UPPERCASE, LIKE fails)

### States Confirmed Available (Socrata)
| State | Dataset | Status |
|-------|---------|--------|
| Colorado | wffy-3uut + 8upq-58vz + 4am6-w6u4 + ap62-sav4 | ✅ LIVE |
| Illinois | snfi-f79b (404), 2kf7-i54h, hmp5-cmyh | ⚠️ Need re-probe |
| Connecticut | xfev-8smz | ⚠️ Need probe |
| New York (ACRIS) | sv7x-dduq | ⚠️ Need probe |

### Lead Quality Standard (Hard Rules)
- NO proxy signals or inference
- Collateral field must contain specific equipment description
- UNKNOWN / GENERAL DESCRIPTION / IRS LIEN / PROCEEDS OF COLLATERAL = EXCLUDED
- Company must have an organizationname (no individual consumers)
- Lapse date must be in the future (min 1 day)

### auto_router.py Usage
```bash
python3 auto_router.py --preview-db    # Show DB contents
python3 auto_router.py --preview       # Dry run delivery
python3 auto_router.py                 # Live delivery to partners
python3 auto_router.py --limit 50      # Cap at 50 leads
```

### Next States to Add
1. Connecticut (xfev-8smz) — probe next
2. Illinois (re-probe 2kf7-i54h, hmp5-cmyh)
3. New York ACRIS (sv7x-dduq)
4. Washington State
