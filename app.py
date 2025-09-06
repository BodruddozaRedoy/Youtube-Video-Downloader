import os
import shutil
import tempfile
import threading
import uuid

from flask import (
    Flask, render_template, request, jsonify,
    send_file, after_this_request
)
from yt_dlp import YoutubeDL, DownloadError

app = Flask(__name__)
app.secret_key = "dev"  # change in production

# Branding (your credit)
DEV_NAME = os.environ.get("DEV_NAME", "bodruddozaredoy")
DEV_URL  = os.environ.get("DEV_URL",  "https://www.devredoy.com/")

# Optional: path to a server-side cookies.txt (Netscape format)
# e.g., set COOKIES_FILE=/cookies/cookies.txt and mount that file in Docker
COOKIES_FILE = os.environ.get("COOKIES_FILE")  # file path or None

# Optional: read cookies directly from a local browser profile when running
# the app on your desktop (not in Docker). Example values:
#   COOKIES_FROM_BROWSER=chrome
#   COOKIES_FROM_BROWSER=chrome:Default
#   COOKIES_FROM_BROWSER=edge:Default
#   COOKIES_FROM_BROWSER=firefox:default-release
# This maps to yt-dlp's --cookies-from-browser.
COOKIES_FROM_BROWSER = os.environ.get("COOKIES_FROM_BROWSER")  # e.g., "chrome:Default"

# Auto-detect a local cookies file next to app.py if env var not set
if not COOKIES_FILE:
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("cookies.txt", "cookie.txt", "cookie"):
        cand = os.path.join(here, name)
        if os.path.exists(cand):
            COOKIES_FILE = cand
            break

# Optional: YouTube client selection to reduce friction on some videos
# Comma-separated list. Common picks: android,web,ios
YTDLP_PLAYER_CLIENT = os.environ.get("YTDLP_PLAYER_CLIENT", "android,web")

# In-memory progress state
PROGRESS = {}  # job_id -> dict(status, percent, speed, eta, msg, tmpdir, final_path, error, filename)


# ---------- Utilities ----------

def have_ffmpeg() -> bool:
    """Return True if ffmpeg/ffprobe are available either via env var or PATH."""
    loc = os.environ.get("FFMPEG_LOCATION")
    if loc:
        # Accept a directory or a direct path to the binary
        if os.path.isdir(loc):
            ff = os.path.join(loc, "ffmpeg")
            fp = os.path.join(loc, "ffprobe")
            return os.path.exists(ff) and os.path.exists(fp)
        else:
            # If pointing to ffmpeg binary, assume ffprobe sits alongside or is in PATH
            return os.path.exists(loc)
    # Fallback: PATH lookup
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def make_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    PROGRESS[job_id] = {
        "status": "queued",
        "percent": 0.0,
        "speed": None,
        "eta": None,
        "msg": "Waiting...",
        "tmpdir": None,
        "final_path": None,
        "filename": None,
        "error": None,
    }
    return job_id


def build_opts(tmpdir: str, mode: str, job_id: str) -> dict:
    """
    Build yt-dlp options based on mode and whether ffmpeg is available.
    Modes:
      - best: highest quality (needs ffmpeg for top tiers; else fallback to progressive)
      - progressive: single-file MP4 (≤1080p), no ffmpeg needed
      - audio: best audio; MP3 if ffmpeg present, else original container (m4a/webm)
    """
    ff = have_ffmpeg()
    ff_loc = os.environ.get("FFMPEG_LOCATION")  # optional
    player_clients = [c.strip() for c in YTDLP_PLAYER_CLIENT.split(",") if c.strip()]

    def hook(d):
        job = PROGRESS.get(job_id)
        if not job:
            return
        st = d.get("status")
        if st == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            percent = (downloaded / total) * 100 if total else 0.0
            job["status"] = "downloading"
            job["percent"] = round(percent, 2)
            job["speed"] = d.get("speed")  # bytes/sec
            job["eta"] = d.get("eta")      # seconds
            job["msg"] = d.get("_filename") or "Downloading..."
            job["filename"] = os.path.basename(d.get("filename") or job.get("filename") or "")
        elif st == "finished":
            job["status"] = ("postprocessing" if (ff and mode in ("best", "audio"))
                             else "finalizing")
            job["msg"] = "Merging / finalizing..."
            job["filename"] = os.path.basename(d.get("filename") or job.get("filename") or "")

    ydl_opts = {
        "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
        "progress_hooks": [hook],
        "quiet": True,
        "noprogress": True,
        "extractor_args": {"youtube": {"player_client": player_clients}},
    }
    if ff_loc:
        ydl_opts["ffmpeg_location"] = ff_loc  # tell yt-dlp where ffmpeg/ffprobe are
    # If a server-side cookies file exists, use it automatically (users won't be asked)
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
    # If configured, read cookies directly from a local browser profile
    # Only makes sense when running natively on the same machine as the browser
    elif COOKIES_FROM_BROWSER:
        # Accept formats like "chrome", "chrome:Default", "firefox:default-release", "edge:Default"
        # Map to yt-dlp option 'cookiesfrombrowser': (browser, profile, keyring, container)
        try:
            raw = COOKIES_FROM_BROWSER.strip()
            # Support optional container/keyring later if needed
            # For now parse as <browser>[:<profile>]
            if ":" in raw:
                browser, profile = raw.split(":", 1)
            else:
                browser, profile = raw, None
            browser = (browser or "").strip() or None
            profile = (profile or "").strip() or None
            if browser:
                ydl_opts["cookiesfrombrowser"] = (browser, profile, None, None)
        except Exception:
            # Ignore parsing errors and proceed without browser cookies
            pass

    if mode == "audio":
        if ff:
            # Convert to MP3 via ffmpeg
            ydl_opts.update({
                "format": "ba/best",
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
                "merge_output_format": "mp3",
            })
        else:
            # No ffmpeg: just fetch best audio as-is (m4a/webm)
            ydl_opts.update({
                "format": "ba/best",
            })

    elif mode == "progressive":
        # Single MP4 (video+audio together). No merge required.
        ydl_opts.update({
            "format": "b[ext=mp4]/best",
            "merge_output_format": "mp4",
        })

    else:  # mode == "best"
        if ff:
            # Separate best video+audio, then ffmpeg merges → highest quality
            ydl_opts.update({
                "format": "bv*+ba/best",
                "merge_output_format": "mp4",
            })
        else:
            # No ffmpeg: fallback to progressive MP4 to avoid merge errors
            ydl_opts.update({
                "format": "b[ext=mp4]/best",
                "merge_output_format": "mp4",
            })

    return ydl_opts


def resolve_final_path(tmpdir: str, ydl: YoutubeDL, info: dict) -> str:
    """Get the final output file path (postprocessing can change the extension)."""
    final_path = ydl.prepare_filename(info)
    if not os.path.exists(final_path):
        files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
        if files:
            final_path = max(files, key=os.path.getmtime)
    return final_path


# ---------- Worker ----------

def download_worker(url: str, mode: str, job_id: str):
    tmpdir = tempfile.mkdtemp(prefix="yt_")
    PROGRESS[job_id]["tmpdir"] = tmpdir
    PROGRESS[job_id]["status"] = "starting"
    PROGRESS[job_id]["msg"] = "Starting..."

    try:
        ydl_opts = build_opts(tmpdir, mode, job_id)
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            final_path = resolve_final_path(tmpdir, ydl, info)
            PROGRESS[job_id]["final_path"] = final_path
            PROGRESS[job_id]["filename"] = os.path.basename(final_path)
            PROGRESS[job_id]["status"] = "finished"
            PROGRESS[job_id]["percent"] = 100.0
            PROGRESS[job_id]["msg"] = "Ready to download"

    except DownloadError as e:
        PROGRESS[job_id]["status"] = "error"
        PROGRESS[job_id]["error"] = str(e)
        PROGRESS[job_id]["msg"] = "Download error"

    except Exception as e:
        PROGRESS[job_id]["status"] = "error"
        PROGRESS[job_id]["error"] = str(e)
        PROGRESS[job_id]["msg"] = "Unexpected error"


# ---------- Routes ----------

@app.route("/", methods=["GET"])
def index():
    # Pass branding to template
    return render_template("index.html", dev_name=DEV_NAME, dev_url=DEV_URL)


@app.route("/ffmpeg", methods=["GET"])
def ffmpeg_status():
    cookies_file_ok = bool(COOKIES_FILE and os.path.exists(COOKIES_FILE))
    cookies_method = (
        "file" if cookies_file_ok else ("browser" if COOKIES_FROM_BROWSER else "none")
    )
    return jsonify({
        "ok": True,
        "ffmpeg": have_ffmpeg(),
        "cookies": cookies_file_ok or bool(COOKIES_FROM_BROWSER),
        "cookies_method": cookies_method,
    })


@app.route("/start", methods=["POST"])
def start():
    # Users only send URL + mode. Cookies (if any) are server-side and automatic.
    data = request.form or request.json or {}
    url = (data.get("url") or "").strip()
    mode = (data.get("mode") or "best").strip()
    if not url:
        return jsonify({"ok": False, "error": "No URL"}), 400

    job_id = make_job()
    threading.Thread(target=download_worker, args=(url, mode, job_id), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/progress/<job_id>", methods=["GET"])
def progress(job_id: str):
    job = PROGRESS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job"}), 404
    return jsonify({
        "ok": True,
        "status": job["status"],
        "percent": job["percent"],
        "speed": job["speed"],
        "eta": job["eta"],
        "msg": job["msg"],
        "error": job["error"],
        "filename": job["filename"],
    })


@app.route("/fetch/<job_id>", methods=["GET"])
def fetch(job_id: str):
    job = PROGRESS.get(job_id)
    if not job:
        return "Unknown job", 404
    if job["status"] != "finished" or not job["final_path"]:
        return "Not ready", 409

    final_path = job["final_path"]
    tmpdir = job["tmpdir"]
    filename = os.path.basename(final_path)

    @after_this_request
    def cleanup(resp):
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
        PROGRESS.pop(job_id, None)  # free memory
        return resp

    return send_file(final_path, as_attachment=True, download_name=filename)


if __name__ == "__main__":
    # For local dev / Docker / PaaS
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
