"""Snagly: a small web front end for yt-dlp.
Run:  pip install -r requirements.txt  &&  python app.py
Needs ffmpeg installed (for MP3 conversion and merging video+audio).
Only download content you own or have permission to save.
"""
import os, shutil, tempfile, time, threading
from collections import defaultdict, deque
from urllib.parse import urlparse
from flask import Flask, jsonify, request, send_file, send_from_directory, after_this_request
import yt_dlp

app = Flask(__name__, static_folder="static")

# --- public-hosting protections ---
MAX_PER_HOUR = 10          # downloads per visitor IP per hour
MAX_MINUTES = 30           # refuse videos longer than this
MAX_MB = 200               # refuse files larger than this
slots = threading.BoundedSemaphore(2)   # only 2 downloads at once
hits = defaultdict(deque)


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or request.remote_addr


def rate_limited(ip):
    now, q = time.time(), hits[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= MAX_PER_HOUR:
        return True
    q.append(now)
    return False

ALLOWED = ("youtube.com", "youtu.be", "reddit.com", "redd.it", "x.com",
           "twitter.com", "instagram.com", "pinterest.com", "pin.it")


def valid_url(url: str) -> bool:
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
    except ValueError:
        return False
    return u.scheme in ("http", "https") and any(host == d or host.endswith("." + d) for d in ALLOWED)


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/info")
def info():
    url = (request.json or {}).get("url", "").strip()
    if not valid_url(url):
        return jsonify(error="That link isn't from a supported site."), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True}) as ydl:
            d = ydl.extract_info(url, download=False)
        return jsonify(title=d.get("title"), thumbnail=d.get("thumbnail"),
                       uploader=d.get("uploader"), duration=d.get("duration"))
    except Exception as e:
        return jsonify(error=f"Couldn't read that link. {str(e)[:160]}"), 422


@app.post("/api/download")
def download():
    body = request.json or {}
    url, fmt = body.get("url", "").strip(), body.get("format", "mp4")
    if not valid_url(url) or fmt not in ("mp4", "mp3"):
        return jsonify(error="Bad request."), 400

    if rate_limited(client_ip()):
        return jsonify(error="Too many downloads. Try again in an hour."), 429
    if not slots.acquire(blocking=False):
        return jsonify(error="Server is busy. Try again in a minute."), 503

    tmp = tempfile.mkdtemp(prefix="snagly_")
    opts = {"quiet": True, "noplaylist": True, "outtmpl": os.path.join(tmp, "%(title).80s.%(ext)s"),
            "max_filesize": MAX_MB * 1024 * 1024,
            "match_filter": yt_dlp.utils.match_filter_func(f"duration <=? {MAX_MINUTES * 60}")}
    if fmt == "mp3":
        opts.update(format="bestaudio/best",
                    postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}])
    else:
        opts.update(format="bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b", merge_output_format="mp4")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
        files = os.listdir(tmp)
        if not files:
            raise RuntimeError("Video is too long or too large.")
        path = os.path.join(tmp, files[0])
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        slots.release()
        return jsonify(error=f"Download failed. {str(e)[:160]}"), 422

    @after_this_request
    def cleanup(resp):
        shutil.rmtree(tmp, ignore_errors=True)
        slots.release()
        return resp

    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
