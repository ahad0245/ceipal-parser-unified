import os
import io
import re
import time
import mimetypes
from datetime import datetime, timedelta, timezone

import requests
import schedule
import pdfplumber
import pytesseract
from docx import Document
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ============================================================
# UPDATED CEIPAL WORKER
# Flow:
# 1) Fetch candidates from CEIPAL
# 2) Save/update candidates in MongoDB with resume_token
# 3) Download resume file using CEIPAL resume_token
# 4) Upload resume file to Google Drive
# 5) Save Google Drive link as resume_url in MongoDB
# 6) Parse PDF/DOCX resume text
# 7) Save parsed data in parsed_resumes collection
# ============================================================

load_dotenv()

# ================= ENV CONFIG =================
MONGODB_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "recruitment_db")
CANDIDATE_COLLECTION = os.getenv("COLLECTION_NAME", "ceipal_applicant_details")
PARSED_COLLECTION = os.getenv("PARSED_COLLECTION", "parsed_resumes")
SYNC_COLLECTION = os.getenv("SYNC_COLLECTION", "sync_state")

CEIPAL_BASE_URL = os.getenv("CEIPAL_BASE_URL", "https://api.ceipal.com").rstrip("/")
CEIPAL_EMAIL = os.getenv("CEIPAL_EMAIL")
CEIPAL_PASSWORD = os.getenv("CEIPAL_PASSWORD")
CEIPAL_API_KEY = os.getenv("CEIPAL_API_KEY")

ENDPOINT = os.getenv(
    "CEIPAL_CUSTOM_APPLICANT_ENDPOINT",
    "/getCustomApplicantDetails/UGtpQkJSTEZ3Z0xBaDdsN1QwOXBIUT09/b03006f7db8d37c2aae189e1cd1e177d"
)

PAGE_SIZE = int(os.getenv("PAGE_SIZE", "20"))
SCHEDULE_HOURS = int(os.getenv("SCHEDULE_HOURS", "3"))
RESUME_BATCH_LIMIT = int(os.getenv("RESUME_BATCH_LIMIT", "100"))

# Google Drive
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
DRIVE_ANYONE_PERMISSION = os.getenv("DRIVE_ANYONE_PERMISSION", "true").lower() == "true"

# CEIPAL resume token download endpoint
CEIPAL_DOCUMENT_DOWNLOAD_ENDPOINT = "/v2/documentDownload/"

# Optional OCR path
TESSERACT_CMD = os.getenv("TESSERACT_CMD")
if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

# ================= VALIDATION =================
required_env = {
    "MONGODB_URI/MONGO_URI": MONGODB_URI,
    "CEIPAL_EMAIL": CEIPAL_EMAIL,
    "CEIPAL_PASSWORD": CEIPAL_PASSWORD,
    "CEIPAL_API_KEY": CEIPAL_API_KEY,
    "GOOGLE_SERVICE_ACCOUNT_FILE": GOOGLE_SERVICE_ACCOUNT_FILE,
    "GOOGLE_DRIVE_FOLDER_ID": GOOGLE_DRIVE_FOLDER_ID,
}

missing = [key for key, value in required_env.items() if not value]
if missing:
    raise RuntimeError(f"Missing required env values: {', '.join(missing)}")

# ================= MONGODB =================
client = MongoClient(MONGODB_URI)
db = client[DB_NAME]
candidates_col = db[CANDIDATE_COLLECTION]
parsed_col = db[PARSED_COLLECTION]
sync_col = db[SYNC_COLLECTION]

# ================= TOKEN CACHE =================
access_token = None
token_expiry = None
_drive_service = None


# ============================================================
# CEIPAL AUTH + API
# ============================================================
def get_access_token(force_refresh=False):
    global access_token, token_expiry

    if not force_refresh and access_token and token_expiry and datetime.utcnow() < token_expiry:
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
    token = (
        data.get("access_token")
        or data.get("token")
        or data.get("auth_token")
        or data.get("authtoken")
    )

    if not token:
        raise RuntimeError(f"CEIPAL token not found in response: {data}")

    access_token = token
    token_expiry = datetime.utcnow() + timedelta(hours=23)
    return access_token


def call_ceipal_get(path, params=None):
    token = get_access_token()

    response = requests.get(
        f"{CEIPAL_BASE_URL}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        params=params,
        timeout=60,
    )

    if response.status_code == 401:
        token = get_access_token(force_refresh=True)
        response = requests.get(
            f"{CEIPAL_BASE_URL}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            params=params,
            timeout=60,
        )

    if response.status_code != 200:
        raise RuntimeError(f"CEIPAL GET failed: {response.status_code} - {response.text[:500]}")

    return response.json()


# ============================================================
# GOOGLE DRIVE
# ============================================================
def get_drive_service():
    global _drive_service

    if _drive_service:
        return _drive_service

    credentials = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/drive"],
    )

    _drive_service = build("drive", "v3", credentials=credentials)
    return _drive_service


def upload_bytes_to_drive(file_bytes, file_name, mime_type):
    service = get_drive_service()

    media = MediaIoBaseUpload(
        io.BytesIO(file_bytes),
        mimetype=mime_type,
        resumable=False,
    )

    file_metadata = {
        "name": file_name,
        "parents": [GOOGLE_DRIVE_FOLDER_ID],
    }

    created = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webViewLink, webContentLink",
    ).execute()

    file_id = created["id"]

    if DRIVE_ANYONE_PERMISSION:
        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()

    file_info = service.files().get(
        fileId=file_id,
        fields="id, webViewLink, webContentLink",
    ).execute()

    return {
        "file_id": file_info["id"],
        "view_url": file_info.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view",
        "download_url": file_info.get("webContentLink") or f"https://drive.google.com/uc?id={file_id}&export=download",
    }


# ============================================================
# RESUME DOWNLOAD + FILE DETECTION
# ============================================================
def detect_resume_type(file_bytes, content_type=""):
    content_type = (content_type or "").lower()

    if file_bytes.startswith(b"%PDF") or "pdf" in content_type:
        return "pdf", "application/pdf"

    if file_bytes.startswith(b"PK") or "wordprocessingml.document" in content_type or "docx" in content_type:
        return "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    if "msword" in content_type:
        return "doc", "application/msword"

    guessed = mimetypes.guess_extension(content_type.split(";")[0].strip()) if content_type else None
    extension = guessed.replace(".", "") if guessed else "bin"
    return extension, content_type or "application/octet-stream"


def download_resume_by_token(resume_token):
    token = get_access_token()

    response = requests.post(
        f"{CEIPAL_BASE_URL}{CEIPAL_DOCUMENT_DOWNLOAD_ENDPOINT}",
        json={"resumeToken": resume_token},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
        },
        timeout=90,
    )

    if response.status_code == 401:
        token = get_access_token(force_refresh=True)
        response = requests.post(
            f"{CEIPAL_BASE_URL}{CEIPAL_DOCUMENT_DOWNLOAD_ENDPOINT}",
            json={"resumeToken": resume_token},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "*/*",
            },
            timeout=90,
        )

    if response.status_code == 410:
        raise RuntimeError("CEIPAL resume_token expired. Fetch fresh candidate data and try again.")

    if response.status_code != 200:
        raise RuntimeError(f"CEIPAL resume download failed: {response.status_code} - {response.text[:500]}")

    file_bytes = response.content
    if not file_bytes:
        raise RuntimeError("CEIPAL returned empty resume file.")

    content_type = response.headers.get("Content-Type", "")
    if "json" in content_type.lower():
        try:
            json_body = response.json()
            raise RuntimeError(f"CEIPAL returned JSON instead of file: {json_body}")
        except ValueError:
            pass

    file_type, mime_type = detect_resume_type(file_bytes, content_type)
    return file_bytes, file_type, mime_type


# ============================================================
# TEXT EXTRACTION
# ============================================================
def extract_phone_number(text):
    phone_pattern = r'(?:(?:\+?\d{1,3}\s?)?(?:\(\d{1,4}\)\s?)?|(?:\+?\d{1,3}\s)?\d{1,4}[\s./-]?)?\(?(?:\d{2,3})\)?[\s./-]?\d{1,5}[\s./-]?\d{1,5}(?:[\s./-]?\d{1,5})?(?:[\s./-]?\d{1,5})?'
    phone_matches = re.findall(phone_pattern, text or "")
    cleaned = [re.sub(r"\D", "", num) for num in phone_matches]
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
                text = pytesseract.image_to_string(pil_image, config="--psm 1")
                ocr_text.append(text)
            full_text = "\n".join(ocr_text)

    return full_text


def extract_resume_text(file_bytes, file_type):
    if file_type == "pdf":
        return extract_text_from_pdf_bytes(file_bytes)

    if file_type == "docx":
        return extract_text_from_docx_bytes(file_bytes)

    raise RuntimeError(f"Unsupported parsing format: {file_type}")


# ============================================================
# DATA NORMALIZATION
# ============================================================
def normalize_applicant(app):
    email = app.get("email_address") or app.get("email")
    job = app.get("job_title") or app.get("position")
    first_name = app.get("first_name")
    last_name = app.get("last_name")
    full_name = app.get("full_name") or f"{first_name or ''} {last_name or ''}".strip()

    resume_token = (
        app.get("resume_token")
        or app.get("resumeToken")
        or app.get("raw", {}).get("resume_token")
        or app.get("raw", {}).get("resumeToken")
    )

    return {
        "id": app.get("id") or app.get("applicant_id"),
        "first_name": first_name,
        "middle_name": app.get("middle_name"),
        "last_name": last_name,
        "email_address": email,
        "mobile_number": app.get("mobile_number") or app.get("phone"),
        "linkedin_profile_url": app.get("linkedin_profile_url") or app.get("linkedin"),
        "job_title": job,
        "location": app.get("location") or app.get("city"),
        "city": app.get("city"),
        "state": app.get("state"),
        "country": app.get("country"),
        "full_name": full_name,
        "resume_token": resume_token,
        "old_ceipal_resume_url": app.get("resume_path") or app.get("resume_url"),
        "api_created_at": app.get("created_on"),
        "api_modified_at": app.get("modified_date"),
        "raw": app,
        "updated_at": datetime.utcnow(),
    }


def safe_file_name(value):
    value = value or "resume"
    value = value.strip().replace(" ", "_")
    value = re.sub(r"[^A-Za-z0-9_\-.]", "", value)
    return value[:120] or "resume"


def get_candidate_ref_id(candidate):
    return str(candidate.get("id") or candidate.get("applicant_id") or candidate.get("_id"))


# ============================================================
# CEIPAL CANDIDATE SYNC
# ============================================================
def get_starting_page():
    state = sync_col.find_one({"name": "ceipal_candidate_sync"})
    if state and state.get("last_success_page"):
        return max(1, int(state["last_success_page"]) - 5)

    actual_count = candidates_col.count_documents({})
    rounded_count = (actual_count // 1000) * 1000
    page = int(rounded_count / PAGE_SIZE) if rounded_count else 1
    return max(1, page)


def sync_candidates_from_ceipal():
    print(f"\n[{datetime.utcnow()}] Candidate sync started")

    page = get_starting_page()
    print(f"Starting from page {page}")

    total_processed = 0
    last_success_page = page

    while True:
        data = call_ceipal_get(ENDPOINT, {"page": page, "paging_length": PAGE_SIZE})

        applicants = []
        if isinstance(data, list):
            applicants = data
        elif isinstance(data, dict):
            applicants = data.get("data") or data.get("applicants") or data.get("results") or data.get("records") or []

        if not applicants:
            print(f"No more applicants found. Stopped at page {page}.")
            break

        ops = []
        for app in applicants:
            candidate = normalize_applicant(app)

            email = candidate.get("email_address")
            job = candidate.get("job_title")

            if not email:
                print(f"Skipping applicant with no email: {candidate.get('first_name')}")
                continue

            set_data = {
                **candidate,
                "resume_download_status": "pending",
                "parsed_status": "pending",
            }

            ops.append(
                UpdateOne(
                    {"email_address": email, "job_title": job},
                    {
                        "$set": set_data,
                        "$setOnInsert": {
                            "status": "applied",
                            "created_at": datetime.utcnow(),
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
            )

        if ops:
            result = candidates_col.bulk_write(ops, ordered=False)
            total_processed += result.upserted_count + result.modified_count

        sync_col.update_one(
            {"name": "ceipal_candidate_sync"},
            {"$set": {"last_success_page": page, "last_run_at": datetime.utcnow(), "last_status": "running"}},
            upsert=True,
        )

        if page % 10 == 0:
            print(f"Page {page} processed. Total handled: {total_processed}")

        last_success_page = page
        page += 1
        time.sleep(0.2)

    sync_col.update_one(
        {"name": "ceipal_candidate_sync"},
        {"$set": {"last_success_page": last_success_page, "last_completed_at": datetime.utcnow(), "last_status": "completed", "last_total_processed": total_processed}},
        upsert=True,
    )

    print(f"Candidate sync complete. Total processed: {total_processed}")


# ============================================================
# RESUME DRIVE UPLOAD + PARSE
# ============================================================
def save_parsed_resume(candidate, resume_text):
    candidate_id = get_candidate_ref_id(candidate)

    extracted_phone = extract_phone_number(resume_text)
    extracted_emails = extract_emails(resume_text)

    candidate_phone = candidate.get("mobile_number") or candidate.get("phone")
    candidate_email = candidate.get("email_address")

    phone = candidate_phone if candidate_phone else extracted_phone
    email = candidate_email if candidate_email else (extracted_emails[0] if extracted_emails else None)

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
        "job_title": candidate.get("job_title"),
        "location": candidate.get("location"),
        "full_name": candidate.get("full_name"),
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


def process_one_candidate_resume(candidate):
    candidate_mongo_id = candidate.get("_id")
    candidate_id = get_candidate_ref_id(candidate)

    resume_token = candidate.get("resume_token") or candidate.get("resumeToken")
    if not resume_token:
        candidates_col.update_one(
            {"_id": candidate_mongo_id},
            {"$set": {"resume_download_status": "missing_token", "resume_download_error": "No resume_token found", "resume_last_attempt_at": datetime.utcnow()}},
        )
        print(f"{candidate_id}: no resume_token")
        return False

    print(f"Processing resume for candidate {candidate_id}")

    try:
        file_bytes, file_type, mime_type = download_resume_by_token(resume_token)

        full_name = candidate.get("full_name") or f"{candidate.get('first_name') or ''} {candidate.get('last_name') or ''}".strip()
        file_name = f"{safe_file_name(full_name)}_{safe_file_name(candidate_id)}.{file_type}"

        drive_result = upload_bytes_to_drive(file_bytes=file_bytes, file_name=file_name, mime_type=mime_type)

        resume_fields = {
            "resume_url": drive_result["view_url"],
            "resume_drive_url": drive_result["view_url"],
            "resume_download_url": drive_result["download_url"],
            "resume_drive_file_id": drive_result["file_id"],
            "resume_file_type": file_type,
            "resume_download_status": "uploaded",
            "resume_download_error": None,
            "resume_uploaded_at": datetime.utcnow(),
            "resume_last_attempt_at": datetime.utcnow(),
        }

        candidates_col.update_one({"_id": candidate_mongo_id}, {"$set": resume_fields})
        candidate.update(resume_fields)

        resume_text = extract_resume_text(file_bytes, file_type)
        if not resume_text.strip():
            raise RuntimeError("Resume text extraction returned empty text.")

        save_parsed_resume(candidate, resume_text)

        candidates_col.update_one(
            {"_id": candidate_mongo_id},
            {"$set": {"parsed_status": "parsed", "parsed_error": None, "parsed_at": datetime.utcnow()}},
        )

        print(f"{candidate_id}: uploaded + parsed successfully")
        return True

    except Exception as exc:
        error_text = str(exc)
        print(f"{candidate_id}: failed - {error_text}")

        candidates_col.update_one(
            {"_id": candidate_mongo_id},
            {
                "$set": {
                    "resume_download_status": "failed",
                    "resume_download_error": error_text[:1000],
                    "resume_last_attempt_at": datetime.utcnow(),
                    "parsed_status": "failed",
                    "parsed_error": error_text[:1000],
                }
            },
        )
        return False


def process_pending_resumes(limit=100):
    print(f"\n[{datetime.utcnow()}] Resume upload/parse worker started")

    query = {
        "$and": [
            {"resume_token": {"$exists": True, "$ne": None, "$ne": ""}},
            {
                "$or": [
                    {"resume_download_status": {"$exists": False}},
                    {"resume_download_status": {"$in": ["pending", "failed"]}},
                    {"resume_url": {"$exists": False}},
                    {"resume_url": None},
                    {"parsed_status": {"$in": ["pending", "failed"]}},
                ]
            },
        ]
    }

    candidates = list(candidates_col.find(query).limit(limit))

    if not candidates:
        print("No pending resume candidates found.")
        return

    success_count = 0
    fail_count = 0

    for candidate in candidates:
        ok = process_one_candidate_resume(candidate)
        if ok:
            success_count += 1
        else:
            fail_count += 1
        time.sleep(0.5)

    print(f"Resume worker complete. Success: {success_count}, Failed: {fail_count}")


# ============================================================
# MASTER JOB
# ============================================================
def run_full_job():
    try:
        sync_candidates_from_ceipal()
    except Exception as exc:
        print(f"Candidate sync failed: {exc}")

    try:
        process_pending_resumes(limit=RESUME_BATCH_LIMIT)
    except Exception as exc:
        print(f"Resume worker failed: {exc}")


if __name__ == "__main__":
    run_full_job()

    print(f"Scheduler started. Running every {SCHEDULE_HOURS} hours.")
    schedule.every(SCHEDULE_HOURS).hours.do(run_full_job)

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
