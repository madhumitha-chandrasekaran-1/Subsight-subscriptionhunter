"""
FastAPI app + APScheduler.

Runs the full pipeline every Sunday at 8am:
  1. Scan Gmail for subscription emails
  2. Store / upsert results into SQLite
  3. Verify active subscriptions via Exa
  4. Send weekly email report
  5. Log the run (heartbeat)

Endpoints:
  GET  /health             — last scan status, subscription counts
  GET  /subscriptions      — all subscriptions (?status=active|cancelled|all)
  POST /trigger/scan       — run just the Gmail scan + DB upsert
  POST /trigger/verify     — run just the Exa verification pass
  POST /trigger/report     — send the report email right now
  POST /trigger/full       — run the complete weekly pipeline now
"""

import os
import traceback
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from auth import get_gmail_service
from database import (
    init_db,
    get_all_subscriptions,
    get_active_subscriptions,
    get_cancelled_subscriptions,
    get_last_scan,
    log_scan,
    upsert_subscription,
)
from reporter import send_crash_alert, send_report
from scanner import scan
from verifier import verify_all

load_dotenv()

REPORT_EMAIL = os.environ.get("REPORT_EMAIL", "")  # set this to your email


# ---------------------------------------------------------------------------
# Weekly pipeline
# ---------------------------------------------------------------------------

def run_weekly_job() -> None:
    print("=== Weekly job starting ===")
    gmail = get_gmail_service()

    if not REPORT_EMAIL:
        print("WARNING: REPORT_EMAIL not set — report will not be sent.")

    try:
        # 1. Scan Gmail
        results = scan()
        inserted = updated = 0
        for info in results:
            action = upsert_subscription(info)
            if action == "inserted":
                inserted += 1
            else:
                updated += 1
        print(f"Scan done: {inserted} new, {updated} updated")
        recent_services = {r.service_name for r in results}

        # 2. Verify via Exa
        verify_all(stale_after_days=7)

        # 3. Send report — only show what this scan actually found
        if REPORT_EMAIL:
            send_report(gmail, REPORT_EMAIL, recent_services=recent_services)

        # 4. Log success
        log_scan(
            emails_scanned=50,
            subs_found=len(results),
            status="ok",
        )
        print(" Weekly job complete ")

    except Exception as e:
        err = traceback.format_exc()
        print(f"Weekly job FAILED:\n{err}")
        log_scan(emails_scanned=0, subs_found=0, status="error", error_message=str(e))
        if REPORT_EMAIL:
            send_crash_alert(gmail, REPORT_EMAIL, err)
        raise

scheduler = BackgroundScheduler(timezone="America/New_York")

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(
        run_weekly_job,
        CronTrigger(day_of_week="sun", hour=8, minute=0),
        id="weekly_scan",
        replace_existing=True,
        misfire_grace_time=3600,  # if the server was down, run within 1 hour of Sunday 8am
    )
    scheduler.start()
    print("Scheduler started — weekly job fires every Sunday at 8am ET.")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="SubSight", lifespan=lifespan)

@app.get("/health")
def health():
    last = get_last_scan()
    active = get_active_subscriptions()
    cancelled = get_cancelled_subscriptions()
    next_run = scheduler.get_job("weekly_scan")
    return {
        "status": "ok",
        "last_scan": last,
        "active_count": len(active),
        "cancelled_count": len(cancelled),
        "next_scheduled_run": str(next_run.next_run_time) if next_run else None,
    }


@app.get("/subscriptions")
def subscriptions(status: str = Query(default="all", pattern="^(all|active|cancelled)$")):
    if status == "active":
        return get_active_subscriptions()
    if status == "cancelled":
        return get_cancelled_subscriptions()
    return get_all_subscriptions()


@app.post("/trigger/scan")
def trigger_scan():
    try:
        results = scan()
        counts = {"inserted": 0, "updated": 0}
        for info in results:
            action = upsert_subscription(info)
            key = "inserted" if action == "inserted" else "updated"
            counts[key] += 1
        log_scan(emails_scanned=50, subs_found=len(results))
        # Store recent service names in app state so /trigger/report can use them
        app.state.last_scan_services = {r.service_name for r in results}
        return {"ok": True, "found": len(results), **counts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/trigger/verify")
def trigger_verify():
    try:
        results = verify_all(stale_after_days=0)  # force re-verify everything
        return {"ok": True, "verified": len(results)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/trigger/report")
def trigger_report():
    if not REPORT_EMAIL:
        raise HTTPException(status_code=400, detail="REPORT_EMAIL env var not set")
    try:
        gmail = get_gmail_service()
        # Use recent scan services if available, otherwise show everything
        recent = getattr(app.state, "last_scan_services", None)
        send_report(gmail, REPORT_EMAIL, recent_services=recent)
        return {"ok": True, "sent_to": REPORT_EMAIL}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    

@app.post("/trigger/full")
def trigger_full():
    try:
        run_weekly_job()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
