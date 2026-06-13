"""
Builds and sends the weekly subscription report email via Gmail API.
"""

import base64
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from database import (
    get_active_subscriptions,
    get_cancelled_subscriptions,
    get_all_subscriptions,
    get_expiring_trials,
    mark_trial_alert_sent,
    clear_price_change_note,
)


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def _row(cells: list[str], tag: str = "td") -> str:
    inner = "".join(f"<{tag}>{c}</{tag}>" for c in cells)
    return f"<tr>{inner}</tr>"


def build_html_report(recent_services: set[str] | None = None) -> tuple[str, list[str], list[str]]:
    """
    Build the HTML email body.

    recent_services: service names found in the current scan. When provided,
    the active subscriptions table is filtered to only those — so stale DB
    entries from old emails don't bleed into the report.
    If None (e.g. manual trigger), all active subscriptions are shown.
    """
    today = date.today().strftime("%B %d, %Y")
    all_active = get_active_subscriptions()

    # Filter to only what appeared in the current scan
    if recent_services is not None:
        active = [s for s in all_active if s["service_name"] in recent_services]
    else:
        active = all_active

    cancelled = get_cancelled_subscriptions()
    expiring = get_expiring_trials(days_ahead=7)
    price_alerts = [s for s in get_all_subscriptions() if s["price_change_note"]]
    gone = [s for s in active if s.get("verified_active") == 0]

    # Monthly spend estimate — unknown billing cycle assumed monthly
    monthly_spend = 0.0
    for s in active:
        if not s["price"]:
            continue
        cycle = (s["billing_cycle"] or "").lower()
        if "annual" in cycle or "year" in cycle:
            monthly_spend += s["price"] / 12
        elif "3-month" in cycle or "quarter" in cycle:
            monthly_spend += s["price"] / 3
        elif "week" in cycle:
            monthly_spend += s["price"] * 4.33
        else:
            # monthly or unspecified — count at face value
            monthly_spend += s["price"]

    # ---- Alerts section ------------------------------------------------
    alerts_html = ""

    for t in expiring:
        alerts_html += f"""
        <div class="alert">
            <strong>Trial ending {t['trial_end_date']}:</strong> {t['service_name']} —
            your card will be charged when the trial period closes.
        </div>"""

    for p in price_alerts:
        alerts_html += f"""
        <div class="alert">
            <strong>Price change:</strong> {p['price_change_note']}
        </div>"""

    for g in gone:
        note = f" {g['verification_note']}" if g.get("verification_note") else ""
        alerts_html += f"""
        <div class="danger">
            <strong>Possibly inactive:</strong> {g['service_name']} may no longer be operating.{note}
            Verify whether you are still being charged.
        </div>"""

    alerts_block = f"<h2>Alerts</h2>{alerts_html}" if alerts_html else ""

    # ---- Active subscriptions table ------------------------------------
    active_rows = ""
    for s in active:
        price_str = f"${s['price']:.2f}" if s["price"] else "—"
        cycle_str = s["billing_cycle"] or "—"
        next_bill = s["next_billing_date"] or "—"
        trial_badge = " <span class='trial'>[trial]</span>" if s["is_trial"] else ""
        active_rows += _row([
            f"{s['service_name']}{trial_badge}",
            price_str,
            cycle_str,
            next_bill,
        ])

    active_block = f"""
    <h2>Active Subscriptions ({len(active)})</h2>
    <table>
      <thead>{_row(['Service', 'Price', 'Cycle', 'Next bill'], 'th')}</thead>
      <tbody>{active_rows}</tbody>
    </table>
    <p class="total">Estimated monthly spend: <strong>${monthly_spend:.2f}</strong></p>
    """ if active else "<h2>Active Subscriptions</h2><p>None found.</p>"

    # ---- Cancelled section --------------------------------------------
    cancelled_items = "".join(f"<li>{s['service_name']}</li>" for s in cancelled)
    cancelled_block = f"""
    <h2>Cancelled ({len(cancelled)})</h2>
    <ul>{cancelled_items}</ul>
    """ if cancelled else ""

    # ---- Assemble ------------------------------------------------------
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: Arial, sans-serif; max-width: 620px; margin: 0 auto; color: #333; padding: 16px; }}
  h1 {{ color: #1a1a2e; border-bottom: 2px solid #eee; padding-bottom: 8px; }}
  h2 {{ color: #2c3e50; margin-top: 24px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 12px 0; }}
  th {{ background: #f4f4f4; text-align: left; padding: 8px 10px; font-size: 0.85em; color: #555; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #eee; font-size: 0.95em; }}
  .alert {{ background: #fff8e1; border-left: 4px solid #f39c12; padding: 10px 14px; margin: 8px 0; border-radius: 2px; }}
  .danger {{ background: #fdecea; border-left: 4px solid #e74c3c; padding: 10px 14px; margin: 8px 0; border-radius: 2px; }}
  .total {{ font-size: 1.05em; margin: 12px 0; }}
  .trial {{ color: #c0392b; font-size: 0.85em; font-weight: normal; }}
  .footer {{ margin-top: 32px; font-size: 0.8em; color: #999; border-top: 1px solid #eee; padding-top: 12px; }}
  ul {{ padding-left: 20px; }}
  li {{ margin: 4px 0; }}
</style>
</head>
<body>
  <h1>SubSight — Weekly Subscription Report</h1>
  <p style="color:#777">{today}</p>

  {alerts_block}
  {active_block}
  {cancelled_block}

  <div class="footer">
    SubSight monitors your Gmail for subscriptions, price changes, and trial expirations.
  </div>
</body>
</html>"""

    trial_names = [t["service_name"] for t in expiring]
    price_change_names = [p["service_name"] for p in price_alerts]
    return html, trial_names, price_change_names


# ---------------------------------------------------------------------------
# Send via Gmail API
# ---------------------------------------------------------------------------

def send_report(gmail_service, to_email: str, recent_services: set[str] | None = None) -> None:
    html, trial_names, price_change_names = build_html_report(recent_services)
    today = date.today().strftime("%B %d, %Y")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"SubSight — Weekly Subscription Report, {today}"
    msg["From"] = "me"
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Report sent to {to_email}")

    # Clear one-time alerts only after the email actually goes out
    for name in trial_names:
        mark_trial_alert_sent(name)
    for name in price_change_names:
        clear_price_change_note(name)


def send_crash_alert(gmail_service, to_email: str, error: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "SubSight — Weekly scan failed"
    msg["From"] = "me"
    msg["To"] = to_email
    body = f"The weekly scan job crashed and did not complete.\n\nError:\n{error}"
    msg.attach(MIMEText(body, "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()
        print(f"Crash alert sent to {to_email}")
    except Exception as e:
        print(f"Failed to send crash alert: {e}")
