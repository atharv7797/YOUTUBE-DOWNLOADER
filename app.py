"""
YouTube Video Downloader - Backend API
---------------------------------------
Built with Flask + yt-dlp.

Endpoints:
  GET  /api/health                -> simple health check
  POST /api/info                  -> get video metadata + available formats
  POST /api/download              -> download a video (by format_id) and return the file

Run:
  pip install -r requirements.txt
  python app.py
  # server starts on http://localhost:5000
"""

import glob
import os
import re
import uuid
import threading
import time

from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)  # allow requests from any frontend (e.g. React dev server)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# how long (seconds) to keep a downloaded file on disk before auto-deleting it
FILE_TTL_SECONDS = 60 * 30  # 30 minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_valid_youtube_url(url: str) -> bool:
    pattern = re.compile(
        r"^(https?://)?(www\.)?(youtube\.com|youtu\.be|m\.youtube\.com)/.+$"
    )
    return bool(pattern.match(url.strip()))


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()


def schedule_file_deletion(path: str, delay: int = FILE_TTL_SECONDS):
    """Delete a file after `delay` seconds, in a background thread."""

    def _delete():
        time.sleep(delay)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    threading.Thread(target=_delete, daemon=True).start()


# Only ever offer these resolutions, capped at 1080p, so the frontend
# shows a clean, fixed set of choices instead of yt-dlp's full raw dump.
ALLOWED_HEIGHTS = [1080, 720, 480, 360, 240, 144]


def build_format_list(info: dict):
    """
    Build one selectable video format for each target resolution.
    Accepts any video format up to 1080p instead of requiring an
    exact height match.
    """

    target_heights = [1080, 720, 480, 360, 240, 144]
    formats = []

    raw_formats = info.get("formats", [])

    # Keep video formats only
    video_formats = []

    for f in raw_formats:
        vcodec = f.get("vcodec")

        if not vcodec or vcodec == "none":
            continue

        height = f.get("height")

        if not height:
            continue

        # Never allow anything above 1080p
        if height > 1080:
            continue

        video_formats.append(f)

    # Pick the closest available format for each target resolution
    for target in target_heights:

        candidates = [
            f for f in video_formats
            if f.get("height") <= target
        ]

        if not candidates:
            continue

        # Prefer the highest resolution <= target.
        # If equal, prefer mp4 and higher FPS.
        candidates.sort(
            key=lambda f: (
                f.get("height") or 0,
                1 if f.get("ext") == "mp4" else 0,
                f.get("fps") or 0
            ),
            reverse=True
        )

        best = candidates[0]

        # Don't show duplicate resolutions
        if any(x["format_id"] == best.get("format_id") for x in formats):
            continue

        formats.append({
            "format_id": best.get("format_id"),
            "ext": "mp4",
            "resolution": f"{best.get('height')}p",
            "fps": best.get("fps"),
            "filesize_approx": (
                best.get("filesize")
                or best.get("filesize_approx")
            ),
            "type": "video+audio"
        })

    return formats


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/info", methods=["POST"])
def get_info():
    """
    Request JSON:  { "url": "<youtube url>" }
    Response JSON: { title, thumbnail, duration, uploader, formats: [...] }
    """
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "Missing 'url' in request body"}), 400
    if not is_valid_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"}), 400

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": {"youtube": ["player_client=web,mweb"]},
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Could not fetch video info: {str(e)}"}), 422

    result = {
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "view_count": info.get("view_count"),
        "formats": build_format_list(info),
    }
    return jsonify(result)


@app.route("/api/download", methods=["POST"])
def download_video():
    """
    Request JSON:  { "url": "<youtube url>", "format_id": "<id>" (optional),
                     "audio_only": false (optional) }
    Response:      the downloaded file as an attachment
    """
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    format_id = data.get("format_id")
    audio_only = data.get("audio_only", False)

    if not url:
        return jsonify({"error": "Missing 'url' in request body"}), 400
    if not is_valid_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"}), 400

    job_id = uuid.uuid4().hex[:10]
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{job_id}_%(title)s.%(ext)s")

    if audio_only:
        fmt_selector = "ba/b"
        postprocessors = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        fmt_selector = (
            f"{format_id}+ba/b"
            if format_id
            else "bv*[height<=1080]+ba/b[height<=1080]/best"
        )
        postprocessors = []

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": fmt_selector,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4" if not audio_only else None,
        "postprocessors": postprocessors,
        "noplaylist": True,
        "extractor_args": {"youtube": ["player_client=web,mweb"]},
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        # Locate the output file created with the job_id prefix
        matching_files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}_*"))
        if not matching_files:
            return jsonify({"error": "File was not created on server"}), 500

        filepath = matching_files[0]

    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Download failed: {str(e)}"}), 422

    download_name = sanitize_filename(os.path.basename(filepath))

    @after_this_request
    def cleanup(response):
        schedule_file_deletion(filepath)
        return response

    return send_file(
        filepath,
        as_attachment=True,
        download_name=download_name,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)