#!/usr/bin/env python3
# v6_web.py — local website for the V6 downloader (iPad / phone / any LAN device).
#
# Run:  python v6_web.py                 (random 6-digit PIN)
#       python v6_web.py --pin 123456 --port 8765
#
# Optional but recommended: ffmpeg + ffprobe on the PC for the auto-convert
# fallback (handles weird codecs Safari can't decode directly):
#     winget install ffmpeg
#
# Notes
#   * History kept in ~/.video_downloader/history.json.
#   * MEGA is decrypted here; the iPad only receives plain bytes.
#   * Content-Disposition is only sent for actual downloads, never for the
#     inline <video>/<audio>/<img>/<iframe> streams — Safari rejects media
#     with that header sometimes, especially when the filename has non-ASCII
#     characters (which MEGA files often do).
#   * If Safari can't play a file directly (moov at end, odd audio codec,
#     10-bit H.264, HEVC on older iPads), it auto-switches to a converted
#     stream produced by ffmpeg. No user action needed.
#   * Plain HTTP on your LAN, protected by the PIN. Don't port-forward.
from __future__ import annotations

import argparse
import atexit
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

try:
    import v6_simple as core
except ImportError:
    sys.exit("v6_web.py needs v6_simple.py in the same folder.")

try:
    SOURCES       = core.SOURCES
    safe_filename = core.safe_filename
    unique_path   = core.unique_path
    DownloadError = core.DownloadError
except AttributeError as exc:
    sys.exit(f"v6_simple.py is missing a required attribute: {exc}")


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

HISTORY_PATH = Path.home() / ".video_downloader" / "history.json"
_HIST_LOCK = threading.RLock()
_HIST = None
_MAX_EVENTS = 2000


def _hist():
    global _HIST
    if _HIST is not None:
        return _HIST
    try:
        _HIST = json.loads(HISTORY_PATH.read_text("utf-8"))
        if not isinstance(_HIST, dict):
            raise ValueError
    except Exception:
        _HIST = {"version": 1, "events": [], "links": []}
    _HIST.setdefault("events", [])
    _HIST.setdefault("links", [])
    return _HIST


def _hist_flush():
    if _HIST is None:
        return
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_HIST), "utf-8")
        tmp.replace(HISTORY_PATH)
    except Exception:
        pass


def _fb_scan_title(source, items):
    if not items:
        return f"{source} scan"
    if len(items) == 1:
        return items[0].name
    # A numbered-episode folder ("01.mp4", "02.mp4", ...) used to title itself
    # after whichever file sorted first ("01 (+887 more)") - technically
    # accurate but useless for telling scans apart in the recents list.
    # Prefer the longest common prefix across filenames (catches a real show/
    # project naming pattern); fall back to a plain count+category summary.
    names = [i.name.rsplit(".", 1)[0] for i in items]
    prefix = os.path.commonprefix(names).rstrip(" ._-")
    if len(prefix) >= 4 and not prefix.isdigit():
        return f"{prefix}… ({len(items)} files)"
    cats = Counter(i.category for i in items)
    dominant, dcount = cats.most_common(1)[0]
    if dcount == len(items):
        return f"{len(items)} {dominant} files"
    return f"{len(items)} files ({dominant}, {len(items) - dcount} other)"


def _fb_history_link(url, source, title, count, device):
    with _HIST_LOCK:
        h = _hist()
        # Scanning the same link again used to append a brand-new row every
        # time, so the same folder would pile up multiple near-identical
        # entries over time. Update the existing one in place instead.
        for l in h["links"]:
            if l.get("url") == url:
                l["title"], l["count"], l["last"] = title, int(count or 0), time.time()
                if device and device not in l["devices"]:
                    l["devices"].append(device)
                _hist_flush()
                return l["id"]
        lid = secrets.token_hex(6)
        h["links"].append({"id": lid, "url": url, "source": source, "title": title,
                           "count": int(count or 0), "last": time.time(),
                           "devices": [device] if device else []})
        _hist_flush()
        return lid


def _fb_history_event(action, name, size, source, link, device):
    with _HIST_LOCK:
        h = _hist()
        h["events"].append({"action": action, "name": name, "size": int(size or 0),
                            "source": source, "link": link or "",
                            "device": device or "", "t": time.time()})
        if len(h["events"]) > _MAX_EVENTS:
            del h["events"][:len(h["events"]) - _MAX_EVENTS]
        for l in h["links"]:
            if l.get("id") == link:
                l["last"] = time.time()
                if device and device not in l.setdefault("devices", []):
                    l["devices"].append(device)
                break
        _hist_flush()


def _fb_history_snapshot():
    with _HIST_LOCK:
        h = _hist()
        return {"events": list(h["events"]), "links": list(h["links"])}


def _fb_history_clear():
    global _HIST
    with _HIST_LOCK:
        _HIST = {"version": 1, "events": [], "links": []}
        _hist_flush()


def _fb_history_url(link_id):
    with _HIST_LOCK:
        for l in _hist()["links"]:
            if l.get("id") == link_id:
                return l.get("url")
    return None


def _fb_banner(subtitle=""):
    line = "Video Downloader" + (f"  -  {subtitle}" if subtitle else "")
    bar = "=" * max(48, len(line) + 4)
    print(f"\n{bar}\n  {line}\n{bar}")


def _fb_section(t): print(f"\n-- {t} --")
def _fb_info(t):    print(f"  {t}")
def _fb_c(t, *s):   return str(t)


def _bind(name, fallback):
    fn = getattr(core, name, None)
    return fn if callable(fn) else fallback


scan_title       = _bind("scan_title",       _fb_scan_title)
history_link     = _bind("history_link",     _fb_history_link)
history_event    = _bind("history_event",    _fb_history_event)
history_snapshot = _bind("history_snapshot", _fb_history_snapshot)
history_clear    = _bind("history_clear",    _fb_history_clear)
history_url      = _bind("history_url",      _fb_history_url)
banner           = _bind("banner",           _fb_banner)
section          = _bind("section",          _fb_section)
info             = _bind("info",             _fb_info)
c                = _bind("c",                _fb_c)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

PIN = ""
LOCK = threading.RLock()
TOKENS: set = set()
FAILS: dict = {}
SCANS: dict = {}
SCAN_JOBS: dict = {}
SAVE_JOBS: dict = {}
MAX_SCANS = 8
SCAN_JOB_TTL = 15 * 60
SAVE_JOB_MAX = 60

# -- HLS on-the-fly transcode state ------------------------------------------
# Safari's <video> element enforces a minimum byte-arrival rate on a plain
# 206 range response; through the MEGA proxy that's often too slow and it
# bails with error 4 even though the bytes are valid. HLS is Safari's native
# streaming format and is built to tolerate exactly this kind of variable
# latency, so it's the default playback path for video items. ffmpeg remuxes
# (copies, when the codec is already Safari-compatible) the decrypted local
# stream into small .ts segments + an index.m3u8 on disk as bytes arrive;
# Safari pulls segments on its own schedule instead of one long connection.
HLS_ROOT = Path(tempfile.gettempdir()) / "v6web_hls"
_HLS: dict = {}          # (sid, idx) -> {"dir": Path, "proc": Popen|None, "error": str|None}
_HLS_LOCKS: dict = {}    # (sid, idx) -> threading.Lock()

ALLOWED_HOSTS = ("mega.nz", "mega.io", "mega.co.nz", "dropbox.com",
                 "dropboxusercontent.com")
NATIVE = {".mp4", ".m4v", ".mov", ".mp3", ".m4a", ".aac", ".wav"}
MIME = {
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".avi": "video/x-msvideo",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".pdf": "application/pdf",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


def host_of(url): return (urlparse(url.strip()).hostname or "").lower()


def host_ok(url):
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in ALLOWED_HOSTS)


def source_for(url):
    h = host_of(url)
    return "mega" if (h.endswith("mega.nz") or h.endswith("mega.io")
                      or h.endswith("mega.co.nz")) else "dropbox"


def item_json(it, idx):
    return {"id": idx, "name": it.name, "size": it.size, "cat": it.category,
            "ext": it.ext, "native": it.ext in NATIVE,
            "parent": (it.extra or {}).get("parent", "")}


def serve_mime(it, attach):
    if attach:
        return MIME.get(it.ext, "application/octet-stream")
    if it.category == "text":
        return "text/plain; charset=utf-8"
    return MIME.get(it.ext) or "application/octet-stream"


def drop_scan_files(sc):
    zp = (sc.get("ctx") or {}).get("zip_path")
    if zp:
        try:
            Path(zp).unlink(missing_ok=True)
        except Exception:
            pass
    _drop_hls_for_scan(sc["id"])


def _drop_hls_for_scan(sid):
    with LOCK:
        keys = [k for k in _HLS if k[0] == sid]
        states = [_HLS.pop(k) for k in keys]
        for k in keys:
            _HLS_LOCKS.pop(k, None)
    for st in states:
        proc = st.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        shutil.rmtree(HLS_ROOT / sid, ignore_errors=True)
    except Exception:
        pass


def cleanup():
    with LOCK:
        for sc in SCANS.values():
            drop_scan_files(sc)


def _prune_scan_jobs():
    now = time.time()
    dead = [k for k, j in SCAN_JOBS.items()
            if j.get("state") in ("done", "error") and now - j.get("t", now) > SCAN_JOB_TTL]
    for k in dead:
        SCAN_JOBS.pop(k, None)


def _prune_save_jobs():
    while len(SAVE_JOBS) > SAVE_JOB_MAX:
        cand = [k for k, j in SAVE_JOBS.items()
                if j.get("state") in ("done", "error")]
        if not cand:
            break
        oldest = min(cand, key=lambda k: SAVE_JOBS[k]["t"])
        SAVE_JOBS.pop(oldest, None)


# ---------------------------------------------------------------------------
# Background work
# ---------------------------------------------------------------------------

def run_scan(job_id, url, device):
    job = SCAN_JOBS[job_id]
    try:
        name = source_for(url)
        kind, items, ctx = SOURCES[name].scan(url)
        items = [i for i in items if not i.is_folder]
        items.sort(key=lambda i: i.name.lower())
        title = scan_title(name, items)
        lid = history_link(url, name, title, len(items), device)
        with LOCK:
            # Rescanning the same link used to always mint a new scan id, so
            # the recents chips piled up duplicates of the same folder every
            # time ("01 (+887 more)" x3). Refresh the existing scan in place
            # instead, when we already have one for this exact URL.
            existing = next((s for s in SCANS.values() if s.get("url") == url), None)
            sid = existing["id"] if existing else uuid.uuid4().hex[:10]
            if existing:
                # Item indices may no longer line up with the old list -
                # drop any in-flight HLS/thumb state and cached zip tied to
                # the old indices/items before the scan gets replaced.
                drop_scan_files(existing)
            SCANS[sid] = {"id": sid, "source": name, "title": title, "url": url,
                          "items": items, "ctx": ctx, "t": time.time(),
                          "link": lid}
            while len(SCANS) > MAX_SCANS:
                oldest = min(SCANS, key=lambda k: SCANS[k]["t"])
                drop_scan_files(SCANS.pop(oldest))
        job.update(state="done", scan=sid,
                   warnings=len((ctx or {}).get("scan_warnings") or []), t=time.time())
    except Exception as exc:
        job.update(state="error", msg=str(exc) or exc.__class__.__name__, t=time.time())


def save_worker(sc, jobs, device):
    src = SOURCES[sc["source"]]
    out = Path.home() / "Downloads" / f"{sc['source'].capitalize()}_Videos"
    out.mkdir(parents=True, exist_ok=True)
    for job, item in jobs:
        dest = unique_path(out / safe_filename(item.name))
        try:
            job["state"] = "running"
            if item.size > 0:
                with dest.open("wb") as f:
                    for piece in src.stream_range(item, 0, item.size - 1):
                        f.write(piece)
                        job["done"] += len(piece)
                if job["done"] != item.size:
                    raise DownloadError("incomplete download")
            else:
                src.download(item, dest, sc["ctx"])
            job["state"] = "done"
            job["msg"] = str(dest)
            history_event("save", item.name, item.size, sc["source"],
                          sc.get("link", ""), device)
        except Exception as exc:
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
            job["state"] = "error"
            job["msg"] = str(exc) or exc.__class__.__name__


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe
# ---------------------------------------------------------------------------

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROBE_LOCKS = {}


def find_tool(name):
    exe = shutil.which(name)
    if exe:
        return exe
    here = Path(__file__).resolve().parent
    for cand in (here / f"{name}.exe", here / name):
        if cand.is_file():
            return str(cand)
    return None


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _moov_at_start(head):
    if not head:
        return None
    try:
        idx = head.find(b"moov")
        return idx >= 0 and idx < 32768
    except Exception:
        return None


def probe_item(sc, idx, local_url):
    with LOCK:
        if len(_PROBE_LOCKS) > 500:
            _PROBE_LOCKS.clear()
        lk = _PROBE_LOCKS.setdefault((sc["id"], idx), threading.Lock())
    with lk:
        with LOCK:
            hit = sc.setdefault("probe", {}).get(idx)
        if hit:
            return hit
        item = sc["items"][idx]
        out = {"ok": False, "ffprobe": bool(find_tool("ffprobe")),
               "ffmpeg": bool(find_tool("ffmpeg"))}
        try:
            if item.size <= 0:
                raise DownloadError("source didn't report a file size")
            n = min(item.size, 65536)
            head = b"".join(SOURCES[sc["source"]].stream_range(item, 0, n - 1))
        except Exception as exc:
            out["error"] = f"server could not read file: {exc or exc.__class__.__name__}"
            return out
        out["moov_at_start"] = _moov_at_start(head)
        ffprobe = find_tool("ffprobe")
        if not ffprobe:
            out.update(ok=True, noprobe=True)
            return out
        try:
            r = subprocess.run([ffprobe, "-v", "error", "-print_format", "json",
                                "-show_format", "-show_streams", local_url],
                               capture_output=True, timeout=75, creationflags=NOWIN)
            j = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
        except subprocess.TimeoutExpired:
            out["error"] = "ffprobe timed out"
            return out
        except Exception as exc:
            out["error"] = f"ffprobe failed: {exc}"
            return out
        streams = j.get("streams") or []
        if not streams:
            out["error"] = "file doesn't parse as media"
            return out
        fmt = j.get("format") or {}
        v = next((x for x in streams if x.get("codec_type") == "video"
                  and not (x.get("disposition") or {}).get("attached_pic")), None)
        a = next((x for x in streams if x.get("codec_type") == "audio"), None)
        out.update(ok=True, container=fmt.get("format_name", ""),
                   duration=_num(fmt.get("duration")) or
                            (_num(v.get("duration")) if v else 0.0))
        if v:
            out["v"] = {"codec": v.get("codec_name", ""), "profile": v.get("profile", ""),
                        "pix_fmt": v.get("pix_fmt", ""), "w": v.get("width"),
                        "h": v.get("height")}
        if a:
            out["a"] = {"codec": a.get("codec_name", ""), "ch": a.get("channels")}
        with LOCK:
            sc["probe"][idx] = out
        return out


def _hls_dir(sid, idx):
    return HLS_ROOT / sid / str(idx)


def ensure_hls(sc, idx, local_url):
    """(Re)start the background ffmpeg HLS remux for one item, idempotently.
    Returns the shared state dict; callers poll st['dir'] for files showing
    up rather than blocking here, since segments appear progressively."""
    key = (sc["id"], idx)
    with LOCK:
        if len(_HLS_LOCKS) > 500:
            _HLS_LOCKS.clear()
        lk = _HLS_LOCKS.setdefault(key, threading.Lock())
    with lk:
        with LOCK:
            st = _HLS.get(key)
        if st is not None:
            return st  # already started (running, finished, or failed)

        d = _hls_dir(sc["id"], idx)
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)

        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            st = {"dir": d, "proc": None, "error": "ffmpeg not installed on the PC"}
            with LOCK:
                _HLS[key] = st
            return st

        probe = probe_item(sc, idx, local_url)
        v = probe.get("v") or {}
        a = probe.get("a") or {}
        copy_v = (v.get("codec") == "h264"
                  and v.get("pix_fmt") in ("yuv420p", "yuvj420p")
                  and "10" not in str(v.get("profile", "")))
        copy_a = a.get("codec") == "aac"

        cmd = [ffmpeg, "-nostdin", "-loglevel", "error",
               "-i", local_url, "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
        cmd += ["-c:v", "copy"] if copy_v else [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        cmd += ["-c:a", "copy"] if copy_a else ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
        cmd += ["-f", "hls", "-hls_time", "6", "-hls_list_size", "0",
                "-hls_playlist_type", "event",
                "-hls_flags", "independent_segments+temp_file",
                "-hls_segment_filename", str(d / "seg%05d.ts"),
                str(d / "index.m3u8")]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=NOWIN)
        except Exception as exc:
            st = {"dir": d, "proc": None, "error": f"could not start ffmpeg: {exc}"}
            with LOCK:
                _HLS[key] = st
            return st
        st = {"dir": d, "proc": proc, "error": None, "started": time.time(),
              "thumb_started": False}
        with LOCK:
            _HLS[key] = st
        return st


def _make_thumb(d: Path):
    """Grab one frame from the already-downloaded first HLS segment as a
    poster thumbnail. Pure local disk I/O - reuses bytes already fetched for
    playback, so this can NEVER open a second MEGA connection (that's what
    broke playback before: a thumbnail path that fetched its own bytes,
    which MEGA throttled as a concurrent stream on the same file)."""
    ffmpeg = find_tool("ffmpeg")
    seg = d / "seg00000.ts"
    out = d / "thumb.jpg"
    if not ffmpeg or not seg.exists():
        return
    try:
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", "1",
                        "-i", str(seg), "-frames:v", "1",
                        "-vf", "scale=320:-2", str(out)],
                       timeout=20, creationflags=NOWIN,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def check_core():
    missing = [n for n, src in SOURCES.items() if not hasattr(src, "stream_range")]
    if missing:
        raise RuntimeError("v6_simple.py is too old (no stream_range for "
                           + ", ".join(missing) + ").")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def parse_range(h, total):
    if not h or not h.startswith("bytes="):
        return 0, total - 1, False
    a, _, b = h[6:].split(",")[0].strip().partition("-")
    try:
        if a == "":
            n = int(b)
            if n <= 0:
                return None
            return max(0, total - n), total - 1, True
        start = int(a)
        end = total - 1 if b == "" else min(int(b), total - 1)
    except ValueError:
        return None
    if start > end or start >= total:
        return None
    return start, end, True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "V6Web/2.1"
    timeout = 120

    def log_message(self, *a):
        pass

    # Safari aborts requests constantly (seeking, stopping, etc). Without this
    # override, Python prints a full traceback for every one. Harmless noise
    # that drowns out real errors.
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                ConnectionError, OSError):
            self.close_connection = True

    def _token(self):
        try:
            ck = SimpleCookie(self.headers.get("Cookie", ""))
            t = ck["vdl"].value if "vdl" in ck else ""
        except Exception:
            t = ""
        if t in TOKENS:
            return t
        q = parse_qs(urlparse(self.path).query).get("t", [""])[0]
        return q if q in TOKENS else ""

    def _send(self, status, body, ctype, head=False, extra=None):
        if self.command == "HEAD":
            head = True
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if not head and body:
            self.wfile.write(body)

    def _json(self, obj, status=200, extra=None):
        self._send(status, json.dumps(obj).encode("utf-8"),
                   "application/json; charset=utf-8", extra=extra)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > 65536:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw.decode("utf-8") or "{}")

    def _device(self, name=None):
        n = re.sub(r"[\x00-\x1f]", "", str(name or "")).strip()[:40]
        if n:
            return n
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):
            return "PC (browser)"
        ua = self.headers.get("User-Agent", "")
        for k in ("iPad", "iPhone", "Android"):
            if k in ua:
                return k
        return f"Device {ip}"

    def _local_url(self, kind, sid, idx):
        host = self.server.server_address[0]
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return (f"http://{host}:{self.server.server_address[1]}/{kind}/{sid}/{idx}"
                f"?t={self._token()}")

    def _item(self, sid, idx):
        with LOCK:
            sc = SCANS.get(sid)
        if not sc or not (0 <= idx < len(sc["items"])):
            return None, None
        return sc, sc["items"][idx]

    def do_HEAD(self): self._route(True)
    def do_GET(self):  self._route(False)

    def _route(self, head):
        p = urlparse(self.path).path
        tok = self._token()
        if p == "/":
            page = APP_HTML.replace("__TOKEN__", tok) if tok else LOGIN_HTML
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", head,
                       extra=[("Content-Security-Policy",
                               "default-src 'self'; img-src 'self' data:; "
                               "media-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "script-src 'self' 'unsafe-inline'; "
                               "frame-src 'self'; connect-src 'self'")])
            return
        if p == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if not tok:
            self._json({"error": "login required"}, 401)
            return

        m = re.fullmatch(r"/(stream|download)/([0-9a-f]{10})/(\d+)", p)
        if m:
            sc, item = self._item(m.group(2), int(m.group(3)))
            if item is None:
                self._json({"error": "not found"}, 404)
            else:
                self._serve_file(sc, item, m.group(1) == "download", head)
            return

        m = re.fullmatch(r"/api/probe/([0-9a-f]{10})/(\d+)", p)
        if m:
            sc, item = self._item(m.group(1), int(m.group(2)))
            if item is None:
                self._json({"error": "not found"}, 404)
            else:
                idx = int(m.group(2))
                self._json(probe_item(sc, idx,
                                      self._local_url("stream", sc["id"], idx)))
            return

        m = re.fullmatch(r"/thumb/([0-9a-f]{10})/(\d+)", p)
        if m:
            sc, item = self._item(m.group(1), int(m.group(2)))
            if item is None:
                self._json({"error": "not found"}, 404)
            else:
                self._serve_thumb(sc, int(m.group(2)), head)
            return

        m = re.fullmatch(r"/hls/([0-9a-f]{10})/(\d+)/([\w.-]+)", p)
        if m:
            sc, item = self._item(m.group(1), int(m.group(2)))
            if item is None:
                self._json({"error": "not found"}, 404)
            else:
                self._serve_hls(sc, int(m.group(2)), m.group(3), head)
            return

        m = re.fullmatch(r"/live/([0-9a-f]{10})/(\d+)", p)
        if m:
            sc, item = self._item(m.group(1), int(m.group(2)))
            if item is None:
                self._json({"error": "not found"}, 404)
            else:
                q = parse_qs(urlparse(self.path).query)
                try:
                    ss = max(0.0, float(q.get("ss", ["0"])[0]))
                except ValueError:
                    ss = 0.0
                self._serve_live(sc, int(m.group(2)), ss, head)
            return

        if p == "/api/scans":
            with LOCK:
                rows = sorted(SCANS.values(), key=lambda s: -s["t"])
                out = [{"id": s["id"], "title": s["title"], "count": len(s["items"]),
                        "source": s["source"]} for s in rows]
            self._json({"scans": out})
            return
        m = re.fullmatch(r"/api/scans/([0-9a-f]{10})", p)
        if m:
            with LOCK:
                sc = SCANS.get(m.group(1))
                data = None if not sc else {
                    "id": sc["id"], "title": sc["title"], "source": sc["source"],
                    "items": [item_json(i, n) for n, i in enumerate(sc["items"])],
                    "warnings": (sc.get("ctx") or {}).get("scan_warnings") or [],
                    "folders": (sc.get("ctx") or {}).get("folders") or {}}
            self._json(data or {"error": "not found"}, 200 if data else 404)
            return
        m = re.fullmatch(r"/api/scan/([0-9a-f]{12})", p)
        if m:
            job = SCAN_JOBS.get(m.group(1))
            self._json(job or {"state": "error", "msg": "unknown job"})
            return
        if p == "/api/history":
            q = parse_qs(urlparse(self.path).query)
            src = q.get("source", [""])[0]
            dev = q.get("device", [""])[0]
            text = q.get("q", [""])[0].strip().lower()
            data = history_snapshot()
            evs = [e for e in reversed(data["events"])
                   if (not src or e.get("source") == src)
                   and (not dev or e.get("device") == dev)
                   and (not text or text in str(e.get("name", "")).lower())][:400]
            links = [l for l in sorted(data["links"], key=lambda l: -l.get("last", 0))
                     if (not src or l.get("source") == src)
                     and (not dev or dev in l.get("devices", []))
                     and (not text or text in str(l.get("title", "")).lower())][:200]
            devices = sorted(({e.get("device", "") for e in data["events"]}
                              | {d for l in data["links"] for d in l.get("devices", [])})
                             - {""})
            self._json({"events": evs, "devices": devices,
                        "links": [{"id": l.get("id"), "title": l.get("title", ""),
                                   "source": l.get("source", ""),
                                   "count": l.get("count", 0),
                                   "last": l.get("last", 0),
                                   "devices": l.get("devices", [])}
                                  for l in links]})
            return
        if p == "/api/jobs":
            with LOCK:
                jobs = sorted(SAVE_JOBS.values(), key=lambda j: -j["t"])[:30]
                out = [{k: j[k] for k in ("id", "name", "size", "done", "state", "msg")}
                       for j in jobs]
            self._json({"jobs": out})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            body = self._body()
        except Exception:
            self._json({"error": "bad request"}, 400)
            return

        if p == "/login":
            ip = self.client_address[0]
            now = time.time()
            with LOCK:
                cnt, until = FAILS.get(ip, (0, 0.0))
                if now < until:
                    locked = True
                else:
                    locked = False
                    pin = str(body.get("pin", "")).strip()
                    pin_digits = re.sub(r"\D", "", pin)
                    pin_ok = hmac.compare_digest(pin.encode(), PIN.encode()) or (
                        pin_digits and hmac.compare_digest(pin_digits.encode(), PIN.encode()))
                    if pin_ok:
                        token = secrets.token_urlsafe(24)
                        TOKENS.add(token)
                        FAILS.pop(ip, None)
                        cnt = -1
                    else:
                        cnt += 1
                        FAILS[ip] = (0, now + 60) if cnt >= 5 else (cnt, 0.0)
                        print(f"[login] wrong PIN from {ip}: got {len(pin)} char(s) "
                              f"({'digits only' if pin == pin_digits else 'contains non-digits'}), "
                              f"expected {len(PIN)} digits", flush=True)
            if locked:
                self._json({"error": "Too many tries. Wait a minute."}, 429)
                return
            if cnt == -1:
                self._json({"ok": True}, extra=[(
                    "Set-Cookie", f"vdl={token}; Path=/; HttpOnly; SameSite=Lax; "
                                  f"Max-Age=2592000")])
            else:
                time.sleep(0.7)
                self._json({"error": "Wrong PIN"}, 401)
            return

        if not self._token():
            self._json({"error": "login required"}, 401)
            return

        if p == "/api/scan":
            url = str(body.get("url", "")).strip()
            if not url and body.get("link"):
                url = history_url(str(body["link"])) or ""
                if not url:
                    self._json({"error": "That link is no longer in history."}, 404)
                    return
            if not url or not host_ok(url):
                self._json({"error": "Only public MEGA or Dropbox links are supported."},
                           400)
                return
            with LOCK:
                _prune_scan_jobs()
                jid = uuid.uuid4().hex[:12]
                SCAN_JOBS[jid] = {"state": "running", "t": time.time()}
            threading.Thread(target=run_scan,
                             args=(jid, url, self._device(body.get("device"))),
                             daemon=True).start()
            self._json({"job": jid})
            return

        if p == "/api/save":
            with LOCK:
                sc = SCANS.get(str(body.get("scan", "")))
            if not sc:
                self._json({"error": "unknown scan"}, 404)
                return
            jobs = []
            for i in body.get("ids", []):
                if type(i) is int and 0 <= i < len(sc["items"]):
                    it = sc["items"][i]
                    job = {"id": uuid.uuid4().hex[:8], "name": it.name, "size": it.size,
                           "done": 0, "state": "queued", "msg": "",
                           "t": time.time() + i / 1e6}
                    jobs.append((job, it))
            with LOCK:
                for job, _ in jobs:
                    SAVE_JOBS[job["id"]] = job
                _prune_save_jobs()
            if jobs:
                threading.Thread(target=save_worker,
                                 args=(sc, jobs, self._device(body.get("device"))),
                                 daemon=True).start()
            self._json({"queued": len(jobs)})
            return

        if p == "/api/hist":
            with LOCK:
                sc = SCANS.get(str(body.get("scan", "")))
            idx = body.get("id")
            if sc and body.get("event") == "preview" and type(idx) is int \
                    and 0 <= idx < len(sc["items"]):
                it = sc["items"][idx]
                history_event("preview", it.name, it.size, sc["source"],
                              sc.get("link", ""), self._device(body.get("device")))
            self._json({"ok": True})
            return

        if p == "/api/history/clear":
            history_clear()
            self._json({"ok": True})
            return

        if p == "/api/drop":
            with LOCK:
                sc = SCANS.pop(str(body.get("scan", "")), None)
            if sc:
                drop_scan_files(sc)
            self._json({"ok": True})
            return

        self._json({"error": "not found"}, 404)

    # -- converted playback -------------------------------------------------

    def _serve_live(self, sc, idx, ss, head):
        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            self._json({"error": "ffmpeg not installed on the PC"}, 501)
            return
        v = ((sc.get("probe") or {}).get(idx) or {}).get("v") or {}
        copy_v = (v.get("codec") == "h264"
                  and v.get("pix_fmt") in ("yuv420p", "yuvj420p")
                  and "10" not in str(v.get("profile", "")))
        cmd = [ffmpeg, "-nostdin", "-loglevel", "error"]
        if ss > 0:
            cmd += ["-ss", f"{ss:.2f}"]
        cmd += ["-i", self._local_url("stream", sc["id"], idx),
                "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
        if copy_v:
            cmd += ["-c:v", "copy"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p",
                    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ac", "2",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "-f", "mp4", "pipe:1"]
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if head:
            return
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, creationflags=NOWIN)
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except OSError:
            pass
        finally:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    # -- HLS streaming (Safari-native, default for video) -------------------

    def _serve_hls(self, sc, idx, name, head):
        if name != "index.m3u8" and not re.fullmatch(r"seg\d{5}\.ts", name):
            self._json({"error": "bad segment name"}, 400)
            return
        local_url = self._local_url("stream", sc["id"], idx)
        st = ensure_hls(sc, idx, local_url)
        if st.get("error"):
            self._json({"error": st["error"]}, 501)
            return

        path = st["dir"] / name
        # The playlist and each segment appear on disk progressively as
        # ffmpeg processes the source; wait briefly for this one to show up
        # instead of racing it. -hls_flags temp_file means ffmpeg writes to
        # a .tmp file and renames atomically, so an existing file is always
        # complete/consistent to read - no risk of a torn read.
        deadline = time.time() + 20
        while not path.exists():
            proc = st.get("proc")
            if proc is not None and proc.poll() is not None:
                break  # ffmpeg exited; one more existence check, then give up
            if time.time() > deadline:
                break
            time.sleep(0.2)
        if not path.exists():
            self._json({"error": "segment not ready"}, 503)
            return

        if name == "seg00000.ts":
            with LOCK:
                already = st.get("thumb_started")
                st["thumb_started"] = True
            if not already:
                threading.Thread(target=_make_thumb, args=(st["dir"],), daemon=True).start()

        try:
            data = path.read_bytes()
        except OSError as exc:
            self._json({"error": f"segment read failed: {exc}"}, 500)
            return

        ctype = ("application/vnd.apple.mpegurl" if name.endswith(".m3u8")
                 else "video/mp2t")
        self._send(200, data, ctype, head, extra=[
            ("Cache-Control", "no-store" if name.endswith(".m3u8")
             else "public, max-age=3600"),
            ("Access-Control-Allow-Origin", "*"),
        ])

    def _serve_thumb(self, sc, idx, head):
        # Only serves a thumbnail for an item whose HLS stream has already
        # been started by actually opening it - never kicks off a fetch of
        # its own, so browsing the list can't trigger a second MEGA stream.
        with LOCK:
            st = _HLS.get((sc["id"], idx))
        if not st:
            self._json({"error": "not loading yet"}, 404)
            return
        path = st["dir"] / "thumb.jpg"
        if not path.exists():
            self._json({"error": "thumb not ready"}, 404)
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._json({"error": f"thumb read failed: {exc}"}, 500)
            return
        self._send(200, data, "image/jpeg", head, extra=[
            ("Cache-Control", "private, max-age=3600"),
            ("Access-Control-Allow-Origin", "*"),
        ])

    # -- file streaming -----------------------------------------------------

    def _serve_file(self, sc, item, attach, head):
        total = item.size
        if total <= 0:
            self._json({"error": "Unknown file size; use Save to PC."}, 500)
            return
        rng = parse_range(self.headers.get("Range"), total)
        if rng is None:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            return
        start, end, partial = rng
        if attach and not head and start == 0:
            q = parse_qs(urlparse(self.path).query)
            history_event("download", item.name, item.size, sc["source"],
                          sc.get("link", ""), self._device(q.get("d", [""])[0]))

        try:
            gen = SOURCES[sc["source"]].stream_range(item, start, end)
        except DownloadError as exc:
            self._json({"error": str(exc)}, 503)
            return
        except Exception as exc:
            self._json({"error": f"upstream: {exc or exc.__class__.__name__}"}, 502)
            return

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", serve_mime(item, attach))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        # IMPORTANT: Content-Disposition ONLY for downloads. Sending it on the
        # inline <video>/<audio>/<img>/<iframe> stream makes Safari refuse
        # media with error code 4 in some cases (especially when filename has
        # non-ASCII characters, which MEGA files often do).
        if attach:
            ascii_name = re.sub(r'[^\x20-\x7e]', "_", item.name).replace('"', "_")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{ascii_name}"; '
                             f"filename*=UTF-8''{quote(item.name)}")
        self.send_header("Cache-Control", "private, max-age=600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if item.category in ("text", "other", "archive"):
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; sandbox")
        self.end_headers()
        if head:
            gen.close()
            return
        try:
            sent = 0
            for piece in gen:
                self.wfile.write(piece)
                sent += len(piece)
            if sent != end - start + 1:
                self.close_connection = True
        except OSError:
            self.close_connection = True
        except Exception:
            self.close_connection = True
        finally:
            gen.close()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

LOGIN_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Video Downloader</title><style>
html,body{margin:0;height:100%;background:#1a1a1c;color:#e6e6ea;font:17px -apple-system,system-ui,Segoe UI,sans-serif}
body{display:flex;align-items:center;justify-content:center}
form{background:#232326;padding:26px;border-radius:16px;width:min(340px,86vw);display:flex;flex-direction:column;gap:14px}
h1{margin:0;font-size:20px}input,button{font:inherit;padding:14px;border-radius:12px;border:1px solid #3a3a42;background:#2b2b30;color:#e6e6ea}
button{background:#0e639c;border-color:#0e639c}#e{color:#e07070;min-height:1.2em;font-size:15px}
</style></head><body><form id="f"><h1>Video Downloader</h1>
<input id="pin" inputmode="numeric" autocomplete="off" placeholder="PIN shown on your PC" autofocus>
<button>Enter</button><div id="e"></div></form>
<script>
document.getElementById('f').addEventListener('submit',async e=>{e.preventDefault();
const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:document.getElementById('pin').value})});
const j=await r.json();if(j.ok)location.reload();else document.getElementById('e').textContent=j.error||'Error';});
</script></body></html>"""

APP_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Video Downloader</title><style>
:root{--bg:#1a1a1c;--panel:#232326;--alt:#2b2b30;--fg:#e6e6ea;--dim:#9a9aa2;--acc:#0e639c;--acc2:#1177bb;--bd:#3a3a42;--sel:#124a70;--bad:#e07070;--ok:#4caf7d}
*{box-sizing:border-box}[hidden]{display:none!important}
html,body{margin:0;background:var(--bg);color:var(--fg);font:16px -apple-system,system-ui,Segoe UI,sans-serif}
body{padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}
header{display:flex;gap:8px;padding:10px;background:var(--panel);position:sticky;top:0;z-index:5;flex-wrap:wrap;align-items:center}
h1{font-size:17px;margin:0 8px 0 4px}
input,select,button,a.btn{font:inherit;color:var(--fg);background:var(--alt);border:1px solid var(--bd);border-radius:10px;padding:10px 12px;min-height:44px;text-decoration:none;display:inline-flex;align-items:center}
button,a.btn{cursor:pointer}.pri{background:var(--acc)!important;border-color:var(--acc)!important}button:active,a.btn:active{background:var(--acc2)}
#url{flex:1;min-width:220px}
main{display:grid;grid-template-columns:1fr;gap:10px;padding:10px}
@media(min-width:1000px){main{grid-template-columns:minmax(340px,5fr) 7fr;align-items:start}#lp{grid-column:1;grid-row:1}#pv{grid-column:2;grid-row:1;position:sticky;top:70px}}
.card{background:var(--panel);border-radius:12px;padding:10px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px}.bar input[type=search]{flex:1;min-width:120px}
.dim{color:var(--dim)}.sm{font-size:13px}.pad{padding:18px}.bad{color:var(--bad)}.msg{padding:0 12px;min-height:1px}
.chips{display:flex;gap:8px;flex-wrap:wrap;padding:8px 10px 0}.chip{background:var(--alt);border:1px solid var(--bd);border-radius:20px;padding:7px 12px;font-size:14px;cursor:pointer}.chip.on{border-color:var(--acc2)}.chip b{margin-left:8px;color:var(--dim)}
.row{display:flex;gap:12px;align-items:center;padding:10px;border-radius:10px;border-bottom:1px solid var(--bd);cursor:pointer}.row.on{background:var(--sel)}
.chk{width:26px;height:26px;min-height:0;flex:none}.nm{min-width:0;flex:1}.nm>div:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#list.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px}
.gcard{border-radius:8px;overflow:hidden;cursor:pointer;background:var(--alt)}
.gcard.on{outline:2px solid var(--acc2)}
.cardthumb{width:100%;aspect-ratio:16/9;background:#000 center/cover no-repeat;display:flex;align-items:center;justify-content:center;font-size:28px;position:relative}
.cardchk{position:absolute;top:4px;right:4px;background:var(--acc);color:#fff;border-radius:50%;width:18px;height:18px;font-size:11px;display:flex;align-items:center;justify-content:center}
.cardnm{font-size:12px;padding:4px 6px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cardsz{font-size:11px;padding:0 6px 6px;opacity:.6}
details.tfold{margin-left:14px}
details.tfold>summary{cursor:pointer;padding:4px 0;list-style:none}
details.tfold>summary::-webkit-details-marker{display:none}
details.tfold>summary::before{content:'▸ ';display:inline-block}
details.tfold[open]>summary::before{content:'▾ '}
video,audio{width:100%;background:#000;border-radius:10px;max-height:68vh}pre{white-space:pre-wrap;word-break:break-word;max-height:68vh;overflow:auto;background:#161618;padding:10px;border-radius:10px;font-size:13px}
.pb{height:6px;background:var(--bd);border-radius:3px;margin:5px 0 10px}.pb i{display:block;height:100%;background:var(--ok);border-radius:3px}
label{display:inline-flex;gap:6px;align-items:center}
.hrow{display:flex;padding:10px;border-bottom:1px solid var(--bd);border-radius:10px;cursor:pointer}.hrow:active{background:var(--sel)}
</style></head><body>
<header><h1>Video Downloader</h1>
<input id="url" type="url" inputmode="url" autocapitalize="off" autocorrect="off" placeholder="Paste a public MEGA or Dropbox link">
<button class="pri" id="scanBtn">Scan</button><button id="histBtn">History</button><button id="devBtn" class="sm"></button><button id="dbgBtn" class="sm">Debug</button></header>
<div id="msg" class="msg"></div><div id="recents" class="chips"></div>
<section class="card" id="dbgPanel" hidden style="margin:8px 10px 0">
<div class="bar"><b>Debug log</b><span class="dim sm">captures every video error / probe result, doesn't auto-clear</span>
<button id="dbgClear" class="sm">Clear</button><button id="dbgCopy" class="sm">Copy</button></div>
<pre id="dbgLog" style="max-height:34vh;font-size:12px"></pre></section>
<section class="card" id="hist" hidden style="margin:8px 10px 0">
<div class="bar"><button id="hFiles" class="pri">Files</button><button id="hLinks">Links</button>
<select id="hsrc"><option value="">all sources</option><option>mega</option><option>dropbox</option></select>
<select id="hdev"><option value="">all devices</option></select>
<input id="hq" type="search" placeholder="Search history"><button id="hClear">Clear</button></div>
<div id="hlist"></div></section>
<main>
<section class="card" id="pv" hidden><div id="stage"></div><div id="ctl"></div></section>
<section class="card" id="lp">
<div class="bar"><input id="q" type="search" placeholder="Search">
<select id="kind"><option>all</option><option>video</option><option>audio</option><option>image</option><option>text</option><option>pdf</option><option>archive</option><option>other</option></select>
<select id="sort"><option value="name">Name</option><option value="size">Size</option><option value="type">Type</option></select>
<select id="viewMode"><option value="list">List</option><option value="grid">Grid</option><option value="tree">Tree</option></select></div>
<div class="bar"><button id="selAll">Select all</button><button id="selNone">Clear</button>
<button class="pri" id="saveBtn">Save selected to PC</button><span id="count" class="dim sm"></span></div>
<div id="list"><div class="dim pad">Paste a link above and tap Scan.</div></div></section>
</main>
<div id="jobs" class="card" style="margin:0 10px 14px" hidden></div>
<script>
const T='__TOKEN__';
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>{if(!n)return'—';const u=['B','KB','MB','GB','TB'];let i=0;while(n>=1024&&i<4){n/=1024;i++}return(i?n.toFixed(1):n)+' '+u[i]};
const U=(k,it)=>`/${k}/${S.id}/${it.id}?t=${T}&d=${encodeURIComponent(DEV)}`;
const UF=(k,it,file)=>`/${k}/${S.id}/${it.id}/${file}?t=${T}&d=${encodeURIComponent(DEV)}`;
const absU=(k,it)=>location.origin+U(k,it);
const LS={get:(k,d)=>{try{const v=localStorage.getItem(k);return v===null?d:v}catch(e){return d}},set:(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}},del:k=>{try{localStorage.removeItem(k)}catch(e){}}};
let S=null,items=[],view=[],cur=-1,sel=new Set();
const guessDev=()=>{const ua=navigator.userAgent;if(/iPad/.test(ua)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1))return'iPad';if(/iPhone/.test(ua))return'iPhone';if(/Android/.test(ua))return'Android';return'Browser'};
let DEV=LS.get('vdl:dev','')||guessDev();

// -- persistent debug log: survives the usual #err div being overwritten
// a split second later by the auto-convert fallback -----------------------
let DBG=[];
function dbg(msg){
  const t=new Date().toLocaleTimeString();
  DBG.push(`[${t}] ${msg}`);
  if(DBG.length>300)DBG.shift();
  const el=$('#dbgLog');if(el)el.textContent=DBG.join('\n');
  const panel=$('#dbgPanel');if(panel)panel.scrollTop=1e9;
}
window.addEventListener('error',e=>dbg('JS error: '+e.message));
window.addEventListener('unhandledrejection',e=>dbg('Promise rejection: '+(e.reason&&e.reason.message||e.reason)));
document.addEventListener('DOMContentLoaded',()=>{
  const b=$('#dbgBtn');if(b)b.onclick=()=>{$('#dbgPanel').hidden=!$('#dbgPanel').hidden};
  const c=$('#dbgClear');if(c)c.onclick=()=>{DBG=[];$('#dbgLog').textContent=''};
  const cp=$('#dbgCopy');if(cp)cp.onclick=async()=>{
    const text=DBG.join('\n');
    try{
      if(navigator.clipboard&&navigator.clipboard.writeText){
        await navigator.clipboard.writeText(text);
      }else{
        // Clipboard API needs a secure context (https/localhost); this LAN
        // server is plain http, so fall back to a selected, copyable textarea.
        const ta=document.createElement('textarea');
        ta.value=text;ta.style.position='fixed';ta.style.opacity='0';
        document.body.appendChild(ta);ta.focus();ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      cp.textContent='Copied!';setTimeout(()=>cp.textContent='Copy',1200);
    }catch(e){
      dbg('clipboard copy failed: '+e);
      const el=$('#dbgLog');if(el){const r=document.createRange();r.selectNodeContents(el);
        const s=window.getSelection();s.removeAllRanges();s.addRange(r);}
      cp.textContent='Selected - tap & hold to copy';setTimeout(()=>cp.textContent='Copy',2500);
    }
  };
});

async function api(p,b){
  let r;
  try{
    r=await fetch(p,b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{});
  }catch(err){dbg(`FETCH FAIL ${p} : ${err}`);throw err}
  if(r.status===401){location.reload();throw 0}
  if(!r.ok){
    let body='';try{body=await r.clone().text()}catch(e){}
    dbg(`API ${p} -> HTTP ${r.status} ${body.slice(0,300)}`);
  }
  return r.json()
}
function msg(t,bad){const m=$('#msg');m.textContent=t||'';m.className='msg '+(bad?'bad':'dim')}

async function runScan(body,openName){
  $('#scanBtn').disabled=true;msg('Scanning…');
  try{
    const j=await api('/api/scan',{...body,device:DEV});
    if(j.error){msg(j.error,true);return}
    await new Promise(res=>{
      let fails=0;
      const t=setInterval(async()=>{
        try{
          const s=await api('/api/scan/'+j.job);
          if(s.state==='done'){
            clearInterval(t);
            msg(s.warnings?`Scanned. ${s.warnings} item(s) couldn't be read - see Debug log.`:'');
            loadScan(s.scan,openName);
            res();
          }
          else if(s.state==='error'){clearInterval(t);msg(s.msg,true);res()}
        }catch(e){if(++fails>10){clearInterval(t);msg('Lost connection.',true);res()}}
      },1000);
    });
  }catch(e){msg('Request failed',true)}finally{$('#scanBtn').disabled=false}
}
function scanUrl(){const url=$('#url').value.trim();if(url)runScan({url})}
async function loadScan(id,openName){
  const d=await api('/api/scans/'+id);if(d.error){msg(d.error,true);return}
  S=d;items=d.items;sel.clear();cur=-1;$('#pv').hidden=true;$('#stage').innerHTML='';render();loadRecents();
  if(d.warnings&&d.warnings.length){
    dbg(`SCAN WARNINGS (${d.warnings.length}) - these items could not be decrypted/listed and are not shown below:`);
    d.warnings.slice(0,50).forEach(w=>dbg(`  ${w.type} handle=${w.handle} parent=${w.parent} : ${w.error}`));
    if(d.warnings.length>50) dbg(`  …and ${d.warnings.length-50} more`);
  }
  if(openName){const it=items.find(x=>x.name===openName);if(it)openItem(it.id)}
}
async function loadRecents(){
  const r=await api('/api/scans');
  $('#recents').innerHTML=r.scans.map(s=>`<span class="chip${S&&S.id===s.id?' on':''}" data-id="${s.id}">${esc(s.title)}<b data-drop="${s.id}">×</b></span>`).join('');
  return r.scans;
}
function updCount(){$('#count').textContent=`${sel.size} selected / ${items.length} files`}
const ICONS={video:'🎬',audio:'🎵',image:'🖼️',pdf:'📄',text:'📝',archive:'🗜️',other:'📦'};
// Thumbnails are only ever generated for items already opened this session
// (see pollThumb/_serve_thumb - deliberately never pre-fetched for the
// whole list, since a second MEGA connection per file for browsing is
// exactly what broke playback before). Grid view shows a real thumb once
// one lands, a plain icon otherwise.
let knownThumbs={};

function rowHtml(i){
  return `<div class="row${i.id===cur?' on':''}" data-id="${i.id}"><input type="checkbox" class="chk"${sel.has(i.id)?' checked':''}><div class="nm"><div>${esc(i.name)}</div><div class="dim sm">${i.cat} · ${fmt(i.size)}</div></div></div>`;
}
function cardHtml(i){
  const th=knownThumbs[i.id];
  const bg=th?` style="background-image:url('${th}')"`:'';
  const icon=th?'':`<span>${ICONS[i.cat]||'📦'}</span>`;
  return `<div class="gcard${i.id===cur?' on':''}" data-id="${i.id}">
<div class="cardthumb"${bg}>${icon}${sel.has(i.id)?'<div class="cardchk">✓</div>':''}</div>
<div class="cardnm">${esc(i.name)}</div><div class="cardsz dim">${fmt(i.size)}</div></div>`;
}

function buildTree(){
  const foldersMap=S.folders||{};
  const childrenOf={};
  Object.keys(foldersMap).forEach(h=>childrenOf[h]={folders:[],files:[]});
  Object.entries(foldersMap).forEach(([h,f])=>{
    if(foldersMap[f.parent])childrenOf[f.parent].folders.push(h);
  });
  const rootFolders=Object.keys(foldersMap).filter(h=>!foldersMap[foldersMap[h].parent]);
  const rootFiles=[];
  view.forEach(it=>{
    if(foldersMap[it.parent])childrenOf[it.parent].files.push(it.id);
    else rootFiles.push(it.id);
  });
  return {foldersMap,childrenOf,rootFolders,rootFiles};
}
function folderHasItems(h,childrenOf,seen){
  seen=seen||new Set();if(seen.has(h))return false;seen.add(h);
  const c=childrenOf[h];if(!c)return false;
  if(c.files.length)return true;
  return c.folders.some(fh=>folderHasItems(fh,childrenOf,seen));
}
function renderFolderNode(h,t){
  if(!folderHasItems(h,t.childrenOf))return '';
  const f=t.foldersMap[h],c=t.childrenOf[h];
  const subFolders=c.folders.map(fh=>renderFolderNode(fh,t)).join('');
  const files=c.files.map(id=>rowHtml(items[id])).join('');
  return `<details class="tfold" open><summary>${esc(f.name)}</summary>${subFolders}${files}</details>`;
}
function renderTree(){
  const t=buildTree();
  const folderHtml=t.rootFolders.map(h=>renderFolderNode(h,t)).join('');
  const fileHtml=t.rootFiles.map(id=>rowHtml(items[id])).join('');
  return folderHtml+fileHtml;
}

function render(){
  const q=$('#q').value.trim().toLowerCase(),k=$('#kind').value,so=$('#sort').value;
  const mode=$('#viewMode').value;
  view=items.filter(i=>i.name.toLowerCase().includes(q)&&(k==='all'||i.cat===k));
  view.sort(so==='size'?(a,b)=>b.size-a.size:so==='type'?(a,b)=>a.cat.localeCompare(b.cat)||a.name.localeCompare(b.name):(a,b)=>a.name.localeCompare(b.name));
  const list=$('#list');
  list.classList.toggle('grid',mode==='grid');
  if(mode==='grid') list.innerHTML=view.map(cardHtml).join('')||'<div class="dim pad">No files.</div>';
  else if(mode==='tree'&&S&&S.folders&&Object.keys(S.folders).length) list.innerHTML=renderTree()||'<div class="dim pad">No files.</div>';
  else list.innerHTML=view.map(rowHtml).join('')||'<div class="dim pad">No files.</div>';
  updCount();
}
function markCur(){document.querySelectorAll('.row,.gcard').forEach(r=>r.classList.toggle('on',+r.dataset.id===cur))}

let conv=null;
// Playback tiers for video, tried in order as each fails:
//   0 = HLS (Safari-native, tolerates the slow/variable Worker->MEGA hop)
//   1 = raw direct byte-range stream (works fine on PC/VLC, sometimes on iPad)
//   2 = single-shot ffmpeg-converted stream (last resort)
let playTier=0;
const ERRN={1:'aborted',2:'network error',3:'decode error',4:'format not supported'};

function openItem(id){
  cur=id;conv=null;playTier=0;const it=items[id];markCur();
  if(it.cat!=='other'&&it.cat!=='archive')
    api('/api/hist',{event:'preview',scan:S.id,id:it.id,device:DEV}).catch(()=>{});
  const u=U('stream',it),d=U('download',it);let h='';
  const hu=UF('hls',it,'index.m3u8');
  if(it.cat==='video')h=`<video id="m" controls playsinline preload="metadata" src="${hu}"></video>`;
  else if(it.cat==='audio')h=`<audio id="m" controls preload="metadata" src="${u}"></audio>`;
  else if(it.cat==='image')h=`<img src="${u}" style="max-width:100%;max-height:72vh;display:block;margin:auto;border-radius:8px">`;
  else if(it.cat==='pdf')h=`<iframe src="${u}" style="width:100%;height:72vh;border:0;border-radius:8px;background:#fff"></iframe>`;
  else if(it.cat==='text')h='<pre id="tx">Loading…</pre>';
  else h='<div class="dim pad">No preview for this file type.</div>';
  $('#pv').hidden=false;$('#stage').innerHTML=h;
  const media=it.cat==='video'||it.cat==='audio';
  if(it.cat==='video') pollThumb(it);
  // Safari's <video> enforces a minimum byte-arrival rate over a 206 response;
  // through Worker->MEGA that's often too slow and it bails with error 4 even
  // though the bytes are valid. VLC/Infuse/nPlayer don't have that timeout -
  // same URL, same bytes, just a client that tolerates the latency.
  const vlcLink=media?`vlc://${absU('stream',it)}`:'';
  $('#ctl').innerHTML=`<div class="bar" style="margin-top:10px"><button id="prv">⏮ Prev</button><button id="nxt">Next ⏭</button>
<a class="btn pri" href="${d}">Download</a><a class="btn" href="${u}" target="_blank" rel="noopener">Open</a>${media?`<a class="btn" href="${vlcLink}">Open in VLC</a>`:''}<button id="sv1">Save to PC</button></div>
${media?`<div class="bar"><label>Speed <select id="spd">${['0.5','0.75','1','1.25','1.5','2'].map(x=>`<option value="${x}">${x}x</option>`).join('')}</select></label>
<label><input type="checkbox" id="loop"> Loop</label><label><input type="checkbox" id="auto"> Auto-next</label></div>`:''}
<div id="err" class="dim sm"></div><div id="info" class="dim sm"></div><div class="dim sm">${esc(it.name)} · ${fmt(it.size)}</div>`;
  $('#prv').onclick=()=>step(-1,false);$('#nxt').onclick=()=>step(1,false);
  $('#sv1').onclick=()=>save([it.id]);
  if(media){
    const el=$('#m'),spd=$('#spd'),loop=$('#loop'),auto=$('#auto');
    spd.value=LS.get('vdl:spd','1');loop.checked=LS.get('vdl:loop','0')==='1';auto.checked=LS.get('vdl:auto','0')==='1';
    const key='vdl:pos:'+it.name+':'+it.size;
    spd.onchange=()=>{el.playbackRate=+spd.value;LS.set('vdl:spd',spd.value)};
    loop.onchange=()=>{el.loop=loop.checked;LS.set('vdl:loop',loop.checked?'1':'0')};
    auto.onchange=()=>LS.set('vdl:auto',auto.checked?'1':'0');
    el.loop=loop.checked;
    el.addEventListener('loadedmetadata',()=>{
      if(conv) return;
      const p=parseFloat(LS.get(key,'0'));
      if(p>5&&p<el.duration-10)el.currentTime=p;
      el.playbackRate=+spd.value;
    });
    let last=0;el.addEventListener('timeupdate',()=>{
      if(conv)return;
      const n=Date.now();if(n-last>4000){last=n;LS.set(key,el.currentTime)}
    });
    el.addEventListener('ended',()=>{LS.del(key);if(auto.checked&&!el.loop)step(1,true)});
    el.addEventListener('error',()=>fail(it,el));
    el.addEventListener('abort',()=>dbg(`VIDEO abort file="${it.name}"`));
    el.addEventListener('stalled',()=>dbg(`VIDEO stalled file="${it.name}"`));
    el.addEventListener('loadstart',()=>dbg(`VIDEO loadstart file="${it.name}" src=${el.currentSrc||el.src}`));
    probeAndShow(it,el);
  }
  if(it.cat==='text')fetch(u,{headers:{Range:'bytes=0-131071'}}).then(r=>r.text()).then(t=>{$('#tx').textContent=t}).catch(()=>{$('#tx').textContent='Could not load.'});
  window.scrollTo({top:0,behavior:'smooth'});
}

function playConv(it, ss){
  const el=$('#m');if(!el)return;
  dbg(`CONVERT fallback -> file="${it.name}" ss=${(ss||0).toFixed ? (ss||0).toFixed(1) : ss||0}`);
  conv={id:it.id,ss:ss||0};
  el.src=U('live',it)+'&ss='+Math.floor(ss||0);
  el.play().catch(e=>dbg('CONVERT play() rejected: '+e));
  $('#err').className='dim sm';
  $('#err').textContent='Converted stream (ffmpeg on PC). Seeking is limited to buffered range.';
}

function pollThumb(it){
  // The server only generates a thumb from the first HLS segment once it
  // exists on disk (bytes already fetched for playback - never a fresh
  // MEGA request), so this just checks every so often until it shows up.
  const thumbUrl=`/thumb/${S.id}/${it.id}`;
  let tries=0;
  const tryOnce=()=>{
    if(tries>=15) return;
    tries++;
    const img=new Image();
    img.onload=()=>{
      const u=thumbUrl+'?v='+Date.now();
      knownThumbs[it.id]=u;
      if($('#viewMode').value==='grid') render();
      if(cur!==it.id) return;
      const el=$('#m');
      if(el && el.tagName==='VIDEO'){el.poster=u;dbg(`THUMB ready file="${it.name}"`);}
    };
    img.onerror=()=>{if(cur===it.id)setTimeout(tryOnce,1500)};
    img.src=thumbUrl+'?v='+Date.now();
  };
  tryOnce();
}

function fail(it, el){
  const e=el.error, p=el._probe;
  dbg(`VIDEO ERROR file="${it.name}" src=${el.currentSrc||el.src} code=${e?e.code:0}`
     +` network=${el.networkState} ready=${el.readyState} tier=${playTier}`
     +` probe=${p?JSON.stringify(p):'(not back yet)'}`);
  const err=$('#err'); if(!err || cur!==it.id) return;
  err.className='bad sm';
  const code = e ? e.code : 0;
  const codeTxt = ERRN[code] || 'unknown';
  let why = '';
  if(p && p.ok && p.v){
    const v=p.v;
    const bits = [v.codec.toUpperCase()];
    if(v.profile) bits.push(v.profile);
    if(v.pix_fmt) bits.push(v.pix_fmt);
    why = bits.join(' ');
  } else if(p && !p.ok){
    why = p.error || 'unreadable';
  } else {
    why = 'checking…';
  }

  if(it.cat==='video' && playTier===0){
    err.innerHTML = `HLS preview failed (${codeTxt}). ${esc(why)} <span class="dim">Trying direct stream…</span>`;
    playTier=1;
    el.src=U('stream',it);el.load();el.play().catch(()=>{});
    return;
  }
  if(it.cat==='video' && playTier===1 && p && p.ffmpeg){
    err.innerHTML = `Direct stream failed too (${codeTxt}). <span class="dim">Trying converted stream…</span>`;
    playTier=2;
    setTimeout(()=>{ if(cur===it.id && playTier===2) playConv(it, el.currentTime||0); }, 400);
    return;
  }

  err.innerHTML = `Safari error ${code} (${codeTxt}). ${esc(why)} `;
  if(playTier>=2){
    err.innerHTML += ' Converted stream also failed - check the PC terminal for ffmpeg errors, or use the "Open in VLC" button instead.';
  } else if(p && !p.ffmpeg){
    err.innerHTML += '<span class="dim">Install ffmpeg on the PC for HLS/auto-convert, or use the "Open in VLC" button.</span>';
  }
}

async function probeAndShow(it, el){
  let p; try{p = await api('/api/probe/'+S.id+'/'+it.id)}catch(e){dbg('probe call threw: '+e);return}
  dbg(`PROBE file="${it.name}" => ${JSON.stringify(p)}`);
  if(cur!==it.id) return;
  el._probe = p;
  const info=$('#info');
  if(info && p.ok && !p.noprobe && p.v){
    const v=p.v;
    const bits = [v.codec.toUpperCase()];
    if(v.profile) bits.push(v.profile);
    if(v.pix_fmt) bits.push(v.pix_fmt);
    if(v.w) bits.push(v.w+'×'+v.h);
    if(p.a) bits.push('audio '+p.a.codec.toUpperCase());
    if(p.duration) bits.push(Math.round(p.duration)+'s');
    info.textContent = bits.join(' · ');
  } else if(info && !p.ok){
    info.textContent = p.error || 'unreadable';
  }
  if(el.error && playTier<2) fail(it, el);
}

function step(d, mediaOnly){
  const idx=view.findIndex(i=>i.id===cur);
  for(let j=idx+d;j>=0&&j<view.length;j+=d){
    if(!mediaOnly||view[j].cat==='video'||view[j].cat==='audio'){
      openItem(view[j].id);const m=$('#m');if(m)m.play().catch(()=>{});return;
    }
  }
}
async function save(ids){
  if(!ids.length){msg('Select some files first.',true);return}
  const j=await api('/api/save',{scan:S.id,ids,device:DEV});
  msg(j.error?j.error:`Saving ${j.queued} file(s) to your PC's Downloads folder…`,!!j.error);pollJobs();
}
async function pollJobs(){
  try{
    const j=await api('/api/jobs');$('#jobs').hidden=!j.jobs.length;
    $('#jobs').innerHTML='<b>Saving to PC</b>'+j.jobs.map(x=>{const p=x.size?Math.min(100,x.done*100/x.size):0;
      return `<div style="margin-top:8px"><div>${esc(x.name)} <span class="dim sm">${x.state}${x.state==='error'?': '+esc(x.msg):''}</span></div><div class="pb"><i style="width:${x.state==='done'?100:p}%"></i></div></div>`}).join('');
  }catch(e){}
}

let H={events:[],links:[],devices:[]},hTab='files';
const ago=t=>{const s=Math.max(0,Date.now()/1000-t);if(s<60)return'just now';if(s<3600)return Math.floor(s/60)+' min ago';if(s<86400)return Math.floor(s/3600)+' h ago';return new Date(t*1000).toLocaleDateString()};
const ACT={preview:'Previewed',download:'Downloaded',save:'Saved to PC'};
async function loadHistory(){
  const p=new URLSearchParams({source:$('#hsrc').value,device:$('#hdev').value,q:$('#hq').value.trim()});
  H=await api('/api/history?'+p);
  const cd=$('#hdev').value;
  $('#hdev').innerHTML='<option value="">all devices</option>'+H.devices.map(d=>`<option value="${esc(d)}"${d===cd?' selected':''}>${esc(d)}</option>`).join('');
  renderHistory();
}
function renderHistory(){
  $('#hFiles').classList.toggle('pri',hTab==='files');$('#hLinks').classList.toggle('pri',hTab==='links');
  const rows=hTab==='files'
    ?H.events.map((e,i)=>`<div class="hrow" data-i="${i}"><div class="nm"><div>${esc(e.name)}</div><div class="dim sm">${ACT[e.action]||esc(e.action)} · ${esc(e.device)} · ${ago(e.t)} · ${esc(e.source)} · ${fmt(e.size)}</div></div></div>`)
    :H.links.map((l,i)=>`<div class="hrow" data-i="${i}"><div class="nm"><div>${esc(l.title)}</div><div class="dim sm">${esc(l.source)} · ${l.count} files · ${esc(l.devices.join(', '))} · ${ago(l.last)}</div></div></div>`);
  $('#hlist').innerHTML=rows.join('')||'<div class="dim pad">Nothing here yet.</div>';
}
$('#histBtn').onclick=()=>{const h=$('#hist');h.hidden=!h.hidden;if(!h.hidden)loadHistory()};
$('#hFiles').onclick=()=>{hTab='files';renderHistory()};
$('#hLinks').onclick=()=>{hTab='links';renderHistory()};
['#hsrc','#hdev'].forEach(s=>$(s).addEventListener('change',loadHistory));
$('#hq').addEventListener('input',loadHistory);
$('#hClear').onclick=async()=>{if(confirm('Clear ALL history?')){await api('/api/history/clear',{});loadHistory()}};
$('#hlist').addEventListener('click',e=>{
  const r=e.target.closest('.hrow');if(!r)return;const i=+r.dataset.i;
  $('#hist').hidden=true;
  if(hTab==='files'){const x=H.events[i];if(x&&x.link)runScan({link:x.link},x.name);else msg('That entry has no link.',true)}
  else runScan({link:H.links[i].id});
});
const showDev=()=>{$('#devBtn').textContent='Device: '+DEV};
$('#devBtn').onclick=()=>{const n=prompt('Name this device',DEV);if(n&&n.trim()){DEV=n.trim().slice(0,40);LS.set('vdl:dev',DEV);showDev()}};
showDev();
$('#scanBtn').onclick=scanUrl;
$('#url').addEventListener('keydown',e=>{if(e.key==='Enter')scanUrl()});
['#q','#kind','#sort'].forEach(s=>$(s).addEventListener('input',render));
$('#viewMode').value=LS.get('vdl:viewMode','list');
$('#viewMode').addEventListener('change',()=>{LS.set('vdl:viewMode',$('#viewMode').value);render()});
$('#selAll').onclick=()=>{view.forEach(i=>sel.add(i.id));render()};
$('#selNone').onclick=()=>{sel.clear();render()};
$('#saveBtn').onclick=()=>save([...sel]);
$('#list').addEventListener('click',e=>{
  const row=e.target.closest('.row,.gcard');if(!row)return;const id=+row.dataset.id;
  if(e.target.classList.contains('chk')){e.target.checked?sel.add(id):sel.delete(id);updCount();return}
  openItem(id);
});
$('#recents').addEventListener('click',async e=>{
  const dr=e.target.dataset.drop;
  if(dr){await api('/api/drop',{scan:dr});if(S&&S.id===dr){S=null;items=[];view=[];render();$('#pv').hidden=true}loadRecents();return}
  const ch=e.target.closest('.chip');if(ch)loadScan(ch.dataset.id);
});
loadRecents().then(r=>{if(r.length)loadScan(r[0].id)});
setInterval(pollJobs,2000);pollJobs();
</script></body></html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = ai[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def server_urls(port):
    urls = [f"This PC     http://127.0.0.1:{port}"]
    urls += [f"iPad / LAN  http://{ip}:{port}" for ip in lan_ips()]
    return urls


def start_server(host="0.0.0.0", port=8765, pin=None):
    global PIN
    check_core()
    shutil.rmtree(HLS_ROOT, ignore_errors=True)
    PIN = str(pin) if pin else f"{secrets.randbelow(10 ** 6):06d}"
    with LOCK:
        TOKENS.clear()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    atexit.register(cleanup)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.25},
                     daemon=True).start()
    return srv, PIN, server_urls(port)


def stop_server(srv):
    try:
        srv.shutdown()
        srv.server_close()
    finally:
        with LOCK:
            TOKENS.clear()
        cleanup()


def main():
    ap = argparse.ArgumentParser(description="Local web UI for the Video Downloader.")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pin", default=None)
    a = ap.parse_args()
    try:
        srv, pin, urls = start_server(a.host, a.port, a.pin)
    except (OSError, RuntimeError) as exc:
        sys.exit(f"Could not start on {a.host}:{a.port} - {exc}")

    banner(subtitle="web server  -  open it on your iPad")
    section("Open one of these")
    for u in urls:
        info(u)
    print()
    print(c(f"  PIN: {pin}", "yellow", "bold"))
    if not find_tool("ffmpeg") or not find_tool("ffprobe"):
        warn("ffmpeg / ffprobe not found - auto-convert disabled.")
        info("Install with: winget install ffmpeg")
    else:
        info("ffmpeg detected - auto-convert enabled.")
    info("Allow Python through the Windows firewall (Private networks) if asked.")
    info("Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
    finally:
        stop_server(srv)


if __name__ == "__main__":
    main()