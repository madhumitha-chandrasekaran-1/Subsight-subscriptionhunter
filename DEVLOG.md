# SubSight — Development Journal

A record of how this product was built, what broke, what was discovered, and how every problem was resolved.

---

## The Starting Point

**The idea:** Build an AI agent that scans Gmail for subscription emails, extracts billing data, verifies if services are still active, stores it in a database, and sends a weekly email report. No manual checking. No spreadsheets. Fully autonomous.

**Stack chosen:** Python, FastAPI, APScheduler, SQLite, Gmail API, Anthropic Claude, Exa search API.

**The plan (in order):**
1. Gmail OAuth (`auth.py`)
2. Email scanner + Claude extraction (`scanner.py`)
3. SQLite database (`database.py`)
4. Exa web verification (`verifier.py`)
5. Email reporter (`reporter.py`)
6. FastAPI app + scheduler (`main.py`)

---

## Phase 1 — Gmail Authentication

### What was built
`auth.py` handles Gmail OAuth 2.0. On first run it opens a browser for consent, saves the token to `token.json`, and auto-refreshes on future runs. No login required after the first time.

### Problem: Access blocked — app not verified
When the browser opened for OAuth consent, Google showed:
> "subscription-hunter has not completed the Google verification process"

**Why:** The Google Cloud app was in "Testing" mode, which only allows explicitly added test users.

**Fix:** Google Cloud Console → APIs & Services → OAuth consent screen → Test users → added the Gmail address manually.

### Problem: ModuleNotFoundError — `google` module not found
Running `python3 auth.py` threw:
```
ModuleNotFoundError: No module named 'google'
```

**Why:** The packages were installed inside the virtual environment (`venv`), but the command was using the system Python which had no access to them.

**Fix:** Activate the venv first:
```bash
source venv/bin/activate
python auth.py
```

### Output
```
Authenticated as: madhumitha.chandrasekaran@gmail.com
Total messages: 27441
```
OAuth working. Token saved. Never needs repeating.

---

## Phase 2 — Email Scanner

### What was built
`scanner.py` searches Gmail for emails matching subscription-related keywords in the subject (receipt, invoice, billing, renewal, etc.) from the last 30 days. For each email it:
1. Decodes the MIME body (base64, handles multipart, strips HTML)
2. Sends the email text to Claude with a structured output schema
3. Returns a `SubscriptionInfo` Pydantic model or `None` if not a real subscription

### The data model
```python
class SubscriptionInfo(BaseModel):
    service_name: str
    price: Optional[float]
    currency: Optional[str]
    billing_cycle: Optional[str]
    next_billing_date: Optional[str]
    is_trial: bool
    trial_end_date: Optional[str]
    email_subject: str
    email_date: str
```

### Problem: TypeError — authentication method not resolved
Running the scanner threw:
```
TypeError: Could not resolve authentication method. Expected one of api_key, auth_token...
```

**Why:** The `ANTHROPIC_API_KEY` environment variable was not set in the terminal session.

**Fix:**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```
Later replaced by a `.env` file with `python-dotenv` so this never needs to be typed again.

### Problem: API credits exhausted
After setting the key, the scanner failed because the account had no credits.

**Clarification learned:** Claude Pro (claude.ai subscription) and the Anthropic API are completely separate products with separate billing. Claude Pro gives access to the chat interface only — it does not provide API credits. Credits must be added at console.anthropic.com.

### First successful scan output
```
Found 20 candidate emails — fetching details…
Done. Found 10 subscriptions across 20 emails.
```
Results included: Generative AI Fundamentals, Google AI, Hulu, Claude Pro, iCloud+, Mint Mobile.

Some had null prices — expected, because cancellation emails don't include the billing amount.

---

## Phase 3 — Database

### What was built
`database.py` creates a SQLite database with two tables:
- `subscriptions` — one row per unique service, upserted on every scan
- `scan_log` — one row per run (used as heartbeat)

The upsert logic uses `COALESCE` so a null field from one email never overwrites a real value from another.

---

## Phase 4 — Edge Cases (the important part)

These were not planned upfront. They came up through real-world use and observation.

---

### Edge Case 1 — Re-subscription after cancellation

**Problem raised:** What if someone subscribes, cancels, then subscribes again? Would it show two subscriptions, or would the cancellation override the re-subscription?

**What the original code did:** Used `MAX(is_cancelled, new_value)` — meaning once a service was marked cancelled, it could never become active again, even if a newer subscription email came in.

**Why it was wrong:**
- Gmail doesn't return emails in strict chronological order
- A cancellation email processed after a re-subscription email would permanently mark it cancelled

**Fix:** Added `status_email_date` column to track which email's date last determined `is_cancelled`. On every upsert, compared the new email's date to the stored one:
```python
newer_email_wins = email_date >= stored_status_date
```
The newer email always sets the status, regardless of processing order. Sub → cancel → resub now correctly ends as active.

---

### Edge Case 2 — Price changes

**Problem raised:** Spotify raises prices. The agent sees a new charge amount and might think it's a new subscription. What should happen?

**Fix:** On upsert, compared `old_price` (from DB) vs `new_price` (from email) with a 0.001 epsilon to avoid floating point noise. If they differ:
- Updated the stored price to the new value
- Wrote a `price_change_note`: `"Spotify increased from $9.99 to $11.99"`
- Included the note in the weekly report as a one-time alert

**Additional fix needed later:** The note was persisting in the DB and showing in every weekly report indefinitely. Added `clear_price_change_note()` which is called after the report is sent — so the alert fires once and is cleared.

---

### Edge Case 3 — Free trial expiry

**Problem raised:** If a free trial is ending and the card is about to be charged, an alert should be sent before it happens — not after.

**Fix:** Extended `SubscriptionInfo` with:
- `is_trial: bool` — True if the email mentions a trial
- `trial_end_date: Optional[str]` — ISO date when the trial ends and the card is first charged

Updated the Claude prompt to look for phrases like "your trial ends on", "you'll be charged on", "free period ends".

Added to database:
- `trial_end_date` column
- `trial_alert_sent` flag — set to 1 after alerting, so the warning doesn't repeat every week

Added `get_expiring_trials(days_ahead=7)` — returns trials ending within 7 days where the alert hasn't been sent yet.

---

## Phase 5 — Real-world Problems Discovered After First Run

### Problem: "Premium" and "Premium subscription" appearing as service names
Claude extracted the word "Premium" from emails that said things like "your Premium subscription" — taking the plan tier as the service name instead of the actual company.

**Fix:** Added to the Claude prompt:
> "Generic words like 'Premium', 'Pro', 'Basic', or 'Plus' are plan tiers, not service names. If you cannot determine the actual company name, set service_name to NOT_SUBSCRIPTION."

Deleted the stale rows directly from the DB:
```sql
DELETE FROM subscriptions WHERE service_name IN ('Premium', 'Premium subscription');
```

### Problem: Exa `use_autoprompt` crash
The verifier was calling Exa with `use_autoprompt=True` which is not a valid parameter in the current exa-py version, causing the search to fail silently.

**Fix:** Removed the parameter. Also added `_is_unsearchable()` to skip searching for generic names like "Premium", "Pro", "Plus" that Exa can't meaningfully search.

### Problem: Coursera split into two entries
One email was parsed as "Coursera", another as "Generative AI Fundamentals" — two separate rows in the DB when they should be one.

**Fix:** Added to the Claude prompt:
> "For course platforms (Coursera, Udemy, LinkedIn Learning), always use the format 'Platform - Course Name'. Never use just the course name alone."

Deleted the split rows and rescanned.

### Problem: Visible ($25) not counted in monthly spend estimate
The monthly spend calculation only counted subscriptions with a known `billing_cycle`. Visible had a null billing cycle, so it was excluded.

**Fix:** Changed the logic — if price is known but billing cycle is unknown, treat as monthly:
```python
# annual → price / 12
# 3-month / quarterly → price / 3
# weekly → price × 4.33
# everything else (monthly or unknown) → price as-is
```

### Problem: Stale "next bill" dates in the report
Mint Mobile showed "2026-02-18" and iCloud+ showed "2026-03-23" even though it was June and neither had appeared in the last 30 days. These were old DB entries from emails scanned months earlier.

**Root cause:** The report was pulling all active subscriptions from the DB, not just what the current scan found.

**Fix:** The scanner now returns a set of service names found in the current run. This set is passed to the reporter, which filters the active subscriptions table to only those services:
```python
recent_services = {r.service_name for r in results}
send_report(gmail, REPORT_EMAIL, recent_services=recent_services)
```

**The principle:** DB stores full history for price change comparison. Report shows only what the current scan found.

### Problem: Price change alert showing every week
After detecting "Visible decreased from $25.00 to $5.00", the note was stored in the DB and included in every subsequent weekly report — even months later.

**Fix:** After the report email is sent successfully, `clear_price_change_note()` is called for every service whose price change was included. The alert fires once, then the note is cleared. If the price changes again in the future, a new note is written and the cycle repeats.

---

## Phase 6 — Reporter and Email Design

### What was built
`reporter.py` builds an HTML email and sends it via the Gmail API (reusing the same OAuth token, no SMTP needed).

### Design decisions
- No emojis — kept strictly professional
- Section headings: Alerts, Active Subscriptions, Cancelled
- Table layout for active subscriptions (Service / Price / Cycle / Next bill)
- Monthly spend estimate at the bottom
- Trial entries marked with `[trial]` in red
- Alerts use coloured left-border boxes (amber for warnings, red for critical)

### Project name
Renamed from "Subscription Graveyard Hunter" to **SubSight** — cleaner, professional, descriptive.

Email subject line: `SubSight — Weekly Subscription Report, June 13, 2026`

---

## Phase 7 — FastAPI + Scheduler

### What was built
`main.py` ties everything together:
- APScheduler fires `run_weekly_job()` every Sunday at 8am (America/New_York)
- `misfire_grace_time=3600` — if the server was off at 8am, it catches up within an hour of restart
- Crash alerting — if `run_weekly_job()` throws, a plain-text alert email is sent immediately

### Manual trigger endpoints (for testing)
| Endpoint | What it does |
|---|---|
| `POST /trigger/scan` | Gmail scan + DB upsert only |
| `POST /trigger/verify` | Exa verification only |
| `POST /trigger/report` | Send the email only |
| `POST /trigger/full` | Full pipeline end-to-end |
| `GET /health` | Status, last scan time, next run |

---

## Phase 8 — Credentials and Automation

### Problem: ANTHROPIC_API_KEY not found on server restart
Every time the server restarted, the environment variable was gone and the weekly job crashed.

**Fix:** Added `python-dotenv`. Created a `.env` file in the project root with all keys. `load_dotenv()` is called at startup — no manual exports ever needed.

**Additional fix:** `anthropic.Anthropic()` and `Exa()` were being initialised at module import time, before `load_dotenv()` had run. Switched to lazy initialisation — clients are created on first use, by which point the `.env` has been loaded.

### Automation on macOS
Created a LaunchAgent plist at `~/Library/LaunchAgents/com.subsight.agent.plist`:
- Starts SubSight automatically at login
- Restarts it automatically if it crashes
- Logs all output to `subsight.log`
- No terminal required after initial setup

```bash
launchctl load ~/Library/LaunchAgents/com.subsight.agent.plist
```

---

## Final State

**What runs automatically, every Sunday, without any interaction:**
1. Gmail is searched for subscription/billing emails from the last 30 days
2. Claude extracts service name, price, billing cycle, trial status from each email
3. Results are upserted into SQLite — deduplicating, detecting price changes, tracking trials
4. Exa searches the web to verify each active service is still operating
5. A formatted HTML email is sent with: alerts, active subscriptions, monthly spend, cancelled list
6. The scan is logged for heartbeat monitoring

**What triggers an immediate alert email:**
- The weekly job crashes for any reason
- (Trial expiry warnings appear in the Sunday report, not as separate emails)

**What the weekly email looks like:**
- Clean, professional, no emojis
- Only shows subscriptions found in the current 30-day scan window
- Price changes appear once, are cleared after reporting
- Trial expiry warnings appear once, flagged before the charge hits

---

## Lessons

- **Claude Pro ≠ Anthropic API.** Two separate products, separate billing.
- **Gmail doesn't return emails chronologically.** Any logic that assumes order will break.
- **Module-level initialisation is a trap.** If a module reads an env var at import time, it breaks when dotenv hasn't loaded yet. Lazy initialisation is safer.
- **The DB and the report have different jobs.** DB = full history. Report = current window only.
- **One-time alerts need a "sent" flag.** Without it, the same alert repeats forever.
- **Generic names from LLMs need explicit rules.** Without constraints, Claude extracts "Premium" instead of "Spotify Premium".


File	Role
auth.py	Gmail OAuth — login once, never again
scanner.py	Gmail → Claude → structured subscription data
database.py	SQLite with all 3 edge cases handled
verifier.py	Exa web search → is it still alive?
reporter.py	Builds + sends the weekly HTML email
main.py	FastAPI + scheduler + crash alerting
README.md	GitHub documentation
DEVLOG.md	Full development journal
Runs every Sunday. Alerts you to price changes, expiring trials, and dead services. Zero touch required. When you're ready to take it cloud — come back and we'll deploy it to Railway in one session.
