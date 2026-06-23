import argparse
import base64
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from pymongo import MongoClient


load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "recruitment_db")
PARSED_COLLECTION = os.getenv("PARSED_COLLECTION", "parsed_resumes")
CANDIDATE_COLLECTION = os.getenv("COLLECTION_NAME", "ceipal_applicant_details")

CEIPAL_BASE_URL = os.getenv("CEIPAL_BASE_URL", "https://api.ceipal.com").rstrip("/")
CEIPAL_REFRESH_ENDPOINT = os.getenv("CEIPAL_REFRESH_ENDPOINT", "/v2/refreshToken/")
CEIPAL_EMAIL = os.getenv("CEIPAL_EMAIL")
CEIPAL_PASSWORD = os.getenv("CEIPAL_PASSWORD")
CEIPAL_API_KEY = os.getenv("CEIPAL_API_KEY")
CEIPAL_CUSTOM_APPLICANT_ENDPOINT = os.getenv(
    "CEIPAL_CUSTOM_APPLICANT_ENDPOINT",
    "/v2/getCustomApplicantDetails/Z3RkUkt2OXZJVld2MjFpOVRSTXoxZz09/8935e84722ea4c76bd6f4ed3f75b516a/",
)
TOKEN_REFRESH_BUFFER_MINUTES = int(os.getenv("TOKEN_REFRESH_BUFFER_MINUTES", "55"))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "20"))
START_PAGE = int(os.getenv("START_PAGE", "1"))

APPS_SCRIPT_UPLOAD_URL = os.getenv("APPS_SCRIPT_UPLOAD_URL")
APPS_SCRIPT_SECRET = os.getenv("APPS_SCRIPT_SECRET")

required_env = {
    "MONGODB_URI or MONGO_URI": MONGODB_URI,
    "CEIPAL_EMAIL": CEIPAL_EMAIL,
    "CEIPAL_PASSWORD": CEIPAL_PASSWORD,
    "CEIPAL_API_KEY": CEIPAL_API_KEY,
    "APPS_SCRIPT_UPLOAD_URL": APPS_SCRIPT_UPLOAD_URL,
    "APPS_SCRIPT_SECRET": APPS_SCRIPT_SECRET,
}
missing = [key for key, value in required_env.items() if not value]
if missing:
    raise RuntimeError("Missing required .env values: " + ", ".join(missing))

client = MongoClient(MONGODB_URI)
db = client[DB_NAME]
parsed_col = db[PARSED_COLLECTION]
candidates_col = db[CANDIDATE_COLLECTION]

access_token = None
refresh_token = None
token_expiry = None


def now_utc():
    return datetime.now(timezone.utc)


def get_token_value(data, *keys):
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if value:
            return value
    return None


def set_access_token_expiry():
    global token_expiry
    token_expiry = now_utc() + timedelta(minutes=TOKEN_REFRESH_BUFFER_MINUTES)


def create_new_auth_token(max_retries=3):
    global access_token, refresh_token

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                f"{CEIPAL_BASE_URL}/v1/createAuthtoken/",
                data={
                    "email": CEIPAL_EMAIL,
                    "password": CEIPAL_PASSWORD,
                    "api_key": CEIPAL_API_KEY,
                    "json": 1,
                },
                headers={"Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as exc:
            print(f"CEIPAL auth exception. Retry {attempt}/{max_retries}: {exc}")
            time.sleep(10 * attempt)
            continue

        if response.status_code in [429, 500, 502, 503, 504]:
            print(f"CEIPAL auth error {response.status_code}. Retry {attempt}/{max_retries}")
            time.sleep(10 * attempt)
            continue

        if response.status_code != 200:
            raise RuntimeError(f"CEIPAL auth failed: {response.status_code} - {response.text[:500]}")

        data = response.json()
        access_token = get_token_value(data, "access_token", "token", "auth_token", "authtoken")
        refresh_token = get_token_value(data, "refresh_token", "refreshToken")
        if not access_token:
            raise RuntimeError(f"CEIPAL access token not found in auth response: {data}")

        set_access_token_expiry()
        return access_token

    raise RuntimeError("CEIPAL auth failed after retries")


def refresh_access_token(max_retries=3):
    global access_token, refresh_token

    if not access_token:
        raise RuntimeError("Cannot refresh CEIPAL token because access_token is missing.")

    refresh_path = CEIPAL_REFRESH_ENDPOINT
    if not refresh_path.startswith("/"):
        refresh_path = "/" + refresh_path

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                f"{CEIPAL_BASE_URL}{refresh_path}",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Token": f"Bearer {access_token}",
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            print(f"CEIPAL refresh exception. Retry {attempt}/{max_retries}: {exc}")
            time.sleep(10 * attempt)
            continue

        if response.status_code in [429, 500, 502, 503, 504]:
            print(f"CEIPAL refresh error {response.status_code}. Retry {attempt}/{max_retries}")
            time.sleep(10 * attempt)
            continue

        if response.status_code != 200:
            raise RuntimeError(f"CEIPAL refresh failed: {response.status_code} - {response.text[:500]}")

        data = response.json()
        new_access_token = get_token_value(data, "access_token", "token", "auth_token", "authtoken")
        new_refresh_token = get_token_value(data, "refresh_token", "refreshToken")
        if not new_access_token:
            raise RuntimeError(f"CEIPAL access token not found in refresh response: {data}")

        access_token = new_access_token
        if new_refresh_token:
            refresh_token = new_refresh_token
        set_access_token_expiry()
        return access_token

    raise RuntimeError("CEIPAL refresh failed after retries")


def get_access_token(force_refresh=False):
    if not force_refresh and access_token and token_expiry and now_utc() < token_expiry:
        return access_token

    if access_token and refresh_token:
        try:
            return refresh_access_token()
        except Exception as exc:
            print(f"CEIPAL refresh failed, creating new token. Error: {exc}")

    return create_new_auth_token()


def call_ceipal_get(path, params=None, max_retries=3):
    params = params or {}
    token = get_access_token()

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(
                f"{CEIPAL_BASE_URL}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                params=params,
                timeout=90,
            )
        except requests.RequestException as exc:
            print(f"CEIPAL request exception on page {params.get('page')}: {exc}. Retry {attempt}/{max_retries}")
            time.sleep(10 * attempt)
            continue

        if response.status_code == 401:
            token = get_access_token(force_refresh=True)
            continue
        if response.status_code == 200:
            return response.json()
        if response.status_code in [429, 500, 502, 503, 504]:
            print(f"CEIPAL page {params.get('page')} error {response.status_code}. Retry {attempt}/{max_retries}")
            time.sleep(10 * attempt)
            continue
        raise RuntimeError(f"CEIPAL GET failed: {response.status_code} - {response.text[:500]}")

    raise RuntimeError(f"CEIPAL GET failed after retries on page {params.get('page')}")


def extract_applicants(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("results") or data.get("data") or data.get("applicants") or data.get("records") or []
    return []


def normalize_applicant(app):
    first_name = app.get("first_name")
    last_name = app.get("last_name")
    full_name = app.get("full_name") or f"{first_name or ''} {last_name or ''}".strip()
    email = app.get("email_address") or app.get("email")
    applicant_id = app.get("applicant_id") or app.get("id")

    return {
        "id": app.get("id") or applicant_id,
        "applicant_id": applicant_id,
        "first_name": first_name,
        "middle_name": app.get("middle_name"),
        "last_name": last_name,
        "full_name": full_name,
        "email_address": email,
        "mobile_number": app.get("mobile_number") or app.get("phone"),
        "linkedin_profile_url": app.get("linkedin_profile_url") or app.get("linkedin"),
        "work_authorization": app.get("work_authorization"),
        "experience": app.get("experience"),
        "job_title": app.get("job_title") or app.get("position"),
        "location": app.get("location") or app.get("city"),
        "city": app.get("city"),
        "state": app.get("state"),
        "country": app.get("country"),
        "resume_token": app.get("resume_token") or app.get("resumeToken"),
        "old_ceipal_resume_url": app.get("resume_path") or app.get("resume_url"),
        "api_created_at": app.get("created_on"),
        "api_modified_at": app.get("modified_date"),
        "raw": app,
        "updated_at": now_utc(),
    }


def detect_resume_type(file_bytes, content_type=""):
    content_type = (content_type or "").lower()
    if file_bytes.startswith(b"%PDF") or "pdf" in content_type:
        return "pdf", "application/pdf"
    if file_bytes.startswith(b"PK") or "wordprocessingml.document" in content_type or "docx" in content_type:
        return "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if "msword" in content_type:
        return "doc", "application/msword"
    return "bin", content_type or "application/octet-stream"


def download_resume_by_token(resume_token, max_retries=2):
    token = get_access_token()
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                f"{CEIPAL_BASE_URL}/v2/documentDownload/",
                json={"resumeToken": resume_token},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                },
                timeout=120,
            )
        except requests.RequestException as exc:
            print(f"Resume token download exception. Retry {attempt}/{max_retries}: {exc}")
            time.sleep(10 * attempt)
            continue

        if response.status_code == 401:
            token = get_access_token(force_refresh=True)
            continue
        if response.status_code == 410:
            raise RuntimeError("CEIPAL resume_token expired.")
        if response.status_code == 200:
            file_bytes = response.content
            if not file_bytes:
                raise RuntimeError("CEIPAL returned empty resume file.")
            content_type = response.headers.get("Content-Type", "")
            if "json" in content_type.lower():
                raise RuntimeError(f"CEIPAL returned JSON instead of file: {response.text[:500]}")
            file_type, mime_type = detect_resume_type(file_bytes, content_type)
            return file_bytes, file_type, mime_type
        if response.status_code in [429, 500, 502, 503, 504]:
            print(f"Resume token download error {response.status_code}. Retry {attempt}/{max_retries}")
            time.sleep(10 * attempt)
            continue
        raise RuntimeError(f"CEIPAL resume download failed: {response.status_code} - {response.text[:500]}")
    raise RuntimeError("CEIPAL resume download failed after retries")


def upload_resume_to_drive(file_bytes, file_name, mime_type):
    payload = {
        "secret": APPS_SCRIPT_SECRET,
        "fileName": file_name,
        "mimeType": mime_type,
        "base64File": base64.b64encode(file_bytes).decode("utf-8"),
    }
    response = requests.post(APPS_SCRIPT_UPLOAD_URL, json=payload, timeout=180)
    if response.status_code != 200:
        raise RuntimeError(f"Apps Script upload failed: {response.status_code} - {response.text[:500]}")
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(f"Apps Script returned non-JSON response: {response.text[:500]}")
    if not data.get("success"):
        raise RuntimeError(f"Apps Script error: {data}")
    return {
        "file_id": data.get("fileId"),
        "view_url": data.get("viewUrl"),
        "download_url": data.get("downloadUrl"),
    }


def safe_file_name(value):
    value = str(value or "resume").strip().replace(" ", "_")
    value = re.sub(r"[^A-Za-z0-9_\-.]", "", value)
    return value[:120] or "resume"


def clean_email(value):
    return str(value or "").strip().lower()


def old_doc_email(old_doc):
    return clean_email(old_doc.get("primary_email") or old_doc.get("email_address"))


def parse_created_at(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return now_utc()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return now_utc()
    return now_utc()


def top_level_fields_to_unset():
    fields = [
        "supabase_id",
        "phone",
        "emails",
        "status",
        "location",
        "full_name",
        "job_title",
        "last_name",
        "first_name",
        "resume_url",
        "middle_name",
        "resume_text",
        "linkedin_url",
        "email_address",
        "primary_email",
        "linkedin_profile_url",
        "work_authorization",
        "experience",
        "api_created_at",
        "api_modified_at",
    ]
    return {field: "" for field in fields}


def build_query(only_id=None):
    query = {
        "data": {"$exists": False},
        "resume_url": {"$regex": r"^https://api\.ceipal\.com/download/"},
    }
    if only_id:
        from bson import ObjectId

        query["_id"] = ObjectId(only_id)
    return query


def load_pending_old_docs(limit=None, only_id=None):
    cursor = parsed_col.find(build_query(only_id=only_id)).sort("_id", 1)
    if limit:
        cursor = cursor.limit(limit)

    pending_by_email = {}
    skipped = 0
    for old_doc in cursor:
        email = old_doc_email(old_doc)
        if not email:
            skipped += 1
            continue
        pending_by_email.setdefault(email, []).append(old_doc)

    return pending_by_email, skipped


def build_new_data(old_doc, candidate, drive_result):
    updated_at = now_utc()
    email = old_doc.get("primary_email") or old_doc.get("email_address") or candidate.get("email_address")
    return {
        "resume_text": old_doc.get("resume_text"),
        "resume_url": drive_result["view_url"],
        "resume_drive_url": drive_result["view_url"],
        "resume_download_url": drive_result["download_url"],
        "resume_drive_file_id": drive_result["file_id"],
        "phone": old_doc.get("phone") or candidate.get("mobile_number"),
        "emails": old_doc.get("emails") or ([email] if email else []),
        "primary_email": email,
        "first_name": old_doc.get("first_name") or candidate.get("first_name"),
        "middle_name": old_doc.get("middle_name") or candidate.get("middle_name"),
        "last_name": old_doc.get("last_name") or candidate.get("last_name"),
        "email_address": email,
        "linkedin_profile_url": old_doc.get("linkedin_profile_url") or old_doc.get("linkedin_url") or candidate.get("linkedin_profile_url"),
        "work_authorization": old_doc.get("work_authorization") or candidate.get("work_authorization"),
        "experience": old_doc.get("experience") if old_doc.get("experience") is not None else candidate.get("experience"),
        "job_title": old_doc.get("job_title") or candidate.get("job_title"),
        "location": old_doc.get("location") or candidate.get("location"),
        "full_name": old_doc.get("full_name") or candidate.get("full_name"),
        "api_created_at": old_doc.get("api_created_at") or candidate.get("api_created_at"),
        "api_modified_at": old_doc.get("api_modified_at") or candidate.get("api_modified_at"),
        "status": old_doc.get("status") or "applied",
        "updated_at": updated_at,
    }


def build_candidate_insert_data(old_doc, candidate):
    return {
        "first_name": old_doc.get("first_name") or candidate.get("first_name"),
        "middle_name": old_doc.get("middle_name") or candidate.get("middle_name"),
        "last_name": old_doc.get("last_name") or candidate.get("last_name"),
        "full_name": old_doc.get("full_name") or candidate.get("full_name"),
        "email_address": old_doc.get("email_address") or old_doc.get("primary_email") or candidate.get("email_address"),
        "mobile_number": old_doc.get("phone") or candidate.get("mobile_number"),
        "linkedin_profile_url": old_doc.get("linkedin_profile_url") or old_doc.get("linkedin_url") or candidate.get("linkedin_profile_url"),
        "work_authorization": old_doc.get("work_authorization") or candidate.get("work_authorization"),
        "experience": old_doc.get("experience") if old_doc.get("experience") is not None else candidate.get("experience"),
        "job_title": old_doc.get("job_title") or candidate.get("job_title"),
        "location": old_doc.get("location") or candidate.get("location"),
        "status": old_doc.get("status") or "applied",
        "created_at": parse_created_at(old_doc.get("created_at")),
    }


def repair_old_doc(old_doc, candidate, page_number=None, dry_run=False):
    doc_id = old_doc["_id"]
    candidate_id = old_doc.get("candidate_id")
    resume_token = candidate.get("resume_token")

    if not candidate_id:
        raise RuntimeError("old parsed doc missing candidate_id")
    if not resume_token:
        raise RuntimeError("matched CEIPAL candidate has no resume_token")

    if dry_run:
        print(f"{doc_id}: would use CEIPAL applicant {candidate.get('applicant_id')} from page {page_number}")
        return True

    print(f"{doc_id}: downloading fresh resume by CEIPAL API token")
    file_bytes, file_type, mime_type = download_resume_by_token(resume_token)
    full_name = old_doc.get("full_name") or candidate.get("full_name")
    file_name = f"{safe_file_name(full_name)}_{safe_file_name(candidate_id)}.{file_type}"

    print(f"{doc_id}: uploading fresh resume to Drive")
    drive_result = upload_resume_to_drive(file_bytes, file_name, mime_type)
    updated_at = now_utc()
    data_json = build_new_data(old_doc, candidate, drive_result)

    parsed_col.update_one(
        {"_id": doc_id},
        {
            "$set": {
                "candidate_id": candidate_id,
                "created_at": parse_created_at(old_doc.get("created_at")),
                "data": data_json,
                "updated_at": updated_at,
                "repair_status": "api_repaired",
                "repair_source": "ceipal_applicant_api",
                "repair_page": page_number,
            },
            "$unset": top_level_fields_to_unset(),
        },
    )

    candidate_update_data = dict(candidate)
    candidate_update_data.update(
        {
            "last_seen_page": page_number,
            "resume_url": drive_result["view_url"],
            "resume_drive_url": drive_result["view_url"],
            "resume_download_url": drive_result["download_url"],
            "resume_drive_file_id": drive_result["file_id"],
            "resume_file_type": file_type,
            "resume_download_status": "uploaded",
            "resume_download_error": None,
            "resume_uploaded_at": updated_at,
            "resume_last_attempt_at": updated_at,
            "parsed_status": "parsed",
            "parsed_error": None,
            "parsed_at": updated_at,
            "updated_at": updated_at,
        }
    )
    candidate_insert_data = build_candidate_insert_data(old_doc, candidate)

    candidates_col.update_one(
        {"_id": candidate_id},
        {
            "$set": candidate_update_data,
            "$setOnInsert": {
                key: value
                for key, value in candidate_insert_data.items()
                if key not in candidate_update_data
            },
        },
        upsert=True,
    )

    print(f"{doc_id}: repaired successfully from CEIPAL API")
    return True


def mark_repair_failed(old_doc, exc, page_number):
    parsed_col.update_one(
        {"_id": old_doc["_id"]},
        {
            "$set": {
                "repair_status": "api_failed",
                "repair_error": str(exc)[:1000],
                "repair_last_attempt_at": now_utc(),
                "repair_page": page_number,
            }
        },
    )


def process_repair_doc(old_doc, candidate, page_number=None, dry_run=False):
    try:
        ok = repair_old_doc(old_doc, candidate, page_number=page_number, dry_run=dry_run)
        return old_doc["_id"], ok, None
    except Exception as exc:
        if not dry_run:
            mark_repair_failed(old_doc, exc, page_number)
        return old_doc["_id"], False, exc


def run_repair(start_page=None, end_page=None, max_pages=None, limit=None, only_id=None, dry_run=False, workers=1):
    pending_by_email, skipped = load_pending_old_docs(limit=limit, only_id=only_id)
    pending_total = sum(len(items) for items in pending_by_email.values())
    page = start_page or START_PAGE
    pages_done = 0
    repaired = 0
    failed = 0
    workers = max(1, workers)

    print(f"Using database: {DB_NAME}")
    print(f"Pending old docs with email: {pending_total}")
    print(f"Skipped old docs without email: {skipped}")
    print(f"Dry run: {dry_run}")
    print(f"Workers: {workers}")

    while pending_total > 0:
        if end_page and page > end_page:
            print(f"Reached end page {end_page}")
            break
        if max_pages and pages_done >= max_pages:
            print(f"Reached max pages {max_pages}")
            break

        print(f"Scanning CEIPAL page {page}")
        try:
            data = call_ceipal_get(CEIPAL_CUSTOM_APPLICANT_ENDPOINT, {"page": page, "paging_length": PAGE_SIZE})
        except Exception as exc:
            print(f"CEIPAL page {page} failed after retries, skipping page. Error: {str(exc)[:1000]}")
            pages_done += 1
            page += 1
            time.sleep(2)
            continue

        applicants = extract_applicants(data)
        if not applicants:
            print(f"No applicants found on page {page}")
            break

        repair_tasks = []
        for app in applicants:
            candidate = normalize_applicant(app)
            email = clean_email(candidate.get("email_address"))
            if not email or email not in pending_by_email:
                continue

            old_docs = list(pending_by_email[email])
            for old_doc in old_docs:
                repair_tasks.append((email, old_doc, candidate))

        if workers == 1:
            results = []
            for email, old_doc, candidate in repair_tasks:
                doc_id, ok, exc = process_repair_doc(old_doc, candidate, page_number=page, dry_run=dry_run)
                results.append((email, old_doc, doc_id, ok, exc))
        else:
            results = []
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(process_repair_doc, old_doc, candidate, page, dry_run): (email, old_doc)
                    for email, old_doc, candidate in repair_tasks
                }
                for future in as_completed(futures):
                    email, old_doc = futures[future]
                    doc_id, ok, exc = future.result()
                    results.append((email, old_doc, doc_id, ok, exc))

        for email, old_doc, doc_id, ok, exc in results:
            if ok:
                repaired += 1
                if email in pending_by_email and old_doc in pending_by_email[email]:
                    pending_by_email[email].remove(old_doc)
                    pending_total -= 1
            else:
                failed += 1
                if exc:
                    print(f"{doc_id}: failed - {str(exc)[:1000]}")

            if email in pending_by_email and not pending_by_email[email]:
                pending_by_email.pop(email, None)

        pages_done += 1
        page += 1
        time.sleep(0.5)

    print(f"Done. Pages scanned: {pages_done}, Repaired/matched: {repaired}, Failed: {failed}, Remaining pending: {pending_total}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair old flat parsed_resumes docs by finding fresh CEIPAL resume tokens from applicant API.")
    parser.add_argument("--start-page", type=int, default=None, help="CEIPAL page to start scanning")
    parser.add_argument("--end-page", type=int, default=None, help="Stop after this CEIPAL page")
    parser.add_argument("--max-pages", type=int, default=None, help="Maximum CEIPAL pages to scan")
    parser.add_argument("--limit", type=int, default=None, help="Maximum old parsed docs to load for repair")
    parser.add_argument("--only-id", default=None, help="Repair one parsed_resumes _id")
    parser.add_argument("--dry-run", action="store_true", help="Find matches without downloading/uploading/updating")
    parser.add_argument("--workers", type=int, default=1, help="Parallel matched records to repair at once")
    args = parser.parse_args()

    run_repair(
        start_page=args.start_page,
        end_page=args.end_page,
        max_pages=args.max_pages,
        limit=args.limit,
        only_id=args.only_id,
        dry_run=args.dry_run,
        workers=args.workers,
    )
