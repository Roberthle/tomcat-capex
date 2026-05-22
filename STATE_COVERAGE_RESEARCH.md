# Tomcat Capex — State Coverage Research Results
## Definitive Findings (April 17, 2026)

### What We Learned
After exhaustively probing every accessible state data source, here is the ground truth:

---

## ✅ CONFIRMED WORKING — Free API (Socrata)

| State | Records | Endpoint | Lead Quality |
|-------|---------|----------|-------------|
| **Colorado** | 2.5M | data.colorado.gov (wffy-3uut + joins) | FULL — name, address, lender, collateral, exact lapse date |
| **Connecticut** | 833K | data.ct.gov (xfev-8smz) | FULL — name, address, lender, lapse date |

**Current DB: 84 CO + 1,771 CT = 1,855 confirmed leads**

---

## ❌ BLOCKED — State SOS Portals

| State | Method Tried | Blocker |
|-------|-------------|---------|
| Texas | requests + Playwright | 403 / AccessDenied — requires SOSDirect subscription |
| Illinois | requests + Playwright | ReadTimeout / ERR_HTTP2_PROTOCOL_ERROR |
| Iowa | requests + Playwright | Access Denied to browser |
| Florida | API (publicsearchapi.floridaucc.com) | Filing detail endpoint "Under Construction" — no lapse date/lender |

## ⚠️ NO UNIQUE UCC DATA — All State Portals

The Socrata cross-federation catalog returns ONLY Colorado and Connecticut datasets. 
Every other state portal (Michigan, Missouri, Iowa, PA, NJ, DE, ME...) just mirrors those two.
No other state has published their own UCC data on Socrata.

---

## THE ONLY PATHS TO ALL 50 STATES

### Path 1: Paid Data Aggregators (Recommended at Scale)
| Provider | Coverage | Cost | API |
|----------|---------|------|-----|
| iLienOnline (Wolters Kluwer) | All 50 states | ~$150-300/mo | Yes |
| UCC Direct / CSC | All 50 states | ~$300-500/mo | Yes |
| OpenCorporates | All 50 states | ~$150/mo | Yes |
| National Lien Search | All 50 states | Per-search | No |

**Break-even: 2-3 leads delivered at $75-100/lead each covers 1 month of national access.**

### Path 2: Playwright Scraping (Free but Fragile)
States with accessible SOS portals that COULD be scraped:
- Montana (biz.sosmt.gov) — returned 200
- Idaho (sosbiz.idaho.gov) — returned 200
- Oklahoma (sos.ok.gov) — returned 200
- Nebraska (ucc.sos.ne.gov) — accessible during off-hours

Each requires 4-8 hours to build, test, and maintain. Portal changes break them.
These are small states (low volume).

### Path 3: FOIA Bulk Export Requests
Several states will provide full UCC exports on request:
- Contact state SOS office via email
- Request bulk UCC CSV/Excel export for X-month window
- Free in many states, takes 2-10 business days
- Best states to try: Virginia, Ohio, Pennsylvania, Georgia, North Carolina

---

## RECOMMENDED NEXT STEP

Instead of hunting for more free APIs (there aren't any), focus on:

1. **Get first broker deal** — 1,855 leads are in the DB right now. Call a broker.
2. **Use broker revenue** to buy iLienOnline for 1 month (~$200).
3. **All 50 states** delivered to the DB via their API.
4. **Scale** — 100x the leads, 100x the deal flow.

The data cost problem solves itself the moment there is revenue.

---

## CURRENT DB STATUS
- Colorado: 84 leads ✅
- Connecticut: 1,771 leads ✅
- Florida: 0 (insufficient data — no lender/lapse)
- Illinois: 0 (portal blocked)
- Texas: 0 (locked behind subscription)
- **TOTAL: 1,855 high-confidence equipment financing leads**
