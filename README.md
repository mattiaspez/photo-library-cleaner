# Photo Library Cleaner

A local macOS app that scans your photo library and helps you reclaim disk space by finding and removing duplicates, screenshots, junk files, and large videos.

## Features

- **Duplicate detection** — finds identical photos using content hashing and groups them, pre-selecting the best copy to keep
- **Screenshot cleanup** — identifies screenshots taken on device or imported from elsewhere
- **Junk removal** — flags burst photos, corrupt/tiny files, and Live Photo sidecars
- **Video browser** — lists large video files so you can decide what to keep
- **Thumbnail previews** — inline thumbnails and a full lightbox viewer with side-by-side duplicate comparison
- **Safe deletion** — moves files to the macOS Trash (recoverable) or into a `_cleaner/` subfolder
- **Sort & filter** — sort by name or size, filter by selected/unselected, search by filename
- **Fast scanning** — disk thumbnail cache and lazy loading keep the UI snappy on large libraries

## Requirements

- macOS (uses the system Trash and a native folder picker)
- Python 3.10+

## Installation

```bash
git clone https://github.com/mattiaspez/photo-library-cleaner.git
cd photo-library-cleaner
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
./launch.sh
```

The script starts the local server and opens the app in your default browser. Paste your photo library path or click **Browse…** to pick a folder, then click **Scan**.

When the scan finishes you'll see a breakdown of what was found:

| Tab | What's shown |
|---|---|
| Screenshots | Images detected as screenshots |
| Junk | Burst photos, sidecars, corrupt/tiny files |
| Duplicates | Groups of identical photos |
| Videos | All video files |

Select the files you want to remove, choose **Move to Trash** or **Move to subfolder**, and confirm.

## How it works

1. A FastAPI server runs locally on `http://127.0.0.1:8484`
2. The scanner walks the folder tree, hashes every image, and groups files by category
3. The browser-based UI communicates with the server over a simple REST API
4. The server shuts itself down automatically when you close the browser tab

## Privacy

Everything runs locally. No files or metadata leave your machine.

## License

MIT
