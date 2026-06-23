import argparse
import base64
import os
import re
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient


load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "recruitment_db")
PARSED_COLLECTION = os.getenv("PARSED_COLLECTION", "parsed_resumes")
CANDIDATE_COLLECTION = os.getenv("COLLECTION_NAME", "ceipal_applicant_details")

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


def now_utc():
    return datetime.now(timezone.utc)


def safe_file_name(value):
    value = str(value or "resume").strip().replace(" ", "_")
    value = re.sub(r"[^A-Za-z0-9_\-.]", "", value)
    return value[:120] or "resume"


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


def normalize_pdf_text(value):
    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    return text.strip()


def escape_pdf_text(value):
    text = str(value or "")
    text = text.encode("cp1252", errors="replace").decode("latin1")
    text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return text


SECTION_HEADINGS = {
    "PROFESSIONALSUMMARY": "PROFESSIONAL SUMMARY",
    "CAREERSUMMARY": "CAREER SUMMARY",
    "SUMMARY": "SUMMARY",
    "CORECOMPETENCIES": "CORE COMPETENCIES",
    "TECHNICALSKILLS": "TECHNICAL SKILLS",
    "SKILLS": "SKILLS",
    "PROFESSIONALEXPERIENCE": "PROFESSIONAL EXPERIENCE",
    "WORKEXPERIENCE": "WORK EXPERIENCE",
    "EXPERIENCE": "EXPERIENCE",
    "PROJECTS": "PROJECTS",
    "TECHNICALPROJECTS": "TECHNICAL PROJECTS",
    "EDUCATION": "EDUCATION",
    "CERTIFICATIONS": "CERTIFICATIONS",
    "LANGUAGES": "LANGUAGES",
}


def compact_key(value):
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def is_same_name(line, full_name):
    if not full_name:
        return False
    return compact_key(line) == compact_key(full_name)


def normalize_resume_line(value):
    line = re.sub(r"\s+", " ", str(value or "")).strip()
    line = line.replace("", "-").replace("●", "-").replace("•", "-")
    return line


def section_heading(line):
    key = compact_key(line)
    if key in SECTION_HEADINGS:
        return SECTION_HEADINGS[key]
    letters = re.sub(r"[^A-Za-z]", "", line)
    if 3 <= len(line) <= 70 and letters and line.upper() == line and len(line.split()) <= 7:
        return line
    return None


def build_resume_items(text, full_name=None):
    items = []
    first_content_seen = False
    for raw_line in normalize_pdf_text(text).split("\n"):
        line = normalize_resume_line(raw_line)
        if not line:
            if items and items[-1]["style"] != "blank":
                items.append({"style": "blank", "text": ""})
            continue

        if not first_content_seen:
            first_content_seen = True
            if is_same_name(line, full_name):
                continue

        heading = section_heading(line)
        if heading:
            if items and items[-1]["style"] != "blank":
                items.append({"style": "blank", "text": ""})
            items.append({"style": "heading", "text": heading})
            continue

        if re.match(r"^[-*]\s+", line):
            items.append({"style": "bullet", "text": re.sub(r"^[-*]\s+", "", line)})
        else:
            items.append({"style": "body", "text": line})
    return items


def approximate_text_width(text, font_size):
    return len(str(text or "")) * font_size * 0.48


def wrap_for_width(text, font_size, x_position, page_width=612, right_margin=50):
    available_width = page_width - right_margin - x_position
    max_chars = max(28, int(available_width / (font_size * 0.48)))
    return textwrap.wrap(
        str(text or ""),
        width=max_chars,
        break_long_words=False,
        replace_whitespace=False,
    ) or [""]


def render_pages(items):
    pages = []
    page = []
    page_index = 1
    y_position = 704
    bottom_margin = 48

    for item in items:
        style = item["style"]
        if style == "blank":
            before, after, line_height, font, font_size, x_position = 4, 2, 10, "F1", 9, 54
            wrapped_lines = [""]
        elif style == "heading":
            before, after, line_height, font, font_size, x_position = 9, 4, 13, "F2", 10, 50
            wrapped_lines = wrap_for_width(item["text"], font_size, x_position)
        elif style == "bullet":
            before, after, line_height, font, font_size, x_position = 2, 1, 11, "F1", 9, 68
            wrapped_lines = wrap_for_width("- " + item["text"], font_size, x_position)
        else:
            before, after, line_height, font, font_size, x_position = 2, 1, 11, "F1", 9, 54
            wrapped_lines = wrap_for_width(item["text"], font_size, x_position)

        needed_height = before + (len(wrapped_lines) * line_height) + after
        if y_position - needed_height < bottom_margin:
            pages.append(page)
            page = []
            page_index += 1
            y_position = 730

        y_position -= before
        for wrapped_line in wrapped_lines:
            if wrapped_line:
                page.append(
                    {
                        "font": font,
                        "font_size": font_size,
                        "x": x_position,
                        "y": y_position,
                        "text": wrapped_line,
                    }
                )
            y_position -= line_height
        y_position -= after

    if page or not pages:
        pages.append(page)
    return pages


def pdf_object(data):
    if isinstance(data, str):
        return data.encode("latin1", errors="replace")
    return data


def make_pdf_from_resume_text(resume_text, full_name=None, email=None, phone=None):
    title = (full_name or "Resume").strip() or "Resume"
    subtitle_parts = [part for part in [email, phone] if part]
    subtitle = " | ".join(subtitle_parts)
    pages = render_pages(build_resume_items(resume_text, full_name=full_name))

    objects = []
    objects.append(None)
    objects.append(None)
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    page_ids = []
    for page_index, page_lines in enumerate(pages, start=1):
        commands = []
        if page_index == 1:
            title_size = 17
            title_x = max(50, 306 - approximate_text_width(title, title_size) / 2)
            commands.append("BT")
            commands.append(f"/F2 {title_size} Tf")
            commands.append(f"{title_x:.2f} 758 Td")
            commands.append(f"({escape_pdf_text(title)}) Tj")
            commands.append("ET")
            if subtitle:
                subtitle_size = 9
                subtitle_x = max(50, 306 - approximate_text_width(subtitle, subtitle_size) / 2)
                commands.append("BT")
                commands.append(f"/F1 {subtitle_size} Tf")
                commands.append(f"{subtitle_x:.2f} 738 Td")
                commands.append(f"({escape_pdf_text(subtitle)}) Tj")
                commands.append("ET")
            commands.append("50 724 m 562 724 l S")
        else:
            commands.append("BT")
            commands.append("/F2 9 Tf")
            commands.append("50 762 Td")
            commands.append(f"({escape_pdf_text(title)}) Tj")
            commands.append("ET")
            commands.append("50 748 m 562 748 l S")

        for line in page_lines:
            commands.append("BT")
            commands.append(f"/{line['font']} {line['font_size']} Tf")
            commands.append(f"{line['x']:.2f} {line['y']:.2f} Td")
            commands.append(f"({escape_pdf_text(line['text'])}) Tj")
            commands.append("ET")

        footer = f"Page {page_index}"
        commands.append("BT")
        commands.append("/F1 8 Tf")
        commands.append("540 28 Td")
        commands.append(f"({escape_pdf_text(footer)}) Tj")
        commands.append("ET")
        content = "\n".join(commands).encode("latin1", errors="replace")
        content_object = b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream"
        content_id = len(objects) + 1
        objects.append(content_object)
        page_id = len(objects) + 1
        page_ids.append(page_id)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        )

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[0] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"

    output = bytearray()
    output.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(pdf_object(obj))
        output.extend(b"\nendobj\n")

    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


def upload_pdf_to_drive(pdf_bytes, file_name, max_retries=3):
    payload = {
        "secret": APPS_SCRIPT_SECRET,
        "fileName": file_name,
        "mimeType": "application/pdf",
        "base64File": base64.b64encode(pdf_bytes).decode("utf-8"),
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


def build_query(failed_only=True, only_id=None):
    query = {
        "data": {"$exists": False},
        "resume_text": {"$exists": True, "$nin": [None, ""]},
    }
    if failed_only:
        query["migration_status"] = "failed"
    if only_id:
        query["_id"] = ObjectId(only_id)
    return query


def fetch_old_doc_batch(base_query, last_id=None, batch_size=25):
    query = dict(base_query)
    if last_id:
        query["_id"] = {"$gt": last_id}
    return list(parsed_col.find(query).sort("_id", 1).limit(batch_size))


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
        "migration_error",
    ]
    return {field: "" for field in fields}


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
    return {
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


def migrate_one_text_pdf(old_doc, dry_run=False):
    doc_id = old_doc["_id"]
    candidate_id = old_doc.get("candidate_id")
    resume_text = old_doc.get("resume_text")
    if not candidate_id:
        raise RuntimeError("old parsed doc missing candidate_id")
    if not normalize_pdf_text(resume_text):
        raise RuntimeError("old parsed doc missing resume_text")

    full_name = old_doc.get("full_name") or f"{old_doc.get('first_name') or ''} {old_doc.get('last_name') or ''}".strip()
    email = old_doc.get("primary_email") or old_doc.get("email_address")
    phone = old_doc.get("phone")
    file_name = f"{safe_file_name(full_name)}_{safe_file_name(candidate_id)}_generated.pdf"

    if dry_run:
        print(f"{doc_id}: would generate PDF from resume_text and upload {file_name}")
        return True

    print(f"{doc_id}: generating PDF from resume_text")
    pdf_bytes = make_pdf_from_resume_text(resume_text, full_name=full_name, email=email, phone=phone)

    print(f"{doc_id}: uploading generated PDF to Drive")
    drive_result = upload_pdf_to_drive(pdf_bytes, file_name)
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
                "migration_status": "text_pdf_generated",
                "migration_source": "resume_text",
            },
            "$unset": top_level_fields_to_unset(),
        },
    )

    candidate_update_data = {
        "resume_url": drive_result["view_url"],
        "resume_drive_url": drive_result["view_url"],
        "resume_download_url": drive_result["download_url"],
        "resume_drive_file_id": drive_result["file_id"],
        "resume_file_type": "pdf",
        "resume_download_status": "uploaded",
        "resume_download_error": None,
        "resume_uploaded_at": updated_at,
        "resume_last_attempt_at": updated_at,
        "parsed_status": "parsed",
        "parsed_error": None,
        "parsed_at": updated_at,
        "updated_at": updated_at,
    }
    candidate_insert_data = build_candidate_insert_data(old_doc)

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

    print(f"{doc_id}: generated PDF migrated successfully")
    return True


def mark_failed(old_doc, exc):
    parsed_col.update_one(
        {"_id": old_doc["_id"]},
        {
            "$set": {
                "text_pdf_status": "failed",
                "text_pdf_error": str(exc)[:1000],
                "text_pdf_last_attempt_at": now_utc(),
            }
        },
    )


def process_doc(old_doc, dry_run=False):
    try:
        ok = migrate_one_text_pdf(old_doc, dry_run=dry_run)
        return old_doc["_id"], ok, None
    except Exception as exc:
        if not dry_run:
            mark_failed(old_doc, exc)
        return old_doc["_id"], False, exc


def run_migration(limit=None, batch_size=25, workers=1, failed_only=True, only_id=None, dry_run=False):
    query = build_query(failed_only=failed_only, only_id=only_id)
    last_id = None
    processed = 0
    succeeded = 0
    failed = 0
    workers = max(1, workers)

    print(f"Using database: {DB_NAME}")
    print(f"Parsed collection: {PARSED_COLLECTION}")
    print(f"Candidate collection: {CANDIDATE_COLLECTION}")
    print(f"Failed only: {failed_only}")
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
                doc_id, ok, exc = process_doc(old_doc, dry_run=dry_run)
                if ok:
                    succeeded += 1
                else:
                    failed += 1
                    if exc:
                        print(f"{doc_id}: failed - {str(exc)[:1000]}")
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(process_doc, old_doc, dry_run) for old_doc in old_docs]
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
    parser = argparse.ArgumentParser(description="Generate Drive PDFs from old parsed resume_text records.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum old docs to process")
    parser.add_argument("--batch-size", type=int, default=25, help="Mongo docs to fetch per short batch")
    parser.add_argument("--workers", type=int, default=1, help="Parallel records to process at once")
    parser.add_argument("--all-old-text", action="store_true", help="Process all old text docs, not only migration_status=failed")
    parser.add_argument("--only-id", default=None, help="Process one parsed_resumes _id")
    parser.add_argument("--dry-run", action="store_true", help="Show matching docs without generating/uploading/updating")
    args = parser.parse_args()

    run_migration(
        limit=args.limit,
        batch_size=args.batch_size,
        workers=args.workers,
        failed_only=not args.all_old_text,
        only_id=args.only_id,
        dry_run=args.dry_run,
    )
