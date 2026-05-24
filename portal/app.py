"""
Tomcat Capex — Broker Portal Backend
/Users/robertle/tomcat_capex/portal/app.py

Run: python3 app.py
Access: http://localhost:5050
"""

import os, sys, json, hashlib, sqlite3, secrets
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, abort, g, make_response, redirect
import stripe
from apollo_enricher import fetch_apollo_contacts, init_contact_cache, get_unlock_stats

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')
STATIC   = os.path.join(os.path.dirname(__file__), 'static')

app = Flask(__name__, static_folder=STATIC, static_url_path='')
app.secret_key = secrets.token_hex(32)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

DEFAULT_BROKER = 'demo'

# ── Simple broker accounts (in production: move to DB) ──────────────────────
BROKERS = {
    "demo":  {"password_hash": hashlib.sha256(b"demo2026").hexdigest(), "name": "Demo Broker", "states": []},
    "admin": {"password_hash": hashlib.sha256(b"tomcat2026").hexdigest(), "name": "Tomcat Admin", "states": []},
}
# Active session tokens: {token: {"broker": name, "expires": datetime}}
SESSIONS = {}

# ── DB helpers ───────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_portal_tables():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lead_claims (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id     TEXT NOT NULL,
            broker_name TEXT NOT NULL,
            status      TEXT DEFAULT 'claimed',  -- claimed, contacted, closed, dead
            notes       TEXT,
            claimed_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(lead_id, broker_name)
        )
    """)
    # Add phone/contact columns if missing
    try:
        conn.execute("ALTER TABLE ucc_leads ADD COLUMN phone TEXT")
        conn.execute("ALTER TABLE ucc_leads ADD COLUMN contact_name TEXT")
        conn.execute("ALTER TABLE ucc_leads ADD COLUMN company_website TEXT")
        conn.execute("ALTER TABLE ucc_leads ADD COLUMN enriched_at TEXT")
    except:
        pass  # columns already exist
    conn.commit()
    conn.close()


# ── Auth ─────────────────────────────────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-Auth-Token') or request.args.get('token')
        if not token or token not in SESSIONS:
            return jsonify({"error": "Unauthorized"}), 401
        session = SESSIONS[token]
        if datetime.fromisoformat(session['expires']) < datetime.now():
            del SESSIONS[token]
            return jsonify({"error": "Session expired"}), 401
        g.broker = session['broker']
        g.broker_name = BROKERS[session['broker']]['name']
        return f(*args, **kwargs)
    return decorated


# ── Auth routes ──────────────────────────────────────────────────────────────

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    username = data.get('username', '').strip().lower()
    password = data.get('password', '')
    pw_hash  = hashlib.sha256(password.encode()).hexdigest()

    broker = BROKERS.get(username)
    if not broker or broker['password_hash'] != pw_hash:
        return jsonify({"error": "Invalid credentials"}), 401

    token = secrets.token_hex(32)
    SESSIONS[token] = {
        "broker":  username,
        "expires": (datetime.now() + timedelta(hours=24)).isoformat()
    }
    return jsonify({"token": token, "name": broker['name'], "username": username})


@app.route('/api/logout', methods=['POST'])
@require_auth
def logout():
    token = request.headers.get('X-Auth-Token')
    SESSIONS.pop(token, None)
    return jsonify({"ok": True})


@app.route('/api/me')
@require_auth
def me():
    return jsonify({"broker": g.broker, "name": g.broker_name})

# ── Company name privacy gate ────────────────────────────────────────────────

def _mask_name(name):
    """Obfuscate company name — show first letter + bullets per word."""
    if not name:
        return 'Confidential Business'
    SUFFIXES = {'LLC','INC','CORP','LTD','LP','DBA','L.L.C.','INC.','CORP.','L.P.','CO.','CO'}
    parts = name.split()
    out = []
    for p in parts:
        if p.upper().rstrip('.') in SUFFIXES or len(p) <= 2:
            out.append(p)
        else:
            out.append(p[0] + '\u2022' * min(len(p) - 1, 8))
    return ' '.join(out)


def _is_purchased(lead_id):
    """Check if a lead has been purchased (completed Stripe session)."""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT id FROM lead_purchases WHERE lead_id=? AND status='completed'",
            [str(lead_id)]
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def _apply_mask(d):
    """Strip private fields from a lead dict unless purchased."""
    lead_id = d.get('id')
    if _is_purchased(lead_id):
        d['locked'] = False
        return d
    d['company_name']    = _mask_name(d.get('company_name', ''))
    d['address']         = '••••••••••••••••'
    d['phone']           = None
    d['email']           = None
    d['contact_name']    = None
    d['company_website'] = None
    d['website']         = None
    d['locked']          = True
    return d


# ── Leads API ────────────────────────────────────────────────────────────────

@app.route('/api/leads')
def get_leads():
    state      = request.args.get('state', '')
    urgency    = request.args.get('urgency', '')    # hot | warm | cold | 7d | 14d
    status_f   = request.args.get('status', 'all')  # unclaimed | claimed | all
    signal_f   = request.args.get('signal', 'all')  # all | expansion | tech | hiring
    search     = request.args.get('q', '').strip()
    tier_f     = request.args.get('tier', '')        # A | B | C | D (paydex bands)
    category_f = request.args.get('category', '')   # EQUIPMENT | IT_OEM | PRINT_IMAGING
    page       = max(1, int(request.args.get('page', 1)))
    per_page   = min(100, int(request.args.get('per_page', 50)))
    offset     = (page - 1) * per_page

    conn  = get_db()
    where = ["1=1"]
    params = []

    if state and state != 'all':
        where.append("source_state = ?")
        params.append(state)

    if urgency == '7d':
        where.append("days_to_lapse >= -90 AND days_to_lapse <= 7")
    elif urgency == '14d':
        where.append("days_to_lapse >= -90 AND days_to_lapse <= 14")
    elif urgency == 'hot':
        where.append("days_to_lapse >= -90 AND days_to_lapse <= 30")
    elif urgency == 'warm':
        where.append("days_to_lapse > 30 AND days_to_lapse <= 365")
    elif urgency == 'cold':
        where.append("days_to_lapse > 365")

    # Paydex tier filter: A=80+, B=65-79, C=50-64, D<50
    if tier_f == 'A':
        where.append("paydex_score >= 80")
    elif tier_f == 'B':
        where.append("paydex_score >= 65 AND paydex_score < 80")
    elif tier_f == 'C':
        where.append("paydex_score >= 50 AND paydex_score < 65")
    elif tier_f == 'D':
        where.append("paydex_score < 50")

    # Tech category filter
    if category_f and category_f != 'all':
        where.append("tech_category = ?")
        params.append(category_f)

    # Signal filters
    if signal_f == 'expansion':
        where.append("u.signals_json LIKE '%S2_NEWS%'")
    elif signal_f == 'tech':
        where.append("(u.tech_company = 'true' OR u.tech_category IN ('IT_OEM','IT_CHANNEL','CLOUD_SAAS'))")
    elif signal_f == 'hiring':
        where.append("u.signals_json LIKE '%S3_HIRING%'")
    elif signal_f == 'multifiler':
        where.append("u.company_name IN (SELECT company_name FROM ucc_leads WHERE days_to_lapse > -30 GROUP BY company_name HAVING COUNT(*) >= 3)")

    if search:
        where.append("(company_name LIKE ? OR city LIKE ? OR secured_party LIKE ?)")
        params += [f"%{search}%", f"%{search}%", f"%{search}%"]

    # Join with claims to show status
    claim_join = f"""
        LEFT JOIN lead_claims lc 
        ON lc.lead_id = u.id AND lc.broker_name = ?
    """
    params_with_broker = [DEFAULT_BROKER] + params

    if status_f == 'unclaimed':
        where.append("lc.id IS NULL")
    elif status_f == 'claimed':
        where.append("lc.id IS NOT NULL")

    where_sql = " AND ".join(where)

    # Count
    count_sql = f"""
        SELECT COUNT(*) FROM ucc_leads u
        {claim_join}
        WHERE {where_sql}
    """
    total = conn.execute(count_sql, params_with_broker).fetchone()[0]

    # Fetch
    leads_sql = f"""
        SELECT u.*, lc.status as claim_status, lc.notes, lc.claimed_at
        FROM ucc_leads u
        {claim_join}
        WHERE {where_sql}
        ORDER BY
            -- Expiring soonest first (0-90d), then lapsed (negative), nulls last
            CASE
                WHEN days_to_lapse IS NULL THEN 99999
                WHEN days_to_lapse >= 0   THEN days_to_lapse        -- 0 → 90 → best
                ELSE 10000 + ABS(days_to_lapse)                     -- lapsed: after positives
            END ASC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(leads_sql, params_with_broker + [per_page, offset]).fetchall()
    conn.close()

    leads = []
    for row in rows:
        d = dict(row)
        dtl = d.get('days_to_lapse')
        d['urgency_tier'] = 'hot' if (dtl is not None and dtl <= 30) else \
                            'warm' if (dtl is not None and dtl <= 90) else 'cold'
        d['deal_score'] = compute_deal_score(d)
        d['deal_narrative'] = generate_narrative(d)
        px = estimate_paydex(d)
        d['est_paydex'] = px['score']
        d['est_paydex_label'] = px['label']
        d['est_paydex_rationale'] = px['rationale']
        tier_key, tier_info = get_lead_tier(d)
        d['price_tier']    = tier_key
        d['price_display'] = tier_info['label'] + ' · ' + f"${tier_info['price']//100}"
        d['price_cents']   = tier_info['price']
        _apply_mask(d)
        leads.append(d)

    return jsonify({
        "leads": leads,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page
    })


@app.route('/api/leads/<lead_id>/unlock')
def unlock_lead(lead_id):
    """Return full unmasked lead — only after confirmed Stripe purchase."""
    session_id = request.args.get('session_id')
    if not session_id or not _is_purchased(lead_id, session_id):
        return jsonify({'error': 'Purchase required', 'locked': True}), 402
    conn = get_db()
    row = conn.execute('SELECT * FROM ucc_leads WHERE id = ?', [lead_id]).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    d = dict(row)
    d['locked'] = False
    return jsonify(d)


# ── Deal Score Engine ────────────────────────────────────────────────────────

LENDER_VULN = {
    'WELLS FARGO': 10, 'IBM': 9, 'AMAZON': 9, 'SACHEM': 9,
    'HEWLETT': 8, 'HP INC': 8, 'HPE': 8,
    'XEROX': 8, 'FITTLE': 8, 'MARLIN': 8, 'LEAF': 7, 'PEOPLE': 7,
    'CIT': 7, 'ONDECK': 7, 'ON DECK': 7, 'PAYPAL': 7, 'SQUARE': 7,
    'DELL': 6, 'CISCO': 6, 'LENOVO': 6, 'KABBAGE': 6,
    'CANON': 5, 'RICOH': 5, 'KONICA': 5, 'SIEMENS': 5,
    'GREATAMERICA': 4, 'DLL': 4, 'DE LAGE': 4,
    'KEY EQUIPMENT': 3, 'KEYBANK': 3,
    'CATERPILLAR': 2, 'CAT FINANCIAL': 2, 'JOHN DEERE': 2,
}

def get_lender_vuln(name):
    lu = (name or '').upper()
    for key, score in LENDER_VULN.items():
        if key in lu:
            return score
    if any(b in lu for b in ['BANK','TRUST','NATIONAL']): return 6
    if any(c in lu for c in ['FINANCIAL','CAPITAL','CREDIT']): return 4
    return 5

def compute_deal_score(lead):
    score = 0
    dtl = lead.get('days_to_lapse')
    # Urgency (55 pts max) — this is the conversion driver
    if dtl is not None:
        if dtl < 0:      score += 50  # lapsed — deal is wide open
        elif dtl == 0:    score += 55  # expires TODAY
        elif dtl <= 3:    score += 52
        elif dtl <= 7:    score += 48
        elif dtl <= 14:   score += 42
        elif dtl <= 30:   score += 32
        elif dtl <= 60:   score += 18
        elif dtl <= 90:   score += 9
    # Lender Vulnerability (25 pts max)
    vuln = get_lender_vuln(lead.get('secured_party', ''))
    score += int(vuln * 2.5)
    # Signal Density (20 pts max)
    signals = []
    try: signals = json.loads(lead.get('signals_json', '[]') or '[]')
    except: pass
    sig_types = set(s.get('type', '') for s in signals)
    if 'S2_EXPANSION' in sig_types: score += 8
    if 'S3_HIRING' in sig_types: score += 4
    if 'S4_EDGAR' in sig_types: score += 4
    if 'S5_PERMIT' in sig_types: score += 8
    if 'S6_CONTRACT' in sig_types: score += 10
    if 'S7_FUNDING' in sig_types: score += 7
    if 'S8_FLEET' in sig_types: score += 6
    sig_count = len([t for t in sig_types if t.startswith('S')])
    if sig_count >= 3: score += 6  # multi-signal stack bonus
    elif sig_count >= 2: score += 3
    return min(100, max(0, score))

def generate_narrative(lead):
    lender = lead.get('secured_party', 'Unknown')
    dtl = lead.get('days_to_lapse')
    lu = lender.upper()
    if dtl is not None and dtl < 0:
        tp = f"lien lapsed {abs(dtl)}d ago"
    elif dtl is not None and dtl == 0:
        tp = "filing expires today"
    elif dtl is not None and dtl <= 7:
        tp = f"filing expires in {dtl}d"
    elif dtl is not None and dtl <= 30:
        tp = f"filing matures in {dtl}d"
    else:
        tp = "filing approaching maturity"
    angles = {
        'WELLS FARGO': "Wells exited small-ticket — deal will NOT renew. Borrower needs a new lender.",
        'XEROX': "Xerox rebranded to FITTLE — client may not know who holds their lease.",
        'FITTLE': "FITTLE (fmr. Xerox) — brand confusion creates displacement window.",
        'DELL': "Dell pushes subscription lock-in at renewal. Offer a $1 buyout EFA.",
        'CANON': "Canon only finances Canon-brand. Multi-brand needs = instant displacement.",
        'RICOH': "Ricoh bundles service into lease. Unbundling is a strong opening angle.",
        'KONICA': "Konica renewal desk is understaffed. Low resistance to displacement.",
        'MARLIN': "Marlin acquired by HPS — renewal rates up 20%. Beat by 50bps.",
        'IBM': "IBM sold its financing ops. Client may not know who holds the lease.",
        'CISCO': "Cisco pushes subscription conversion. Hardware buyers need independent finance.",
        'CIT': "CIT acquired by First Citizens — service disruptions create openings.",
        'CATERPILLAR': "CAT marks up non-CAT equipment 200-400bps. Offer multi-brand.",
        'CAT FINANCIAL': "CAT Financial can't finance Deere/Komatsu. Multi-brand is your edge.",
        'GREATAMERICA': "GreatAmerica starts renewals 120d early. Unbundle their service package.",
        'DLL': "DLL has 7-10 day approvals. Speed of close is your advantage.",
        'DE LAGE': "DLL/De Lage Landen: slow underwriting. Position with 24-48hr funding.",
        'AMAZON': "Amazon Capital charges 12-16% APR on seller advances. Offer traditional equipment line at 6-8% — near-certain displacement.",
        'SACHEM': "Sachem is a bridge/hard-money lender — borrower is paying 10-15%. Offer a 6-8% equipment-specific line and they'll jump.",
        'ONDECK': "OnDeck tightened post-acquisition. Renewal terms are worse — position with better rates.",
        'ON DECK': "OnDeck tightened post-acquisition. Renewal terms are worse — position with better rates.",
        'PAYPAL': "PayPal Working Capital has no relationship manager. Borrower is used to automated lending — a human touch wins.",
        'SQUARE': "Square Capital auto-debits daily from card processing. Offer fixed monthly payments for predictability.",
        'KABBAGE': "Kabbage (now AmEx) tightened credit criteria post-acquisition. Many renewals are being declined.",
    }
    angle = "Position with competitive rates and faster close times."
    for key, val in angles.items():
        if key in lu:
            angle = val
            break
    else:
        if any(b in lu for b in ['BANK OF AMERICA','CHASE','CITIBANK','PNC','TD BANK']):
            angle = "Big bank equipment desks are deprioritized. Renewal quotes come late with above-market rates."
    sigs = []
    try: sigs = json.loads(lead.get('signals_json', '[]') or '[]')
    except: pass
    suffix = ""
    for s in sigs:
        if s.get('type') == 'S2_EXPANSION':
            suffix = " Expansion activity detected — capital needs increasing."
            break
    return f"Their {lender} {tp}. {angle}{suffix}"


def estimate_paydex(lead: dict) -> dict:
    """
    Signal-based Paydex proxy estimate.
    Real D&B Paydex = 1-100 based on payment history.
    We approximate using lender vetting rigor, filing tenure,
    signal density, and collateral class as proxies.

    Returns dict with: score (int), label (str), rationale (list[str])
    """
    score = 55  # Base: average UCC-filed company
    rationale = []

    lender = (lead.get('secured_party') or '').upper()
    col    = (lead.get('collateral') or '').lower()
    dtl    = lead.get('days_to_lapse')
    signals = []
    try: signals = json.loads(lead.get('signals_json') or '[]')
    except: pass
    sig_types = {s.get('type', '') for s in signals}

    # ── Lender vetting rigor ──────────────────────────────────────────────
    BIG_BANKS = ['WELLS FARGO','BANK OF AMERICA','CHASE','CITIBANK','US BANK',
                 'PNC','TD BANK','REGIONS','FIFTH THIRD','KEYBANK']
    CAPTIVES  = ['CATERPILLAR','CAT FINANCIAL','JOHN DEERE','KOMATSU','CNH',
                 'KUBOTA','MANITOWOC']
    A_BANKS   = ['DLL','DE LAGE','GREATAMERICA','STEARNS','MARLIN','LEAF',
                 'NAVITAS','CIT','CISCO','DELL','HP','HPE','HEWLETT']
    MCA       = ['ONDECK','ON DECK','SQUARE','PAYPAL','KABBAGE','SHOPIFY',
                 'AMAZON CAPITAL','FUNDBOX','BLUEVINE','CREDIBLY']
    BRIDGE    = ['SACHEM','RED BRIDGE','FLATIRON','STORMFIELD','EVERGREEN CAPITAL']

    if any(b in lender for b in BIG_BANKS):
        score += 12
        rationale.append('Big-bank lender (+12): rigorous credit vetting')
    elif any(c in lender for c in CAPTIVES):
        score += 8
        rationale.append('Captive lender (+8): manufacturer-backed approval')
    elif any(a in lender for a in A_BANKS):
        score += 4
        rationale.append('A-bank/lessor (+4): standard credit check passed')
    elif any(m in lender for m in MCA):
        score -= 20
        rationale.append('MCA/fintech lender (-20): bank-declined, high-rate product')
    elif any(b in lender for b in BRIDGE):
        score -= 15
        rationale.append('Bridge lender (-15): hard-money / last-resort lender')

    # ── Filing tenure (years since first filing = payment track record) ──
    filing_date = lead.get('filing_date') or ''
    try:
        from datetime import date
        fd = date.fromisoformat(filing_date[:10])
        tenure_years = (date.today() - fd).days / 365.25
        tenure_bump = min(10, int(tenure_years * 2))  # +2 per year, cap at 10
        if tenure_bump > 0:
            score += tenure_bump
            rationale.append(f'Filing tenure {tenure_years:.1f}yr (+{tenure_bump})')
    except:
        pass

    # ── Multi-filer = repeat approvals = better payer ────────────────────
    filing_count = lead.get('filing_count') or 0
    try:
        # Count from DB context if not available on lead object
        if not filing_count and lead.get('company_name'):
            filing_count = 1  # At minimum they have this one
    except: pass
    if filing_count >= 4:
        score += 8
        rationale.append(f'Multi-filer {filing_count}x (+8)')
    elif filing_count >= 2:
        score += 4
        rationale.append(f'Multi-filer {filing_count}x (+4)')

    # ── Signal bumps (growth = financial health) ─────────────────────────
    if 'S6_CONTRACT' in sig_types:
        score += 8
        rationale.append('Govt contract (+8): stable govt revenue stream')
    if 'S2_EXPANSION' in sig_types or 'S2_NEWS' in sig_types:
        score += 5
        rationale.append('Expansion signal (+5): growing business')
    if 'S3_HIRING' in sig_types:
        score += 3
        rationale.append('Hiring signal (+3): operational growth')
    if 'S7_FUNDING' in sig_types:
        score += 4
        rationale.append('Funding signal (+4): VC/PE-backed')

    # ── Lapsed lien penalty (they let it run out = less organized) ───────
    if dtl is not None and dtl < -60:
        score -= 8
        rationale.append(f'Lapsed {abs(dtl)}d (-8): missed renewal')
    elif dtl is not None and dtl < 0:
        score -= 3
        rationale.append(f'Recently lapsed (-3)')

    # ── Clamp to realistic range ─────────────────────────────────────────
    score = max(10, min(95, score))

    if score >= 80:   label = 'Low Risk'
    elif score >= 60: label = 'Moderate'
    elif score >= 40: label = 'Elevated Risk'
    else:             label = 'High Risk'

    return {'score': score, 'label': label, 'rationale': rationale}


@app.route('/api/heatmap')
def heatmap():
    state      = request.args.get('state', '')
    status_f   = request.args.get('status', 'all')
    signal_f   = request.args.get('signal', 'all')
    tier_f     = request.args.get('tier', 'all')
    category_f = request.args.get('category', 'all')
    urgency    = request.args.get('urgency', '')

    where = ["city IS NOT NULL AND city != ''"]
    params = []

    if state and state != 'all':
        where.append("source_state = ?")
        params.append(state)

    if urgency == '7d':
        where.append("days_to_lapse <= 7")
    elif urgency == '14d':
        where.append("days_to_lapse <= 14")
    elif urgency == 'hot':
        where.append("days_to_lapse <= 30")
    elif urgency == 'warm':
        where.append("days_to_lapse > 30 AND days_to_lapse <= 365")
    elif urgency == 'cold':
        where.append("days_to_lapse > 365")

    if tier_f == 'A':
        where.append("paydex_score >= 80")
    elif tier_f == 'B':
        where.append("paydex_score >= 65 AND paydex_score < 80")
    elif tier_f == 'C':
        where.append("paydex_score >= 50 AND paydex_score < 65")
    elif tier_f == 'D':
        where.append("paydex_score < 50")

    if category_f and category_f != 'all':
        where.append("tech_category = ?")
        params.append(category_f)

    if signal_f == 'expansion':
        where.append("signals_json LIKE '%S2_EXPANSION%'")
    elif signal_f == 'tech':
        where.append("(tech_company = 'true' OR tech_category IN ('IT_OEM','IT_CHANNEL','CLOUD_SAAS'))")
    elif signal_f == 'hiring':
        where.append("signals_json LIKE '%S3_HIRING%'")

    claim_join = ""
    if status_f != 'all':
        claim_join = "LEFT JOIN lead_claims lc ON lc.lead_id = ucc_leads.id AND lc.broker_name = ?"
        params.insert(0, DEFAULT_BROKER)
        if status_f == 'unclaimed':
            where.append("lc.id IS NULL")
        elif status_f == 'claimed':
            where.append("lc.id IS NOT NULL")

    where_sql = " AND ".join(where)

    conn = get_db()
    rows = conn.execute(f"""
        SELECT city, source_state,
               COUNT(*) as total,
               SUM(CASE WHEN days_to_lapse >= 0 AND days_to_lapse <= 30 THEN 1 ELSE 0 END) as hot,
               SUM(CASE WHEN days_to_lapse < 0 AND days_to_lapse >= -30 THEN 1 ELSE 0 END) as lapsed
        FROM ucc_leads
        {claim_join}
        WHERE {where_sql}
        GROUP BY city, source_state ORDER BY total DESC LIMIT 40
    """, params).fetchall()
    conn.close()
    return jsonify({"cities": [
        {"city": r[0], "state": r[1], "total": r[2], "hot": r[3],
         "lapsed": r[4], "pipeline": (r[2] or 0) * 75000}
        for r in rows
    ]})


@app.route('/api/leads/<lead_id>')
def get_lead(lead_id):
    conn = get_db()
    row = conn.execute("""
        SELECT u.*, lc.status as claim_status, lc.notes, lc.claimed_at
        FROM ucc_leads u
        LEFT JOIN lead_claims lc ON lc.lead_id = u.id AND lc.broker_name = ?
        WHERE u.id = ?
    """, [DEFAULT_BROKER, lead_id]).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))


@app.route('/api/leads/<lead_id>/claim', methods=['POST'])
def claim_lead(lead_id):
    data   = request.get_json() or {}
    status = data.get('status', 'claimed')
    notes  = data.get('notes', '')
    conn   = get_db()
    conn.execute("""
        INSERT INTO lead_claims (lead_id, broker_name, status, notes)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(lead_id, broker_name) DO UPDATE SET
            status = excluded.status,
            notes  = excluded.notes,
            updated_at = datetime('now')
    """, [lead_id, DEFAULT_BROKER, status, notes])
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "status": status})


@app.route('/api/leads/<lead_id>/unclaim', methods=['POST'])
def unclaim_lead(lead_id):
    conn = get_db()
    conn.execute("DELETE FROM lead_claims WHERE lead_id=? AND broker_name=?",
                 [lead_id, DEFAULT_BROKER])
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Expiry Timeline API (chart data) ──────────────────────────────────────

@app.route('/api/expiry-timeline')
def expiry_timeline():
    state      = request.args.get('state', '')
    status_f   = request.args.get('status', 'all')
    signal_f   = request.args.get('signal', 'all')
    tier_f     = request.args.get('tier', 'all')
    category_f = request.args.get('category', 'all')
    urgency    = request.args.get('urgency', '')

    where = ["days_to_lapse >= 0 AND days_to_lapse <= 90"]
    params = []

    if state and state != 'all':
        where.append("source_state = ?")
        params.append(state)

    if urgency == '7d':
        where.append("days_to_lapse <= 7")
    elif urgency == '14d':
        where.append("days_to_lapse <= 14")
    elif urgency == 'hot':
        where.append("days_to_lapse <= 30")

    if tier_f == 'A':
        where.append("paydex_score >= 80")
    elif tier_f == 'B':
        where.append("paydex_score >= 65 AND paydex_score < 80")
    elif tier_f == 'C':
        where.append("paydex_score >= 50 AND paydex_score < 65")
    elif tier_f == 'D':
        where.append("paydex_score < 50")

    if category_f and category_f != 'all':
        where.append("tech_category = ?")
        params.append(category_f)

    if signal_f == 'expansion':
        where.append("signals_json LIKE '%S2_EXPANSION%'")
    elif signal_f == 'tech':
        where.append("(tech_company = 'true' OR tech_category IN ('IT_OEM','IT_CHANNEL','CLOUD_SAAS'))")
    elif signal_f == 'hiring':
        where.append("signals_json LIKE '%S3_HIRING%'")

    claim_join = ""
    if status_f != 'all':
        claim_join = "LEFT JOIN lead_claims lc ON lc.lead_id = ucc_leads.id AND lc.broker_name = ?"
        params.insert(0, DEFAULT_BROKER)
        if status_f == 'unclaimed':
            where.append("lc.id IS NULL")
        elif status_f == 'claimed':
            where.append("lc.id IS NOT NULL")

    where_sql = " AND ".join(where)

    conn = get_db()
    rows = conn.execute(f"""
        SELECT
            CAST((days_to_lapse / 7) AS INTEGER) as week_bucket,
            COUNT(*) as count,
            SUM(CASE WHEN phone IS NOT NULL AND phone != '' THEN 1 ELSE 0 END) as enriched
        FROM ucc_leads
        {claim_join}
        WHERE {where_sql}
        GROUP BY week_bucket
        ORDER BY week_bucket ASC
    """, params).fetchall()
    conn.close()
    
    weeks = []
    for r in rows:
        w = r[0]
        weeks.append({
            "week": w,
            "label": f"Wk {w+1}" if w < 12 else "13+",
            "count": r[1],
            "enriched": r[2]
        })
    return jsonify({"weeks": weeks})



# ── Capex Intelligence Engines ───────────────────────────────────────────────
import re as _re, time as _time, concurrent.futures as _cf
import feedparser as _fp
try:
    import requests as _req
except ImportError:
    import urllib.request as _req
from urllib.parse import quote_plus as _qp

_CAPEX_TO = 7

def _capex_headers():
    return {'User-Agent': 'Mozilla/5.0 (compatible; TomcatCapex/1.0)'}

# ── Tier inference from secured_party name ────────────────────────────────────
def _infer_tier(secured_party):
    sp = (secured_party or '').upper()
    CAPTIVE = ['DEERE & COMPANY','CNH INDUSTRIAL','CNH CAPITAL','CAT FINANCIAL',
               'CATERPILLAR FINANCIAL','PACCAR FINANCIAL','VOLVO FINANCIAL',
               'KOMATSU FINANCIAL','AGCO FINANCE','KUBOTA CREDIT']
    BANK    = ['BANK','CREDIT UNION','FARM CREDIT','FEDERAL CREDIT','SAVINGS',
               'TRUST','NATIONAL BANK','STATE BANK','COMMUNITY BANK']
    FIN     = ['CAPITAL','FINANCE','LEASING','FINANCIAL','EQUIPMENT FINANCE',
               'EQUIPMENT LEASING','DLL','DE LAGE','PAWNEE','STEARNS']
    if any(c in sp for c in CAPTIVE):  return 'D'
    if any(b in sp for b in BANK):     return 'A'
    if any(f in sp for f in FIN):      return 'B'
    return 'C'

_CAP_TIER_COLOR = {'A':'#34d399','B':'#60a5fa','C':'#fbbf24','D':'#f87171',None:'#64748b'}
_CAP_TIER_LABEL = {'A':'Prime Bank','B':'Equipment Finance Co','C':'Alt/Regional Lender','D':'OEM Captive',None:'Unknown'}
_CAP_TIER_RANK  = {'A':4,'B':3,'C':2,'D':1,None:0}

def _clean_capex_lender(name):
    n = (name or '').strip()
    n = _re.sub(r',?\s*(PCA|FLCA|ACA|FLCA|WHOLLY OWNED.*|A WHOLLY.*)',r'',n,flags=_re.I)
    n = _re.sub(r'\s+SERIES\s+\d+','',n,flags=_re.I)
    n = _re.sub(r'\s{2,}',' ',n).strip().rstrip(',').strip()
    return n[:50]

def _collateral_quality(collateral):
    c = (collateral or '').lower()
    if any(w in c for w in ['new ','brand new','year model','202','2019','2020','2021','2022','2023','2024','2025']):
        return 'new'
    if any(w in c for w in ['used','all assets','all personal property','blanket','general']):
        return 'generic'
    return 'described'

def _capex_narrative(events):
    if not events: return None
    n = len(events)
    tiers = [e['tier'] for e in events]
    ranks = [_CAP_TIER_RANK.get(t,0) for t in tiers]
    first, last = events[0], events[-1]

    unique_lenders = []
    seen = set()
    for e in events:
        cl = e['lender_clean']
        if cl and cl not in seen:
            seen.add(cl)
            unique_lenders.append({'lender':cl,'tier':e['tier']})

    rank_delta = (ranks[-1]-ranks[0]) if len(ranks)>=2 else 0

    cycle_days = []
    for i in range(1, len(events)):
        d0,d1 = events[i-1].get('filing_date',''), events[i].get('filing_date','')
        if d0 and d1:
            try:
                from datetime import datetime as _dt
                diff = (_dt.strptime(d1,'%Y-%m-%d')-_dt.strptime(d0,'%Y-%m-%d')).days
                if 0<diff<5000: cycle_days.append(diff)
            except: pass
    avg_cycle = int(sum(cycle_days)/len(cycle_days)) if cycle_days else None
    shrinking = (len(cycle_days)>=3 and cycle_days[-1]<cycle_days[0]*0.7) if cycle_days else False

    col_qualities = [e.get('collateral_quality') for e in events]
    col_degraded  = (col_qualities[0]=='new' and col_qualities[-1]=='generic') if len(col_qualities)>=2 else False

    key_signals = []
    if n==1:
        key_signals.append({'label':'1 filing in our database','type':'neutral',
            'detail':'Only one UCC equipment filing found across our scraped states (CO, CT, CA, OR, FL). Prior history may exist in other states — this is a DB coverage limitation, not a confirmed clean slate.'})
    elif n<=3:
        key_signals.append({'label':f'{n} equipment financings','type':'neutral','detail':f'Moderate equipment financing history across {n} filings.'})
    else:
        key_signals.append({'label':f'{n} equipment financings (serial borrower)','type':'warning','detail':f'Heavy UCC history — {n} filings. High probability of active positions.'})

    if rank_delta<=-2:
        key_signals.append({'label':'Lender quality decline','type':'negative','detail':f'Went from {_CAP_TIER_LABEL.get(first["tier"])} to {_CAP_TIER_LABEL.get(last["tier"])} — creditworthiness deteriorating.'})
    elif rank_delta>=2:
        key_signals.append({'label':'Lender quality improvement','type':'positive','detail':f'Financing upgraded from {_CAP_TIER_LABEL.get(first["tier"])} to {_CAP_TIER_LABEL.get(last["tier"])}.'})
    if 'D' in tiers:
        key_signals.append({'label':'OEM captive financing on record','type':'warning','detail':'Used manufacturer captive (Deere, CNH, CAT) — either couldn\'t qualify for bank rate or needed OEM terms. Approach with brand-agnostic alternative.'})
    if shrinking and avg_cycle:
        key_signals.append({'label':f'Accelerating cycle ({avg_cycle}d avg)','type':'negative','detail':'Returning to market faster — possible cash flow compression or equipment fleet expansion.'})
    elif avg_cycle and avg_cycle < 730:
        key_signals.append({'label':f'Short financing cycle ({avg_cycle}d avg)','type':'warning','detail':'Equipment turnover or financing cycle shorter than typical 3–5yr term.'})
    if col_degraded:
        key_signals.append({'label':'Collateral quality declining','type':'negative','detail':'Early filings show specific named equipment; later filings use blanket/all-assets — lenders taking broader security.'})

    repeat_count = n - len(unique_lenders)
    if n>=3 and repeat_count>=2:
        key_signals.append({'label':f'Lender loyalty ({repeat_count} repeat engagements)','type':'positive','detail':'Returns to same lender — relationship-based, stable financing.'})
    elif n>=4 and len(unique_lenders)==n:
        key_signals.append({'label':'Always switches lenders','type':'warning','detail':'Every filing used a different lender — relationships not maintained or declined to renew.'})

    neg = sum(1 for s in key_signals if s['type']=='negative')
    warn = sum(1 for s in key_signals if s['type']=='warning')
    if neg>=2 or (neg>=1 and n>=5): risk_level,risk_color,risk_bg,risk_border='HIGH','#f87171','rgba(239,68,68,.08)','rgba(239,68,68,.25)'
    elif neg>=1 or warn>=2:         risk_level,risk_color,risk_bg,risk_border='ELEVATED','#fbbf24','rgba(251,191,36,.08)','rgba(251,191,36,.25)'
    elif n==1 and ranks[0]>=3:      risk_level,risk_color,risk_bg,risk_border='LOW','#34d399','rgba(52,211,153,.08)','rgba(52,211,153,.25)'
    else:                           risk_level,risk_color,risk_bg,risk_border='MODERATE','#60a5fa','rgba(96,165,250,.08)','rgba(96,165,250,.25)'

    if n==1:
        headline = f"1 equipment financing on record from a {_CAP_TIER_LABEL.get(last['tier'])} — no additional history in our database (CO/CT/CA/OR/FL coverage)."
    elif rank_delta<=-2 and n>=3:
        headline = f"{n} equipment financings with lender decline: {_CAP_TIER_LABEL.get(first['tier'])} → {_CAP_TIER_LABEL.get(last['tier'])}. Approach with aggressive terms."
    elif 'D' in tiers and n>=2:
        headline = f"Serial equipment borrower ({n} filings) who reached OEM captive financing. Brand-agnostic alternative is a strong pitch."
    elif col_degraded:
        headline = f"Collateral quality has degraded — early specific equipment filings, now blanket liens. Lenders are taking broader security."
    else:
        headline = f"{n} equipment financings across {len(unique_lenders)} lender(s). Current: {_CAP_TIER_LABEL.get(last['tier'])}."

    return {'headline':headline,'risk_level':risk_level,'risk_color':risk_color,
            'risk_bg':risk_bg,'risk_border':risk_border,'key_signals':key_signals,
            'unique_lenders':unique_lenders,'avg_cycle_days':avg_cycle,'total_fundings':n,
            'tier_start':first['tier'],'tier_end':last['tier'],
            'tier_start_color':_CAP_TIER_COLOR.get(first['tier'],'#64748b'),
            'tier_end_color':_CAP_TIER_COLOR.get(last['tier'],'#64748b'),
            'col_degraded':col_degraded}

@app.route('/api/leads/<lead_id>/stack-history')
def capex_stack_history(lead_id):
    conn = get_db()
    lead = conn.execute('SELECT * FROM ucc_leads WHERE id=?',[lead_id]).fetchone()
    if not lead:
        conn.close(); return jsonify({'error':'Not found'}),404
    lead = dict(lead)
    company,state = lead.get('company_name','').strip(), lead.get('state','').strip()
    rows = conn.execute("""
        SELECT id,filing_date,lapse_date,secured_party,collateral,days_to_lapse,state
        FROM ucc_leads WHERE LOWER(company_name)=LOWER(?)
        ORDER BY filing_date ASC NULLS LAST, id ASC LIMIT 30
    """,[company]).fetchall()
    conn.close()
    events = []
    for r in rows:
        tier = _infer_tier(r['secured_party'])
        lc   = _clean_capex_lender(r['secured_party'])
        cq   = _collateral_quality(r['collateral'])
        events.append({'id':r['id'],'filing_date':r['filing_date'] or '',
            'lapse_date':r['lapse_date'] or '','tier':tier,'tier_color':_CAP_TIER_COLOR.get(tier,'#64748b'),
            'tier_label':_CAP_TIER_LABEL.get(tier,'Unknown'),'lender_raw':(r['secured_party'] or '')[:60],
            'lender_clean':lc,'collateral':(r['collateral'] or '')[:60],
            'collateral_quality':cq,'days_to_lapse':r['days_to_lapse'],
            'state': r['state'] or state,
            'is_current':str(r['id'])==str(lead_id)})
    return jsonify({'company':company,'state':state,'events':events,'narrative':_capex_narrative(events)})

# ── Court Sweep (same 4 sources as MCA) ───────────────────────────────────────
def _cx_court(company):
    try:
        url=f"https://www.courtlistener.com/api/rest/v3/search/?q=%22{_qp(company)}%22&type=r&order_by=score+desc&page_size=5"
        r=_req.get(url,headers=_capex_headers(),timeout=_CAPEX_TO)
        if r.status_code!=200: return []
        out=[]
        for item in (r.json().get('results') or [])[:5]:
            name=(item.get('caseName') or '')[:80]; court=item.get('court','')
            is_bk=any(x in name.lower() for x in ['bankrupt','chapter 7','chapter 11'])
            out.append({'source':'CourtListener','provider_class':'court',
                'type':'Bankruptcy' if is_bk else 'Federal Civil Record','headline':name,
                'detail':f"Court: {court.upper()} · Filed: {(item.get('dateFiled') or '')[:10]}",
                'date':(item.get('dateFiled') or '')[:10],'status':item.get('status',''),
                'url':f"https://www.courtlistener.com{item.get('absolute_url','')}",
                'severity':'high' if is_bk else 'medium'})
        return out
    except: return []

def _cx_cfpb(company):
    try:
        url=f"https://api.consumerfinance.gov/data/complaints/.json?search_term={_qp(company)}&field=company&size=5&sort=created_date_desc"
        r=_req.get(url,headers=_capex_headers(),timeout=_CAPEX_TO)
        if r.status_code!=200: return {'count':0,'items':[]}
        data=r.json(); hits=data.get('hits',{}); total=hits.get('total',0)
        if isinstance(total,dict): total=total.get('value',0)
        items=[]
        for h in (hits.get('hits') or [])[:4]:
            s=h.get('_source',{})
            items.append({'source':'CFPB','provider_class':'cfpb','type':'Consumer Complaint',
                'headline':f"{s.get('product','')}: {(s.get('issue') or '')[:60]}",
                'detail':f"Response: {s.get('company_response','—')}",
                'date':(s.get('date_received') or '')[:10],'status':s.get('company_response',''),
                'url':'https://www.consumerfinance.gov/data-research/consumer-complaints/','severity':'medium'})
        return {'count':int(total),'items':items}
    except: return {'count':0,'items':[]}

def _cx_epa(company):
    out=[]
    for media,label in [('caa','Clean Air Act'),('rcra','Hazardous Waste')]:
        try:
            url=f"https://echodata.epa.gov/echo/{media}_rest_services.get_facilities?p_fn={_qp(company)}&output=JSON&p_rows=3"
            r=_req.get(url,headers=_capex_headers(),timeout=_CAPEX_TO)
            if r.status_code!=200: continue
            for f in ((r.json().get('Results') or {}).get('Facilities') or [])[:2]:
                qtrs=int(f.get(f'{media.upper()}QtrsWithNC',0) or 0)
                if qtrs==0: continue
                out.append({'source':'EPA ECHO','provider_class':'epa','type':label,
                    'headline':f"{f.get('FacilityName',company)[:50]} — {qtrs} quarter(s) non-compliant",
                    'detail':f"Registry: {f.get('RegistryID','')}",
                    'date':'','status':'Non-Compliant',
                    'url':f"https://echo.epa.gov/facilities/facility-search/results?p_fn={_qp(company)}",
                    'severity':'high' if qtrs>=4 else 'medium'})
        except: continue
    return out

def _cx_osha(company):
    try:
        url=(f"https://www.osha.gov/ords/imis/establishment.html?establishment_name={_qp(company)}"
             f"&state=All&officetype=fed&startmonth=01&startyear=2015&endmonth=12&endyear=2026"
             f"&action=31&p_start=&p_finish=0&p_sort=14&p_desc=DESC&p_direction=Next&p_show=5")
        r=_req.get(url,headers=_capex_headers(),timeout=_CAPEX_TO)
        if r.status_code!=200 or 'no records' in r.text.lower(): return []
        rows=_re.findall(r'<td[^>]*>\s*(\d{2}/\d{2}/\d{4})\s*</td>.*?penalty.*?>\s*\$?([\d,]*)\s*<',r.text,_re.DOTALL|_re.IGNORECASE)
        out=[]
        for date_str,pen in rows[:3]:
            p=int(pen.replace(',','')) if pen.replace(',','').isdigit() else 0
            out.append({'source':'OSHA','provider_class':'osha','type':'Workplace Safety Inspection',
                'headline':f"OSHA inspection — ${p:,} penalty" if p else "OSHA inspection on record",
                'detail':f"Inspection date: {date_str}",'date':date_str,
                'status':'Cited' if p>0 else 'Inspected',
                'url':f"https://www.osha.gov/ords/imis/establishment.html?establishment_name={_qp(company)}",
                'severity':'high' if p>=5000 else 'medium'})
        return out
    except: return []

def _cx_legal_risk(cr,cfpb,epa,osha):
    s=cr*3+(3 if cfpb>=10 else 2 if cfpb>=3 else 1 if cfpb>=1 else 0)+epa*3+osha*2
    if s==0:   return {'level':'CLEAR',   'color':'#34d399','bg':'rgba(52,211,153,.08)', 'border':'rgba(52,211,153,.25)','icon':'✓'}
    elif s<=3: return {'level':'LOW',     'color':'#60a5fa','bg':'rgba(96,165,250,.08)', 'border':'rgba(96,165,250,.25)','icon':'◎'}
    elif s<=7: return {'level':'ELEVATED','color':'#fbbf24','bg':'rgba(251,191,36,.08)', 'border':'rgba(251,191,36,.25)','icon':'⚠'}
    else:      return {'level':'HIGH',    'color':'#f87171','bg':'rgba(239,68,68,.08)',  'border':'rgba(239,68,68,.25)', 'icon':'⛔'}

@app.route('/api/leads/<lead_id>/court-sweep')
def capex_court_sweep(lead_id):
    conn=get_db()
    lead=conn.execute('SELECT * FROM ucc_leads WHERE id=?',[lead_id]).fetchone()
    conn.close()
    if not lead: return jsonify({'error':'Not found'}),404
    company=dict(lead).get('company_name','').strip()
    t0=_time.time()
    with _cf.ThreadPoolExecutor(max_workers=4) as ex:
        fc=ex.submit(_cx_court,company); ff=ex.submit(_cx_cfpb,company)
        fe=ex.submit(_cx_epa,company);   fo=ex.submit(_cx_osha,company)
        try: cr=fc.result(timeout=10)
        except: cr=[]
        try: cfpb=ff.result(timeout=10)
        except: cfpb={'count':0,'items':[]}
        try: epa=fe.result(timeout=10)
        except: epa=[]
        try: osha=fo.result(timeout=10)
        except: osha=[]
    cc=cfpb.get('count',0)
    all_items=cr+(cfpb.get('items') or [])+epa+osha
    risk=_cx_legal_risk(len(cr),cc,len(epa),len(osha))
    return jsonify({'company':company,'risk':risk,'items':all_items,'cfpb_total':cc,
        'sources_swept':['CourtListener','CFPB','EPA ECHO','OSHA'],
        'sources_hit':[s for s,v in [('CourtListener',cr),('CFPB',cc>0),('EPA ECHO',epa),('OSHA',osha)] if v],
        'total_findings':len(all_items),'elapsed_s':round(_time.time()-t0,2)})

# ── Market Intelligence (equipment sector keywords) ───────────────────────────
_CX_INDUSTRY_KW=[
    ('Agricultural',        ['farm','agricultural','crop','harvest','grain','livestock','dairy','poultry','irrigation','tractor','combine','ranch','orchard']),
    ('Construction',        ['construction','contractor','excavat','paving','concrete','masonry','roofing','builder','demolition','earthwork','plumbing','hvac','electrical','renovation','renovations','remodel','exterior','siding','framing','drywall','flooring','carpentry']),
    ('Transportation',      ['truck','transport','logistics','freight','fleet','carrier','hauling','shipping','delivery','semi','courier','moving']),
    ('Automotive',          ['auto dealer','car dealer','motorcycle','harley','motor co','motors ','powersports','motorsport','auto group','auto repair','auto shop','mechanic','body shop','collision center','tire ']),
    ('Manufacturing',       ['manufactur','industrial','fabricat','production','processing','assembly','plant','mill','machining','welding']),
    ('Healthcare',          ['medical','dental','health','clinic','hospital','pharmacy','therapy','chiro','veterinary','optometry','imaging','rehab']),
    ('Technology',          ['technology','software','data center','server','telecom','communications','IT ','tech ','cybersecurity','cloud']),
    ('Energy',              ['energy','oil','gas','pipeline','mining','drilling','solar','wind','utilities','petroleum','electric']),
    ('Hospitality',         ['golf','country club','resort','hotel','motel','inn','spa','tourism','hospitality','lodge','marina','yacht']),
    ('Food Service',        ['restaurant','catering','bakery','cafe','cafeteria','food service','kitchen','diner','bar ','tavern','brewery','winery','distillery']),
    ('Retail',              ['retail','store','shop ','boutique','dealership','auto sales','furniture','hardware','pharmacy','supermarket','grocery']),
    ('Real Estate',         ['real estate','realty','propert','apartment','housing','landlord','commercial property','storage']),
    ('Legal',               ['law firm','law office','attorney','counsel',' llp',' pc,','legal services']),
    ('Sports & Recreation', ['sport','fitness','gym','recreation','athletic','stadium','arena','pool','bowling','tennis','yoga','dance']),
    ('Landscaping',         ['landscap','lawn','garden','tree service','irrigation','grounds']),
    ('Small Business',      []),
]
_CX_POS=['growth','expansion','revenue','record','award','contract','hire','new facility','opens','acquisition']
_CX_NEG=['bankrupt','default','lawsuit','fraud','recall','shutdown','penalty','investigation','layoff','closure']

_CX_INDUSTRY_QUERY={
    'Agricultural':        'agriculture farming industry 2026 crop prices commodity outlook seasonal demand',
    'Construction':        'construction industry 2026 contractor outlook permits infrastructure spending costs',
    'Transportation':      'trucking freight industry 2026 owner operator carrier rates fuel diesel outlook',
    'Automotive':          'auto dealership motorcycle powersports 2026 vehicle sales service consumer demand trends',
    'Manufacturing':       'manufacturing sector 2026 production activity supply chain reshoring trends',
    'Healthcare':          'healthcare medical industry 2026 practice growth patient demand regulation',
    'Technology':          'technology small business 2026 IT spending software SaaS market trends outlook',
    'Energy':              'energy sector 2026 oil gas solar utilities business outlook prices',
    'Hospitality':         'golf resort hotel hospitality 2026 tourism travel demand industry trends',
    'Food Service':        'restaurant food service industry 2026 dining consumer spending trends',
    'Retail':              'retail sales 2026 consumer spending small business market trends outlook',
    'Real Estate':         'commercial real estate 2026 property market rents vacancy investment trends',
    'Legal':               'law firm legal services 2026 small firm market billable hours trends',
    'Sports & Recreation': 'fitness gym recreation industry 2026 consumer health spending trends',
    'Landscaping':         'landscaping lawn care industry 2026 seasonal demand labor market trends',
}

def _cx_infer_industry(company, collateral=''):
    """Two-pass classification: company name first, collateral only as fallback.
    Prevents equipment type (Tech Equipment, Construction Equipment) from
    overriding the company's actual business sector."""
    import re
    # Pass 1: company name alone
    co_txt = company.lower()
    for label, kws in _CX_INDUSTRY_KW:
        if kws and any(k in co_txt for k in kws):
            return label
    # Pass 2: add collateral
    # Strip: parentheticals (lender names), AND Capex enricher-generated generic labels
    # that always false-fire Technology for non-tech companies
    clean_col = re.sub(r'\([^)]*\)', '', collateral, flags=re.I)
    clean_col = re.sub(r'\btech(?:nology)?\s+equipment\b', '', clean_col, flags=re.I)
    clean_col = re.sub(r'\bequipment\s+financing\b', '', clean_col, flags=re.I)
    clean_col = re.sub(r'\ball\s+(?:assets|inventory|equipment)\b', '', clean_col, flags=re.I)
    clean_col = clean_col.strip()
    if not clean_col:  # nothing meaningful left — don't risk a false positive
        return 'Small Business'
    full_txt = (company + ' ' + clean_col).lower()
    for label, kws in _CX_INDUSTRY_KW:
        if kws and any(k in full_txt for k in kws):
            return label
    return 'Small Business'

def _cx_industry_query(industry, company):
    """Build a targeted news query — use company name words when industry is generic."""
    if industry in _CX_INDUSTRY_QUERY:
        return _CX_INDUSTRY_QUERY[industry]
    # Fallback: extract meaningful words from company name
    stopwords = {'llc','inc','corp','co','ltd','the','and','of','in','for','a','an','group','services','solutions'}
    words = [w.strip('.,') for w in company.split() if len(w) > 2 and w.lower().strip('.,') not in stopwords]
    if words:
        return f"{' '.join(words[:3])} industry news business"
    return 'small business industry news'

def _cx_fetch_gnews(query,max_items=5):
    try:
        url=f"https://news.google.com/rss/search?q={_qp(query)}&hl=en-US&gl=US&ceid=US:en"
        feed=_fp.parse(url)
        out=[]
        for e in feed.entries[:max_items]:
            out.append({'headline':(e.get('title') or '')[:120],'url':e.get('link',''),
                'provider':'Google News','summary':'','scope':'company','published':e.get('published','')})
        return out
    except: return []

def _cx_fetch_bing(query,max_items=5):
    try:
        url=f"https://www.bing.com/news/search?q={_qp(query)}&format=rss"
        feed=_fp.parse(url)
        out=[]
        for e in feed.entries[:max_items]:
            out.append({'headline':(e.get('title') or '')[:120],'url':e.get('link',''),
                'provider':'Bing News','summary':'','scope':'industry','published':e.get('published','')})
        return out
    except: return []

def _cx_fetch_reddit(query,max_items=4):
    try:
        url=f"https://www.reddit.com/search.json?q={_qp(query)}&sort=new&limit={max_items}"
        r=_req.get(url,headers={'User-Agent':'TomcatCapex/1.0'},timeout=_CAPEX_TO)
        if r.status_code!=200: return []
        out=[]
        for p in (r.json().get('data',{}).get('children') or [])[:max_items]:
            d=p.get('data',{})
            out.append({'headline':(d.get('title') or '')[:120],'url':f"https://reddit.com{d.get('permalink','')}",
                'provider':'Reddit','summary':d.get('selftext','')[:200],'scope':'company','published':''})
        return out
    except: return []

@app.route('/api/leads/<lead_id>/intel')
def capex_intel(lead_id):
    conn=get_db()
    lead=conn.execute('SELECT * FROM ucc_leads WHERE id=?',[lead_id]).fetchone()
    conn.close()
    if not lead: return jsonify({'error':'Not found'}),404
    lead=dict(lead)
    company=lead.get('company_name','').strip()
    collateral=lead.get('collateral','')
    industry=_cx_infer_industry(company,collateral)
    ind_query=_cx_industry_query(industry, company)
    t0=_time.time()
    articles=[]
    import re as _re
    _co_stopwords = {'llc','inc','corp','co','ltd','the','and','of','in','for','a','an','group','services','solutions'}
    def _co_relevant(headline):
        tokens = {w.lower().strip('.,&') for w in company.split() if len(w) > 2}
        tokens -= _co_stopwords
        if not tokens: return True
        h = headline.lower()
        return any(t in h for t in tokens)

    seen_urls, seen_titles = set(), set()
    def safe_add(lst, scope):
        for a in (lst or []):
            h = a.get('headline','')
            u = a.get('url','')
            if u and u in seen_urls: continue
            if h and h in seen_titles: continue
            # Relevance gate for company-scoped articles
            if scope == 'company' and not _co_relevant(h): continue
            if u: seen_urls.add(u)
            if h: seen_titles.add(h)
            a['scope'] = scope
            articles.append(a)
    # Use quoted exact name for company searches — private small businesses have no media coverage
    company_q = f'"{company}"'
    with _cf.ThreadPoolExecutor(max_workers=3) as ex:
        fc = ex.submit(_cx_fetch_gnews, company_q)
        fi = ex.submit(_cx_fetch_bing,  ind_query)
        fg = ex.submit(_cx_fetch_gnews, ind_query)
        fb = ex.submit(_cx_fetch_bing,  company_q)
        try: safe_add(fc.result(timeout=_CAPEX_TO), 'company')
        except: pass
        try: safe_add(fi.result(timeout=_CAPEX_TO), 'industry')
        except: pass
        try: safe_add(fg.result(timeout=_CAPEX_TO), 'industry')
        except: pass
        try: safe_add(fb.result(timeout=_CAPEX_TO), 'company')
        except: pass
    elapsed=round(_time.time()-t0,2)
    co=[a for a in articles if a['scope']=='company'][:6]
    ind=[a for a in articles if a['scope']=='industry'][:10]
    score=0
    for a in ind:
        txt=(a['headline']+' '+a.get('summary','')).lower()
        score+=sum(1 for w in _CX_POS if w in txt)
        score-=sum(1 for w in _CX_NEG if w in txt)
    if score>=2:   sentiment={'label':'Positive','color':'#34d399','icon':'🟢'}
    elif score<=-2:sentiment={'label':'Negative','color':'#f87171','icon':'🔴'}
    else:          sentiment={'label':'Neutral', 'color':'#fbbf24','icon':'🟡'}
    sources={a['provider'] for a in articles}
    return jsonify({'company':co,'industry':ind,'industry_label':industry,
        'company_empty': len(co)==0,
        'sector_sentiment':sentiment,'sources_swept':list(sources),
        'elapsed_s':elapsed,'total_articles':len(articles)})


# ── Stats API ────────────────────────────────────────────────────────────────


@app.route('/api/stats')
def stats():
    state      = request.args.get('state', '')
    status_f   = request.args.get('status', 'all')
    signal_f   = request.args.get('signal', 'all')
    tier_f     = request.args.get('tier', 'all')
    category_f = request.args.get('category', 'all')
    urgency    = request.args.get('urgency', '')

    where = ["1=1"]
    params = []

    if state and state != 'all':
        where.append("u.source_state = ?")
        params.append(state)

    if urgency == '7d':
        where.append("u.days_to_lapse <= 7")
    elif urgency == '14d':
        where.append("u.days_to_lapse <= 14")
    elif urgency == 'hot':
        where.append("u.days_to_lapse <= 30")
    elif urgency == 'warm':
        where.append("u.days_to_lapse > 30 AND u.days_to_lapse <= 90")
    elif urgency == 'cold':
        where.append("u.days_to_lapse > 90")

    if tier_f == 'A':
        where.append("u.paydex_score >= 80")
    elif tier_f == 'B':
        where.append("u.paydex_score >= 65 AND u.paydex_score < 80")
    elif tier_f == 'C':
        where.append("u.paydex_score >= 50 AND u.paydex_score < 65")
    elif tier_f == 'D':
        where.append("u.paydex_score < 50")

    if category_f and category_f != 'all':
        where.append("u.tech_category = ?")
        params.append(category_f)

    if signal_f == 'expansion':
        where.append("u.signals_json LIKE '%S2_NEWS%'")
    elif signal_f == 'tech':
        where.append("(u.tech_company = 'true' OR u.tech_category IN ('IT_OEM','IT_CHANNEL','CLOUD_SAAS'))")
    elif signal_f == 'hiring':
        where.append("u.signals_json LIKE '%S3_HIRING%'")
    elif signal_f == 'multifiler':
        where.append("u.company_name IN (SELECT company_name FROM ucc_leads WHERE days_to_lapse > -30 GROUP BY company_name HAVING COUNT(*) >= 3)")

    claim_join = "LEFT JOIN lead_claims lc ON lc.lead_id = u.id AND lc.broker_name = ?"
    params.insert(0, DEFAULT_BROKER)

    if status_f == 'unclaimed':
        where.append("lc.id IS NULL")
    elif status_f == 'claimed':
        where.append("lc.id IS NOT NULL")

    where_sql = " AND ".join(where)
    base_from = f"FROM ucc_leads u {claim_join} WHERE {where_sql}"

    conn = get_db()
    total     = conn.execute(f"SELECT COUNT(*) {base_from}", params).fetchone()[0]
    hot       = conn.execute(f"SELECT COUNT(*) {base_from} AND u.days_to_lapse <= 30", params).fetchone()[0]
    warm      = conn.execute(f"SELECT COUNT(*) {base_from} AND u.days_to_lapse > 30 AND u.days_to_lapse <= 90", params).fetchone()[0]
    cold      = conn.execute(f"SELECT COUNT(*) {base_from} AND u.days_to_lapse > 90", params).fetchone()[0]
    expansion = conn.execute(f"SELECT COUNT(*) {base_from} AND u.signals_json LIKE '%S2_NEWS%'", params).fetchone()[0]
    states    = conn.execute(f"SELECT u.source_state, COUNT(*) as cnt {base_from} GROUP BY u.source_state ORDER BY cnt DESC", params).fetchall()
    my_claims = conn.execute(f"SELECT lc.status, COUNT(*) {base_from} AND lc.id IS NOT NULL GROUP BY lc.status", params).fetchall()
    enriched  = conn.execute(f"SELECT COUNT(*) {base_from} AND u.phone IS NOT NULL AND u.phone != ''", params).fetchone()[0]
    tech      = conn.execute(f"SELECT COUNT(*) {base_from} AND u.tech_company='true'", params).fetchone()[0]
    hiring    = conn.execute(f"SELECT COUNT(*) {base_from} AND u.signals_json LIKE '%S3_HIRING%'", params).fetchone()[0]
    osint_sweeps = conn.execute(f"SELECT COUNT(*) {base_from} AND u.enriched_at IS NOT NULL", params).fetchone()[0]

    # Revenue-focused metrics
    ready_now = conn.execute(f"""
        SELECT COUNT(*) {base_from}
        AND u.days_to_lapse <= 30 AND u.days_to_lapse >= 0
        AND u.phone IS NOT NULL AND u.phone != ''
    """, params).fetchone()[0]
    
    expiring_week = conn.execute(f"""
        SELECT COUNT(*) {base_from}
        AND u.days_to_lapse >= 0 AND u.days_to_lapse <= 7
    """, params).fetchone()[0]
    
    multi_filer = conn.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT u.company_name {base_from}
            AND u.days_to_lapse > -30
            GROUP BY u.company_name HAVING COUNT(*) >= 3
        )
    """, params).fetchone()[0]

    # ── Weighted Avg Gross Margin by lender type ──
    filtered_lenders = conn.execute(f"SELECT u.secured_party {base_from}", params).fetchall()

    gm_total = 0
    gm_count = 0
    for (lender_raw,) in filtered_lenders:
        lu = (lender_raw or '').upper()
        if any(b in lu for b in ['CATERPILLAR','JOHN DEERE','KOMATSU','CNH','KUBOTA']):
            gm = 4.0   # Captive — tight margins
        elif any(b in lu for b in ['WELLS FARGO','BANK OF AMERICA','CHASE','US BANK','PNC','TD BANK','CITIBANK']):
            gm = 5.0   # Big bank — moderate
        elif any(b in lu for b in ['DELL','LENOVO','IBM','CISCO','HEWLETT','HP ']):
            gm = 7.5   # IT/Tech OEM — good margins on refresh
        elif any(b in lu for b in ['XEROX','CANON','RICOH','KONICA','SHARP']):
            gm = 8.0   # Print/Imaging — high margins, small ticket
        elif any(b in lu for b in ['GREATAMERICA','MARLIN','LEAF','NAVITAS','CIT','DLL','STEARNS']):
            gm = 8.5   # Independent lessor — competitive spread
        elif any(b in lu for b in ['SACHEM','RED BRIDGE','FLATIRON','STORMFIELD']):
            gm = 12.0  # Bridge/hard money — max margin
        elif any(b in lu for b in ['MERCEDES','BMW','TOYOTA','FORD MOTOR']):
            gm = 5.5   # Auto captive
        elif any(b in lu for b in ['AMAZON']):
            gm = 10.0  # Amazon Capital — high displacement margin
        else:
            gm = 7.0   # Default specialty
        gm_total += gm
        gm_count += 1

    avg_gm = round(gm_total / gm_count, 1) if gm_count > 0 else 7.0

    uncontested = conn.execute(f"""
        SELECT COUNT(*) {base_from}
        AND u.days_to_lapse < 0 AND u.days_to_lapse >= -30
    """, params).fetchone()[0]

    conn.close()

    return jsonify({
        "total": total,
        "hot": hot,
        "warm": warm,
        "cold": cold,
        "expansion": expansion,
        "osint_sweeps": osint_sweeps,
        "enriched": enriched,
        "tech": tech,
        "hiring": hiring,
        "ready_now": ready_now,
        "expiring_week": expiring_week,
        "multi_filer": multi_filer,
        "avg_gm": avg_gm,
        "uncontested": uncontested,
        "states": [dict(r) for r in states],
        "my_pipeline": {r[0]: r[1] for r in my_claims}
    })


# ── Lead Purchase System ──────────────────────────────────────────────────────

# ── Urgency-based pricing (days_to_lapse drives value) ───────────────────────
# Individual lead prices (in cents)
LEAD_TIERS = {
    'hot_urgent': {'label': '🔴 Urgent',  'desc': 'Expires ≤7 days',  'price': 17500},  # $175
    'hot':        {'label': '🔥 Hot',     'desc': 'Expires ≤30 days', 'price': 12500},  # $125
    'warm':       {'label': '🟡 Warm',    'desc': '31–180 days',      'price':  6500},  # $65
    'cold':       {'label': '🔵 Cold',    'desc': '180+ days',        'price':  3500},  # $35
}

# Bulk pack pricing (per-lead rate with volume discount)
BULK_PACKS = {
    'pack_10':  {'qty': 10,  'label': '10-Lead Pack',  'discount': 0.10, 'price_per': None},
    'pack_25':  {'qty': 25,  'label': '25-Lead Pack',  'discount': 0.15, 'price_per': None},
    'pack_50':  {'qty': 50,  'label': '50-Lead Pack',  'discount': 0.20, 'price_per': None},
}


def get_lead_tier(lead):
    """Determine lead tier based on days_to_lapse (urgency drives price)."""
    dtl = lead.get('days_to_lapse')
    if dtl is not None and dtl <= 7:
        return 'hot_urgent', LEAD_TIERS['hot_urgent']
    elif dtl is not None and dtl <= 30:
        return 'hot', LEAD_TIERS['hot']
    elif dtl is not None and dtl <= 180:
        return 'warm', LEAD_TIERS['warm']
    return 'cold', LEAD_TIERS['cold']


def init_purchase_tables():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lead_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id TEXT NOT NULL,
            buyer_email TEXT,
            tier TEXT,
            price_cents INTEGER,
            stripe_session_id TEXT,
            stripe_payment_intent TEXT,
            status TEXT DEFAULT 'pending',
            purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(lead_id, status)
        )
    """)
    conn.commit()
    conn.close()


@app.route('/api/leads/<lead_id>/pricing')
def lead_pricing(lead_id):
    """Get pricing for a specific lead."""
    conn = get_db()
    row = conn.execute("SELECT * FROM ucc_leads WHERE id = ?", [lead_id]).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    lead = dict(row)
    score = compute_deal_score(lead)
    tier_key, tier = get_lead_tier(lead)

    purchased = conn.execute(
        "SELECT id FROM lead_purchases WHERE lead_id = ? AND status = 'completed'",
        [lead_id]
    ).fetchone()
    conn.close()

    return jsonify({
        "lead_id":      lead_id,
        "tier":         tier_key,
        "tier_label":   tier['label'],
        "tier_desc":    tier['desc'],
        "price_cents":  tier['price'],
        "price_display": f"${tier['price'] / 100:.0f}",
        "deal_score":   score,
        "is_purchased": purchased is not None,
        "exclusive":    True,
    })





@app.route('/api/leads/<lead_id>/checkout', methods=['POST'])
def create_checkout(lead_id):
    """Create a Stripe Checkout session for a lead."""
    if not stripe.api_key:
        return jsonify({"error": "Stripe not configured"}), 500

    conn = get_db()
    row = conn.execute("SELECT * FROM ucc_leads WHERE id = ?", [lead_id]).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    lead = dict(row)

    # Check if already purchased
    existing = conn.execute(
        "SELECT id FROM lead_purchases WHERE lead_id = ? AND status = 'completed'",
        [lead_id]
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "Lead already purchased"}), 409

    score = compute_deal_score(lead)
    tier_key, tier = get_lead_tier(lead)

    company = lead.get('company_name', 'Unknown Company')
    masked_company = _mask_name(company)
    host = request.host_url.rstrip('/')

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'unit_amount': tier['price'],
                    'product_data': {
                        'name': f'Tomcat Capex — {tier["label"]} Lead',
                        'description': f'{masked_company} | Score: {score} | Exclusive access',
                    },
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=f'{host}/purchase-success?session_id={{CHECKOUT_SESSION_ID}}&lead_id={lead_id}',
            cancel_url=f'{host}/',
            metadata={
                'lead_id': lead_id,
                'tier': tier_key,
                'company': masked_company,
            }
        )

        # Record pending purchase
        conn.execute(
            "INSERT INTO lead_purchases (lead_id, tier, price_cents, stripe_session_id, status) VALUES (?, ?, ?, ?, 'pending')",
            [lead_id, tier_key, tier['price'], session.id]
        )
        conn.commit()
        conn.close()

        return jsonify({"checkout_url": session.url, "session_id": session.id})

    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500


@app.route('/api/purchase/verify')
def verify_purchase():
    """Verify a completed purchase and unlock the lead."""
    session_id = request.args.get('session_id', '')
    lead_id = request.args.get('lead_id', '')
    if not session_id or not lead_id:
        return jsonify({"error": "Missing parameters"}), 400

    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status == 'paid':
            conn = get_db()
            conn.execute(
                "UPDATE lead_purchases SET status = 'completed', buyer_email = ?, stripe_payment_intent = ? WHERE stripe_session_id = ?",
                [session.customer_details.email if session.customer_details else '', session.payment_intent, session_id]
            )
            conn.commit()
            conn.close()
            return jsonify({"status": "completed", "lead_id": lead_id})
        else:
            return jsonify({"status": session.payment_status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/purchase-success')
def purchase_success():
    return send_from_directory(STATIC, 'index.html')


@app.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe Webhook events."""
    endpoint_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')

    try:
        if endpoint_secret and sig_header:
            event = stripe.Webhook.construct_event(
                payload, sig_header, endpoint_secret
            )
        else:
            # Fallback for local testing without webhook secret
            event = json.loads(payload)
    except ValueError as e:
        return jsonify({"error": "Invalid payload"}), 400
    except stripe.error.SignatureVerificationError as e:
        return jsonify({"error": "Invalid signature"}), 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        session_id = session.get('id')
        payment_intent = session.get('payment_intent')
        customer_details = session.get('customer_details')
        customer_email = customer_details.get('email', '') if customer_details else ''
        
        lead_id = session.get('metadata', {}).get('lead_id')
        
        conn = get_db()
        try:
            if lead_id:
                # Update existing pending purchase or insert if not present
                conn.execute(
                    "UPDATE lead_purchases SET status = 'completed', buyer_email = ?, stripe_payment_intent = ? WHERE stripe_session_id = ? OR (lead_id = ? AND status = 'pending')",
                    [customer_email, payment_intent, session_id, lead_id]
                )
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    tier = session.get('metadata', {}).get('tier', 'unknown')
                    price = session.get('amount_total', 0)
                    conn.execute(
                        "INSERT INTO lead_purchases (lead_id, tier, price_cents, stripe_session_id, stripe_payment_intent, buyer_email, status) VALUES (?, ?, ?, ?, ?, ?, 'completed')",
                        [lead_id, tier, price, session_id, payment_intent, customer_email]
                    )
            else:
                conn.execute(
                    "UPDATE lead_purchases SET status = 'completed', buyer_email = ?, stripe_payment_intent = ? WHERE stripe_session_id = ?",
                    [customer_email, payment_intent, session_id]
                )
            conn.commit()
        except Exception as e:
            print(f"Webhook DB error: {e}")
        finally:
            conn.close()

    return jsonify({"status": "success"}), 200



# ── Apollo On-Demand Contact Unlock ────────────────────────────────────────

@app.route('/api/leads/<lead_id>/contacts', methods=['POST'])
def capex_contact_unlock(lead_id):
    """
    On-demand Apollo contact fetch. Burns 1 Apollo credit per company per day.
    Gate: lead must be purchased first.
    """
    if not _is_purchased(lead_id):
        return jsonify({'error': 'Purchase required', 'locked': True}), 402

    conn = get_db()
    try:
        init_contact_cache(conn)

        row = conn.execute(
            'SELECT company_name, city, state FROM ucc_leads WHERE id = ?',
            [lead_id]
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'error': 'Lead not found'}), 404

        company_name = row['company_name'] or ''
        city         = row['city'] or ''
        state        = row['state'] or ''
        body         = request.get_json(silent=True, force=True) or {}
        buyer_email  = body.get('email', '')

        contacts = fetch_apollo_contacts(
            company_name, city, state, conn, lead_id, buyer_email
        )
        conn.close()

        return jsonify({
            'lead_id':  lead_id,
            'company':  company_name,
            'contacts': contacts,
            'count':    len(contacts),
            'source':   'apollo',
        })
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/unlock-stats')
def capex_unlock_stats():
    """Admin: engagement analytics — which leads are being worked."""
    conn = get_db()
    try:
        init_contact_cache(conn)
        stats = get_unlock_stats(conn)
        conn.close()
        return jsonify({'unlocks': stats, 'count': len(stats)})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve(path=''):
    if path and os.path.exists(os.path.join(STATIC, path)):
        return send_from_directory(STATIC, path)
    resp = make_response(send_from_directory(STATIC, 'index.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


init_portal_tables()
init_purchase_tables()

if __name__ == '__main__':
    print("\n" + "="*55)
    print("  TOMCAT CAPEX BROKER PORTAL")
    print("  http://localhost:5050")
    print("  No login required — dashboard loads directly")
    print("="*55 + "\n")
    app.run(host='0.0.0.0', port=5050, debug=False)
