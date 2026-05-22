"""
Tomcat Capex — Auto Router
/Users/robertle/tomcat_capex/auto_router.py

Reads qualified UCC leads from the database and delivers them to
registered broker partners via webhook POST or email.

Partner config lives in /tomcat_capex/config/partners.json
Each lead is delivered ONCE per partner. Delivery is logged in the DB.

Usage:
    python3 auto_router.py                   # Route all new leads to all active partners
    python3 auto_router.py --preview         # Preview what would be sent, no delivery
    python3 auto_router.py --partner acme    # Route only to one specific partner
    python3 auto_router.py --limit 50        # Cap delivery at 50 leads
"""

import os
import json
import time
import sqlite3
import logging
import smtplib
import argparse
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [TomcatCapex-Router] %(levelname)s - %(message)s'
)
logger = logging.getLogger("TomcatCapex.Router")

# ─── PATHS ─────────────────────────────────────────────────────────────────────

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DB_PATH      = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')
PARTNERS_CFG = os.path.join(BASE_DIR, 'config', 'partners.json')
LOG_DIR      = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)


# ─── PARTNER CONFIG SCHEMA ─────────────────────────────────────────────────────
#
# partners.json structure:
# [
#   {
#     "id": "vectra_equipment",
#     "name": "Vectra Equipment Finance",
#     "active": true,
#     "delivery_method": "email",        // "email" or "webhook"
#     "email": "leads@partnerbank.com",
#     "webhook_url": "",                 // Used if delivery_method = webhook
#     "webhook_secret": "",              // Optional HMAC header for webhook auth
#     "batch_size": 25,                  // Leads per delivery batch
#     "filters": {
#       "states": ["CO", "TX", "FL"],    // Only send leads from these states (empty = all)
#       "max_days_to_lapse": 90,         // Only send leads expiring within N days
#       "min_days_to_lapse": 1           // Don't send already-expired leads
#     },
#     "smtp": {
#       "host": "smtp.gmail.com",
#       "port": 587,
#       "user": "your@email.com",
#       "password": "app_password_here"
#     }
#   }
# ]


def load_partners() -> list:
    """Load partner config from JSON file. Creates sample config if missing."""
    if not os.path.exists(PARTNERS_CFG):
        os.makedirs(os.path.dirname(PARTNERS_CFG), exist_ok=True)
        sample = [
            {
                "id": "sample_partner",
                "name": "Sample Equipment Finance Partner",
                "active": False,
                "delivery_method": "email",
                "email": "partner@example.com",
                "webhook_url": "",
                "webhook_secret": "",
                "batch_size": 25,
                "filters": {
                    "states": [],
                    "max_days_to_lapse": 180,
                    "min_days_to_lapse": 1
                },
                "smtp": {
                    "host": "smtp.gmail.com",
                    "port": 587,
                    "user": "",
                    "password": ""
                }
            }
        ]
        with open(PARTNERS_CFG, 'w') as f:
            json.dump(sample, f, indent=2)
        logger.warning(f"No partners.json found. Sample config created at: {PARTNERS_CFG}")
        logger.warning("Add your broker partners to this file and set 'active': true")
        return []

    with open(PARTNERS_CFG) as f:
        partners = json.load(f)

    active = [p for p in partners if p.get('active', False)]
    logger.info(f"Loaded {len(active)} active partners (of {len(partners)} total)")
    return active


# ─── DATABASE ──────────────────────────────────────────────────────────────────

def init_routing_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS routing_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id       TEXT NOT NULL,
            partner_id    TEXT NOT NULL,
            delivered_at  TEXT DEFAULT (datetime('now')),
            method        TEXT,
            status        TEXT,
            response      TEXT,
            UNIQUE(lead_id, partner_id)
        )
    """)
    conn.commit()
    conn.close()


def get_unrouted_leads(partner_id: str, filters: dict, limit: int = None) -> list:
    """Fetch leads not yet routed to this specific partner, applying filters."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    query = """
        SELECT l.*
        FROM ucc_leads l
        WHERE l.status = 'new'
          AND NOT EXISTS (
              SELECT 1 FROM routing_log r
              WHERE r.lead_id = l.id AND r.partner_id = ?
          )
    """
    params = [partner_id]

    # State filter
    states = filters.get('states', [])
    if states:
        placeholders = ','.join('?' * len(states))
        query += f" AND l.state IN ({placeholders})"
        params.extend(states)

    # Lapse window filter
    min_days = filters.get('min_days_to_lapse', 1)
    max_days = filters.get('max_days_to_lapse', 180)
    query += " AND l.days_to_lapse >= ? AND l.days_to_lapse <= ?"
    params.extend([min_days, max_days])

    # Order by urgency (soonest expiry first)
    query += " ORDER BY l.days_to_lapse ASC"

    if limit:
        query += f" LIMIT {limit}"

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_delivery(lead_id: str, partner_id: str, method: str, status: str, response: str):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT OR IGNORE INTO routing_log (lead_id, partner_id, method, status, response)
            VALUES (?, ?, ?, ?, ?)
        """, [lead_id, partner_id, method, status, response[:500] if response else ''])
        conn.commit()
    finally:
        conn.close()


def mark_lead_routed(lead_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE ucc_leads SET status = 'routed', routed_at = datetime('now') WHERE id = ?", [lead_id])
    conn.commit()
    conn.close()


# ─── LEAD CARD FORMATTERS ──────────────────────────────────────────────────────

def format_lead_card_text(lead: dict) -> str:
    """Plain text lead card for email body."""
    lapse_note = f"{lead['days_to_lapse']} days" if lead.get('days_to_lapse') else "N/A"
    return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOMCAT CAPEX — EQUIPMENT FINANCING LEAD
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

COMPANY     : {lead.get('company_name', 'N/A')}
ADDRESS     : {lead.get('address', '')}
CITY/STATE  : {lead.get('city', '')}, {lead.get('state', '')} {lead.get('zipcode', '')}

EQUIPMENT   : {lead.get('collateral', 'N/A')}
PRIOR LENDER: {lead.get('secured_party', 'N/A')}

FILING DATE : {lead.get('filing_date', 'N/A')}
LAPSE DATE  : {lead.get('lapse_date', 'N/A')}  (expires in {lapse_note})
SOURCE STATE: {lead.get('source_state', 'N/A')}
UCC FILE ID : {lead.get('file_id', 'N/A')}

SIGNAL TYPE : UCC-1 Expiring Equipment Lien (Public Record)
CONFIDENCE  : CONFIRMED — Not inferred. Verified via state filing.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""".strip()


def format_batch_email(leads: list, partner_name: str) -> tuple[str, str]:
    """Returns (subject, html_body) for a batch email delivery."""
    subject = f"[Tomcat Capex] {len(leads)} Equipment Financing Leads — UCC Expiring This Week"

    lead_rows = ""
    for lead in leads:
        lapse_note = f"{lead['days_to_lapse']}d" if lead.get('days_to_lapse') else "?"
        urgency_color = "#d73a49" if (lead.get('days_to_lapse') or 999) <= 30 else "#e36209" if (lead.get('days_to_lapse') or 999) <= 90 else "#28a745"
        lead_rows += f"""
        <tr>
            <td style="padding:10px 8px;border-bottom:1px solid #f0f0f0;font-weight:600;">{lead.get('company_name','')}</td>
            <td style="padding:10px 8px;border-bottom:1px solid #f0f0f0;">{lead.get('city','')}, {lead.get('state','')}</td>
            <td style="padding:10px 8px;border-bottom:1px solid #f0f0f0;font-size:0.85em;color:#555;">{lead.get('collateral','')[:60]}</td>
            <td style="padding:10px 8px;border-bottom:1px solid #f0f0f0;font-size:0.85em;color:#555;">{lead.get('secured_party','')[:40]}</td>
            <td style="padding:10px 8px;border-bottom:1px solid #f0f0f0;text-align:center;">
                <span style="background:{urgency_color};color:#fff;padding:2px 8px;border-radius:4px;font-size:0.8em;font-weight:700;">{lapse_note}</span>
            </td>
            <td style="padding:10px 8px;border-bottom:1px solid #f0f0f0;font-size:0.8em;color:#888;">{lead.get('lapse_date','')}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Inter,sans-serif;background:#f8f8f8;padding:20px;">
<div style="max-width:900px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,0.08);">

  <div style="background:#111;padding:24px 32px;">
    <h1 style="color:#fff;margin:0;font-size:1.3rem;letter-spacing:1px;">TOMCAT CAPEX</h1>
    <p style="color:#888;margin:4px 0 0;font-size:0.85rem;">Equipment Financing Lead Delivery — Batch {datetime.now().strftime('%Y-%m-%d')}</p>
  </div>

  <div style="padding:24px 32px;">
    <p style="color:#333;">Hi {partner_name},</p>
    <p style="color:#555;">Here are <strong>{len(leads)} confirmed equipment financing leads</strong> with UCC-1 liens expiring in the next 180 days. Each company has an active equipment lien on public record — not inferred. Prior lender and equipment type are included.</p>
    <p style="color:#555;font-size:0.85em;">Sorted by urgency (soonest expiry first). Red = expiring within 30 days.</p>

    <table style="width:100%;border-collapse:collapse;margin-top:20px;">
      <thead>
        <tr style="background:#f5f5f5;">
          <th style="padding:10px 8px;text-align:left;font-size:0.8rem;color:#888;text-transform:uppercase;">Company</th>
          <th style="padding:10px 8px;text-align:left;font-size:0.8rem;color:#888;text-transform:uppercase;">Location</th>
          <th style="padding:10px 8px;text-align:left;font-size:0.8rem;color:#888;text-transform:uppercase;">Equipment</th>
          <th style="padding:10px 8px;text-align:left;font-size:0.8rem;color:#888;text-transform:uppercase;">Prior Lender</th>
          <th style="padding:10px 8px;text-align:center;font-size:0.8rem;color:#888;text-transform:uppercase;">Days Left</th>
          <th style="padding:10px 8px;text-align:left;font-size:0.8rem;color:#888;text-transform:uppercase;">Lapse Date</th>
        </tr>
      </thead>
      <tbody>{lead_rows}</tbody>
    </table>

    <div style="margin-top:32px;padding:16px;background:#f9f9f9;border-radius:6px;font-size:0.8rem;color:#888;">
      <strong>Data Source:</strong> Colorado Secretary of State UCC filing database (public record).<br>
      <strong>Signal:</strong> UCC-1 Equipment Lien — confirms prior equipment financing. Expiry date = active renewal window.<br>
      <strong>Tomcat Capex</strong> — Autonomous Equipment Lead Engine
    </div>
  </div>

</div>
</body>
</html>"""
    return subject, html


def format_webhook_payload(leads: list, partner: dict) -> dict:
    """JSON payload for webhook delivery."""
    return {
        "source": "tomcat_capex",
        "delivered_at": datetime.now().isoformat(),
        "partner_id": partner['id'],
        "lead_count": len(leads),
        "leads": leads
    }


# ─── DELIVERY ENGINES ──────────────────────────────────────────────────────────

def deliver_via_email(leads: list, partner: dict, preview: bool = False) -> bool:
    """Send a batch of leads via SMTP email."""
    smtp_cfg = partner.get('smtp', {})
    if not smtp_cfg.get('user') or not smtp_cfg.get('password'):
        logger.error(f"Partner {partner['id']}: SMTP credentials not configured in partners.json")
        return False

    to_email   = partner.get('email', '')
    from_email = smtp_cfg['user']

    if not to_email:
        logger.error(f"Partner {partner['id']}: No email address configured")
        return False

    subject, html_body = format_batch_email(leads, partner.get('name', 'Partner'))

    if preview:
        logger.info(f"[PREVIEW] Would email {len(leads)} leads to {to_email}")
        logger.info(f"[PREVIEW] Subject: {subject}")
        return True

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = from_email
        msg['To']      = to_email
        msg.attach(MIMEText(html_body, 'html'))

        with smtplib.SMTP(smtp_cfg.get('host', 'smtp.gmail.com'), smtp_cfg.get('port', 587)) as server:
            server.starttls()
            server.login(from_email, smtp_cfg['password'])
            server.sendmail(from_email, to_email, msg.as_string())

        logger.info(f"✅ Emailed {len(leads)} leads to {to_email} ({partner['name']})")
        return True

    except Exception as e:
        logger.error(f"Email delivery failed for {partner['id']}: {e}")
        return False


def deliver_via_webhook(leads: list, partner: dict, preview: bool = False) -> bool:
    """POST a batch of leads to partner's webhook endpoint."""
    webhook_url = partner.get('webhook_url', '')
    if not webhook_url:
        logger.error(f"Partner {partner['id']}: No webhook_url configured")
        return False

    payload = format_webhook_payload(leads, partner)

    if preview:
        logger.info(f"[PREVIEW] Would POST {len(leads)} leads to {webhook_url}")
        return True

    headers = {'Content-Type': 'application/json'}
    secret = partner.get('webhook_secret', '')
    if secret:
        import hmac, hashlib
        body = json.dumps(payload).encode()
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers['X-Tomcat-Signature'] = sig

    try:
        r = requests.post(webhook_url, json=payload, headers=headers, timeout=15)
        r.raise_for_status()
        logger.info(f"✅ Webhook delivered {len(leads)} leads to {partner['name']} — HTTP {r.status_code}")
        return True
    except Exception as e:
        logger.error(f"Webhook delivery failed for {partner['id']}: {e}")
        return False


# ─── MAIN ROUTING LOOP ─────────────────────────────────────────────────────────

def route(preview: bool = False, partner_filter: str = None, limit: int = None):
    init_routing_table()
    partners = load_partners()

    if not partners:
        logger.warning("No active partners configured. Add partners to: " + PARTNERS_CFG)
        logger.warning("Set 'active': true and provide email or webhook_url + SMTP config.")
        print_lead_preview()
        return

    if partner_filter:
        partners = [p for p in partners if p['id'] == partner_filter]
        if not partners:
            logger.error(f"No active partner found with id='{partner_filter}'")
            return

    total_routed = 0

    for partner in partners:
        pid     = partner['id']
        filters = partner.get('filters', {})
        batch   = partner.get('batch_size', 25)
        method  = partner.get('delivery_method', 'email')

        logger.info(f"\n─── Routing to partner: {partner['name']} ({pid}) ───")

        leads = get_unrouted_leads(pid, filters, limit=limit)

        if not leads:
            logger.info(f"No new leads to route to {partner['name']}")
            continue

        logger.info(f"Found {len(leads)} unrouted leads for {partner['name']}")

        # Deliver in batches
        for i in range(0, len(leads), batch):
            batch_leads = leads[i:i+batch]

            if method == 'webhook':
                success = deliver_via_webhook(batch_leads, partner, preview)
            else:
                success = deliver_via_email(batch_leads, partner, preview)

            status = 'delivered' if success else 'failed'

            # Log each lead delivery
            if not preview:
                for lead in batch_leads:
                    log_delivery(lead['id'], pid, method, status, '')
                    if success:
                        mark_lead_routed(lead['id'])
                        total_routed += 1

            if not success:
                logger.error(f"Batch {i//batch + 1} failed for {partner['name']}. Stopping this partner.")
                break

            time.sleep(1)  # Brief pause between batches

    logger.info(f"\n=== Routing Complete: {total_routed} leads delivered across {len(partners)} partners ===")
    return total_routed


def print_lead_preview():
    """Show a sample of what is in the DB, useful when no partners are configured yet."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        total  = conn.execute("SELECT COUNT(*) FROM ucc_leads WHERE status='new'").fetchone()[0]
        sample = conn.execute(
            "SELECT * FROM ucc_leads WHERE status='new' ORDER BY days_to_lapse ASC LIMIT 10"
        ).fetchall()
    except:
        total, sample = 0, []
    conn.close()

    print(f"\n{'='*65}")
    print(f"  TOMCAT CAPEX LEAD DATABASE PREVIEW")
    print(f"  {total} confirmed equipment UCC leads ready to route")
    print(f"{'='*65}")

    for lead in sample:
        lead = dict(lead)
        lapse = f"{lead.get('days_to_lapse')}d" if lead.get('days_to_lapse') else '?'
        urgency = "🔴" if (lead.get('days_to_lapse') or 999) <= 30 else "🟡" if (lead.get('days_to_lapse') or 999) <= 90 else "🟢"
        print(f"\n  {urgency} {lead.get('company_name','')}")
        print(f"     Location : {lead.get('city','')}, {lead.get('state','')} {lead.get('zipcode','')}")
        print(f"     Equipment: {lead.get('collateral','')[:70]}")
        print(f"     Lender   : {lead.get('secured_party','')}")
        print(f"     Expires  : {lead.get('lapse_date','')} ({lapse} from today)")


# ─── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tomcat Capex Auto Router")
    parser.add_argument('--preview',  action='store_true', help='Dry run — show what would be sent without delivering')
    parser.add_argument('--partner',  type=str,  default=None, help='Route only to this partner ID')
    parser.add_argument('--limit',    type=int,  default=None, help='Max leads to route per partner')
    parser.add_argument('--preview-db', action='store_true', help='Show sample leads from database and exit')
    args = parser.parse_args()

    if args.preview_db:
        print_lead_preview()
    else:
        route(preview=args.preview, partner_filter=args.partner, limit=args.limit)
