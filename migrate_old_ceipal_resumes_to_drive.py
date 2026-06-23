import argparse
import base64
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient


load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "recruitment_db")
PARSED_COLLECTION = os.getenv("PARSED_COLLECTION", "parsed_resumes")
CANDIDATE_COLLECTION = os.getenv("COLLECTION_NAME", "ceipal_applicant_details")

CEIPAL_BASE_URL = os.getenv("CEIPAL_BASE_URL", "https://api.ceipal.com").rstrip("/")
CEIPAL_EMAIL = os.getenv("CEIPAL_EMAIL")
CEIPAL_PASSWORD = os.getenv("CEIPAL_PASSWORD")
CEIPAL_API_KEY = os.getenv("CEIPAL_API_KEY")
TOKEN_REFRESH_BUFFER_MINUTES = int(os.getenv("TOKEN_REFRESH_BUFFER_MINUTES", "55"))

APPS_SCRIPT_UPLOAD_URL = os.getenv("APPS_SCRIPT_UPLOAD_URL")
APPS_SCRIPT_SECRET = os.getenv("APPS_SCRIPT_SECRET")

required_env = {
    "MONGODB_URI or MONGO_URI": MONGODB_URI,
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


def get_access_token():
    global access_token, token_expiry

    if access_token and token_expiry and now_utc() < token_expiry:
        return access_token

    missing_ceipal = [
        key
        for key, value in {
            "CEIPAL_EMAIL": CEIPAL_EMAIL,
            "CEIPAL_PASSWORD": CEIPAL_PASSWORD,
            "CEIPAL_API_KEY": CEIPAL_API_KEY,
        }.items()
        if not value
    ]
    if missing_ceipal:
        raise RuntimeError("CEIPAL auth required but missing .env values: " + ", ".join(missing_ceipal))

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
    if response.status_code != 200:
        raise RuntimeError(f"CEIPAL auth failed: {response.status_code} - {response.text[:500]}")

    data = response.json()
    access_token = get_token_value(data, "access_token", "token", "auth_token", "authtoken")
    if not access_token:
        raise RuntimeError(f"CEIPAL access token not found in auth response: {data}")

    token_expiry = now_utc() + timedelta(minutes=TOKEN_REFRESH_BUFFER_MINUTES)
    return access_token


def detect_resume_type(file_bytes, content_type=""):
    content_type = (content_type or "").lower()
    if file_bytes.startswith(b"%PDF") or "pdf" in content_type:
        return "pdf", "application/pdf"
    if file_bytes.startswith(b"PK") or "wordprocessingml.document" in content_type or "docx" in content_type:
        return "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if "msword" in content_type:
        return "doc", "application/msword"
    return "bin", content_type or "application/octet-stream"


def safe_file_name(value):
    value = str(value or "resume").strip().replace(" ", "_")
    value = re.sub(r"[^A-Za-z0-9_\-.]", "", value)
    return value[:120] or "resume"


def is_ceipal_download_url(url):
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.netloc.lower().endswith("ceipal.com") and "/download/" in parsed.path


def download_resume_from_ceipal_url(url, max_retries=3):
    headers = {"Accept": "*/*"}

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=120)
        except requests.RequestException as exc:
            print(f"CEIPAL download exception. Retry {attempt}/{max_retries}: {exc}")
            time.sleep(10 * attempt)
            continue

        if response.status_code in [401, 403]:
            token = get_access_token()
            auth_headers = {
                "Accept": "*/*",
                "Authorization": f"Bearer {token}",
                "Token": f"Bearer {token}",
            }
            try:
                response = requests.get(url, headers=auth_headers, timeout=120)
            except requests.RequestException as exc:
                print(f"CEIPAL auth download exception. Retry {attempt}/{max_retries}: {exc}")
                time.sleep(10 * attempt)
                continue

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
            print(f"CEIPAL download error {response.status_code}. Retry {attempt}/{max_retries}")
            time.sleep(10 * attempt)
            continue

        raise RuntimeError(f"CEIPAL download failed: {response.status_code} - {response.text[:500]}")

    raise RuntimeError("CEIPAL download failed after retries.")


def upload_resume_to_drive(file_bytes, file_name, mime_type, max_retries=3):
    payload = {
        "secret": APPS_SCRIPT_SECRET,
        "fileName": file_name,
        "mimeType": mime_type,
        "base64File": base64.b64encode(file_bytes).decode("utf-8"),
    }
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(APPS_SCRIPT_UPLOAD_URL, json=payload, timeout=240)
        except requests.RequestException as exc:
            print(f"Apps Script upload exception. Retry {attempt}/{max_retries}: {exc}")
            time.sleep(15 * attempt)
            continue

        if response.status_code in [429, 500, 502, 503, 504]:
            print(f"Apps Script upload error {response.status_code}. Retry {attempt}/{max_retries}")
            time.sleep(15 * attempt)
            continue

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

    raise RuntimeError("Apps Script upload failed after retries.")


def parse_created_at(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return now_utc()
    if isinstance(value, str):
        try:
            normalized = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return now_utc()
    return now_utc()


def build_new_data(old_doc, drive_result):
    updated_at = now_utc()
    return {
        "resume_text": old_doc.get("resume_text"),
        "resume_url": drive_result["view_url"],
        "resume_drive_url": drive_result["view_url"],
        "resume_download_url": drive_result["download_url"],
        "resume_drive_file_id": drive_result["file_id"],
        "phone": old_doc.get("phone"),
        "emails": old_doc.get("emails") or [],
        "primary_email": old_doc.get("primary_email") or old_doc.get("email_address"),
        "first_name": old_doc.get("first_name"),
        "middle_name": old_doc.get("middle_name"),
        "last_name": old_doc.get("last_name"),
        "email_address": old_doc.get("email_address") or old_doc.get("primary_email"),
        "linkedin_profile_url": old_doc.get("linkedin_profile_url") or old_doc.get("linkedin_url"),
        "work_authorization": old_doc.get("work_authorization"),
        "experience": old_doc.get("experience"),
        "job_title": old_doc.get("job_title"),
        "location": old_doc.get("location"),
        "full_name": old_doc.get("full_name"),
        "api_created_at": old_doc.get("api_created_at"),
        "api_modified_at": old_doc.get("api_modified_at"),
        "status": old_doc.get("status") or "applied",
        "updated_at": updated_at,
    }


def build_candidate_insert_data(old_doc):
    candidate_id = old_doc.get("candidate_id")
    return {
        "_id": candidate_id,
        "first_name": old_doc.get("first_name"),
        "middle_name": old_doc.get("middle_name"),
        "last_name": old_doc.get("last_name"),
        "full_name": old_doc.get("full_name"),
        "email_address": old_doc.get("email_address") or old_doc.get("primary_email"),
        "mobile_number": old_doc.get("phone"),
        "linkedin_profile_url": old_doc.get("linkedin_profile_url") or old_doc.get("linkedin_url"),
        "work_authorization": old_doc.get("work_authorization"),
        "experience": old_doc.get("experience"),
        "job_title": old_doc.get("job_title"),
        "location": old_doc.get("location"),
        "status": old_doc.get("status") or "applied",
        "api_created_at": old_doc.get("api_created_at"),
        "api_modified_at": old_doc.get("api_modified_at"),
        "created_at": parse_created_at(old_doc.get("created_at")),
    }


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


def build_query(force=False, only_id=None):
    query = {}
    if only_id:
        query["_id"] = ObjectId(only_id)
        return query

    if not force:
        query["data"] = {"$exists": False}
        query["resume_url"] = {"$regex": r"^https://api\.ceipal\.com/download/"}
    return query


def fetch_old_doc_batch(base_query, last_id=None, batch_size=25):
    query = dict(base_query)
    if last_id:
        query["_id"] = {"$gt": last_id}
    return list(parsed_col.find(query).sort("_id", 1).limit(batch_size))


def migrate_one(old_doc, dry_run=False):
    doc_id = old_doc["_id"]
    candidate_id = old_doc.get("candidate_id")
    ceipal_resume_url = old_doc.get("resume_url")

    if not candidate_id:
        print(f"{doc_id}: missing candidate_id, skipping")
        return False
    if not is_ceipal_download_url(ceipal_resume_url):
        print(f"{doc_id}: resume_url is not a CEIPAL download URL, skipping")
        return False

    if dry_run:
        candidate_exists = candidates_col.count_documents({"_id": candidate_id}, limit=1) > 0
        action = "update existing candidate" if candidate_exists else "create missing candidate"
        print(f"{doc_id}: would download CEIPAL resume, upload to Drive, and {action} {candidate_id}")
        return True

    print(f"{doc_id}: downloading old CEIPAL resume")
    file_bytes, file_type, mime_type = download_resume_from_ceipal_url(ceipal_resume_url)

    full_name = old_doc.get("full_name") or f"{old_doc.get('first_name') or ''} {old_doc.get('last_name') or ''}".strip()
    file_name = f"{safe_file_name(full_name)}_{safe_file_name(candidate_id)}.{file_type}"

    print(f"{doc_id}: uploading resume to Drive")
    drive_result = upload_resume_to_drive(file_bytes, file_name, mime_type)
    data_json = build_new_data(old_doc, drive_result)
    updated_at = now_utc()

    parsed_col.update_one(
        {"_id": doc_id},
        {
            "$set": {
                "candidate_id": candidate_id,
                "created_at": parse_created_at(old_doc.get("created_at")),
                "data": data_json,
                "updated_at": updated_at,
            },
            "$unset": top_level_fields_to_unset(),
        },
    )

    candidate_insert_data = build_candidate_insert_data(old_doc)
    candidate_update_data = {
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

    candidates_col.update_one(
        {"_id": candidate_id},
        {
            "$set": candidate_update_data,
            "$setOnInsert": {
                key: value
                for key, value in candidate_insert_data.items()
                if key != "_id" and key not in candidate_update_data
            },
        },
        upsert=True,
    )

    print(f"{doc_id}: migrated successfully")
    return True


def mark_migration_failed(old_doc, exc):
    parsed_col.update_one(
        {"_id": old_doc["_id"]},
        {
            "$set": {
                "migration_status": "failed",
                "migration_error": str(exc)[:1000],
                "migration_last_attempt_at": now_utc(),
            }
        },
    )


def process_migration_doc(old_doc, dry_run=False):
    try:
        ok = migrate_one(old_doc, dry_run=dry_run)
        return old_doc["_id"], ok, None
    except Exception as exc:
        if not dry_run:
            mark_migration_failed(old_doc, exc)
        return old_doc["_id"], False, exc


def run_migration(limit=None, dry_run=False, force=False, only_id=None, batch_size=25, workers=1):
    query = build_query(force=force, only_id=only_id)
    last_id = None
    processed = 0
    succeeded = 0
    failed = 0
    workers = max(1, workers)

    print(f"Using database: {DB_NAME}")
    print(f"Parsed collection: {PARSED_COLLECTION}")
    print(f"Candidate collection: {CANDIDATE_COLLECTION}")
    print(f"Dry run: {dry_run}")
    print(f"Workers: {workers}")

    while True:
        remaining = None if limit is None else limit - processed
        if remaining is not None and remaining <= 0:
            break

        current_batch_size = batch_size if remaining is None else min(batch_size, remaining)
        old_docs = fetch_old_doc_batch(query, last_id=last_id, batch_size=current_batch_size)
        if not old_docs:
            break

        last_id = old_docs[-1]["_id"]

        if workers == 1:
            for old_doc in old_docs:
                processed += 1
                doc_id, ok, exc = process_migration_doc(old_doc, dry_run=dry_run)
                if ok:
                    succeeded += 1
                else:
                    failed += 1
                    if exc:
                        print(f"{doc_id}: failed - {str(exc)[:1000]}")
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(process_migration_doc, old_doc, dry_run) for old_doc in old_docs]
                for future in as_completed(futures):
                    processed += 1
                    doc_id, ok, exc = future.result()
                    if ok:
                        succeeded += 1
                    else:
                        failed += 1
                        if exc:
                            print(f"{doc_id}: failed - {str(exc)[:1000]}")

    print(f"Done. Processed: {processed}, Succeeded: {succeeded}, Failed/skipped: {failed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate old flat parsed_resumes docs to Drive-backed nested structure.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum old docs to process")
    parser.add_argument("--dry-run", action="store_true", help="Show matching docs without downloading/uploading/updating")
    parser.add_argument("--force", action="store_true", help="Scan all docs instead of only old flat CEIPAL-link docs")
    parser.add_argument("--only-id", default=None, help="Process one parsed_resumes _id")
    parser.add_argument("--batch-size", type=int, default=25, help="Mongo docs to fetch per short batch")
    parser.add_argument("--workers", type=int, default=1, help="Parallel records to process at once")
    args = parser.parse_args()

    run_migration(
        limit=args.limit,
        dry_run=args.dry_run,
        force=args.force,
        only_id=args.only_id,
        batch_size=args.batch_size,
        workers=args.workers,
    )
