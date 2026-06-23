import os

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from vercel_worker.service import get_state, run_once_with_restarts


app = FastAPI(title="CEIPAL Vercel Worker")


def authorize_request(user_agent: str | None, token: str | None) -> None:
    cron_secret = os.getenv("VERCEL_CRON_SECRET")
    cron_user_agent = (user_agent or "").lower()

    if "vercel-cron/1.0" in cron_user_agent:
        return

    if cron_secret and token == cron_secret:
        return

    raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/")
def root() -> JSONResponse:
    return JSONResponse(
        {
            "service": "ceipal-vercel-worker",
            "status": "ok",
            "cron_path": "/api/cron",
            "status_path": "/api/status",
        }
    )


@app.get("/cron")
def run_cron(
    token: str | None = Query(default=None),
    user_agent: str | None = Header(default=None),
) -> JSONResponse:
    authorize_request(user_agent=user_agent, token=token)
    result = run_once_with_restarts()
    status_code = 200 if result["ok"] else 500
    return JSONResponse(result, status_code=status_code)


@app.get("/status")
def status(
    token: str | None = Query(default=None),
    user_agent: str | None = Header(default=None),
) -> JSONResponse:
    authorize_request(user_agent=user_agent, token=token)
    return JSONResponse(get_state())
