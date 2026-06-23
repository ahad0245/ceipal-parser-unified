import os
import smtplib
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Dict

from ceipal_inline_resume_worker import get_start_page, now_utc, run_inline_worker, sync_col


WORKER_STATE_NAME = "ceipal_inline_worker_vercel"
ALERT_TO_EMAIL = os.getenv("ALERT_TO_EMAIL", "abdulahad@i8is.com")
MAX_PAGES_PER_RUN = int(os.getenv("VERCEL_MAX_PAGES", os.getenv("SCHEDULE_MAX_PAGES", "3")))
MAX_RESTART_ATTEMPTS = int(os.getenv("VERCEL_MAX_RESTART_ATTEMPTS", "3"))
RESTART_DELAY_SECONDS = int(os.getenv("VERCEL_RESTART_DELAY_SECONDS", "15"))
ALERT_COOLDOWN_MINUTES = int(os.getenv("VERCEL_ALERT_COOLDOWN_MINUTES", "120"))


def utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.isoformat()


def get_state() -> Dict[str, Any]:
    return sync_col.find_one({"name": WORKER_STATE_NAME}) or {"name": WORKER_STATE_NAME}


def update_state(fields: Dict[str, Any]) -> None:
    sync_col.update_one(
        {"name": WORKER_STATE_NAME},
        {"$set": fields},
        upsert=True,
    )


def send_failure_email(subject: str, body: str) -> bool:
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER") or os.getenv("smtpuserName")
    smtp_password = os.getenv("SMTP_PASSWORD") or os.getenv("SMTP_PASS") or os.getenv("smtpPassword")
    smtp_from = os.getenv("SMTP_FROM_EMAIL") or os.getenv("MAIL_FROM") or smtp_user

    if not smtp_host or not smtp_from:
        print("Email alert skipped: SMTP_HOST/SMTP_FROM_EMAIL not configured.")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_from
    message["To"] = ALERT_TO_EMAIL
    message.set_content(body)

    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as smtp:
            if smtp_user and smtp_password:
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
            smtp.starttls()
            if smtp_user and smtp_password:
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)

    return True


def should_send_alert(state: Dict[str, Any]) -> bool:
    last_alert_at = state.get("last_alert_at")
    if not last_alert_at:
        return True

    if isinstance(last_alert_at, str):
        try:
            last_alert_at = datetime.fromisoformat(last_alert_at)
        except ValueError:
            return True

    if last_alert_at.tzinfo is None:
        last_alert_at = last_alert_at.replace(tzinfo=timezone.utc)

    return datetime.now(timezone.utc) - last_alert_at >= timedelta(minutes=ALERT_COOLDOWN_MINUTES)


def build_alert_body(error_text: str, attempts: int, start_page: int) -> str:
    state = get_state()
    lines = [
        "CEIPAL Vercel worker failed after all restart attempts.",
        "",
        f"Time (UTC): {utc_iso()}",
        f"Attempts: {attempts}",
        f"Start page: {start_page}",
        f"Max pages this run: {MAX_PAGES_PER_RUN}",
        f"Last success page: {state.get('last_success_page')}",
        f"Last completed at: {state.get('last_completed_at')}",
        "",
        "Error:",
        error_text[:12000],
    ]
    return "\n".join(lines)


def run_once_with_restarts() -> Dict[str, Any]:
    state = get_state()
    start_page = int(state.get("next_start_page") or get_start_page())
    attempt = 0
    last_error = None

    update_state(
        {
            "last_started_at": now_utc(),
            "last_status": "running",
            "last_attempt_count": 0,
            "configured_max_pages": MAX_PAGES_PER_RUN,
            "configured_restart_attempts": MAX_RESTART_ATTEMPTS,
        }
    )

    while attempt < MAX_RESTART_ATTEMPTS:
        attempt += 1
        try:
            print(f"[vercel-worker] attempt {attempt}/{MAX_RESTART_ATTEMPTS}, start_page={start_page}")
            run_inline_worker(start_page=start_page, max_pages=MAX_PAGES_PER_RUN)
            new_state = sync_col.find_one({"name": "ceipal_inline_worker"}) or {}
            last_success_page = new_state.get("last_success_page", start_page)
            result = {
                "ok": True,
                "attempts": attempt,
                "start_page": start_page,
                "last_success_page": last_success_page,
                "pages_done": new_state.get("last_pages_done"),
                "last_completed_at": utc_iso(),
            }
            update_state(
                {
                    "last_status": "completed",
                    "last_completed_at": now_utc(),
                    "last_success_page": last_success_page,
                    "next_start_page": int(last_success_page) + 1,
                    "last_attempt_count": attempt,
                    "last_error": None,
                    "last_alert_error": None,
                }
            )
            return result
        except Exception as exc:
            last_error = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            print(f"[vercel-worker] attempt {attempt} failed\n{last_error}")
            update_state(
                {
                    "last_status": "retrying" if attempt < MAX_RESTART_ATTEMPTS else "failed",
                    "last_failed_at": now_utc(),
                    "last_error": last_error[:12000],
                    "last_attempt_count": attempt,
                    "last_failed_start_page": start_page,
                }
            )
            if attempt < MAX_RESTART_ATTEMPTS:
                time.sleep(RESTART_DELAY_SECONDS)

    alert_sent = False
    final_state = get_state()
    if last_error and should_send_alert(final_state):
        alert_sent = send_failure_email(
            subject="CEIPAL Vercel worker failed",
            body=build_alert_body(last_error, MAX_RESTART_ATTEMPTS, start_page),
        )
        if alert_sent:
            update_state(
                {
                    "last_alert_at": now_utc(),
                    "last_alert_error": last_error[:12000],
                }
            )

    return {
        "ok": False,
        "attempts": MAX_RESTART_ATTEMPTS,
        "start_page": start_page,
        "alert_sent": alert_sent,
        "error": (last_error or "Unknown worker failure")[:4000],
    }
