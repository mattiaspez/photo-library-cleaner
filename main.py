from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import uuid
import subprocess
from pathlib import Path
from io import BytesIO

from scanner import build_report
from PIL import Image

app = FastAPI()

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
            Path(path_str).unlink()
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


@app.get("/thumbnail")
async def thumbnail(path: str = Query(...), size: int = 200):
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404)
    if p.suffix.lower() not in IMAGE_EXTS:
        raise HTTPException(400, "Not an image")
    try:
        with Image.open(p) as img:
            if img.mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGB")
            img.thumbnail((size, size), Image.LANCZOS)
            buf = BytesIO()
            fmt = "PNG" if img.mode == "RGBA" else "JPEG"
            img.save(buf, format=fmt)
            buf.seek(0)
            return StreamingResponse(buf, media_type=f"image/{fmt.lower()}")
    except Exception as exc:
        raise HTTPException(400, f"Cannot open image: {exc}")


@app.get("/")
async def index():
    return FileResponse("index.html")
