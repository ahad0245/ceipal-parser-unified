import argparse
import os
import time
from datetime import datetime, timezone

import schedule


EMBEDDED_ENV = {
    "MONGODB_URI": os.getenv("MONGODB_URI", ""),
    "DB_NAME": os.getenv("DB_NAME", "recruitment_db"),
    "COLLECTION_NAME": os.getenv("COLLECTION_NAME", "ceipal_applicant_details"),
    "PARSED_COLLECTION": os.getenv("PARSED_COLLECTION", "parsed_resumes"),
    "SYNC_COLLECTION": os.getenv("SYNC_COLLECTION", "sync_state"),
    "CEIPAL_BASE_URL": os.getenv("CEIPAL_BASE_URL", "https://api.ceipal.com"),
    "CEIPAL_REFRESH_ENDPOINT": os.getenv("CEIPAL_REFRESH_ENDPOINT", "/v2/refreshToken/"),
    "CEIPAL_EMAIL": os.getenv("CEIPAL_EMAIL", ""),
    "CEIPAL_PASSWORD": os.getenv("CEIPAL_PASSWORD", ""),
    "CEIPAL_API_KEY": os.getenv("CEIPAL_API_KEY", ""),
    "CEIPAL_CUSTOM_APPLICANT_ENDPOINT": os.getenv("CEIPAL_CUSTOM_APPLICANT_ENDPOINT", ""),
    "GOOGLE_SERVICE_ACCOUNT_FILE": os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json"),
    "GOOGLE_DRIVE_FOLDER_ID": os.getenv("GOOGLE_DRIVE_FOLDER_ID", ""),
    "DRIVE_ANYONE_PERMISSION": os.getenv("DRIVE_ANYONE_PERMISSION", "true"),
    "APPS_SCRIPT_UPLOAD_URL": os.getenv("APPS_SCRIPT_UPLOAD_URL", ""),
    "APPS_SCRIPT_SECRET": os.getenv("APPS_SCRIPT_SECRET", ""),
    "PAGE_SIZE": os.getenv("PAGE_SIZE", "20"),
    "START_PAGE": os.getenv("START_PAGE", "1"),
    "SCHEDULE_HOURS": os.getenv("SCHEDULE_HOURS", "8"),
    "SCHEDULE_MAX_PAGES": os.getenv("SCHEDULE_MAX_PAGES", "10"),
    "RETRY_FAILED_RESUMES": os.getenv("RETRY_FAILED_RESUMES", "false"),
    "PROCESS_ALREADY_UPLOADED": os.getenv("PROCESS_ALREADY_UPLOADED", "false"),
}


for key, value in EMBEDDED_ENV.items():
    os.environ[key] = value


from ceipal_inline_resume_worker import run_inline_worker  # noqa: E402


DEFAULT_SCHEDULE_HOURS = int(EMBEDDED_ENV["SCHEDULE_HOURS"])
DEFAULT_MAX_PAGES = int(EMBEDDED_ENV["SCHEDULE_MAX_PAGES"])


def now_label():
    return datetime.now(timezone.utc).isoformat()


def run_job(max_pages=None, start_page=None):
    pages = max_pages if max_pages is not None else DEFAULT_MAX_PAGES
    print(f"\n[{now_label()}] EC2 auto CEIPAL inline job started. Max pages: {pages}")
    run_inline_worker(start_page=start_page, max_pages=pages)
    print(f"[{now_label()}] EC2 auto CEIPAL inline job finished.")


def run_scheduler(hours=None, max_pages=None, run_immediately=True):
    interval_hours = hours if hours is not None else DEFAULT_SCHEDULE_HOURS
    pages = max_pages if max_pages is not None else DEFAULT_MAX_PAGES

    if run_immediately:
        run_job(max_pages=pages)

    schedule.every(interval_hours).hours.do(run_job, max_pages=pages)
    print(f"Scheduler started. Running every {interval_hours} hours. Max pages per run: {pages}")

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            print("Stopping EC2 auto CEIPAL inline scheduler...")
            break
        except Exception as exc:
            print(f"Scheduler loop error: {exc}")
            time.sleep(60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EC2 auto-running CEIPAL inline worker with embedded config.")
    parser.add_argument("--run-once", action="store_true", help="Run one job and exit")
    parser.add_argument("--no-run-immediately", action="store_true", help="Start scheduler without running immediately")
    parser.add_argument("--hours", type=int, default=None, help="Override schedule interval hours")
    parser.add_argument("--max-pages", type=int, default=None, help="Override max CEIPAL pages per run")
    parser.add_argument("--start-page", type=int, default=None, help="Run once from a specific CEIPAL page")
    args = parser.parse_args()

    if args.run_once:
        run_job(max_pages=args.max_pages, start_page=args.start_page)
    else:
        run_scheduler(
            hours=args.hours,
            max_pages=args.max_pages,
            run_immediately=not args.no_run_immediately,
        )
