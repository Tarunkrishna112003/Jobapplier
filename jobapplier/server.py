"""Local web UI.

Start it with `./web.sh`, open http://127.0.0.1:8765, press Start. The run
proceeds one company at a time and parks on each filled application until you
approve or skip it.

Binds to loopback only - this drives a browser holding your real logins and
fills forms with your personal data, so it must not be reachable from the
network.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .cli import load_cfg, load_profile
from .db import Tracker
from .runner import Runner

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

cfg = load_cfg(str(ROOT / "config.yaml"))
profile = load_profile(cfg)
runner = Runner(cfg, profile)

app = FastAPI(title="Jobapplier", docs_url=None, redoc_url=None)


class StartRequest(BaseModel):
    company: str = ""
    max_jobs_per_company: int = 3
    min_score: float = 0.0
    dry_run: bool = False


class DecisionRequest(BaseModel):
    key: str
    action: str          # "submit" | "skip"


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/state")
def state():
    snap = runner.snapshot()
    with Tracker(cfg["storage"]["db"]) as t:
        snap["counts"] = t.counts()
    return JSONResponse(snap)


@app.post("/api/start")
def start(req: StartRequest):
    ok = runner.start(
        companies_filter=req.company,
        max_jobs_per_company=max(1, min(req.max_jobs_per_company, 10)),
        min_score=req.min_score,
        dry_run=req.dry_run,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="A run is already in progress.")
    return {"ok": True}


@app.post("/api/stop")
def stop():
    runner.stop()
    return {"ok": True}


@app.post("/api/decision")
def decision(req: DecisionRequest):
    if req.action not in ("submit", "skip"):
        raise HTTPException(status_code=400, detail="action must be 'submit' or 'skip'")
    if not runner.decide(req.key, req.action):
        raise HTTPException(status_code=409, detail="No application is waiting on that key.")
    return {"ok": True}


@app.get("/api/screenshot")
def screenshot(path: str):
    """Serve a screenshot, but only from inside the configured directory."""
    shots = Path(cfg["storage"]["screenshots"]).resolve()
    target = Path(path).resolve()
    if not str(target).startswith(str(shots)) or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)


@app.get("/api/applications")
def applications(limit: int = 200):
    with Tracker(cfg["storage"]["db"]) as t:
        rows = t.all_jobs()[:limit]
        return [
            {
                "key": r["key"], "company": r["company"], "title": r["title"],
                "location": r["location"], "score": r["score"], "status": r["status"],
                "note": r["status_note"], "url": r["url"], "updated_at": r["updated_at"],
            }
            for r in rows
        ]


def main():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    main()
