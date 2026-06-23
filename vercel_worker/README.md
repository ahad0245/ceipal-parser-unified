# CEIPAL Vercel worker

This folder contains the Vercel-only wrapper for the existing `ceipal_inline_resume_worker.py` flow.

## What it does

- Runs the same CEIPAL -> MongoDB -> Apps Script upload -> parse flow
- Triggers on Vercel Cron once per day on Hobby (`08:00 UTC`)
- Retries the worker automatically inside the same invocation
- Sends a failure email to `abdulahad@i8is.com` after repeated failures

## Files

- `api/index.py` - FastAPI entrypoint for Vercel
- `vercel_worker/service.py` - retry, state tracking, and alert logic
- `vercel.json` - cron schedule and function timeout
- `requirements.txt` - Python dependencies for Vercel

## Required environment variables

Reuse the existing worker env vars:

- `MONGODB_URI` or `MONGO_URI`
- `DB_NAME`
- `COLLECTION_NAME`
- `PARSED_COLLECTION`
- `SYNC_COLLECTION`
- `CEIPAL_BASE_URL`
- `CEIPAL_REFRESH_ENDPOINT`
- `CEIPAL_EMAIL`
- `CEIPAL_PASSWORD`
- `CEIPAL_API_KEY`
- `CEIPAL_CUSTOM_APPLICANT_ENDPOINT`
- `APPS_SCRIPT_UPLOAD_URL`
- `APPS_SCRIPT_SECRET`
- `PAGE_SIZE`
- `START_PAGE`
- `END_PAGE`
- `PROCESS_ALREADY_UPLOADED`
- `RETRY_FAILED_RESUMES`
- `SCHEDULE_MAX_PAGES`

Vercel wrapper env vars:

- `VERCEL_MAX_PAGES` - pages per cron run, default `SCHEDULE_MAX_PAGES` or `3`
- `VERCEL_MAX_RESTART_ATTEMPTS` - default `3`
- `VERCEL_RESTART_DELAY_SECONDS` - default `15`
- `VERCEL_ALERT_COOLDOWN_MINUTES` - default `120`
- `VERCEL_CRON_SECRET` - for manual `/cron` or `/status` access
- `ALERT_TO_EMAIL` - default `abdulahad@i8is.com`

SMTP env vars for alerts:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_FROM_EMAIL`

Also supported for compatibility with older env naming:

- `SMTP_PASS`
- `MAIL_FROM`
- `smtpuserName`
- `smtpPassword`

For port `465`, the worker uses SSL automatically. For port `587`, it uses STARTTLS.

## Important limitation

`pytesseract` needs a system Tesseract binary. Vercel usually does not provide that binary. PDF text extraction still works through `pdfplumber`, but OCR fallback may fail unless you move OCR to an external service.

## Vercel Hobby note

Vercel Hobby only allows one cron execution per day. The current `vercel.json` is set to run daily at `08:00 UTC`. If you need every 8 hours, either upgrade to Pro or remove Vercel Cron and use an external scheduler to call `/cron`.
