# SubSight

A personal automation product that scans your Gmail for subscription emails, extracts billing data using Claude AI, verifies service status via Exa web search, and delivers a clean weekly report to your inbox every Sunday.

Built to answer one question: **what am I actually paying for, and is it still worth it?**

---

## What it does

- Scans Gmail for subscription, billing, receipt, and trial emails (last 30 days)
- Uses Claude to extract service name, price, billing cycle, and trial dates
- Detects price changes between billing cycles and alerts you once
- Flags free trials ending within 7 days before your card gets charged
- Detects re-subscriptions after cancellations correctly
- Verifies via Exa whether active services are still operating
- Sends a formatted weekly report every Sunday at 8am
- Sends a crash alert email if the job fails
- Runs autonomously via a macOS LaunchAgent (no terminal required)

---

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| Web / API | FastAPI + Uvicorn |
| Scheduler | APScheduler (cron, every Sunday) |
| Database | SQLite |
| Email read | Gmail API (OAuth 2.0) |
| Email send | Gmail API |
| AI extraction | Anthropic Claude (`claude-opus-4-8`) |
| Web verification | Exa search API |
| Config | python-dotenv |

---

## Project structure

```
subscription-hunter/
├── auth.py          # Gmail OAuth 2.0 — token persistence and refresh
├── scanner.py       # Gmail fetch + Claude extraction pipeline
├── database.py      # SQLite schema, upsert logic, read helpers
├── verifier.py      # Exa web search + Claude verification
├── reporter.py      # HTML email builder and Gmail sender
├── main.py          # FastAPI app + APScheduler weekly job
├── requirements.txt
└── .env             # API keys (not committed)
```

---

## Prerequisites

- Python 3.11+
- A Google Cloud project with the Gmail API enabled
- An Anthropic API key
- An Exa API key

---

## Setup

### 1. Clone and create a virtual environment

```bash
git clone https://github.com/your-username/subscription-hunter.git
cd subscription-hunter
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Gmail API credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → enable the Gmail API
3. OAuth consent screen → External → add your email as a test user
4. Credentials → Create OAuth 2.0 Client ID (Desktop app) → download JSON
5. Save it as `credentials.json` in the project root

### 3. Environment variables

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=sk-ant-...
EXA_API_KEY=your-exa-key
REPORT_EMAIL=your@email.com
```

### 4. Authenticate Gmail

Run this once — it opens a browser for the OAuth consent flow and saves a `token.json`:

```bash
python auth.py
```

---

## Running

```bash
python main.py
```

The server starts on `http://localhost:8000`. The weekly job is scheduled for every Sunday at 8am (America/New_York). To run the full pipeline immediately:

```bash
curl -X POST http://localhost:8000/trigger/full
```

---

## API endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Server status, last scan time, next scheduled run |
| `GET` | `/subscriptions` | All subscriptions (`?status=active\|cancelled\|all`) |
| `POST` | `/trigger/scan` | Run Gmail scan and store results |
| `POST` | `/trigger/verify` | Run Exa verification on all active subscriptions |
| `POST` | `/trigger/report` | Send the report email immediately |
| `POST` | `/trigger/full` | Run the complete pipeline end-to-end |

---

## Running automatically on macOS (LaunchAgent)

To have SubSight start at login and restart if it crashes:

```bash
# Copy the plist (update paths to match your machine)
cp com.subsight.agent.plist ~/Library/LaunchAgents/

# Load it
launchctl load ~/Library/LaunchAgents/com.subsight.agent.plist
```

Logs are written to `subsight.log` in the project root.

```bash
# Watch live logs
tail -f subsight.log

# Stop
launchctl unload ~/Library/LaunchAgents/com.subsight.agent.plist
```

---

## Edge cases handled

**Re-subscription after cancellation**
If you cancel a service and then resubscribe, SubSight uses the email date to determine the current state — the most recent email always wins, regardless of the order emails are processed.

**Price changes**
When a new billing email shows a different price than what is stored, SubSight records the change and includes it in the next weekly report. The alert fires once and is cleared after the report is sent.

**Free trial expiry**
Emails confirming a free trial are detected and stored with the trial end date. If a trial is ending within 7 days, you receive a warning in the report before your card is charged. The alert fires once and is not repeated.

---

## Report format

Every Sunday you receive an email with:

- **Alerts** — price changes, expiring trials, services that may have shut down
- **Active subscriptions** — service, price, billing cycle, next bill date
- **Estimated monthly spend** — calculated across all known active subscriptions
- **Cancelled subscriptions** — services you are no longer paying for

Only subscriptions seen in the current 30-day scan window appear in the report. The database retains full history for price change comparison across billing cycles.

---

## License

MIT
