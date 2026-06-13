"""
SQLite storage for subscription data.

Tables:
  subscriptions  — one row per unique service (upserted on each scan)
  scan_log       — one row per scan run for heartbeat tracking
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from scanner import SubscriptionInfo

DB_PATH = Path(__file__).parent / "subscriptions.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name      TEXT NOT NULL,
    price             REAL,
    currency          TEXT,
    billing_cycle     TEXT,
    next_billing_date TEXT,
    is_cancelled      INTEGER NOT NULL DEFAULT 0,  -- 0 = active, 1 = cancelled
    status_email_date TEXT,   -- email date that last set is_cancelled; newer email wins
    price_change_note TEXT,   -- set when price differs from stored value, e.g. "increased from $9.99 to $11.99"
    is_trial          INTEGER NOT NULL DEFAULT 0,
    trial_end_date    TEXT,   -- ISO date when trial converts to paid and card gets charged
    trial_alert_sent  INTEGER NOT NULL DEFAULT 0,  -- 1 once we've warned the user
    verified_at       TEXT,   -- timestamp of last Exa verification
    verified_active   INTEGER,  -- 1=confirmed active, 0=possibly shut down, NULL=unchecked
    verification_note TEXT,   -- human-readable note from Claude e.g. "Service shut down March 2024"
    last_seen_subject TEXT,
    last_seen_date    TEXT,
    first_found_at    TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(service_name)
);

CREATE TABLE IF NOT EXISTS scan_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at          TEXT NOT NULL,
    emails_scanned  INTEGER NOT NULL DEFAULT 0,
    subs_found      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'ok',  -- 'ok' | 'error'
    error_message   TEXT
);
"""

CANCELLED_KEYWORDS = (
    "cancel", "cancelled", "canceled", "cancellation", "ended", "expired"
)


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
    print(f"Database ready at {DB_PATH}")


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def _is_cancellation(info: SubscriptionInfo) -> bool:
    subject = (info.email_subject or "").lower()
    return any(kw in subject for kw in CANCELLED_KEYWORDS)


def upsert_subscription(info: SubscriptionInfo) -> str:
    """
    Insert or update a subscription row.
    - Non-null fields fill in any existing nulls.
    - is_cancelled is decided by whichever email has the most recent date.
      A resubscription email dated after a cancellation email wins, and vice versa.
    Returns 'inserted' | 'updated'.
    """
    now = datetime.now(timezone.utc).isoformat()
    cancelled = int(_is_cancellation(info))
    email_date = info.email_date or ""

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, price, currency, status_email_date FROM subscriptions WHERE service_name = ?",
            (info.service_name,)
        ).fetchone()

        if existing is None:
            conn.execute(
                """
                INSERT INTO subscriptions
                    (service_name, price, currency, billing_cycle,
                     next_billing_date, is_cancelled, status_email_date,
                     price_change_note, is_trial, trial_end_date, trial_alert_sent,
                     last_seen_subject, last_seen_date,
                     first_found_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    info.service_name,
                    info.price,
                    info.currency,
                    info.billing_cycle,
                    info.next_billing_date,
                    cancelled,
                    email_date,
                    int(info.is_trial),
                    info.trial_end_date,
                    info.email_subject,
                    info.email_date,
                    now,
                    now,
                ),
            )
            return "inserted"
        else:
            stored_status_date = existing["status_email_date"] or ""
            # Only the newer email gets to decide is_cancelled.
            # ISO dates (YYYY-MM-DD) compare correctly as strings.
            newer_email_wins = email_date >= stored_status_date

            # Detect price change — use a small epsilon to avoid float noise.
            price_change_note = None
            old_price = existing["price"]
            new_price = info.price
            if old_price is not None and new_price is not None and abs(new_price - old_price) > 0.001:
                currency = info.currency or existing["currency"] or ""
                sym = "$" if currency == "USD" else f"{currency} "
                direction = "increased" if new_price > old_price else "decreased"
                price_change_note = (
                    f"{info.service_name} {direction} from {sym}{old_price:.2f} to {sym}{new_price:.2f}"
                )

            conn.execute(
                """
                UPDATE subscriptions SET
                    price             = COALESCE(?, price),
                    currency          = COALESCE(?, currency),
                    billing_cycle     = COALESCE(?, billing_cycle),
                    next_billing_date = COALESCE(?, next_billing_date),
                    is_cancelled      = CASE WHEN ? THEN ? ELSE is_cancelled END,
                    status_email_date = CASE WHEN ? THEN ? ELSE status_email_date END,
                    price_change_note = COALESCE(?, price_change_note),
                    last_seen_subject = ?,
                    last_seen_date    = ?,
                    updated_at        = ?
                WHERE service_name = ?
                """,
                (
                    new_price,
                    info.currency,
                    info.billing_cycle,
                    info.next_billing_date,
                    newer_email_wins, cancelled,
                    newer_email_wins, email_date,
                    price_change_note,
                    info.email_subject,
                    info.email_date,
                    now,
                    info.service_name,
                ),
            )

            # Fill in trial info if we now know more than we did before
            if info.is_trial or info.trial_end_date:
                conn.execute(
                    """
                    UPDATE subscriptions SET
                        is_trial       = MAX(is_trial, ?),
                        trial_end_date = COALESCE(trial_end_date, ?)
                    WHERE service_name = ?
                    """,
                    (int(info.is_trial), info.trial_end_date, info.service_name),
                )

            if price_change_note:
                return f"updated (price: ${old_price:.2f} → ${new_price:.2f})"
            return "updated"


def log_scan(emails_scanned: int, subs_found: int,
             status: str = "ok", error_message: str = None):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO scan_log (ran_at, emails_scanned, subs_found, status, error_message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (now, emails_scanned, subs_found, status, error_message),
        )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def get_all_subscriptions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions ORDER BY service_name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_active_subscriptions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE is_cancelled = 0 ORDER BY service_name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_cancelled_subscriptions() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE is_cancelled = 1 ORDER BY service_name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_unverified_subscriptions(stale_after_days: int = 7) -> list[dict]:
    """Return active subscriptions not verified within the last `stale_after_days` days."""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=stale_after_days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM subscriptions
            WHERE is_cancelled = 0
              AND (verified_at IS NULL OR verified_at < ?)
            ORDER BY service_name
            """,
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_verification(service_name: str, is_active: bool, note: str | None):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE subscriptions
            SET verified_at = ?, verified_active = ?, verification_note = ?
            WHERE service_name = ?
            """,
            (now, int(is_active), note, service_name),
        )


def get_expiring_trials(days_ahead: int = 3) -> list[dict]:
    """
    Return trials whose end date is within `days_ahead` days from today
    and for which we haven't already sent an alert.
    """
    from datetime import date, timedelta
    today = date.today().isoformat()
    cutoff = (date.today() + timedelta(days=days_ahead)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM subscriptions
            WHERE is_trial = 1
              AND trial_end_date IS NOT NULL
              AND trial_end_date BETWEEN ? AND ?
              AND trial_alert_sent = 0
            ORDER BY trial_end_date
            """,
            (today, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_trial_alert_sent(service_name: str):
    """Call this after alerting the user so we don't repeat the warning."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET trial_alert_sent = 1 WHERE service_name = ?",
            (service_name,),
        )


def clear_price_change_note(service_name: str):
    """Clear the price change note after it has been included in a report."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET price_change_note = NULL WHERE service_name = ?",
            (service_name,),
        )


def get_last_scan() -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM scan_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Entry point — run a full scan and save results
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from scanner import scan

    init_db()

    print("\nScanning Gmail…")
    try:
        results = scan()
        for info in results:
            action = upsert_subscription(info)
            print(f"  {action:8s} → {info.service_name}")
        log_scan(emails_scanned=20, subs_found=len(results))
    except Exception as e:
        log_scan(emails_scanned=0, subs_found=0, status="error", error_message=str(e))
        raise

    print("\n=== Active subscriptions in DB ===")
    for s in get_active_subscriptions():
        price_str = f"${s['price']:.2f}" if s["price"] else "price unknown"
        flag = f"  ⚠ {s['price_change_note']}" if s["price_change_note"] else ""
        print(f"  {s['service_name']:<30} {price_str} / {s['billing_cycle'] or '?'}{flag}")

    print("\n=== Cancelled subscriptions ===")
    for s in get_cancelled_subscriptions():
        print(f"  {s['service_name']}")

    price_alerts = [s for s in get_all_subscriptions() if s["price_change_note"]]
    if price_alerts:
        print("\n=== Price change alerts ===")
        for s in price_alerts:
            print(f"  {s['price_change_note']}")

    expiring = get_expiring_trials(days_ahead=7)
    if expiring:
        print("\n=== Trials ending soon ===")
        for s in expiring:
            print(f"  {s['service_name']:<30} trial ends {s['trial_end_date']} — card will be charged!")
