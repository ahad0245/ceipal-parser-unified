import os
import io
import re
import time
import base64
import argparse
from datetime import datetime, timedelta, timezone

import requests
import schedule
import pdfplumber
import pytesseract
from docx import Document
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

"""
CEIPAL one-by-one worker using Google Apps Script upload.

Flow:
1. Fetch one CEIPAL page.
2. Save/update each candidate in MongoDB.
3. Immediately use that candidate's resume_token.
4. Download resume from CEIPAL /v2/documentDownload/.
5. Upload resume to Google Drive through Apps Script.
6. Save Drive link as resume_url in MongoDB.
7. Parse resume and save data in parsed_resumes.
8. Move to next candidate/page.
"""

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "recruitment_db")
CANDIDATE_COLLECTION = os.getenv("COLLECTION_NAME", "ceipal_applicant_details")
PARSED_COLLECTION = os.getenv("PARSED_COLLECTION", "parsed_resumes")
SYNC_COLLECTION = os.getenv("SYNC_COLLECTION", "sync_state")

CEIPAL_BASE_URL = os.getenv("CEIPAL_BASE_URL", "https://api.ceipal.com").rstrip("/")
CEIPAL_EMAIL = os.getenv("CEIPAL_EMAIL")
CEIPAL_PASSWORD = os.getenv("CEIPAL_PASSWORD")
CEIPAL_API_KEY = os.getenv("CEIPAL_API_KEY")
CEIPAL_CUSTOM_APPLICANT_ENDPOINT = os.getenv(
    "CEIPAL_CUSTOM_APPLICANT_ENDPOINT",
    "/v2/getCustomApplicantDetails/Z3RkUkt2OXZJVld2MjFpOVRSTXoxZz09/8935e84722ea4c76bd6f4ed3f75b516a/"
)

APPS_SCRIPT_UPLOAD_URL = os.getenv("APPS_SCRIPT_UPLOAD_URL")
APPS_SCRIPT_SECRET = os.getenv("APPS_SCRIPT_SECRET")

PAGE_SIZE = int(os.getenv("PAGE_SIZE", "20"))
SCHEDULE_HOURS = int(os.getenv("SCHEDULE_HOURS", "3"))
START_PAGE = int(os.getenv("START_PAGE", "1"))
END_PAGE = os.getenv("END_PAGE")
END_PAGE = int(END_PAGE) if END_PAGE else None
PROCESS_ALREADY_UPLOADED = os.getenv("PROCESS_ALREADY_UPLOADED", "false").lower() == "true"
RETRY_FAILED_RESUMES = os.getenv("RETRY_FAILED_RESUMES", "true").lower() == "true"

TESSERACT_CMD = os.getenv("TESSERACT_CMD")
if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

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
candidates_col = db[CANDIDATE_COLLECTION]
parsed_col = db[PARSED_COLLECTION]
sync_col = db[SYNC_COLLECTION]

access_token = None
token_expiry = None


def now_utc():
    return datetime.utcnow()


def get_access_token(force_refresh=False):
    global access_token, token_expiry
    if not force_refresh and access_token and token_expiry and now_utc() < token_expiry:
        return access_token

    print("Fetching new CEIPAL access token...")
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
    token = data.get("access_token") or data.get("token") or data.get("auth_token") or data.get("authtoken")
    if not token:
        raise RuntimeError(f"CEIPAL token not found in auth response: {data}")

    access_token = token
    token_expiry = now_utc() + timedelta(hours=23)
    return access_token


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
        if response.status_code in [500, 502, 503, 504, 429]:
            print(f"CEIPAL server/rate error {response.status_code} on page {params.get('page')}. Retry {attempt}/{max_retries}")
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
    job_title = app.get("job_title") or app.get("position")
    applicant_id = app.get("applicant_id") or app.get("id")
    resume_token = app.get("resume_token") or app.get("resumeToken")

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
        "job_title": job_title,
        "location": app.get("location") or app.get("city"),
        "city": app.get("city"),
        "state": app.get("state"),
        "country": app.get("country"),
        "resume_token": resume_token,
        "old_ceipal_resume_url": app.get("resume_path") or app.get("resume_url"),
        "api_created_at": app.get("created_on"),
        "api_modified_at": app.get("modified_date"),
        "raw": app,
        "updated_at": now_utc(),
    }


def upsert_candidate(candidate, page_number=None):
    applicant_id = candidate.get("applicant_id") or candidate.get("id")
    if not applicant_id:
        print(f"Skipping applicant with no applicant_id: {candidate.get('email_address')}")
        return None

    set_data = dict(candidate)
    set_data["last_seen_page"] = page_number
    if not set_data.get("resume_token"):
        set_data.pop("resume_token", None)

    if candidate.get("resume_token"):
        set_data["resume_download_status"] = "pending"
        set_data["parsed_status"] = "pending"
    else:
        set_data["resume_download_status"] = "missing_token"
        set_data["parsed_status"] = "missing_token"

    candidates_col.update_one(
        {"applicant_id": applicant_id},
        {
            "$set": set_data,
            "$setOnInsert": {
                "status": "applied",
                "created_at": now_utc(),
                "resume_url": None,
                "resume_drive_url": None,
                "resume_download_url": None,
                "resume_drive_file_id": None,
                "resume_file_type": None,
                "resume_download_error": None,
                "parsed_error": None,
            },
        },
        upsert=True,
    )
    return candidates_col.find_one({"applicant_id": applicant_id})


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
        if response.status_code == 401:
            token = get_access_token(force_refresh=True)
            continue
        if response.status_code == 410:
            raise RuntimeError("CEIPAL resume_token expired. Fetch fresh candidate data and process immediately.")
        if response.status_code == 200:
            file_bytes = response.content
            if not file_bytes:
                raise RuntimeError("CEIPAL returned empty resume file.")
            content_type = response.headers.get("Content-Type", "")
            if "json" in content_type.lower():
                try:
                    raise RuntimeError(f"CEIPAL returned JSON instead of file: {response.json()}")
                except ValueError:
                    pass
            file_type, mime_type = detect_resume_type(file_bytes, content_type)
            return file_bytes, file_type, mime_type
        if response.status_code in [500, 502, 503, 504, 429]:
            print(f"Resume download server/rate error {response.status_code}. Retry {attempt}/{max_retries}")
            time.sleep(10 * attempt)
            continue
        raise RuntimeError(f"CEIPAL resume download failed: {response.status_code} - {response.text[:500]}")
    raise RuntimeError("CEIPAL resume download failed after retries")


def upload_resume_to_drive_via_apps_script(file_bytes, file_name, mime_type):
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


def extract_phone_number(text):
    phone_pattern = r'(?:(?:\+?\d{1,3}\s?)?(?:\(\d{1,4}\)\s?)?|(?:\+?\d{1,3}\s)?\d{1,4}[\s./-]?)?\(?(?:\d{2,3})\)?[\s./-]?\d{1,5}[\s./-]?\d{1,5}(?:[\s./-]?\d{1,5})?(?:[\s./-]?\d{1,5})?'
    matches = re.findall(phone_pattern, text or "")
    cleaned = [re.sub(r"\D", "", num) for num in matches]
    cleaned = [num for num in cleaned if len(num) >= 10]
    return cleaned[0] if cleaned else None


def extract_emails(text):
    email_pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
    return re.findall(email_pattern, text or "")


def extract_text_from_docx_bytes(file_bytes):
    document = Document(io.BytesIO(file_bytes))
    return "\n".join([para.text for para in document.paragraphs])


def extract_text_from_pdf_bytes(file_bytes):
    full_text = ""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                full_text += page_text + "\n"
    if len(full_text.strip()) < 100:
        print("PDF text extraction low. Trying OCR...")
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            ocr_text = []
            for page in pdf.pages:
                image = page.to_image(resolution=300)
                pil_image = image.original.convert("RGB")
                ocr_text.append(pytesseract.image_to_string(pil_image, config="--psm 1"))
            full_text = "\n".join(ocr_text)
    return full_text


def extract_resume_text(file_bytes, file_type):
    if file_type == "pdf":
        return extract_text_from_pdf_bytes(file_bytes)
    if file_type == "docx":
        return extract_text_from_docx_bytes(file_bytes)
    raise RuntimeError(f"Unsupported parsing format: {file_type}")


def safe_file_name(value):
    value = value or "resume"
    value = value.strip().replace(" ", "_")
    value = re.sub(r"[^A-Za-z0-9_\-.]", "", value)
    return value[:120] or "resume"


def get_candidate_ref_id(candidate):
    return str(candidate.get("applicant_id") or candidate.get("id") or candidate.get("_id"))


def save_parsed_resume(candidate, resume_text):
    candidate_id = get_candidate_ref_id(candidate)
    extracted_phone = extract_phone_number(resume_text)
    extracted_emails = extract_emails(resume_text)
    phone = candidate.get("mobile_number") or extracted_phone
    email = candidate.get("email_address") or (extracted_emails[0] if extracted_emails else None)

    data_json = {
        "resume_text": resume_text,
        "resume_url": candidate.get("resume_url"),
        "resume_drive_url": candidate.get("resume_drive_url"),
        "resume_download_url": candidate.get("resume_download_url"),
        "resume_drive_file_id": candidate.get("resume_drive_file_id"),
        "phone": phone,
        "emails": extracted_emails,
        "primary_email": email,
        "first_name": candidate.get("first_name"),
        "middle_name": candidate.get("middle_name"),
        "last_name": candidate.get("last_name"),
        "email_address": email,
        "linkedin_profile_url": candidate.get("linkedin_profile_url"),
        "work_authorization": candidate.get("work_authorization"),
        "experience": candidate.get("experience"),
        "job_title": candidate.get("job_title"),
        "location": candidate.get("location"),
        "full_name": candidate.get("full_name"),
        "api_created_at": candidate.get("api_created_at"),
"api_modified_at": candidate.get("api_modified_at"),
        "status": "applied",
        "updated_at": datetime.now(timezone.utc),
    }
    parsed_col.update_one(
        {"candidate_id": candidate_id},
        {
            "$set": {"candidate_id": candidate_id, "data": data_json, "updated_at": datetime.now(timezone.utc)},
            "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
        },
        upsert=True,
    )


def should_process_resume(candidate):
    if PROCESS_ALREADY_UPLOADED:
        return True
    if candidate.get("resume_url") and candidate.get("resume_download_status") == "uploaded":
        return False
    if candidate.get("resume_download_status") == "failed" and not RETRY_FAILED_RESUMES:
        return False
    return True


def process_one_candidate_resume(candidate):
    candidate_id = get_candidate_ref_id(candidate)
    mongo_id = candidate.get("_id")
    resume_token = candidate.get("resume_token")

    if not resume_token:
        candidates_col.update_one(
            {"_id": mongo_id},
            {"$set": {"resume_download_status": "missing_token", "resume_download_error": "No resume_token found", "resume_last_attempt_at": now_utc(), "parsed_status": "missing_token"}},
        )
        print(f"{candidate_id}: missing resume_token")
        return False

    if not should_process_resume(candidate):
        print(f"{candidate_id}: already uploaded, skipping")
        return True

    try:
        print(f"{candidate_id}: downloading resume")
        file_bytes, file_type, mime_type = download_resume_by_token(resume_token)
        full_name = candidate.get("full_name") or f"{candidate.get('first_name') or ''} {candidate.get('last_name') or ''}".strip()
        file_name = f"{safe_file_name(full_name)}_{safe_file_name(candidate_id)}.{file_type}"

        print(f"{candidate_id}: uploading to Drive")
        drive_result = upload_resume_to_drive_via_apps_script(file_bytes, file_name, mime_type)

        resume_fields = {
            "resume_url": drive_result["view_url"],
            "resume_drive_url": drive_result["view_url"],
            "resume_download_url": drive_result["download_url"],
            "resume_drive_file_id": drive_result["file_id"],
            "resume_file_type": file_type,
            "resume_download_status": "uploaded",
            "resume_download_error": None,
            "resume_uploaded_at": now_utc(),
            "resume_last_attempt_at": now_utc(),
        }
        candidates_col.update_one({"_id": mongo_id}, {"$set": resume_fields})
        candidate.update(resume_fields)

        try:
            print(f"{candidate_id}: parsing resume")
            resume_text = extract_resume_text(file_bytes, file_type)
            if not resume_text.strip():
                raise RuntimeError("Resume text extraction returned empty text.")
            save_parsed_resume(candidate, resume_text)
            candidates_col.update_one({"_id": mongo_id}, {"$set": {"parsed_status": "parsed", "parsed_error": None, "parsed_at": now_utc()}})
            print(f"{candidate_id}: uploaded + parsed successfully")
        except Exception as parse_exc:
            candidates_col.update_one({"_id": mongo_id}, {"$set": {"parsed_status": "failed", "parsed_error": str(parse_exc)[:1000]}})
            print(f"{candidate_id}: uploaded, but parsing failed - {parse_exc}")
        return True

    except Exception as exc:
        error_text = str(exc)
        candidates_col.update_one(
            {"_id": mongo_id},
            {"$set": {"resume_download_status": "failed", "resume_download_error": error_text[:1000], "resume_last_attempt_at": now_utc(), "parsed_status": "failed", "parsed_error": error_text[:1000]}},
        )
        print(f"{candidate_id}: failed - {error_text}")
        return False


def process_page(page):
    print(f"\n[{now_utc()}] Fetching CEIPAL page {page}")
    try:
        data = call_ceipal_get(CEIPAL_CUSTOM_APPLICANT_ENDPOINT, {"page": page, "paging_length": PAGE_SIZE})
    except Exception as exc:
        print(f"Page {page} failed: {exc}")
        sync_col.update_one(
            {"name": "ceipal_inline_worker"},
            {"$set": {"last_failed_page": page, "last_failed_error": str(exc)[:1000], "last_failed_at": now_utc()}, "$addToSet": {"failed_pages": page}},
            upsert=True,
        )
        return 0, 0, False

    applicants = extract_applicants(data)
    if not applicants:
        print(f"No applicants found on page {page}")
        return 0, 0, True

    success = 0
    failed = 0
    for app in applicants:
        candidate = normalize_applicant(app)
        saved_candidate = upsert_candidate(candidate, page_number=page)
        if not saved_candidate:
            failed += 1
            continue
        ok = process_one_candidate_resume(saved_candidate)
        success += 1 if ok else 0
        failed += 0 if ok else 1
        time.sleep(0.5)

    sync_col.update_one(
        {"name": "ceipal_inline_worker"},
        {"$set": {"last_success_page": page, "last_run_at": now_utc(), "last_status": "running", "last_page_success_count": success, "last_page_failed_count": failed}},
        upsert=True,
    )
    print(f"Page {page} done. Success: {success}, Failed: {failed}")
    return success, failed, True


def get_start_page(cli_start_page=None):
    if cli_start_page:
        return cli_start_page
    state = sync_col.find_one({"name": "ceipal_inline_worker"})
    if state and state.get("last_success_page"):
        return max(1, int(state["last_success_page"]) + 1)
    return START_PAGE


def run_inline_worker(start_page=None, end_page=None, max_pages=None):
    page = get_start_page(start_page)
    page_end = end_page or END_PAGE
    print(f"\nInline worker started from page {page}")

    total_success = 0
    total_failed = 0
    pages_done = 0
    while True:
        if page_end and page > page_end:
            print(f"Reached end page {page_end}")
            break
        if max_pages and pages_done >= max_pages:
            print(f"Reached max pages {max_pages}")
            break
        success, failed, ok = process_page(page)
        total_success += success
        total_failed += failed
        pages_done += 1
        page += 1
        time.sleep(1)

    sync_col.update_one(
        {"name": "ceipal_inline_worker"},
        {"$set": {"last_completed_at": now_utc(), "last_status": "completed", "last_total_success": total_success, "last_total_failed": total_failed, "last_pages_done": pages_done}},
        upsert=True,
    )
    print(f"\nInline worker complete. Pages: {pages_done}, Success: {total_success}, Failed: {total_failed}")


def scheduled_job():
    run_inline_worker(max_pages=int(os.getenv("SCHEDULE_MAX_PAGES", "10")))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-page", type=int, default=None, help="Start from this CEIPAL page")
    parser.add_argument("--end-page", type=int, default=None, help="Stop after this CEIPAL page")
    parser.add_argument("--max-pages", type=int, default=None, help="Process only this many pages")
    parser.add_argument("--schedule", action="store_true", help="Run scheduled mode")
    args = parser.parse_args()

    if args.schedule:
        scheduled_job()
        print(f"Scheduler started. Running every {SCHEDULE_HOURS} hours.")
        schedule.every(SCHEDULE_HOURS).hours.do(scheduled_job)
        while True:
            try:
                schedule.run_pending()
                time.sleep(10)
            except KeyboardInterrupt:
                print("Stopping scheduler...")
                break
            except Exception as exc:
                print(f"Scheduler crash: {exc}")
                time.sleep(30)
    else:
        run_inline_worker(start_page=args.start_page, end_page=args.end_page, max_pages=args.max_pages)
