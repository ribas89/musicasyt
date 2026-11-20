from __future__ import annotations

import hmac
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.common import (
    playlist_id_from_url,
    safe_child,
    user_library,
    validate_playlist_id,
    validate_username,
)
from app.metadata import delete_entry, patch, read
from app.updater import PackageUpdater
from app.logging_config import configure_logging

configure_logging()
logger = logging.getLogger("musicasyt.api")

DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data"))
USERS_ROOT = Path(os.environ.get("USERS_ROOT", "/users"))
updater = PackageUpdater(DATA_ROOT)

@asynccontextmanager
async def lifespan(_application: FastAPI):
    if not os.environ.get("MUSICASYT_API_KEY"):
        raise RuntimeError("MUSICASYT_API_KEY must be set to a nonempty secret")
    threading.Thread(target=updater.boot, daemon=True, name="git-updater").start()
    yield

app = FastAPI(title="MusicasYT", version="1.0.0", docs_url=None, redoc_url=None, lifespan=lifespan)

MAX_QUEUED_JOBS = int(os.environ.get("MAX_QUEUED_JOBS", "20"))
_jobs: dict[str, dict] = {}
_lock = threading.RLock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="music-download")

def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()

def serialize_job(job: dict) -> dict:
    return {k: v for k, v in job.items() if k not in {"_future"}}

def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    expected = os.environ.get("MUSICASYT_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Worker API key not configured")
    if x_api_key is None or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

class SyncRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    playlist_url: str = Field(min_length=10, max_length=2048)
    restart: bool = False

@app.get("/status")
def status():
    state = updater.snapshot()
    with _lock:
        job_statuses = [job["status"] for job in _jobs.values()]
    state["jobs_in_memory"] = len(job_statuses)
    state["active_jobs"] = sum(
        status in {"reading_playlist", "checking_files", "downloading"}
        for status in job_statuses
    )
    state["queued_jobs"] = job_statuses.count("queued")
    return state

@app.post("/sync", status_code=202, dependencies=[Depends(require_api_key)])
def sync(request: SyncRequest):
    try:
        username = validate_username(request.username)
        playlist_id = playlist_id_from_url(request.playlist_url)

        user_library(USERS_ROOT, username)
    except (ValueError, FileNotFoundError, PermissionError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not updater.snapshot()["ready"]:
        raise HTTPException(
            status_code=503,
            detail="Downloader not ready; check GET /status",
            headers={"Retry-After": "5"},
        )

    job_id = f"{username}:{playlist_id}"
    terminal_statuses = {"completed", "completed_with_errors", "error"}
    with _lock:
        existing_job = _jobs.get(job_id)
        if existing_job and (existing_job["status"] not in terminal_statuses or not request.restart):
            logger.debug("Returning existing sync job: %s (status=%s)", job_id, existing_job["status"])
            return {**serialize_job(existing_job), "existing_job": True}
        queued_job_count = sum(
            job["status"] in {"queued", "reading_playlist", "checking_files", "downloading"}
            for job in _jobs.values()
        )
        if queued_job_count >= MAX_QUEUED_JOBS:
            raise HTTPException(status_code=429, detail="Download queue is full")
        job = {
            "job_id": job_id,
            "username": username,
            "playlist_id": playlist_id,
            "status": "queued",
            "title": None,
            "folder": None,
            "total": None,
            "downloaded": 0,
            "existing": 0,
            "failed": 0,
            "unavailable": 0,
            "remaining": None,
            "percent": None,
            "current_track": None,
            "error": None,
            "created_at": utc_timestamp(),
            "updated_at": utc_timestamp(),
        }

        try:
            patch(
                user_library(USERS_ROOT, username),
                playlist_id,
                playlist_url=request.playlist_url,
                status="queued",
                error=None,
                downloaded=0,
                existing=0,
                failed=0,
                unavailable=0,
                remaining=None,
                percent=None,
                current_track=None,
                last_attempt_at=utc_timestamp(),
            )
        except (OSError, ValueError, PermissionError) as error:
            raise HTTPException(status_code=500, detail=f"Cannot save playlist metadata: {error}") from error
        _jobs[job_id] = job
        logger.info("Accepted sync request: user=%s playlist_id=%s", username, playlist_id)
        _executor.submit(run_job, job_id)
        return {**serialize_job(job), "existing_job": False}

@app.get("/jobs/{username}/{playlist_id}", dependencies=[Depends(require_api_key)])
def get_job(username: str, playlist_id: str):
    try:
        validate_username(username)
        validate_playlist_id(playlist_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    key = f"{username}:{playlist_id}"
    with _lock:
        job = _jobs.get(key)
        if not job:
            raise HTTPException(
                status_code=404,
                detail="Job not in memory; press Sync to start a new check",
            )
        return serialize_job(job)

@app.delete("/playlists/{username}/{playlist_id}", dependencies=[Depends(require_api_key)])
def remove_playlist(username: str, playlist_id: str):
    try:
        user_dir = user_library(USERS_ROOT, validate_username(username))
        validate_playlist_id(playlist_id)
        with _lock:
            job = _jobs.get(f"{username}:{playlist_id}")
            if job and job["status"] in {"queued", "reading_playlist", "checking_files", "downloading"}:
                raise HTTPException(
                    status_code=409,
                    detail="Wait for the active sync to finish before deleting",
                )
            metadata = read(user_dir)
            record = metadata["playlists"].get(playlist_id) or {}
            folder_name = record.get("folder_name")
            if not folder_name:
                for folder in user_dir.iterdir():
                    if not folder.is_dir() or folder.is_symlink():
                        continue
                    marker = folder / ".musicasyt.json"
                    if marker.is_file() and not marker.is_symlink():
                        try:
                            if json.loads(marker.read_text(encoding="utf-8")).get("playlist_id") == playlist_id:
                                folder_name = folder.name
                                break
                        except (OSError, ValueError):
                            pass
            if not folder_name and playlist_id not in metadata["playlists"]:
                raise HTTPException(status_code=404, detail="Playlist not found")
            if folder_name:
                if (
                    folder_name in {".", "..", ""}
                    or Path(folder_name).name != folder_name
                    or folder_name == "musicasyt.json"
                ):
                    raise ValueError("Unsafe playlist folder name")
                folder = safe_child(user_dir, folder_name)
                if folder.exists():
                    if not folder.is_dir() or folder.is_symlink():
                        raise PermissionError("Refusing unsafe playlist folder")
                    if not shutil.rmtree.avoids_symlink_attacks:
                        raise RuntimeError("Safe directory removal is not supported")
                    shutil.rmtree(folder)
            delete_entry(user_dir, playlist_id)
            _jobs.pop(f"{username}:{playlist_id}", None)
        return {"deleted": True, "playlist_id": playlist_id, "folder_deleted": bool(folder_name)}
    except HTTPException:
        raise
    except (ValueError, OSError, PermissionError, RuntimeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

def update_job(key: str, changes: dict):
    with _lock:
        job = _jobs.get(key)
        if not job:
            return
        job.update(changes)
        job["updated_at"] = utc_timestamp()
        total = job.get("total")
        if isinstance(total, int):
            processed = sum(job[k] for k in ("downloaded", "existing", "failed", "unavailable"))
            job["remaining"] = max(0, total - processed)
            job["percent"] = round(100 * processed / total, 1) if total else 100.0

def run_job(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        username, playlist_id = job["username"], job["playlist_id"]
    executable = updater.python_bin
    if not executable:
        logger.error("Cannot start job %s: downloader dependencies are unavailable", job_id)
        update_job(job_id, {"status": "error", "error": "Downloader dependencies unavailable"})
        return
    logger.info("Starting sync job: %s", job_id)
    command = [
        str(executable), "-u", "-m", "app.downloader",
        "--username", username, "--playlist-id", playlist_id,
        "--users-root", str(USERS_ROOT), "--data-dir", str(DATA_ROOT),
    ]
    update_job(job_id, {"status": "reading_playlist"})
    env = dict(os.environ)
    env["PYTHONPATH"] = "/app"
    logger.debug(
        "Launching downloader process for %s with LOG_LEVEL=%s; child stderr is forwarded to Docker logs",
        job_id,
        env.get("LOG_LEVEL", "INFO"),
    )
    saw_done = False
    try:
        process = subprocess.Popen(
            command,
            cwd="/app",
            env=env,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Downloader emitted an unexpected event for %s: %s", job_id, line.rstrip())
                continue
            kind = event.get("event")
            if kind == "state":
                new_status = event.get("status", "downloading")
                logger.info("Job %s state: %s", job_id, new_status)
                update_job(job_id, {"status": new_status})
            elif kind == "metadata":
                update_job(
                    job_id,
                    {"title": event.get("title"), "folder": event.get("folder"), "total": event.get("total")},
                )
            elif kind == "progress":
                logger.debug(
                    "Job %s progress: downloaded=%s existing=%s failed=%s unavailable=%s current=%s",
                    job_id, event.get("downloaded"), event.get("existing"), event.get("failed"),
                    event.get("unavailable"), event.get("current_track"),
                )
                update_job(
                    job_id,
                    {
                        key: event[key]
                        for key in ("downloaded", "existing", "failed", "unavailable", "current_track")
                        if key in event
                    },
                )
            elif kind == "track":
                logger.info("Job %s downloading track: %s", job_id, event.get("current_track"))
                update_job(job_id, {"current_track": event.get("current_track")})
            elif kind == "track_error":
                logger.warning("Job %s track failed: %s (%s)", job_id, event.get("current_track"), event.get("message"))
                update_job(job_id, {"last_track_error": event.get("message")})
            elif kind == "done":
                saw_done = True
                logger.info(
                    "Job %s finished: status=%s downloaded=%s existing=%s failed=%s unavailable=%s",
                    job_id, event.get("status", "completed"), event.get("downloaded"),
                    event.get("existing"), event.get("failed"), event.get("unavailable"),
                )
                update_job(
                    job_id,
                    {"status": event.get("status", "completed"), "current_track": None},
                )
            elif kind == "error":
                message = event.get("message", "Unknown downloader error")
                logger.error("Job %s failed: %s", job_id, message)
                update_job(
                    job_id,
                    {"status": "error", "error": message},
                )
        result = process.wait()
        if result and not saw_done:
            logger.error("Downloader process for %s exited with code %s", job_id, result)
            with _lock:
                errored = _jobs[job_id]["error"]
            update_job(
                job_id,
                {"status": "error", "error": errored or f"Downloader exited with code {result}"},
            )
        elif result == 0 and not saw_done:
            logger.error("Downloader process for %s exited without a completion event", job_id)
            update_job(job_id, {"status": "error", "error": "Downloader did not report completion"})
    except Exception as error:
        logger.exception("Unexpected error while running job %s", job_id)
        update_job(job_id, {"status": "error", "error": str(error)[:300]})
