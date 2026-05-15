"""
Scans a photo library folder tree and categorizes files as:
- screenshots
- videos
- junk (burst extras, live photo MOV sidecars, small/corrupt files)
- photos (keepers)
"""

import os
import re
import json
from pathlib import Path
from PIL import Image
import imagehash

IMAGE_EXTS = {".jpg", ".jpeg", ".heic", ".png", ".gif", ".tiff", ".tif", ".bmp", ".webp", ".raw", ".cr2", ".nef", ".arw", ".dng"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".wmv", ".flv", ".mts", ".m2ts"}
SMALL_FILE_BYTES = 50 * 1024  # files under 50KB are suspect

SCREENSHOT_PATTERNS = [
    re.compile(r"^screenshot", re.IGNORECASE),
    re.compile(r"^screen.shot", re.IGNORECASE),
    re.compile(r"^screen recording", re.IGNORECASE),
    re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}\.\d{2}\.\d{2}\.png$", re.IGNORECASE),  # macOS screen shot
]

# iOS Live Photo MOV sidecars share the same stem as the HEIC
def _is_live_sidecar(path: Path, sibling_stems: set) -> bool:
    return path.suffix.lower() == ".mov" and path.stem in sibling_stems

def _is_burst(path: Path) -> bool:
    # iOS burst files: IMG_XXXX_burst00X_cover.jpg or similar
    return bool(re.search(r"burst\d+", path.name, re.IGNORECASE))

def _is_screenshot(path: Path) -> bool:
    for pat in SCREENSHOT_PATTERNS:
        if pat.match(path.name):
            return True
    # Check EXIF for iPhone screenshot flag
    if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        try:
            with Image.open(path) as img:
                exif = img.getexif()
                # 0xA430 = CameraOwnerName, screenshots often lack GPS/camera data
                # Simpler heuristic: PNG screenshots from iOS have specific dimensions
                if path.suffix.lower() == ".png":
                    w, h = img.size
                    # Common iOS screen resolutions
                    ios_screens = {
                        (1170, 2532), (2532, 1170),  # iPhone 12/13
                        (1284, 2778), (2778, 1284),  # iPhone 12/13 Pro Max
                        (1179, 2556), (2556, 1179),  # iPhone 14 Pro
                        (1290, 2796), (2796, 1290),  # iPhone 14 Pro Max
                        (1125, 2436), (2436, 1125),  # iPhone X/XS
                        (750, 1334), (1334, 750),    # iPhone 8
                        (828, 1792), (1792, 828),    # iPhone XR
                        (2048, 2732), (2732, 2048),  # iPad Pro
                        (1668, 2388), (2388, 1668),  # iPad Air
                    }
                    if (w, h) in ios_screens:
                        return True
        except Exception:
            pass
    return False

def _is_corrupt(path: Path) -> bool:
    if path.stat().st_size < SMALL_FILE_BYTES:
        return True
    if path.suffix.lower() in IMAGE_EXTS:
        try:
            with Image.open(path) as img:
                img.verify()
            return False
        except Exception:
            return True
    return False

def _count_files(root: Path) -> int:
    total = 0
    for _, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("_screenshots", "_videos", "_junk")]
        total += len(filenames)
    return total


def scan_folder(root: str, on_progress=None, is_cancelled=None) -> dict:
    root_path = Path(root).resolve()
    results = {
        "root": str(root_path),
        "folders": {}
    }

    for dirpath, dirnames, filenames in os.walk(root_path):
        # Skip our own output subfolders
        dirnames[:] = [d for d in dirnames if d not in ("_screenshots", "_videos", "_junk")]

        dir_path = Path(dirpath)
        rel_dir = str(dir_path.relative_to(root_path)) or "."

        # Collect stems of image files to detect live photo sidecars
        image_stems = {
            Path(f).stem for f in filenames
            if Path(f).suffix.lower() in IMAGE_EXTS
        }

        folder_data = {
            "screenshots": [],
            "videos": [],
            "junk": [],
            "photos": [],
        }

        for fname in filenames:
            if is_cancelled and is_cancelled():
                raise InterruptedError("Cancelled")

            fpath = dir_path / fname
            ext = fpath.suffix.lower()

            try:
                size = fpath.stat().st_size
            except OSError:
                if on_progress:
                    on_progress()
                continue

            entry = {"path": str(fpath), "name": fname, "size": size}

            if ext in VIDEO_EXTS:
                if _is_live_sidecar(fpath, image_stems):
                    entry["reason"] = "live_photo_sidecar"
                    folder_data["junk"].append(entry)
                else:
                    folder_data["videos"].append(entry)
            elif ext in IMAGE_EXTS:
                if _is_burst(fpath):
                    entry["reason"] = "burst"
                    folder_data["junk"].append(entry)
                elif _is_screenshot(fpath):
                    folder_data["screenshots"].append(entry)
                elif _is_corrupt(fpath):
                    entry["reason"] = "corrupt_or_small"
                    folder_data["junk"].append(entry)
                else:
                    folder_data["photos"].append(entry)

            if on_progress:
                on_progress()

        has_content = any(folder_data[k] for k in folder_data)
        if has_content:
            results["folders"][rel_dir] = folder_data

    return results


def compute_hashes(scan_results: dict, hash_size: int = 8, on_progress=None, is_cancelled=None) -> dict:
    """Compute perceptual hashes for all keeper photos."""
    hashes = {}  # path -> hash string
    for folder_data in scan_results["folders"].values():
        for entry in folder_data["photos"]:
            if is_cancelled and is_cancelled():
                raise InterruptedError("Cancelled")
            path = entry["path"]
            try:
                with Image.open(path) as img:
                    h = imagehash.phash(img, hash_size=hash_size)
                    hashes[path] = str(h)
            except Exception:
                pass
            if on_progress:
                on_progress()
    return hashes


def find_duplicates(hashes: dict, threshold: int = 6) -> list:
    """
    Returns groups of duplicate photos.
    threshold: max hamming distance to consider images duplicates (lower = stricter).
    """
    paths = list(hashes.keys())
    hash_objs = {p: imagehash.hex_to_hash(h) for p, h in hashes.items()}

    visited = set()
    groups = []

    for i, p1 in enumerate(paths):
        if p1 in visited:
            continue
        group = [p1]
        for p2 in paths[i + 1:]:
            if p2 in visited:
                continue
            dist = hash_objs[p1] - hash_objs[p2]
            if dist <= threshold:
                group.append(p2)
                visited.add(p2)
        if len(group) > 1:
            visited.add(p1)
            groups.append(group)

    return groups


def build_report(root: str, hash_threshold: int = 6, progress_state: dict = None, is_cancelled=None) -> dict:
    root_path = Path(root).resolve()

    total_files = _count_files(root_path)
    if progress_state is not None:
        progress_state.update({"phase": "scanning", "current": 0, "total": total_files})

    scan_counter = [0]
    def _scan_progress():
        scan_counter[0] += 1
        if progress_state is not None:
            progress_state["current"] = scan_counter[0]

    scan = scan_folder(root, on_progress=_scan_progress, is_cancelled=is_cancelled)

    total_photos = sum(len(fd["photos"]) for fd in scan["folders"].values())
    hash_counter = [0]
    if progress_state is not None:
        progress_state.update({"phase": "hashing", "current": 0, "total": total_photos})

    def _hash_progress():
        hash_counter[0] += 1
        if progress_state is not None:
            progress_state["current"] = hash_counter[0]

    hashes = compute_hashes(scan, on_progress=_hash_progress, is_cancelled=is_cancelled)

    if progress_state is not None:
        progress_state.update({"phase": "deduping", "current": 0, "total": 1})

    dup_groups = find_duplicates(hashes, threshold=hash_threshold)

    # Enrich duplicate groups with file sizes
    enriched_groups = []
    for group in dup_groups:
        members = []
        for path in group:
            try:
                size = Path(path).stat().st_size
            except OSError:
                size = 0
            members.append({"path": path, "name": Path(path).name, "size": size})
        enriched_groups.append(members)

    report = {
        "root": scan["root"],
        "folders": scan["folders"],
        "duplicates": enriched_groups,
    }

    _add_savings(report)
    return report


def _add_savings(report: dict):
    screenshots_bytes = sum(
        e["size"] for fd in report["folders"].values() for e in fd["screenshots"]
    )
    videos_bytes = sum(
        e["size"] for fd in report["folders"].values() for e in fd["videos"]
    )
    junk_bytes = sum(
        e["size"] for fd in report["folders"].values() for e in fd["junk"]
    )

    # For duplicates, keep the largest file in each group (assumed best quality)
    dup_savings = 0
    for group in report["duplicates"]:
        sizes = sorted(e["size"] for e in group)
        dup_savings += sum(sizes[:-1])  # all but the biggest

    report["savings"] = {
        "screenshots_bytes": screenshots_bytes,
        "videos_bytes": videos_bytes,
        "junk_bytes": junk_bytes,
        "duplicates_bytes": dup_savings,
        "total_bytes": screenshots_bytes + videos_bytes + junk_bytes + dup_savings,
    }
