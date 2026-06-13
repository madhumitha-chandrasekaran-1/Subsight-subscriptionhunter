"""
Exa-powered subscription verifier.

For each active subscription, searches the web for current pricing/status,
then asks Claude to decide:
  - Is the service still operating?
  - Does the price we have match what's live today?
"""

import os
from typing import Optional

import anthropic
from exa_py import Exa
from pydantic import BaseModel

from database import (
    get_unverified_subscriptions,
    update_verification,
)

_exa = None
_client = None

def _get_exa():
    global _exa
    if _exa is None:
        _exa = Exa(api_key=os.environ["EXA_API_KEY"])
    return _exa

def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


# ---------------------------------------------------------------------------
# Data model for Claude's structured response
# ---------------------------------------------------------------------------

class VerificationResult(BaseModel):
    is_active: bool                     # False if service is shut down / acquired / gone
    current_price: Optional[float] = None  # Price found on the web right now, if any
    confidence: str                     # "high" | "medium" | "low"
    note: Optional[str] = None          # e.g. "Shut down March 2024" or "Price raised to $12.99"


# ---------------------------------------------------------------------------
# Core verification
# ---------------------------------------------------------------------------

def _search_exa(service_name: str) -> str:
    """Return a text block summarising Exa search results for this service."""
    try:
        results = _get_exa().search_and_contents(
            f"{service_name} subscription pricing plan",
            num_results=3,
            text={"max_characters": 1500},
        )
        chunks = []
        for r in results.results:
            chunks.append(f"Source: {r.url}\n{(r.text or '').strip()}")
        return "\n\n---\n\n".join(chunks) if chunks else "No results found."
    except Exception as e:
        return f"Exa search failed: {e}"


_UNSEARCHABLE = {"premium", "pro", "plus", "basic", "standard", "subscription", "premium subscription"}

def _is_unsearchable(service_name: str) -> bool:
    """Generic tier names that Exa can't meaningfully search."""
    return service_name.lower().strip() in _UNSEARCHABLE


def verify_subscription(
    service_name: str,
    stored_price: Optional[float],
    stored_currency: Optional[str],
) -> Optional[VerificationResult]:
    """Returns None if the service name is too generic to search."""
    if _is_unsearchable(service_name):
        return None
    """Search the web and ask Claude whether this subscription is still active."""
    web_context = _search_exa(service_name)

    price_context = (
        f"We have stored price: {stored_currency or 'USD'} {stored_price:.2f}/month"
        if stored_price is not None
        else "We have no stored price for this service."
    )

    prompt = f"""You are verifying whether a subscription service is still active.

Service name: {service_name}
{price_context}

Web search results about this service:
{web_context}

Based on the search results, answer:
1. is_active: Is this service still operating and accepting subscribers? Set to false only if
   there is clear evidence it has shut down, been discontinued, or been fully acquired/merged
   into something else. If uncertain, default to true.
2. current_price: If the search results clearly show the current subscription price (in the
   same currency as stored), extract the numeric amount. Otherwise leave null.
3. confidence: "high" if the search results directly address the service's current status,
   "medium" if somewhat relevant, "low" if results are unrelated or ambiguous.
4. note: One short sentence worth flagging — e.g. "Service rebranded to X in 2024" or
   "Price raised to $12.99/month". Leave null if nothing notable.
"""

    response = _get_client().messages.parse(
        model="claude-opus-4-8",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
        output_format=VerificationResult,
    )
    return response.parsed_output


# ---------------------------------------------------------------------------
# Batch verification
# ---------------------------------------------------------------------------

def verify_all(stale_after_days: int = 7) -> list[dict]:
    """
    Verify all active subscriptions not checked in the last `stale_after_days` days.
    Updates the DB and returns a list of result dicts for reporting.
    """
    subs = get_unverified_subscriptions(stale_after_days)

    if not subs:
        print("All subscriptions verified recently — nothing to check.")
        return []

    print(f"Verifying {len(subs)} subscription(s) via Exa…\n")
    report = []

    for sub in subs:
        name = sub["service_name"]
        if _is_unsearchable(name):
            print(f"  Skipping: {name}  (generic name — can't verify)")
            continue

        print(f"  Checking: {name}")

        result = verify_subscription(name, sub.get("price"), sub.get("currency"))

        update_verification(
            service_name=name,
            is_active=result.is_active,
            note=result.note,
        )

        # If Exa found a different price, update it in the DB (reuse price-change logic)
        if (
            result.current_price is not None
            and sub.get("price") is not None
            and abs(result.current_price - sub["price"]) > 0.001
        ):
            from database import get_conn, datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            currency = sub.get("currency") or "USD"
            sym = "$" if currency == "USD" else f"{currency} "
            direction = "increased" if result.current_price > sub["price"] else "decreased"
            note = f"{name} {direction} from {sym}{sub['price']:.2f} to {sym}{result.current_price:.2f} (web-verified)"
            with get_conn() as conn:
                conn.execute(
                    """
                    UPDATE subscriptions
                    SET price = ?, price_change_note = COALESCE(price_change_note, ?)
                    WHERE service_name = ?
                    """,
                    (result.current_price, note, name),
                )

        status = "ACTIVE" if result.is_active else "POSSIBLY GONE"
        conf = result.confidence.upper()
        note_str = f" — {result.note}" if result.note else ""
        print(f"    [{conf}] {status}{note_str}")

        report.append({
            "service_name": name,
            "is_active": result.is_active,
            "confidence": result.confidence,
            "note": result.note,
            "current_price": result.current_price,
        })

    return report


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from database import init_db
    init_db()

    results = verify_all()

    gone = [r for r in results if not r["is_active"]]
    if gone:
        print(f"\n=== Services that may be gone ({len(gone)}) ===")
        for r in gone:
            print(f"  {r['service_name']:<30} [{r['confidence']}]{' — ' + r['note'] if r['note'] else ''}")
    else:
        print("\nAll checked services appear to still be active.")
