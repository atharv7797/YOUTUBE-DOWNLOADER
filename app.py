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
    Collapse yt-dlp's raw format list down to one entry per allowed
    resolution (<=1080p), preferring mp4. Every entry returned here gets
    merged with bestaudio at download time (see /api/download), so from
    the frontend's point of view every option is effectively "with audio" —
    that merging detail is not exposed in this list.
    """
    best_by_height = {}

    for f in info.get("formats", []):
        vcodec = f.get("vcodec")
        if vcodec in (None, "none"):
            continue  # video-less (audio-only) formats are handled separately

        height = f.get("height")
        if height not in ALLOWED_HEIGHTS:
            continue

        current = best_by_height.get(height)
        if current is None:
            best_by_height[height] = f
            continue

        # Prefer mp4 over other containers; otherwise prefer the higher fps.
        current_is_mp4 = current.get("ext") == "mp4"
        f_is_mp4 = f.get("ext") == "mp4"
        if f_is_mp4 and not current_is_mp4:
            best_by_height[height] = f
        elif f_is_mp4 == current_is_mp4 and (f.get("fps") or 0) > (current.get("fps") or 0):
            best_by_height[height] = f

    formats = []
    for height in ALLOWED_HEIGHTS:
        f = best_by_height.get(height)
        if not f:
            continue
        formats.append(
            {
                "format_id": f.get("format_id"),
                "ext": "mp4",  # always mp4 after merge, regardless of source container
                "resolution": f"{height}p",
                "fps": f.get("fps"),
                "filesize_approx": f.get("filesize") or f.get("filesize_approx"),
                "type": "video+audio",  # audio is always merged in at download time
            }
        )
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
        "skip_download": True,
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
        fmt_selector = "bestaudio/best"
        postprocessors = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        # If a specific format_id was requested, use it (merged with best audio
        # if it's video-only). Otherwise fall back to the best combo at or
        # below 1080p, so a missing format_id never accidentally pulls 4K/8K.
        fmt_selector = (
            f"{format_id}+bestaudio/{format_id}/best"
            if format_id
            else "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4][height<=1080]/best[height<=1080]"
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
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            if audio_only:
                # postprocessor changes extension to mp3
                filepath = os.path.splitext(filepath)[0] + ".mp3"
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Download failed: {str(e)}"}), 422

    if not os.path.exists(filepath):
        return jsonify({"error": "File was not created on server"}), 500

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