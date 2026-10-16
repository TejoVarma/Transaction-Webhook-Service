from datetime import datetime, timezone

from fastapi import FastAPI

app = FastAPI(title="Transaction Webhook Service")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@app.get("/")
async def health_check():
    return {"status": "HEALTHY", "current_time": utc_now_iso()}
