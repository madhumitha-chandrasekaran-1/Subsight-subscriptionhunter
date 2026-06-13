"""
Gmail subscription scanner.

Pulls subscription-related emails from Gmail, strips them to plain text,
and asks Claude to extract structured subscription data.
"""

import base64
import re
from datetime import datetime, timezone
from typing import Optional

import anthropic
from pydantic import BaseModel

from auth import get_gmail_service

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class SubscriptionInfo(BaseModel):
    service_name: str
    price: Optional[float] = None          # numeric amount, e.g. 9.99
    currency: Optional[str] = None         # ISO code, e.g. "USD"
    billing_cycle: Optional[str] = None    # "monthly" | "annual" | "weekly" | "one-time"
    next_billing_date: Optional[str] = None  # ISO date YYYY-MM-DD, or None
    is_trial: bool = False                 # True if this is a free trial
    trial_end_date: Optional[str] = None   # ISO date when trial ends and card gets charged
    email_subject: str
    email_date: str                         # ISO date of the email itself


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

SEARCH_QUERY = (
    "("
    "subject:subscription OR subject:receipt OR subject:invoice OR "
    "subject:billing OR subject:\"your plan\" OR subject:\"payment confirmation\" OR "
    "subject:renewal OR subject:charged OR "
    "subject:\"your bill\" OR subject:\"bill is ready\" OR subject:\"bill due\" OR "
    "subject:statement OR subject:\"payment received\" OR subject:\"payment processed\" OR "
    "subject:\"monthly plan\" OR subject:\"account summary\" OR subject:\"amount due\""
    ") newer_than:30d"
)

MAX_EMAILS = 50  # cap per run to stay within API budgets


def _decode_part(part: dict) -> str:
    """Base64-decode a single MIME part body."""
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    """Very lightweight HTML → plain text."""
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _extract_body(payload: dict) -> str:
    """Walk the MIME tree and return the best plain-text body."""
    mime = payload.get("mimeType", "")

    # Single-part text
    if mime == "text/plain":
        return _decode_part(payload)
    if mime == "text/html":
        return _strip_html(_decode_part(payload))

    # Multipart: prefer text/plain, fall back to text/html
    parts = payload.get("parts", [])
    plain = next((p for p in parts if p.get("mimeType") == "text/plain"), None)
    if plain:
        return _decode_part(plain)
    html = next((p for p in parts if p.get("mimeType") == "text/html"), None)
    if html:
        return _strip_html(_decode_part(html))

    # Recurse into nested multipart
    for part in parts:
        text = _extract_body(part)
        if text:
            return text

    return ""


def _header(headers: list[dict], name: str) -> str:
    """Pull a specific header value from the Gmail headers list."""
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def fetch_subscription_emails(service) -> list[dict]:
    """Return a list of {subject, date, body} dicts from Gmail."""
    results = (
        service.users()
        .messages()
        .list(userId="me", q=SEARCH_QUERY, maxResults=MAX_EMAILS)
        .execute()
    )

    messages = results.get("messages", [])
    if not messages:
        print("No subscription emails found.")
        return []

    print(f"Found {len(messages)} candidate emails — fetching details…")
    emails = []

    for msg_stub in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_stub["id"], format="full")
            .execute()
        )
        headers = msg.get("payload", {}).get("headers", [])
        subject = _header(headers, "Subject") or "(no subject)"
        date_raw = _header(headers, "Date") or ""
        body = _extract_body(msg.get("payload", {}))

        # Truncate very long bodies so we don't blow the context window
        body = body[:3000]

        emails.append({"subject": subject, "date": date_raw, "body": body})

    return emails


# ---------------------------------------------------------------------------
# Claude extraction
# ---------------------------------------------------------------------------

_client = None

def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def extract_subscription(email: dict) -> Optional[SubscriptionInfo]:
    """
    Ask Claude to extract subscription details from a single email.
    Returns None if the email isn't subscription-related.
    """
    prompt = f"""You are analyzing an email to extract subscription billing information.

Email subject: {email['subject']}
Email date: {email['date']}
Email body:
{email['body']}

Extract the subscription details. If this email is NOT about a paid subscription,
free trial, or recurring billing (e.g. it's a newsletter, promotional spam, or unrelated),
set service_name to "NOT_SUBSCRIPTION" and leave all other fields null.

Rules:
- For email_date, convert the date to ISO format YYYY-MM-DD if possible.
- For next_billing_date, only include it if explicitly mentioned in the email.
- Set is_trial to true if the email mentions a free trial, trial period, or trial membership.
- Set trial_end_date to the ISO date (YYYY-MM-DD) when the trial ends and the card will
  first be charged. Look for phrases like "your trial ends on", "you'll be charged on",
  "your free period ends", "convert to paid on". If the date is not explicitly stated, leave it null.
- A trial confirmation email (e.g. "Your 30-day free trial has started") counts as a
  subscription even though no money has changed hands yet — extract it with is_trial=true.
- service_name MUST be the real company or product brand (e.g. "Spotify", "iCloud+", "Hulu").
  Generic words like "Premium", "Premium subscription", "Pro", "Basic", or "Plus" are plan
  tiers, not service names. If the email says "your Premium subscription", look at the sender,
  logo, or body to identify the actual company. If you truly cannot determine the company name,
  set service_name to "NOT_SUBSCRIPTION" — do not use the tier name alone.
- For course or learning platforms (Coursera, Udemy, LinkedIn Learning, Skillshare, etc.),
  always use the format "Platform - Course Name" (e.g. "Coursera - Generative AI Fundamentals").
  Never use just the course name alone as the service_name.
- Be consistent: if two emails are clearly about the same service, they must produce the
  exact same service_name so they deduplicate correctly in the database.
"""

    response = _get_client().messages.parse(
        model="claude-haiku-4-5",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
        output_format=SubscriptionInfo,
    )

    info: SubscriptionInfo = response.parsed_output

    if info.service_name == "NOT_SUBSCRIPTION":
        return None

    return info


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def scan() -> list[SubscriptionInfo]:
    """Full scan: fetch emails → extract → return results."""
    service = get_gmail_service()
    emails = fetch_subscription_emails(service)

    results: list[SubscriptionInfo] = []
    for i, email in enumerate(emails, 1):
        print(f"  [{i}/{len(emails)}] Processing: {email['subject'][:60]}")
        info = extract_subscription(email)
        if info:
            results.append(info)
            print(f"    ✓ {info.service_name} | {info.price} {info.currency} / {info.billing_cycle}")
        else:
            print(f"    — skipped (not a subscription)")

    print(f"\nDone. Found {len(results)} subscriptions across {len(emails)} emails.")
    return results


if __name__ == "__main__":
    subscriptions = scan()
    print("\n=== Subscriptions Found ===")
    for s in subscriptions:
        print(s.model_dump_json(indent=2))
