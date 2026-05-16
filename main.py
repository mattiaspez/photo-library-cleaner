from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import asyncio
import uuid
import subprocess
import threading
import time
import signal
import os
import hashlib
import tempfile
from pathlib import Path
from io import BytesIO

from scanner import build_report
from PIL import Image
from send2trash import send2trash

_THUMB_CACHE = Path(tempfile.gettempdir()) / "photo_cleaner_thumbs"
_THUMB_CACHE.mkdir(exist_ok=True)

app = FastAPI()

_browser_connected = False
_IDLE_TIMEOUT = 300  # 5-minute fallback if beacon never fires


@app.on_event("startup")
async def _start_idle_watcher():
    def watch():
        for _ in range(60):
            if _browser_connected:
                break
            time.sleep(1)
        else:
            return  # browser never connected — leave server running
        # Browser connected; exit if idle too long with no active scan
        idle_since = time.time()
        while True:
            time.sleep(5)
            if _browser_connected:
                idle_since = time.time()
            if time.time() - idle_since > _IDLE_TIMEOUT:
                if not any(j.get('status') == 'running' for j in _jobs.values()):
                    os._exit(0)
    threading.Thread(target=watch, daemon=True).start()


@app.post("/heartbeat")
async def heartbeat():
    global _browser_connected
    _browser_connected = True
    return {"ok": True}


@app.post("/shutdown")
async def shutdown():
    threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
    return {"ok": True}

_jobs: dict = {}
_cancelled: set = set()

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".tif", ".bmp", ".heic"}


class ScanRequest(BaseModel):
    path: str


class DeleteRequest(BaseModel):
    paths: list[str]


class MoveEntry(BaseModel):
    path: str
    destination: str


class MoveRequest(BaseModel):
    moves: list[MoveEntry]


@app.get("/browse")
async def browse_folder():
    result = subprocess.run(
        ["osascript", "-e", 'POSIX path of (choose folder with prompt "Select your photo library")'],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"path": None}  # user cancelled
    return {"path": result.stdout.strip().rstrip("/")}


@app.post("/scan")
async def start_scan(req: ScanRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "running"}
    background_tasks.add_task(_run_scan, job_id, req.path)
    return {"job_id": job_id}


def _run_scan(job_id: str, path: str):
    progress_state = {}
    _jobs[job_id] = {"status": "running", "progress": progress_state}
    try:
        report = build_report(path, progress_state=progress_state, is_cancelled=lambda: job_id in _cancelled)
        _jobs[job_id] = {"status": "done", "result": report}
    except InterruptedError:
        _jobs[job_id] = {"status": "cancelled"}
    except Exception as exc:
        _jobs[job_id] = {"status": "error", "error": str(exc)}
    finally:
        _cancelled.discard(job_id)


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    _cancelled.add(job_id)
    return {"cancelled": True}


@app.post("/delete")
async def delete_files(req: DeleteRequest):
    deleted = []
    errors = []
    for path_str in req.paths:
        try:
            send2trash(path_str)
            deleted.append(path_str)
        except Exception as exc:
            errors.append({"path": path_str, "error": str(exc)})
    return {"deleted": deleted, "errors": errors}


@app.post("/move")
async def move_files(req: MoveRequest):
    moved = []
    errors = []
    for entry in req.moves:
        try:
            src = Path(entry.path)
            dst_dir = Path(entry.destination)
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / src.name
            if dst.exists():
                stem, suffix, counter = src.stem, src.suffix, 1
                while dst.exists():
                    dst = dst_dir / f"{stem}_{counter}{suffix}"
                    counter += 1
            src.rename(dst)
            moved.append({"from": entry.path, "to": str(dst)})
        except Exception as exc:
            errors.append({"path": entry.path, "error": str(exc)})
    return {"moved": moved, "errors": errors}


VIDEO_MIME = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".3gp": "video/3gpp",
    ".wmv": "video/x-ms-wmv",
}


@app.get("/stream")
async def stream_file(path: str = Query(...)):
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404)
    media_type = VIDEO_MIME.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(str(p), media_type=media_type)


def _make_thumbnail(p, size):
    st = p.stat()
    key = hashlib.md5(f"{p}:{size}:{st.st_mtime}:{st.st_size}".encode()).hexdigest()
    cached = _THUMB_CACHE / f"{key}.jpg"
    if cached.exists():
        return cached.read_bytes(), "JPEG"

    with Image.open(p) as img:
        if p.suffix.lower() in (".jpg", ".jpeg"):
            try:
                img.draft("RGB", (size * 2, size * 2))
            except Exception:
                pass
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((size, size), Image.BILINEAR)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        data = buf.getvalue()

    cached.write_bytes(data)
    return data, "JPEG"


@app.get("/thumbnail")
async def thumbnail(path: str = Query(...), size: int = 200):
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404)
    if p.suffix.lower() not in IMAGE_EXTS:
        raise HTTPException(400, "Not an image")
    try:
        loop = asyncio.get_running_loop()
        data, _ = await loop.run_in_executor(None, _make_thumbnail, p, size)
        return StreamingResponse(BytesIO(data), media_type="image/jpeg")
    except Exception as exc:
        print(f"[thumbnail error] {p}: {exc}")
        raise HTTPException(400, f"Cannot open image: {exc}")


@app.get("/")
async def index():
    return FileResponse("index.html")
