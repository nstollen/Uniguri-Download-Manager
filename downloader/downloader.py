"""Small download manager. Direct files are fetched in parallel range requests,
streams (.m3u8, .mpd) are handed to ffmpeg.

python downloader.py          -> wait for messages from the native host on http://127.0.0.1:8765
python downloader.py <url>    -> download one file or stream right now (for testing)
"""
import atexit
import base64
import collections
import ctypes
import email.utils
import glob
import hashlib
import itertools
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import http.client
import urllib.error
import urllib.parse
import urllib.request
import winreg
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tray

# Installed UDM: this file is built into udm-downloader.exe (PyInstaller), and ffmpeg.exe, udm.ico and the host
# folder sit next to the exe instead of next to this script. There is no python.exe either, so
# "udm-downloader.exe -c code args" stands in for "python.exe -c code args" (the countdown and Save As windows).
FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
PYTHON = sys.executable if FROZEN else os.path.join(os.path.dirname(sys.executable), "python.exe")
if FROZEN and sys.argv[1:2] == ["-c"]:
    code, sys.argv = sys.argv[2], ["-c"] + sys.argv[3:]
    exec(code, {"__name__": "__main__"})
    sys.exit()

PORT = int(os.environ.get("UDM_PORT") or 8765)  # UDM_PORT and UDM_DIR are only for testing
CHUNK = 64 * 1024
RETRIES = 3  # times one part is tried again before the whole job fails
STREAM_WAITS = (5, 15, 30)  # seconds to wait before each automatic retry of a failed stream
# ffmpeg errors worth a retry: the server answered 5xx, or the network failed
TEMPORARY = re.compile(r"Server returned 5|Connection |Error number -\d+ occurred|I/O error|Failed to resolve|timed out")
EXPIRED = "the link may have expired. reload the video page, press play, and download it again from the popup."
OUT_DIR = os.environ.get("UDM_DIR") or os.path.join(os.path.expanduser("~"), "Downloads", "udm")
DATA_DIR = OUT_DIR  # history.json and settings.json live here, also when the download folder is changed
NO_FFMPEG = "ffmpeg not found, run: winget install ffmpeg"
WINDOW = 5  # seconds of history used for speed and time left
MIN_SPLIT = 512 * 1024  # dynamic segmentation: a part is only split when at least this much is left
SPLIT_TIME = 1.0  # ... and it would take at least this many seconds more (or 6 times a new connection's setup)
EXPIRED_CODES = (403, 404, 410)  # a direct download that fails with one of these can get a refreshed link
ACTIVE = ("running", "queued")  # states of a job that is downloading or waiting for its turn
# what the user can change on the settings page; kept in settings.json.
# Speed limits are in KB per second, 0 = no limit.
settings_time = 0  # when settings.json was last written, to notice edits made by hand
settings = {
    "maxJobs": 3, "parts": 8, "speedLimit": 0,
    # connections to one site, all its downloads together, and per-site exceptions ("example.com = 2", one per line)
    "siteConnections": 8, "siteLimits": "",
    "folder": "",  # where files are saved, "" = OUT_DIR
    "sortFolders": True,  # put each file in the subfolder that lists its extension, or in "other"
    "folders": {
        "video": "mp4 mkv avi mov webm flv wmv m4v ts mpg mpeg 3gp",
        "music": "mp3 m4a flac wav aac ogg wma opus",
        "documents": "pdf doc docx xls xlsx ppt pptx txt rtf epub csv odt",
        "programs": "exe msi apk bat",
        "compressed": "zip rar 7z tar gz bz2 xz tgz iso",
    },
    "ask": False,  # show a Save As window before each download
    "notify": True,  # the extension shows a Windows notification when a download finishes or fails
    # scheduler, times as "HH:MM" (24 hour clock)
    "scheduleStart": False, "startTime": "02:00",  # at this time: resume every paused or stopped job
    "scheduleStop": False, "stopTime": "07:00",  # at this time: pause every running or queued job
    # bandwidth schedule: limitSpeed KB/s from limitFrom to limitTo (may pass midnight), else speedLimit
    "limitSchedule": False, "limitFrom": "09:00", "limitTo": "18:00", "limitSpeed": 500,
    # when the whole queue is finished: "nothing" | "sleep" | "shutdown" | "quit"; set back to "nothing" once done
    "afterDone": "nothing",
    # privacy: private mode (new downloads leave nothing in the history, stats, thumbnails or log),
    # auto clean (days after which history, stats, thumbnails and log lines are deleted, 0 = never),
    # and whether stats remember the site of each download
    "privateMode": False, "autoClean": 0, "statsSites": True,
    # network: "none" | "windows" (the proxy set in Windows) | "manual" (proxyType "http" | "socks5", host, port,
    # optional user and password). The password is kept encrypted with Windows DPAPI ("dpapi:" + base64), see dpapi()
    "proxyMode": "none", "proxyType": "http", "proxyHost": "", "proxyPort": 8080, "proxyUser": "", "proxyPassword": "",
    # security: scan each finished download with Windows Defender, except those from the trusted sites (one per line)
    "scanDownloads": True, "trustedSites": "",
}
AFTER_DONE = ("nothing", "sleep", "shutdown", "quit")
AUTO_CLEAN = (0, 7, 30, 90)
VIDEO = ("mp4", "mkv", "avi", "mov", "webm", "flv", "wmv", "m4v", "ts", "mpg", "mpeg", "3gp")  # these get a thumbnail
COMMANDS = {  # what sleep and shut down run (UDM_DRYRUN=1 only prints them; for testing)
    "sleep": ["powershell", "-NoProfile", "-Command", "Add-Type -AssemblyName System.Windows.Forms; "
              "[System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)"],
    "shutdown": ["shutdown", "/s", "/t", "0"],
}
# The countdown before sleep and shut down: a small window near the clock, on top of everything, with
# Cancel. tkinter comes with Python; it runs in its own process. Exit code 0 = go ahead, 1 = canceled.
COUNTDOWN = r"""
import sys, tkinter, winreg
seconds, text = int(sys.argv[1]), sys.argv[2]
try:
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
    light = winreg.QueryValueEx(key, "AppsUseLightTheme")[0]
except OSError:
    light = 1
bg, fg, muted, button = ("#fbf3ea", "#3d1d17", "#7a5a4e", "#5c1a14") if light else ("#2a1a16", "#f3e9dc", "#c3b0a2", "#f1c99a")
root = tkinter.Tk()
root.withdraw()  # hidden until it is in its corner
root.title("UDM: " + text)
root.minsize(300, 0)
try:
    root.iconbitmap(sys.argv[3])  # the hedgehog
except Exception:
    pass
root.attributes("-topmost", True)
root.resizable(False, False)
root.configure(bg=bg, padx=18, pady=14)
tkinter.Label(root, text="Uniguri Download Manager", bg=bg, fg=muted, font=("Segoe UI", 9)).pack(anchor="w")
label = tkinter.Label(root, bg=bg, fg=fg, font=("Segoe UI Semibold", 12))
label.pack(anchor="w", pady=(2, 10))
tkinter.Button(root, text="Cancel", command=root.destroy, font=("Segoe UI", 10), bg=button, fg=bg,
               activebackground=fg, activeforeground=bg, relief="flat", padx=16, pady=3).pack(anchor="e")
result = [1]
def tick(left):
    if left <= 0:
        result[0] = 0
        root.destroy()
        return
    label.config(text="%s in %d s" % (text, left))
    root.after(1000, tick, left - 1)
tick(seconds)
root.update_idletasks()
width, height = max(root.winfo_reqwidth(), 300), root.winfo_reqheight()
root.geometry("+%d+%d" % (root.winfo_screenwidth() - width - 24, root.winfo_screenheight() - height - 80))
root.deiconify()
root.mainloop()
sys.exit(result[0])
"""
queue = []  # ids of queued jobs, first in line first
clock = {}  # job id or "all" -> time when the next chunk may be read (this is how speed limits work)
# job id -> job; the extension reads this. Every job has:
#   id, name, kind (file|stream), url, referrer, userAgent, title, path (where the file is saved)
#   state (queued|running|paused|stopped|done|failed|canceled), detail (short text, or the error)
#   limit (speed limit of this job in KB per second, 0 = none)
#   notified (True once the extension has shown the finished/failed notification)
#   done / total (bytes, total is None when unknown), percent (None when unknown),
#   speed (bytes per second), eta (seconds left, or None), elapsed (seconds spent downloading)
#   estimate (True for streams: total and eta are guesses), position (seconds of video so far)
#   added / finished (clock time, finished is None until the job ends)
#   variant (streams: the quality picked in the popup, ffmpeg's program number; None = highest)
#   subtitles (streams: [{url, name, language}] to save next to the video), subFormat ("vtt" | "srt")
#   audioOnly (save only the audio as mp3)
#   refresh (True when a started direct download failed with 403, 404 or 410: the link can be refreshed)
#   sha256 (checksum of the finished file, "" until the Checksum button), hashing (percent while it runs, or None)
#   private (True: never in history.json, stats.json, thumbs or the log; taken off the list once done or canceled)
#   scan (Windows Defender, after the download): "" (no scan: off, or not finished) | "scanning" | "clean" |
#     "threat" (the user still has to choose) | "deleted" | "kept" | "trusted" (skipped) | "unavailable" | "failed"
#   threat (the name Defender gives the threat, or "")
# "stopped" is a paused stream: ffmpeg cannot pause, so resume starts the stream again.
jobs = {}
# job id -> things that are never shown or saved:
#   stop ("" | "pause" | "cancel": what the worker thread should do), proc (ffmpeg), cookies
live = {}
lock = threading.RLock()  # held while the job list or the queue is changed or sent
disk_lock = threading.Lock()  # held while a file of ours is written, or the speed limit clock is changed
next_id = itertools.count(1)
keep_history = True  # off when one url is downloaded from the command line
DATE = "%Y-%m-%d %H:%M:%S"
LOG = os.path.join(OUT_DIR, "udm.log")  # written when the downloader runs without a console (pythonw)
log_lock = threading.Lock()  # held while a line is written or udm.log is rewritten
LINK = re.compile(r"\b(?:https?|ftp)://[^\s'\"<>|]+", re.I)
STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ")  # the time at the start of a log line
# what history.json keeps of each job. Cookies are never written to disk.
SAVED = ("id", "name", "kind", "url", "referrer", "userAgent", "title", "path", "done", "elapsed", "limit",
         "notified", "variant", "subtitles", "subFormat", "audioOnly", "refresh", "sha256", "scan", "threat")


def log(text, job=None):
    """One line for udm.log (or the console): the time, then the text. A web address is cut down to its
    site, so the log never has full urls (tokens, cookies). Nothing at all is written about a private job."""
    if job and job.get("private"):
        return
    text = LINK.sub(lambda m: urllib.parse.urlsplit(m.group()).hostname or "(link)", str(text))
    with log_lock:
        print(time.strftime(DATE), text, flush=True)


def site_of(job):
    """The site a download came from: the page's, else the file's, without "www." ("" when unknown)."""
    host = urllib.parse.urlsplit(job.get("referrer") or job.get("url") or "").hostname or ""
    return host[4:] if host.startswith("www.") else host


def history_rows():
    """The job list the way history.json (and a backup) keeps it."""
    rows = []
    for job in list(jobs.values()):
        if job["private"]:
            continue
        row = {key: job[key] for key in SAVED}
        row.update(size=job["total"], status=job["state"], error=job["detail"] if job["state"] == "failed" else "",
                   added=time.strftime(DATE, time.localtime(job["added"])),
                   finished=time.strftime(DATE, time.localtime(job["finished"])) if job["finished"] else None)
        rows.append(row)
    return rows


def save_history():
    """Write every job to history.json. Called whenever a job is added, removed or changes state."""
    if not keep_history:
        return
    rows = history_rows()
    with disk_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, "history.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=1)
        os.replace(path + ".tmp", path)


def load_history():
    """Fill the job list from history.json (at startup, and after Import). Jobs that were cut off come back as paused."""
    global next_id
    path = os.path.join(DATA_DIR, "history.json")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except Exception as e:
        log("could not read %s: %s" % (path, e))
        return
    for row in rows:
        job = {key: row.get(key) for key in SAVED}
        state = row.get("status")
        is_stream = job["kind"] == "stream"
        if state in ACTIVE:  # the downloader was closed in the middle
            state = "stopped" if is_stream else "paused"
        if state == "paused":
            job["done"] = saved_bytes(job)
        total = row.get("size")
        job.update(state=state, detail=row.get("error") or "", total=total, speed=0, eta=None,
                   percent=100.0 * job["done"] / total if total and not is_stream else None,
                   estimate=is_stream and state != "done", position=0,
                   added=time.mktime(time.strptime(row["added"], DATE)),
                   finished=time.mktime(time.strptime(row["finished"], DATE)) if row.get("finished") else None)
        if state == "done":
            job["percent"] = 100.0
        job["limit"] = job["limit"] or 0
        job["subtitles"] = job["subtitles"] or []
        job.update(refresh=bool(job["refresh"]), sha256=job["sha256"] or "", hashing=None, private=False,
                   scan=job["scan"] or "", threat=job["threat"] or "")
        if job["scan"] == "scanning":  # the downloader was closed during the scan
            job["scan"] = "failed"
        job["notified"] = job["notified"] is not False  # old news is not announced again
        jobs[job["id"]] = job
        live[job["id"]] = {"stop": "", "proc": None, "cookies": ""}
    next_id = itertools.count(max([int(x) for x in jobs] + [0]) + 1)
    log("history: %d jobs loaded from %s" % (len(jobs), path))


def find_ffmpeg():
    """ffmpeg from PATH, or an ffmpeg.exe dropped next to this script (or udm-downloader.exe)."""
    return shutil.which("ffmpeg") or shutil.which("ffmpeg", path=HERE)


def safe_name(text):
    """Turn a url file name or page title into a valid Windows file name."""
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text or "").strip(". ")[:120]


def free_path(path, url):
    """path itself, or "name (2).ext", "name (3).ext" ... so that nothing is overwritten."""
    stem, ext = os.path.splitext(path)
    n = 1
    while True:
        candidate = path if n == 1 else "%s (%d)%s" % (stem, n, ext)
        used = any(job["path"] == candidate and job["state"] != "done" for job in jobs.values())
        # part files nobody owns may be taken over, but only by the same url (that is a resume)
        left = candidate + ".progress.json"
        mine = True
        if os.path.exists(left):
            try:
                with open(left) as f:
                    mine = json.load(f).get("url") == url
            except Exception:
                mine = False
        if not os.path.exists(candidate) and not used and mine:
            return candidate
        n += 1


def ask_path(job_id):
    """Show a Save As window for a new job, then queue it. Closing the window cancels the job."""
    job = jobs[job_id]
    os.makedirs(os.path.dirname(job["path"]), exist_ok=True)
    # tkinter comes with Python. It runs in its own process so the window cannot block the downloader.
    code = ("import sys, tkinter, tkinter.filedialog as dialog\n"
            "root = tkinter.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
            "name = dialog.asksaveasfilename(parent=root, title='UDM: save as', "
            "initialdir=sys.argv[1], initialfile=sys.argv[2])\n"
            "sys.stdout.buffer.write(name.encode('utf-8'))")
    try:
        chosen = subprocess.run([PYTHON, "-c", code, os.path.dirname(job["path"]), job["name"]],
                                capture_output=True, stdin=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW).stdout.decode("utf-8").strip()
    except Exception as e:
        log("  could not show the Save As window (%s), saving to %s" % (e, job["name"]), job)
        chosen = job["path"]
    with lock:
        if job_id not in jobs:
            return
        if chosen:
            job.update(path=os.path.normpath(chosen), name=os.path.basename(chosen), detail="")
            start(job_id)
        else:
            job.update(state="canceled", detail="", finished=time.time())
    save_history()


def rate(samples, now, value):
    """Add one (time, value) sample and return the average change per second over the last few seconds."""
    samples.append((now, value))
    while len(samples) > 2 and now - samples[1][0] >= WINDOW:
        samples.pop(0)
    span = now - samples[0][0]
    return max(0.0, (value - samples[0][1]) / span) if span > 0 else 0.0


def request_headers(job, ctl):
    """The headers the browser would send for this job."""
    headers = {"User-Agent": job.get("userAgent") or "udm/1.0"}
    if job.get("referrer"):
        headers["Referer"] = job["referrer"]
    if ctl["cookies"]:
        headers["Cookie"] = ctl["cookies"]
    return headers


def source(job):
    """Where the downloaded bytes go. The job's own path, except for an audio only file: its video is
    downloaded to "name.mp3.video" first and turned into the mp3 afterwards."""
    return job["path"] + ".video" if job.get("audioOnly") and job["kind"] == "file" else job["path"]


def saved_bytes(job):
    """How much of a paused direct download is on disk: the progress json says so for the one download file,
    the part files themselves for progress saved by an older UDM (one file per part)."""
    try:
        with open(source(job) + ".progress.json") as f:
            state = json.load(f)
        if state.get("layout") == "one" and os.path.exists(source(job) + ".part"):
            return sum(p["done"] for p in state["parts"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return sum(os.path.getsize(x) for x in glob.glob(glob.escape(source(job)) + ".part[0-9]*"))


def remove_parts(job):
    """Delete what an unfinished job left on disk: part files and the progress json."""
    left = glob.glob(glob.escape(source(job)) + ".part*") + glob.glob(glob.escape(source(job)) + ".progress.json*")
    for path in left:
        try:
            os.remove(path)
        except OSError as e:
            log("  could not delete %s: %s" % (os.path.basename(path), e), job)


def current_limit():
    """The speed limit for all direct downloads together, in KB/s, 0 = none. Inside the bandwidth
    schedule's time range (which may pass midnight, like 22:00 to 06:00) that is limitSpeed."""
    if settings["limitSchedule"]:
        now, start, end = time.strftime("%H:%M"), settings["limitFrom"], settings["limitTo"]
        inside = start <= now < end if start <= end else (now >= start or now < end)
        if inside:
            return settings["limitSpeed"]
    return settings["speedLimit"]


def throttle(job, ctl, n):
    """Called after reading n bytes: wait so this job, and all jobs together, stay under their speed limits."""
    for key, limit in ((job["id"], job["limit"]), ("all", current_limit())):
        if limit:
            with disk_lock:
                # every chunk books the next free slot on the clock; a slot is as long as the chunk may take
                until = clock[key] = max(time.time(), clock.get(key, 0)) + n / (limit * 1024.0)
            while time.time() < until and not ctl["stop"]:
                time.sleep(min(0.2, max(0, until - time.time())))


# ---- per-site connection limits: every connection of every direct download to one site takes one of the site's
# slots (siteConnections, or its line in siteLimits). A connection that finds no free slot waits for one. ----
SLOW_CODES = (429, 503)  # the server asks us to slow down
SLOW_WAIT = 10  # seconds to wait after a slow down answer without a Retry-After header
SLOW_KEEP = 600  # seconds a halved site limit is kept after the site's last slow down answer
SLOW_DOWN = "site asked to slow down"  # job detail while waiting for the site
DROP = 0.5  # the site got at least this much slower after a connection was added: no more connections for it
# site -> used (connections open), waiting (connections waiting for a slot), limit (halved limit, None = the setting),
#   hit (time of the last slow down answer), until (no new connection before this time),
#   cap (most connections allowed because more made it slower, None = no cap),
#   bytes (read from the site so far), samples / sampled / speed (bytes per second over the last few seconds),
#   check (after a connection was added: {at, speed, used} to compare the speed with, or None)
sites = {}
site_lock = threading.Condition()  # held while sites changes, waited on for a free slot


def site_limits_of(text):
    """Per-site overrides, "x.com = 2" one per line -> {"x.com": 2}."""
    found = {}
    for line in text.splitlines():
        site, _, number = line.partition("=")
        site = re.sub(r"^www\.", "", clean_host(site).lower())
        if site and number.strip().isdigit():
            found[site] = min(max(int(number), 1), 32)
    return found


def site_limits():
    """The per-site overrides from the settings."""
    return site_limits_of(settings["siteLimits"])


def site_key(url):
    """The site whose slots a request to url uses: the override it falls under (subdomains too), else its host."""
    host = re.sub(r"^www\.", "", (urllib.parse.urlsplit(url).hostname or "").lower())
    for site in sorted(site_limits(), key=len, reverse=True):  # "a.x.com" before "x.com"
        if in_site(host, site):
            return site
    return host


def site_allowed(site, s):
    """How many connections the site may have now: the setting, halved after slow downs, capped after a speed drop."""
    allowed = site_limits().get(site, settings["siteConnections"])
    if s["limit"] and time.time() - s["hit"] > SLOW_KEEP:
        s["limit"] = None  # the site has not complained for a while
    return min(allowed, s["limit"] or allowed, s["cap"] or allowed)


def site_slowed(site):
    """True while the site asked us to slow down and we are still waiting for it."""
    with site_lock:
        return site in sites and time.time() < sites[site]["until"]


def site_room(site):
    """How many new connections to the site could start right now."""
    with site_lock:
        s = sites.get(site)
        if not s:
            return site_limits().get(site, settings["siteConnections"])
        if time.time() < s["until"]:
            return 0
        return max(0, site_allowed(site, s) - s["used"] - s["waiting"])


def site_take(job, ctl):
    """Take one of the site's connection slots, waiting until one is free and any slow down wait is over.
    Gives the site, or None when the job was paused or canceled while it waited. site_give() hands it back."""
    site = site_key(job["url"])
    with site_lock:
        s = sites.setdefault(site, {"used": 0, "waiting": 0, "limit": None, "hit": 0, "until": 0, "cap": None,
                                    "bytes": 0, "samples": [], "sampled": 0, "speed": 0.0, "check": None})
        s["waiting"] += 1
        while not ctl["stop"] and (time.time() < s["until"] or s["used"] >= site_allowed(site, s)):
            site_lock.wait(0.5)
        s["waiting"] -= 1
        got = not ctl["stop"]
        if got:
            now = time.time()
            # a connection is added while the site is busy: see in a few seconds whether that made it slower
            busy = s["samples"] and now - s["samples"][0][0] >= 2 and now - s["sampled"] < 1 and s["speed"] > 0
            if busy and s["used"] and not s["check"] and not s["cap"]:
                s["check"] = {"at": now, "speed": s["speed"], "used": s["used"]}
            s["used"] += 1
    return site if got else None


def site_give(site):
    """Hand back a slot taken with site_take()."""
    with site_lock:
        s = sites[site]
        s["used"] -= 1
        if not s["used"] and not s["waiting"]:  # the site is idle: its next download measures speed afresh
            s.update(cap=None, check=None, samples=[], sampled=0, speed=0.0)
        site_lock.notify_all()


def site_count(site, n, job):
    """Count n bytes read from the site. Every half second the site's speed is updated, and a few seconds after a
    connection was added it is compared with the speed before: much slower means the site gets no more connections."""
    with site_lock:
        s = sites[site]
        s["bytes"] += n
        now = time.time()
        if now - s["sampled"] < 0.5:
            return
        s["sampled"] = now
        s["speed"] = rate(s["samples"], now, s["bytes"])
        check = s["check"]
        if check and now - check["at"] > WINDOW + 1:  # the speed is all from after the new connection now
            s["check"] = None
            # fewer connections than before (a part finished) or a slow down wait also make it slower: not counted
            if s["used"] > check["used"] and s["until"] < check["at"] and s["speed"] < check["speed"] * DROP:
                s["cap"] = check["used"]
                log("  %s got slower with more connections (%.0f -> %.0f KB/s): no more than %d connections"
                    % (site, check["speed"] / 1024, s["speed"] / 1024, s["cap"]), job)


def retry_after(value):
    """Seconds from a Retry-After header ("120" or an http date), SLOW_WAIT when there is none or it makes no sense."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError, IndexError):
        return SLOW_WAIT


def slow_down(site, error, job):
    """The site answered 429 or 503: halve its connection limit and wait for its Retry-After (or SLOW_WAIT).
    The parts that get the same answer at the same time halve it only once."""
    wait = retry_after(error.headers.get("Retry-After") if error.headers else None)
    with site_lock:
        s = sites[site]
        now = time.time()
        if now >= s["until"]:
            s["limit"] = max(1, site_allowed(site, s) // 2)
            s["hit"] = now
            log("  %s asked to slow down (HTTP %d): %d connections now, waiting %.0fs"
                % (site, error.code, s["limit"], wait), job)
        s["until"] = max(s["until"], now + wait)
    job["detail"] = SLOW_DOWN


def fetch_part(job, headers, path, part, ctl, guard, first=None, wake=None):
    """Write one part's bytes at its place in the download file until its range is complete or the job is told
    to stop. The end of the range can move closer while this runs (dynamic segmentation): guard is held
    while it changes, and while bytes are counted, so no byte past the new end is ever written.
    first is (response, site) when the size check's connection is already reading this part's bytes; it holds a
    site slot, like every other try. A slow down answer (429, 503) is not a failed try. wake is set at the end."""
    left = lambda: part["end"] - part["start"] + 1 - part["done"]  # noqa: E731
    attempt = 0
    try:
        while attempt <= RETRIES and left() > 0 and not ctl["stop"]:
            failed = False
            if first:
                (r, site), first = first, None
            else:
                r, site = None, site_take(job, ctl)
                if site is None:
                    return
            try:
                if r is None:
                    rng = "bytes=%d-%d" % (part["start"] + part["done"], part["end"])
                    req = urllib.request.Request(job["url"], headers={**headers, "Range": rng})
                    r = urllib.request.urlopen(req, timeout=30)
                with r, open(path, "r+b", buffering=0) as f:
                    if r.status != 206:
                        raise IOError("server ignored the range request (status %d)" % r.status)
                    f.seek(part["start"] + part["done"])
                    while left() > 0 and not ctl["stop"]:
                        # small reads while a speed limit is on, so the limit is smooth
                        chunk = 8192 if job["limit"] or current_limit() else CHUNK
                        data = r.read(min(chunk, left()))
                        if not data:
                            break
                        with guard:
                            data = data[:left()]  # the part may have been split while we were reading
                            f.write(data)
                            part["done"] += len(data)
                        site_count(site, len(data), job)
                        throttle(job, ctl, len(data))
            except Exception as e:
                if getattr(e, "code", None) in SLOW_CODES:
                    slow_down(site, e, job)
                    continue  # wait for the site (in site_take), then the same try again
                ctl["error"] = str(e) or repr(e)
                ctl["code"] = getattr(e, "code", None)  # the http status, for Refresh link
                log("  part at byte %d failed (try %d of %d): %s" % (part["start"], attempt + 1, 1 + RETRIES, e), job)
                failed = True
            finally:
                site_give(site)
            attempt += 1
            if failed:
                time.sleep(1)
    finally:
        if first:  # stopped before the open response was read
            first[0].close()
            site_give(first[1])
        if wake:
            wake.set()


def download(job, ctl):
    """Direct file: parallel range requests. Returns an error message, or None when done."""
    # ---- input: the job from the browser ----
    url = job["url"]
    headers = request_headers(job, ctl)
    name = job["name"]
    target = source(job)
    state_path = target + ".progress.json"
    started = time.time() - job["elapsed"]  # time already spent before a pause counts too
    samples = []
    parts = max(1, int(settings["parts"]))

    def report(done, total):
        """Store bytes done, speed and time left in the job for the popup."""
        now = time.time()
        speed = rate(samples, now, done)
        job.update(done=done, total=total or None, speed=speed, elapsed=now - started,
                   percent=100.0 * done / total if total else None,
                   eta=(total - done) / speed if total and speed > 0 else None)

    # ---- saved progress: where the first missing byte is ----
    state = None
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = None
    gaps = [p["start"] + p["done"] for p in (state or {}).get("parts", []) if p["start"] + p["done"] <= p["end"]]
    at = min(gaps) if gaps else 0

    # ---- check range support and size, and start downloading in the same request ----
    # The request asks for everything from the first missing byte on. A 206 reply proves ranges work (same
    # meaning as Accept-Ranges: bytes) and Content-Range carries the full length; the connection then simply
    # goes on reading as the first part, so no round trip is spent on a separate size check.
    size = 0
    first = None  # (response, site): the open size check, handed to the part (or single download) that uses it
    setup = 0.0  # seconds from asking to the answer: what a new connection costs on this site
    while True:  # again after a slow down answer
        site = site_take(job, ctl)
        if site is None:
            return "stopped"
        job["detail"] = ""
        try:
            probe = urllib.request.Request(url, headers={**headers, "Range": "bytes=%d-" % at})
            asked = time.time()
            r = urllib.request.urlopen(probe, timeout=30)
            setup = time.time() - asked
        except urllib.error.HTTPError as e:
            site_give(site)
            if e.code in SLOW_CODES:
                slow_down(site, e, job)
                continue
            ctl["code"] = e.code
            if job["done"]:  # already started: the saved parts are worth more than a fresh single download
                return "%s (the link may have expired)" % e
            log("server refused the range check (HTTP %d), trying a single download" % e.code, job)
            break
        except Exception as e:
            site_give(site)
            # network trouble, not a "no ranges" answer: stop here so saved parts stay resumable
            return "cannot reach server: %s. Resume to try again." % e
        first = (r, site)
        if r.status == 206:
            try:
                where, _, total = r.headers["Content-Range"].partition(" ")[2].partition("/")
                if int(where.split("-")[0]) == at:
                    size = int(total)
            except (AttributeError, ValueError):
                pass
        if not size:
            log("no range support (Accept-Ranges: %s, Content-Length: %s)"
                % (r.headers.get("Accept-Ranges"), r.headers.get("Content-Length")), job)
            if r.status != 200:  # not the whole file from byte 0: no use for a single download
                r.close()
                site_give(site)
                first = None
        break

    def drop_first():
        """Close the size check's connection when nobody is going to read it."""
        nonlocal first
        if first:
            first[0].close()
            site_give(first[1])
            first = None

    def no_room(need):
        """An error message when the disk cannot take `need` more bytes, else None."""
        free = shutil.disk_usage(os.path.dirname(target)).free
        if need > free:
            return ("not enough disk space: %.1f MB needed, %.1f MB free on %s"
                    % (need / 1048576, free / 1048576, os.path.splitdrive(target)[0] or os.path.dirname(target)))

    # ---- no ranges: normal single download (pause works, but resume starts it again) ----
    if not size:
        log("single download: %s" % name, job)
        while True:  # again after a slow down answer
            if first:  # the size check already got the whole file from byte 0: keep reading it
                (r, site), first = first, None
            else:
                r, site = None, site_take(job, ctl)
                if site is None:
                    return "stopped"
            job["detail"] = ""
            try:
                if r is None:
                    r = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)
                with r, open(target + ".part", "wb") as f:
                    total = int(r.headers.get("Content-Length") or 0)
                    if no_room(total):
                        return no_room(total)
                    done = last = 0
                    while not ctl["stop"]:
                        data = r.read(8192 if job["limit"] or current_limit() else CHUNK)
                        if not data:
                            break
                        f.write(data)
                        done += len(data)
                        site_count(site, len(data), job)
                        throttle(job, ctl, len(data))
                        if time.time() - last >= 0.5:
                            report(done, total)
                            last = time.time()
            except Exception as e:
                if getattr(e, "code", None) in SLOW_CODES:
                    slow_down(site, e, job)
                    continue
                ctl["code"] = getattr(e, "code", None)
                return str(e)
            finally:
                site_give(site)
            break
        if ctl["stop"]:
            return "stopped"
        os.replace(target + ".part", target)
        log("done: %s (%d bytes, %.1fs)" % (name, os.path.getsize(target), time.time() - started), job)
        return None

    # ---- ranges: saved progress, or one part for the whole file ----
    # All parts write into one file, name.ext.part, each at its own place, so there is no merge at the end:
    # the finished file is only renamed. The progress json is the truth about which bytes are there.
    one = target + ".part"
    resumed = bool(state and state.get("url") == url and state.get("size") == size and state.get("parts"))
    legacy = resumed and state.get("layout") != "one"  # saved by an older UDM: one file per part
    if resumed and not legacy and (not os.path.exists(one) or os.path.getsize(one) != size):
        resumed = False
    if not resumed:
        state = {"url": url, "size": size, "layout": "one", "parts": [{"start": 0, "end": size - 1, "done": 0}]}
    if no_room(0 if resumed and not legacy else size):
        drop_first()
        return no_room(size)
    if legacy:
        legacy_parts(target, one, state)
    elif not resumed:
        with open(one, "wb") as f:
            f.truncate(size)  # the whole size at once: a full disk shows now, not halfway
    already = sum(p["done"] for p in state["parts"])
    log("%s: %s, %d bytes, up to %d connections (%d bytes already on disk)"
        % ("resuming" if resumed else "starting", name, size, parts, already), job)
    job["total"] = size
    save_history()  # the size is known now, keep it for after a restart

    # ---- process: parallel range requests, progress saved to json ----
    guard = threading.Lock()  # held while a part's range or byte count changes
    wake = threading.Event()  # set when a part's connection ends, so its slot is used again right away
    threads = {}  # part index -> the thread downloading it
    speeds = {}  # part index -> samples, for the speed of each connection
    left_of = lambda i: state["parts"][i]["end"] - state["parts"][i]["start"] + 1 - state["parts"][i]["done"]  # noqa

    def launch(i, given=None):
        threads[i] = threading.Thread(target=fetch_part, daemon=True, args=(
            job, headers, one, state["parts"][i], ctl, guard, given, wake))
        threads[i].start()

    mine = [i for i, p in enumerate(state["parts"]) if p["start"] + p["done"] == at and left_of(i) > 0]
    if first and mine:
        launch(mine[0], first)  # the size check's connection goes on as this part
        first = None
    drop_first()
    # A new connection is only worth it when the part it halves has a while to go: the new one first waits for
    # its answer (the size check shows how long that takes on this site) and then starts slow, the way TCP does.
    # On a far away server a late split of a small rest was measured to end the download a second later.
    split_time = max(SPLIT_TIME, 6 * setup)
    last_print = 0
    while True:
        now = time.time()
        rates = {i: rate(speeds.setdefault(i, []), now, p["done"]) for i, p in enumerate(state["parts"])}
        running = [i for i, t in threads.items() if t.is_alive()]
        failed_part = any(not t.is_alive() and left_of(i) > 0 for i, t in threads.items())
        waiting = [i for i in range(len(state["parts"])) if i not in threads and left_of(i) > 0]  # after a resume
        # More connections, up to `parts` and the site's free slots: a saved part that has no connection gets
        # one first; otherwise the part that will take longest is split in half and a new connection takes the
        # second half. Right after the size check that cuts the file into even pieces; later it goes on to the
        # very end, so a slow last part gets help too. All connections start at once: adding them a few at a
        # time (IDM's 1, 4, 8) was measured slower on far away servers, where each new connection pays its own
        # setup and slow start. Too many connections for a site is caught by site_count's speed check instead.
        # A part that ended with an error stops all of this (the job is going to fail anyway).
        if not ctl["stop"] and not failed_part:
            room = min(parts - len(running), site_room(site_key(url)))
            for _ in range(room):
                if waiting:
                    launch(waiting.pop(0))
                    continue
                with guard:
                    worth = {}
                    known = [rates[i] for i in running if rates.get(i)]
                    typical = sum(known) / len(known) if known else 0.0  # for a part too new to have a speed
                    for i in running:
                        n, speed = left_of(i), rates.get(i) or typical
                        if n >= MIN_SPLIT and (n / speed >= split_time if speed > 0 else n >= 4 * MIN_SPLIT):
                            worth[i] = n / max(speed, 1.0)  # time left
                    if not worth:
                        break
                    slow = max(worth, key=worth.get)
                    old = state["parts"][slow]
                    mid = old["start"] + old["done"] + left_of(slow) // 2
                    state["parts"].append({"start": mid, "end": old["end"], "done": 0})
                    old["end"] = mid - 1
                new = len(state["parts"]) - 1
                launch(new)
                running.append(new)
                log("  split part %d at byte %d: %d connections, %d parts"
                    % (slow, mid, len(running), len(state["parts"])), job)
        with guard:
            with open(state_path + ".tmp", "w") as f:
                json.dump(state, f)
        os.replace(state_path + ".tmp", state_path)
        done = sum(p["done"] for p in state["parts"])
        report(done, size)
        job["detail"] = SLOW_DOWN if site_slowed(site_key(url)) else "%.1f%%" % job["percent"]
        alive = any(t.is_alive() for t in threads.values())
        over = not alive and (not waiting or ctl["stop"] or failed_part)
        if time.time() - last_print > 2 or over:
            log("  %s  %5.1f%%  %d / %d" % (name, 100.0 * done / size, done, size), job)
            last_print = time.time()
        if over:
            break
        wake.wait(0.5)
        wake.clear()

    if done != size:  # paused, canceled, or a part that kept failing: the file and json stay for a resume
        return ("a part still failed after %d retries (%s). %d of %d bytes are saved, Retry continues from there."
                % (RETRIES, ctl.get("error") or "connection closed", done, size))

    # ---- output: the finished file gets its name, clean up ----
    os.replace(one, target)
    remove_parts(job)
    log("done: %s (%d bytes, %.1fs)" % (name, os.path.getsize(target), time.time() - started), job)
    return None


def legacy_parts(target, one, state):
    """Progress saved by an older UDM, one file per part (name.part0, name.part1 ...): copy what each has
    into its place in the one download file, then delete them. The part files are the truth here."""
    with open(one, "wb") as out:
        out.truncate(state["size"])
        for i, part in enumerate(state["parts"]):
            path = "%s.part%d" % (target, i)
            have = os.path.getsize(path) if os.path.exists(path) else 0
            if have > part["end"] - part["start"] + 1:
                have = 0
            if have:
                out.seek(part["start"])
                with open(path, "rb") as f:
                    shutil.copyfileobj(f, out, 1024 * 1024)
            part["done"] = have
    for i in range(len(state["parts"])):
        if os.path.exists("%s.part%d" % (target, i)):
            os.remove("%s.part%d" % (target, i))
    state["layout"] = "one"


# ---- proxy: settings page, Network. Every urllib request (direct downloads, playlists, subtitles) goes through
# the opener installed by use_proxy(); ffmpeg and ffprobe get the proxy with -http_proxy, see ffmpeg_proxy() ----
PROXY_MODES = ("none", "windows", "manual")
PROXY_TYPES = ("http", "socks5")
LOOPBACK = ("127.0.0.1", "localhost", "::1")  # never through a proxy
PROXY_TEST = os.environ.get("UDM_PROXY_TEST") or "http://www.msftconnecttest.com/connecttest.txt"  # what Windows itself asks
NO_SOCKS_FFMPEG = "ffmpeg cannot use a SOCKS5 proxy, so streams connect directly"
SOCKS_ERRORS = {1: "general failure", 2: "not allowed by its rules", 3: "network unreachable", 4: "host unreachable",
                5: "connection refused", 6: "time to live expired", 7: "command not supported", 8: "address type not supported"}
windows_proxy = None  # the Windows proxy settings the installed opener was built with (mode "windows")


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]


def dpapi(data, protect):
    """Windows DPAPI: encrypt (protect=True) or decrypt bytes. Only the same Windows user on this PC can decrypt."""
    crypt32, kernel32 = ctypes.WinDLL("crypt32"), ctypes.WinDLL("kernel32")
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not call(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(out)):  # 1 = no UI
        raise OSError("DPAPI: %s" % ctypes.FormatError().strip())
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        kernel32.LocalFree(ctypes.cast(out.data, ctypes.c_void_p))


def protect(password):
    """A proxy password as settings.json keeps it."""
    return "dpapi:" + base64.b64encode(dpapi(password.encode("utf-8"), True)).decode() if password else ""


def proxy_password():
    """The saved proxy password in plain text ("" when there is none, or it cannot be read: another user or PC)."""
    saved = settings["proxyPassword"]
    if not saved.startswith("dpapi:"):
        return ""
    try:
        return dpapi(base64.b64decode(saved[6:]), False).decode("utf-8")
    except Exception as e:
        log("the saved proxy password cannot be read (%s); type it again on the settings page" % e)
        return ""


def public_settings():
    """The settings for the extension pages and backups: never the password, only whether one is saved."""
    return {**settings, "proxyPassword": "", "proxyPasswordSet": bool(settings["proxyPassword"])}


def proxy_conf(form=None):
    """The proxy as a dict: mode, type, host, port, user, password. From the settings, or from the settings
    page's Test button (form: what is typed there; an empty password means the saved one)."""
    s = {**settings, **(form or {})}
    password = (form or {}).get("proxyPassword") or proxy_password()
    return {"mode": s["proxyMode"], "type": s["proxyType"], "host": clean_host(s["proxyHost"]),
            "port": int(s["proxyPort"] or 0), "user": s["proxyUser"], "password": password if s["proxyUser"] else ""}


def clean_host(text):
    """ "http://proxy.lan:8080/" -> "proxy.lan" (the port has its own box)."""
    host = re.sub(r"^[a-z0-9]+://", "", str(text or "").strip(), flags=re.I).split("/")[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def socks_connect(conf, host, port, timeout):
    """A connection to host:port through a SOCKS5 proxy (RFC 1928, login RFC 1929). The proxy looks up the
    name, so DNS goes through it too. This PC itself is reached directly."""
    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
        timeout = None
    if host in LOOPBACK:
        return socket.create_connection((host, port), timeout)
    sock = socket.create_connection((conf["host"], conf["port"]), timeout)

    def read(n):
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise OSError("the SOCKS5 proxy closed the connection")
            data += chunk
        return data

    try:
        user, password = conf["user"].encode("utf-8"), conf["password"].encode("utf-8")
        sock.sendall(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")  # no login, or user name and password
        version, method = read(2)
        if version != 5:
            raise OSError("this is not a SOCKS5 proxy")
        if method == 2:
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            if read(2)[1] != 0:
                raise OSError("the SOCKS5 proxy did not accept the user name and password")
        elif method != 0:
            raise OSError("the SOCKS5 proxy needs a user name and password" if not user
                          else "the SOCKS5 proxy accepts none of the login methods UDM has")
        name = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + int(port).to_bytes(2, "big"))
        _, reply, _, kind = read(4)
        if reply:
            raise OSError("SOCKS5 proxy: %s" % SOCKS_ERRORS.get(reply, "error %d" % reply))
        read({1: 4, 4: 16}.get(kind) or read(1)[0])  # the address the proxy used, then its port
        read(2)
        return sock
    except BaseException:
        sock.close()
        raise


def socks_connection(base, conf):
    """http.client's connection class, connecting through the SOCKS5 proxy (https is then wrapped as usual)."""
    class Connection(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._create_connection = lambda address, timeout=None, source_address=None: \
                socks_connect(conf, address[0], address[1], timeout)
    return Connection


class SocksHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, conf):
        super().__init__()
        self.conf = conf

    def http_open(self, req):
        return self.do_open(socks_connection(http.client.HTTPConnection, self.conf), req)


class SocksHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, conf):
        super().__init__()
        self.conf = conf

    def https_open(self, req):
        return self.do_open(socks_connection(http.client.HTTPSConnection, self.conf), req, context=self._context)


class HTTPProxyHandler(urllib.request.BaseHandler):
    """Manual HTTP proxy: every request except to this PC goes to it (https through CONNECT)."""
    handler_order = 100  # before the http and https handlers, like urllib's own ProxyHandler

    def __init__(self, conf):
        self.hostport = "[%s]:%d" % (conf["host"], conf["port"]) if ":" in conf["host"] else "%s:%d" % (conf["host"], conf["port"])
        login = "%s:%s" % (conf["user"], conf["password"])
        self.auth = "Basic " + base64.b64encode(login.encode("utf-8")).decode() if conf["user"] else ""

    def http_open(self, req):
        if urllib.parse.urlsplit(req.full_url).hostname in LOOPBACK:
            return None
        if self.auth:
            req.add_header("Proxy-authorization", self.auth)
        req.set_proxy(self.hostport, "http")
        return None  # the http or https handler sends it

    https_open = http_open


def proxy_opener(conf):
    """A urllib opener for this proxy. ProxyHandler({}) keeps urllib from using proxies on its own."""
    direct = urllib.request.ProxyHandler({})
    if conf["mode"] == "windows":
        return urllib.request.build_opener(urllib.request.ProxyHandler(urllib.request.getproxies_registry()))
    if conf["mode"] == "manual" and conf["type"] == "socks5":
        return urllib.request.build_opener(direct, SocksHTTPHandler(conf), SocksHTTPSHandler(conf))
    if conf["mode"] == "manual":
        return urllib.request.build_opener(direct, HTTPProxyHandler(conf))
    return urllib.request.build_opener(direct)


def use_proxy():
    """Install the opener for the proxy settings. Called when they change, and by tick() for the Windows proxy."""
    global windows_proxy
    windows_proxy = urllib.request.getproxies_registry() if settings["proxyMode"] == "windows" else None
    urllib.request.install_opener(proxy_opener(proxy_conf()))


def proxy_text(conf):
    """ "socks5 proxy.lan:1080" for the log and the Test button."""
    if conf["mode"] == "windows":
        proxies = urllib.request.getproxies_registry()
        return "the Windows proxy (%s)" % (proxies.get("https") or proxies.get("http") or "none set in Windows")
    return "%s proxy %s:%d" % (conf["type"], conf["host"], conf["port"]) if conf["mode"] == "manual" else "no proxy"


def test_proxy(form):
    """Settings page, Test: one small request through the proxy typed there. Returns the reply for the page."""
    conf = proxy_conf({k: form[k] for k in ("proxyMode", "proxyType", "proxyHost", "proxyUser", "proxyPassword")
                       if isinstance(form.get(k), str)} | ({"proxyPort": form["proxyPort"]} if type(form.get("proxyPort")) is int else {}))
    if conf["mode"] == "manual" and not (conf["host"] and 0 < conf["port"] < 65536):
        return {"ok": False, "error": "type the proxy's address and port first"}
    started = time.time()
    try:
        req = urllib.request.Request(PROXY_TEST, headers={"User-Agent": "udm/1.0"})
        with proxy_opener(conf).open(req, timeout=15) as r:
            r.read(4096)
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "the proxy wants a user name and password (407)" if e.code == 407
                else "the test page answered %d %s" % (e.code, e.reason)}
    except Exception as e:
        reason = getattr(e, "reason", None) or e
        return {"ok": False, "error": str(reason) or repr(reason)}
    return {"ok": True, "ms": round((time.time() - started) * 1000), "via": proxy_text(conf)}


def ffmpeg_env():
    """ffmpeg would take http_proxy from the environment by itself; UDM's proxy setting decides instead."""
    return {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "no_proxy")}


def ffmpeg_proxy(url):
    """The -http_proxy value for ffmpeg and ffprobe ("" = none), and a note when the proxy cannot be used."""
    host, scheme = urllib.parse.urlsplit(url).hostname or "", urllib.parse.urlsplit(url).scheme
    if host in LOOPBACK:
        return "", ""
    if settings["proxyMode"] == "manual":
        conf = proxy_conf()
        if conf["type"] == "socks5":
            return "", NO_SOCKS_FFMPEG
        login = "%s:%s@" % (urllib.parse.quote(conf["user"], safe=""), urllib.parse.quote(conf["password"], safe="")) if conf["user"] else ""
        return "http://%s%s:%d" % (login, "[%s]" % conf["host"] if ":" in conf["host"] else conf["host"], conf["port"]), ""
    if settings["proxyMode"] == "windows":
        proxy = urllib.request.getproxies_registry().get(scheme, "")
        if not proxy or urllib.request.proxy_bypass_registry(host):
            return "", ""
        if proxy.lower().startswith("socks"):
            return "", NO_SOCKS_FFMPEG
        return proxy if "://" in proxy else "http://" + proxy, ""
    return "", ""


# ---- virus scan: Windows Defender's command line scanner, on each finished download ----
defender = {"exe": "", "available": None, "reason": "", "checked": 0}  # see check_defender()


def find_mpcmdrun():
    """Defender's command line scanner: the newest one in its platform folder, else the old place."""
    if os.environ.get("UDM_MPCMDRUN"):  # only for testing: a stand-in scanner
        return os.environ["UDM_MPCMDRUN"]
    platform = os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData", "Microsoft", "Windows Defender", "Platform")
    found = sorted(glob.glob(os.path.join(glob.escape(platform), "*", "MpCmdRun.exe")),
                   key=lambda x: [int(n) for n in re.findall(r"\d+", os.path.basename(os.path.dirname(x)))])
    old = os.path.join(os.environ.get("ProgramFiles") or r"C:\Program Files", "Windows Defender", "MpCmdRun.exe")
    return found[-1] if found else old if os.path.exists(old) else ""


def mpcmdrun(exe, path):
    """Scan one file. -DisableRemediation: Defender only reports, so the user decides. Its exit code is 2 for a
    threat and for an error alike, so its output decides. Returns ("clean" | "threat" | "failed", threat name or error)."""
    try:
        out = subprocess.run([exe, "-Scan", "-ScanType", "3", "-File", path, "-DisableRemediation"], capture_output=True,
                             text=True, errors="replace", timeout=1800, stdin=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        return "failed", str(e) or repr(e)
    text = out.stdout + out.stderr
    found = re.search(r"found (\d+) threats", text)
    name = re.search(r"^\s*Threat\s*:\s*(.+?)\s*$", text, re.M)
    if found and int(found.group(1)) > 0 or name:
        return "threat", name.group(1) if name else ""
    if "found no threats" in text:
        return "clean", ""
    hr = re.search(r"hr = (0x[0-9a-f]+)", text, re.I)
    return "failed", "MpCmdRun failed (%s)" % (hr.group(1) if hr else "exit %d" % out.returncode)


def check_defender(again=False):
    """Can Defender scan? Not when it is missing, or turned off because another antivirus is installed. Found out
    with a test scan of a small file, at most every 10 minutes (again=True: now, for the settings page)."""
    if defender["available"] is not None and not again and time.time() - defender["checked"] < 600:
        return defender
    exe, available, reason = find_mpcmdrun(), False, ""
    if not exe:
        reason = "Windows Defender is not installed"
    elif not os.environ.get("UDM_MPCMDRUN") and not re.search(r"STATE\s*:\s*4\b", subprocess.run(
            ["sc", "query", "WinDefend"], capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW).stdout):
        reason = "Windows Defender is turned off. Another antivirus may be installed"
    else:
        probe = os.path.join(tempfile.gettempdir(), "udm-scan-check.txt")
        with open(probe, "w") as f:
            f.write("UDM checks that Windows Defender can scan a file.")
        result, detail = mpcmdrun(exe, probe)
        os.remove(probe)
        available = result == "clean"
        reason = "" if available else "Windows Defender did not scan a test file: %s" % (detail or result)
    if available != defender["available"]:
        log("virus scan: %s" % ("Windows Defender is ready" if available else reason))
    defender.update(exe=exe, available=available, reason=reason, checked=time.time())
    return defender


def trusted(job):
    """Is the download's page or file on one of the trusted sites (or a subdomain)?"""
    sites = [re.sub(r"^www\.", "", clean_host(x).lower()) for x in settings["trustedSites"].replace(",", " ").split()]
    hosts = [(urllib.parse.urlsplit(job.get(k) or "").hostname or "").lower() for k in ("referrer", "url")]
    return any(in_site(host, site) for host in hosts for site in sites)


def scan(job):
    """A download just finished: let Defender scan the file (unless the scan is off, the site is trusted, or
    Defender cannot scan). Sets job["scan"] and job["threat"]."""
    job["scan"], job["threat"] = "", ""
    if not settings["scanDownloads"]:
        return
    if trusted(job):
        job["scan"] = "trusted"
        return
    if not check_defender()["available"]:
        job["scan"] = "unavailable"
        return
    job.update(scan="scanning", speed=0, eta=None)
    result, detail = mpcmdrun(defender["exe"], job["path"])
    job.update(scan=result, threat=detail if result == "threat" else "")
    log("scan of %s: %s%s" % (job["name"], result, ": " + detail if detail else ""), job)


def stream_duration(ffmpeg, url, headers, job=None):
    """Total length of a stream in seconds, or 0 when it cannot be found (for example a live stream).
    job: for the log only (a private one is not logged)."""
    # ---- ffprobe, when it sits next to ffmpeg ----
    probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe")
    if os.path.exists(probe):
        try:
            cmd = [probe, "-v", "error", "-user_agent", headers["User-Agent"]]
            if ffmpeg_proxy(url)[0]:
                cmd += ["-http_proxy", ffmpeg_proxy(url)[0]]
            extra = "".join("%s: %s\r\n" % kv for kv in headers.items() if kv[0] != "User-Agent")
            if extra:
                cmd += ["-headers", extra]
            cmd += ["-show_entries", "format=duration", "-of", "csv=p=0", url]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                                 env=ffmpeg_env(), creationflags=subprocess.CREATE_NO_WINDOW).stdout
            return float(out.strip())
        except Exception as e:
            log("  ffprobe gave no duration (%s), reading the playlist instead" % e, job)

    # ---- no ffprobe: read the playlist ourselves ----
    def fetch(link):
        with urllib.request.urlopen(urllib.request.Request(link, headers=headers), timeout=15) as r:
            return r.geturl(), r.read(2000000).decode("utf-8", "replace")

    try:
        url, text = fetch(url)
        # master playlist: follow the highest quality (highest BANDWIDTH) media playlist
        best = None
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        for i, line in enumerate(lines[:-1]):
            if line.startswith("#EXT-X-STREAM-INF"):
                found = re.search(r"BANDWIDTH=(\d+)", line)
                bandwidth = int(found.group(1)) if found else 0
                if not lines[i + 1].startswith("#") and (best is None or bandwidth > best[0]):
                    best = (bandwidth, lines[i + 1])
        if best:
            url, text = fetch(urllib.parse.urljoin(url, best[1]))
        # media playlist: add up the segment lengths. No ENDLIST means it is still growing (live).
        if "#EXT-X-ENDLIST" in text:
            return sum(float(x) for x in re.findall(r"#EXTINF:\s*([\d.]+)", text))
        # DASH manifest: the length is written at the top, like PT1H2M3.5S
        found = re.search(r'mediaPresentationDuration="PT(?:([\d.]+)H)?(?:([\d.]+)M)?(?:([\d.]+)S)?"', text)
        if found:
            h, m, sec = (float(x or 0) for x in found.groups())
            return h * 3600 + m * 60 + sec
    except Exception as e:
        log("  could not read the stream duration: %s" % e, job)
    return 0


def read_master(url, headers, private=False):
    """The qualities and subtitle tracks of an HLS master playlist, for the popup's picker.
    Returns {variants: [{index, width, height, bandwidth, size}], subtitles: [{url, name, language}], duration}.
    index is ffmpeg's program number for that quality; size is an estimate in bytes (None if unknown).
    A media playlist (one quality only) gives two empty lists."""
    # ---- input: the playlist text ----
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
        base, text = r.geturl(), r.read(2000000).decode("utf-8", "replace")

    # ---- process: one entry per #EXT-X-STREAM-INF, and per #EXT-X-MEDIA subtitle track ----
    variants, subtitles = [], []
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    for i, line in enumerate(lines):
        tag, _, rest = line.partition(":")
        attrs = {k: v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', rest)}
        if tag == "#EXT-X-STREAM-INF" and i + 1 < len(lines) and not lines[i + 1].startswith("#"):
            width, _, height = attrs.get("RESOLUTION", "").partition("x")
            variants.append({"index": len(variants), "width": int(width or 0), "height": int(height or 0),
                             "bandwidth": int(attrs.get("BANDWIDTH") or 0),
                             "average": int(attrs.get("AVERAGE-BANDWIDTH") or attrs.get("BANDWIDTH") or 0)})
        elif tag == "#EXT-X-MEDIA" and attrs.get("TYPE") == "SUBTITLES" and attrs.get("URI"):
            link = urllib.parse.urljoin(base, attrs["URI"])
            if not any(sub["url"] == link for sub in subtitles):
                subtitles.append({"url": link, "name": attrs.get("NAME") or attrs.get("LANGUAGE") or "subtitles",
                                  "language": attrs.get("LANGUAGE", "")})

    # ---- output: with an estimated size from the average bitrate and the length ----
    duration = stream_duration(find_ffmpeg() or "", url, headers, {"private": private}) if variants else 0
    for v in variants:
        v["size"] = int(v.pop("average") / 8 * duration) if duration else None
    return {"variants": variants, "subtitles": subtitles, "duration": duration}


def srt_time(stamp):
    """WebVTT time to SubRip time: "01:02.500" or "00:01:02.500" -> "00:01:02,500"."""
    return ("00:" + stamp if stamp.count(":") == 1 else stamp).replace(".", ",")


def save_subtitles(job, ctl):
    """Save the subtitle tracks picked in the popup next to the finished video, as .vtt or .srt.
    Each track is an HLS playlist of WebVTT pieces. Returns a short note for the job's status."""
    headers = request_headers(job, ctl)
    stem = os.path.splitext(job["path"])[0]
    ext = "srt" if job.get("subFormat") == "srt" else "vtt"
    saved, failed = [], []
    for sub in job["subtitles"]:
        try:
            # ---- input: the track's playlist, then every piece it lists ----
            with urllib.request.urlopen(urllib.request.Request(sub["url"], headers=headers), timeout=30) as r:
                base, text = r.geturl(), r.read().decode("utf-8-sig", "replace")
            pieces = [text] if text.startswith("WEBVTT") else []
            for line in text.splitlines() if not pieces else []:
                if line.strip() and not line.startswith("#"):
                    link = urllib.parse.urljoin(base, line.strip())
                    with urllib.request.urlopen(urllib.request.Request(link, headers=headers), timeout=30) as r:
                        pieces.append(r.read().decode("utf-8-sig", "replace"))
            # ---- process: keep the cues, drop each piece's own header and cues repeated across pieces ----
            cues = []
            for piece in pieces:
                if not piece.startswith("WEBVTT"):
                    raise IOError("not a WebVTT subtitle")
                for block in re.split(r"\n\s*\n", piece.replace("\r\n", "\n")):
                    if "-->" in block and block.strip() not in cues:
                        cues.append(block.strip())
            # ---- output: one file next to the video, named after the language ----
            path = "%s.%s.%s" % (stem, safe_name(sub.get("language") or sub.get("name")) or "subtitles", ext)
            if ext == "vtt":
                out = "WEBVTT\n\n" + "\n\n".join(cues) + "\n"
            else:
                blocks = []
                for n, cue in enumerate(cues, 1):
                    lines = cue.split("\n")
                    at = next(i for i, x in enumerate(lines) if "-->" in x)  # a cue may start with its own id
                    start, _, end = lines[at].partition("-->")
                    end = end.split()[0]  # drop cue settings like "align:start"
                    blocks.append("%d\n%s --> %s\n%s" % (n, srt_time(start.strip()), srt_time(end),
                                                        "\n".join(lines[at + 1:])))
                out = "\n\n".join(blocks) + "\n"
            with open(path, "w", encoding="utf-8") as f:
                f.write(out)
            saved.append(os.path.basename(path))
            log("  subtitles saved: %s (%d cues)" % (os.path.basename(path), len(cues)), job)
        except Exception as e:
            failed.append("%s: %s" % (sub.get("name"), e))
            log("  subtitles %s failed: %s" % (sub.get("name"), e), job)
    note = "%d subtitle%s saved" % (len(saved), "" if len(saved) == 1 else "s") if saved else ""
    if failed:
        note += ("; " if note else "") + "subtitles failed: " + ", ".join(failed)
    return note


def to_mp3(job, ctl):
    """Audio only for a direct file: turn the downloaded video into the mp3, then delete the video.
    Returns an error message, or None."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return NO_FFMPEG
    video, part = source(job), job["path"] + ".part"
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", video, "-vn", "-sn",
           "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", part]
    job["detail"] = "converting to mp3"
    proc = ctl["proc"] = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                          creationflags=subprocess.CREATE_NO_WINDOW)
    errors = proc.stdout.read().strip()
    code = proc.wait()
    ctl["proc"] = None
    if code != 0 or ctl["stop"] or not os.path.exists(part):
        if os.path.exists(part):
            os.remove(part)
        return "stopped" if ctl["stop"] else "mp3 conversion failed: %s" % (errors.splitlines()[-1] if errors else code)
    os.replace(part, job["path"])
    os.remove(video)
    return None


def stream(job, ctl):
    """HLS/DASH stream. A 5xx or network error is tried again after 5s, 15s and 30s; 403, 404 and
    anything else fail right away. Returns an error message, or None when done."""
    for tries in range(1 + len(STREAM_WAITS)):
        error = stream_once(job, ctl)
        if not error or ctl["stop"] or "Server returned 4" in error or not TEMPORARY.search(error):
            return error
        if tries == len(STREAM_WAITS):
            return "%s. %s" % (error, EXPIRED)
        log("  %s: %s, retry %d of %d in %ds" % (job["name"], error, tries + 1, len(STREAM_WAITS), STREAM_WAITS[tries]), job)
        # the popup shows this until the new try reports progress
        job.update(detail="retrying (%d/%d)" % (tries + 1, len(STREAM_WAITS)),
                   done=0, total=None, percent=None, position=0, speed=0, eta=None)
        wake = time.time() + STREAM_WAITS[tries]
        while time.time() < wake:
            if ctl["stop"]:  # stop or cancel arrived while waiting
                return "stopped"
            time.sleep(0.2)


def stream_once(job, ctl):
    """One try: let ffmpeg fetch and join the stream. Returns an error message, or None when done."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return NO_FFMPEG

    # ---- input: the job from the browser, turned into ffmpeg options ----
    name = job["name"]
    target = job["path"]
    part = target + ".part"
    headers = request_headers(job, ctl)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
           "-user_agent", headers["User-Agent"]]
    extra = "".join("%s: %s\r\n" % kv for kv in headers.items() if kv[0] != "User-Agent")
    if extra:
        cmd += ["-headers", extra]
    # With a master playlist ffmpeg sees every quality and, with no -map given,
    # keeps the highest resolution video and the best audio. "-c copy" = no re-encoding.
    # ffmpeg itself reconnects for up to about 10 seconds when the server answers 5xx or the network drops
    cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
            "-reconnect_on_http_error", "5xx", "-reconnect_delay_max", "10"]
    proxy, note = ffmpeg_proxy(job["url"])
    if proxy:
        cmd += ["-http_proxy", proxy]
    elif note:
        log("  %s: %s" % (name, note), job)
    cmd += ["-i", job["url"]]
    audio = job.get("audioOnly")
    if job.get("variant") is not None:  # the quality picked in the popup: ffmpeg's program N is quality N
        n = int(job["variant"])
        cmd += ([] if audio else ["-map", "0:p:%d:v?" % n]) + ["-map", "0:p:%d:a?" % n]
    # subtitles never go into the file (-sn): picked ones are saved next to it, see save_subtitles
    if audio:
        cmd += ["-vn", "-sn", "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", part]
    else:
        cmd += ["-sn", "-c", "copy", "-f", "mp4", part]
    started = time.time()
    duration = stream_duration(ffmpeg, job["url"], headers, job)  # 0 = unknown
    log("starting stream: %s (%s)" % (name, "%.0fs of video" % duration if duration else "duration unknown"), job)
    if ctl["stop"]:
        return "stopped"

    # ---- process: run ffmpeg, read its progress lines, keep anything else as the error ----
    proc = ctl["proc"] = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                          env=ffmpeg_env(), creationflags=subprocess.CREATE_NO_WINDOW)
    if ctl["stop"]:  # stop or cancel arrived while ffmpeg was starting
        proc.kill()
    errors = []
    last_print = 0
    size = position = 0  # bytes written and seconds of video written so far
    size_samples, time_samples = [], []
    for line in proc.stdout:
        line = line.strip()
        key, _, value = line.partition("=")
        if key == "total_size" and value.isdigit():
            size = int(value)
        elif key == "out_time_us" and value.isdigit():
            position = int(value) / 1e6
        elif key == "progress":  # last line of each progress block: store everything for the popup
            now = time.time()
            speed = rate(size_samples, now, size)
            pace = rate(time_samples, now, position)  # seconds of video downloaded per second
            job.update(done=size, speed=speed, position=position, elapsed=now - started)
            job["detail"] = "%d:%02d:%02d downloaded" % (position // 3600, position // 60 % 60, position % 60)
            if duration:
                part_done = min(position / duration, 1.0)
                job.update(percent=100.0 * part_done,
                           total=size / part_done if part_done > 0 else None,  # estimate
                           eta=(duration - position) / pace if pace > 0 else None)  # estimate
            if now - last_print > 2:
                log("  %s  %s%s  %.0f KB/s" % (name, "%5.1f%%  " % job["percent"] if duration else "",
                                               job["detail"], speed / 1024), job)
                last_print = now
        elif line and not re.match(r"^\w+=\S*$", line):
            errors.append(line)
    code = proc.wait()
    ctl["proc"] = None

    # ---- output: the finished mp4, or the error ----
    if code != 0 or ctl["stop"] or not os.path.exists(part) or os.path.getsize(part) == 0:
        if os.path.exists(part):
            os.remove(part)
        # Windows shows ffmpeg's negative error numbers as huge ones (2812791304), the low byte is
        # the code other systems show (8)
        return "ffmpeg failed (exit %d): %s" % (code & 0xFF or 1, " | ".join(errors[-3:]) or "no error text")
    os.replace(part, target)
    log("done: %s (%d bytes, %s of video, %.1fs)"
        % (name, os.path.getsize(target), job["detail"].split(" ")[0], time.time() - started), job)
    return None


def run(job_id):
    """Worker thread for one job: download it and record how it ended."""
    job, ctl = jobs[job_id], live[job_id]
    is_stream = job["kind"] == "stream"
    if is_stream:  # a stream always starts again from the beginning
        job.update(done=0, total=None, percent=None, elapsed=0, position=0)
    job.update(detail="", speed=0, eta=None, finished=None, notified=False, refresh=False, scan="", threat="")
    ctl["error"], ctl["code"] = "", None
    started = time.time() - job["elapsed"]
    try:
        os.makedirs(os.path.dirname(job["path"]), exist_ok=True)
        if not job["url"].lower().startswith(("http://", "https://")):
            error = "not an http url"
        else:
            error = stream(job, ctl) if is_stream else download(job, ctl)
            if not error and job.get("audioOnly") and not is_stream:
                error = to_mp3(job, ctl)
            if not error and is_stream and job["subtitles"]:
                job["detail"] = "saving subtitles"
                job["detail"] = save_subtitles(job, ctl)
            elif not error:
                job["detail"] = ""
            if not error:
                scan(job)
    except Exception as e:
        error = str(e) or repr(e)

    # ---- output: the new state ----
    stop, ctl["stop"] = ctl["stop"], ""
    job.update(elapsed=time.time() - started, speed=0, eta=None, finished=time.time())
    if not error:  # the real size is known now
        size = os.path.getsize(job["path"])
        job.update(state="done", done=size, total=size, percent=100.0, estimate=False)
        if not job["private"]:
            log_stats([stats_row(job)])
    elif stop == "cancel":
        remove_parts(job)
        job.update(state="canceled", detail="", done=0, percent=None)
        log("canceled %s" % job["name"], job)
    elif stop == "pause":
        job.update(state="stopped" if is_stream else "paused", detail="")
        log("%s %s" % (job["state"], job["name"]), job)
    else:
        # a direct download that had already started and then got 403, 404 or 410: the link expired
        expired = not is_stream and job["done"] > 0 and ctl.get("code") in EXPIRED_CODES
        job.update(state="failed", detail=error, refresh=expired)
        log("FAILED %s: %s" % (job["name"], error), job)
    forget_private(job_id)
    save_history()
    pump()  # a place is free for the next queued job
    # the whole queue is finished (a download ended by itself, nothing runs or waits): After done
    if job["state"] in ("done", "failed"):
        with lock:
            action = settings["afterDone"]
            if action != "nothing" and not queue and not any(j["state"] in ACTIVE for j in jobs.values()):
                settings["afterDone"] = "nothing"  # taken: a second finished job cannot start it again
                threading.Thread(target=after_done, args=(action,), daemon=True).start()


def after_done(action):
    """The whole queue finished: sleep, shut down (both after a 60 second countdown that can be
    canceled), or close the downloader."""
    change_settings({"afterDone": "nothing"})  # only once: saved, so a restart does not do it again
    log("queue finished: %s" % action)
    if action == "quit":
        drop_private()
        save_history()
        os._exit(0)
    seconds = int(os.environ.get("UDM_COUNTDOWN") or 60)  # UDM_COUNTDOWN: only for testing
    text = "Going to sleep" if action == "sleep" else "Shutting down"
    try:
        canceled = subprocess.run([PYTHON, "-c", COUNTDOWN, str(seconds), text, tray.ICON], stdin=subprocess.DEVNULL,
                                  creationflags=subprocess.CREATE_NO_WINDOW).returncode != 0
    except Exception as e:
        log("  could not show the countdown (%s), so nothing is done" % e)
        return
    if canceled:
        log("  %s canceled" % action)
        return
    if os.environ.get("UDM_DRYRUN"):
        log("  dry run, not running: %s" % " ".join(COMMANDS[action]))
        return
    subprocess.run(COMMANDS[action], creationflags=subprocess.CREATE_NO_WINDOW)


def forget_private(job_id):
    """A private job that is done or canceled leaves the list (a failed one stays, in memory only, for Retry)."""
    with lock:
        job = jobs.get(job_id)
        if job and job["private"] and job["state"] in ("done", "canceled") and job["scan"] != "threat":
            del jobs[job_id], live[job_id]


def drop_private():
    """The downloader is closing: private jobs are not kept, so their unfinished part files go too.
    Running ones are canceled first (that deletes their parts once the files are closed)."""
    ids = [x for x, job in list(jobs.items()) if job["private"] and job["state"] != "done"]
    for job_id in ids:
        if jobs[job_id]["state"] in ACTIVE:
            control({"type": "cancel", "id": job_id})
    until = time.time() + 5
    while time.time() < until and any(jobs.get(x, {}).get("state") == "running" for x in ids):
        time.sleep(0.1)
    for job_id in ids:
        if job_id in jobs:
            remove_parts(jobs[job_id])


def start(job_id):
    """Put one job at the end of the queue. It runs as soon as there is room."""
    with lock:
        jobs[job_id]["state"] = "queued"
        queue.append(job_id)
    pump()


def pump():
    """Start queued jobs, first in line first, until the allowed number is running."""
    with lock:
        while queue and sum(job["state"] == "running" for job in jobs.values()) < max(1, int(settings["maxJobs"])):
            job_id = queue.pop(0)
            jobs[job_id]["state"] = "running"
            threading.Thread(target=run, args=(job_id,), daemon=True).start()


def add(msg):
    """New download from the browser: make the job and start it. Returns the job id, or, when the
    same file was already downloaded (and msg has no "force"), a dict that describes that download."""
    url = msg.get("url", "").strip().replace(" ", "%20")  # a pasted link may have spaces
    path = urllib.parse.urlsplit(url).path
    is_stream = msg.get("kind") in ("hls", "dash") or path.lower().endswith((".m3u8", ".mpd"))
    audio = bool(msg.get("audioOnly"))
    base = safe_name(os.path.basename(urllib.parse.unquote(path)))
    if is_stream:  # name the mp4 (or mp3) after the page title
        name = (safe_name(msg.get("title")) or os.path.splitext(base)[0] or "video") + (".mp3" if audio else ".mp4")
    else:
        name = base or "download.bin"
        if audio:
            name = os.path.splitext(name)[0] + ".mp3"
    # private: from an incognito window, or private mode is on. Never saved, and gone once it finishes
    private = bool(msg.get("private")) or settings["privateMode"]
    variant = msg.get("variant") if type(msg.get("variant")) is int else None
    subtitles = [{"url": x["url"], "name": str(x.get("name") or ""), "language": str(x.get("language") or "")}
                 for x in msg.get("subtitles") or [] if isinstance(x, dict)
                 and str(x.get("url", "")).lower().startswith(("http://", "https://"))]
    if not msg.get("force"):
        headers = request_headers({"userAgent": msg.get("userAgent"), "referrer": msg.get("referrer")},
                                  {"cookies": msg.get("cookies", "")})
        same = duplicate_of(url, name, int(msg.get("size") or 0), headers)
        if same:
            log("duplicate of job %s: %s" % (same["id"], same["name"]), {"private": private})
            return {"id": same["id"], "name": same["name"], "path": same["path"], "size": same["total"]}
    with lock:
        # ---- where to save: the folder for this file type, and a name that is not taken ----
        folder = settings["folder"] or OUT_DIR
        if settings["sortFolders"]:
            ext = os.path.splitext(name)[1][1:].lower()
            folder = os.path.join(folder, next((sub for sub, exts in settings["folders"].items()
                                                if ext in exts.lower().replace(",", " ").split()), "other"))
        target = free_path(os.path.join(folder, name), url)
        name = os.path.basename(target)
        job_id = str(next(next_id))
        jobs[job_id] = {"id": job_id, "name": name, "kind": "stream" if is_stream else "file", "url": url,
                        "referrer": msg.get("referrer", ""), "userAgent": msg.get("userAgent", ""),
                        "title": msg.get("title", ""), "path": target,
                        "state": "queued", "detail": "", "done": 0, "total": None, "percent": None, "limit": 0,
                        "speed": 0, "eta": None, "elapsed": 0, "estimate": is_stream, "position": 0, "notified": False,
                        "added": time.time(), "finished": None, "variant": variant, "subtitles": subtitles,
                        "subFormat": "srt" if msg.get("subFormat") == "srt" else "vtt", "audioOnly": audio,
                        "refresh": False, "sha256": "", "hashing": None, "private": private, "scan": "", "threat": ""}
        live[job_id] = {"stop": "", "proc": None, "cookies": msg.get("cookies", "")}
        job = jobs[job_id]
        log("job %s: %s from %s%s" % (job_id, name, urllib.parse.urlsplit(url).hostname or "?",
                                      " (page %s)" % site_of(job) if job["referrer"] else ""), job)
        if settings["ask"]:
            jobs[job_id]["detail"] = "choose where to save"
            threading.Thread(target=ask_path, args=(job_id,), daemon=True).start()
        else:
            start(job_id)
    save_history()
    return job_id


def control(msg):
    """pause, resume, cancel, retry, delete, open, move or limit one job. Returns an error message, or None."""
    action, job_id = msg["type"], str(msg.get("id"))
    with lock:
        job, ctl = jobs.get(job_id), live.get(job_id)
        if not job:
            return "no such job"
        state = job["state"]
        if action == "threat":  # delete or keep a file Defender found a threat in
            if job["scan"] != "threat":
                return "there is no threat to decide on"
            if msg.get("choice") == "delete":
                try:
                    if os.path.exists(job["path"]):
                        os.remove(job["path"])
                except OSError as e:
                    return "could not delete the file: %s" % (e.strerror or e)
                remove_thumbnail(job_id)
            job["scan"] = "deleted" if msg.get("choice") == "delete" else "kept"
            log("threat in %s: file %s" % (job["name"], job["scan"]), job)
            save_history()
            forget_private(job_id)
            return None
        if action == "open":
            # open the finished file, or show where it is (or will be) in Explorer
            if not msg.get("folder") and job["scan"] == "threat":
                return "Windows Defender found a threat in this file. Delete it or keep it first."
            if not msg.get("folder"):
                if state != "done" or not os.path.isfile(job["path"]):
                    return "the file is not there: %s" % job["path"]
                os.startfile(job["path"])
            elif os.path.exists(job["path"]):
                subprocess.Popen(["explorer", "/select,", job["path"]])
            else:
                os.startfile(os.path.dirname(job["path"]))
            return None
        if action == "hash":
            if state != "done" or not os.path.isfile(job["path"]):
                return "the file is not there: %s" % job["path"]
            if job["hashing"] is None:
                job["hashing"] = 0.0
                threading.Thread(target=hash_file, args=(job_id,), daemon=True).start()
            return None
        if action == "limit":
            job["limit"] = max(0, int(msg.get("limit") or 0))
        elif state == "queued" and job_id not in queue and action in ("move", "pause", "cancel"):
            return "answer the Save As window first"
        elif action == "move" and state == "queued":
            # one place up (by = -1) or down (by = 1) in the queue
            i = queue.index(job_id)
            k = min(max(i + int(msg.get("by", 0)), 0), len(queue) - 1)
            queue[i], queue[k] = queue[k], queue[i]
        elif action in ("pause", "cancel") and state == "queued":
            queue.remove(job_id)
            if action == "cancel":
                remove_parts(job)
                job.update(state="canceled", detail="", done=0, percent=None, finished=time.time())
            else:
                job["state"] = "stopped" if job["kind"] == "stream" else "paused"
        elif action in ("pause", "cancel") and state == "running":
            # the worker thread sees this, stops, and sets the new state itself
            ctl["stop"] = action
            if ctl["proc"]:
                ctl["proc"].kill()
        elif action == "cancel" and state in ("paused", "stopped", "failed"):
            remove_parts(job)
            job.update(state="canceled", detail="", done=0, percent=None, finished=time.time())
        elif action in ("resume", "retry") and state in ("paused", "stopped", "failed", "canceled"):
            # cookies are never saved, so the extension sends fresh ones with resume and retry
            if "cookies" in msg:
                ctl["cookies"] = msg["cookies"]
            if msg.get("userAgent"):
                job["userAgent"] = msg["userAgent"]
            start(job_id)
        elif action == "delete" and state not in ACTIVE:
            remove_parts(job)
            if msg.get("deleteFile") and os.path.isfile(job["path"]):
                os.remove(job["path"])
            remove_thumbnail(job_id)
            del jobs[job_id], live[job_id]
        else:
            return "cannot %s a job that is %s" % (action, state)
        forget_private(job_id)
    save_history()
    return None


def refresh(msg):
    """Refresh link: the browser caught a new url for a download that failed with 403, 404 or 410.
    Continue from the saved parts if the new url is the same file (same size). Returns an error, or None."""
    # ---- input: the failed job and the new url ----
    job_id = str(msg.get("id"))
    job, url = jobs.get(job_id), msg.get("url", "")
    if not job or job["state"] != "failed" or not job.get("refresh"):
        return "this download has no link to refresh"
    if not url.lower().startswith(("http://", "https://")):
        return "not an http url"
    if not job["total"]:
        return "the size of this download is not known, so the new link cannot be checked"

    # ---- process: ask the new url for its size ----
    headers = request_headers({**job, "userAgent": msg.get("userAgent") or job["userAgent"]},
                              {"cookies": msg.get("cookies", "")})
    try:
        probe = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-0"})
        with urllib.request.urlopen(probe, timeout=30) as r:
            size = int(r.headers["Content-Range"].split("/")[1]) if r.status == 206 \
                else int(r.headers.get("Content-Length") or 0)
    except Exception as e:
        return "the new link does not work: %s" % e
    if size != job["total"]:
        return ("size does not match: the new link is %s, this download is %s. It may be a different file, "
                "so the download was not continued." % (format(size, ","), format(job["total"], ",")))

    # ---- output: the new url, in the job and in its progress file, then continue from the parts ----
    with lock:
        job.update(url=url, refresh=False, userAgent=msg.get("userAgent") or job["userAgent"])
        live[job_id]["cookies"] = msg.get("cookies", "")
        state_path = source(job) + ".progress.json"
        if os.path.exists(state_path):
            with open(state_path) as f:
                state = json.load(f)
            state["url"] = url
            with open(state_path, "w") as f:
                json.dump(state, f)
        log("refreshed link of %s: %s" % (job["name"], url), job)
        start(job_id)
    save_history()
    return None


def duplicate_of(url, name, size, headers):
    """Duplicate check before a new download: a finished job, with its file still on disk, that has the
    same url, or the same name and the same size. Returns that job, or None.
    The size is asked from the server only when the name matches and the browser did not send it."""
    done = [j for j in jobs.values() if j["state"] == "done" and os.path.isfile(j["path"])]
    for j in done:
        if j["url"] == url:
            return j
    same_name = [j for j in done if j["name"] == name]
    if same_name and not size:
        try:
            probe = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-0"})
            with urllib.request.urlopen(probe, timeout=10) as r:
                size = int(r.headers["Content-Range"].split("/")[1]) if r.status == 206 \
                    else int(r.headers.get("Content-Length") or 0)
        except Exception:
            return None
    return next((j for j in same_name if size and j["total"] == size), None)


def hash_file(job_id):
    """Checksum button: SHA-256 of a finished file. Runs in its own thread; progress in job["hashing"]."""
    job = jobs[job_id]
    digest = hashlib.sha256()
    try:
        size, done = os.path.getsize(job["path"]), 0
        with open(job["path"], "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
                done += len(chunk)
                job["hashing"] = 100.0 * done / size if size else 100.0
        job["sha256"] = digest.hexdigest()
        log("sha-256 of %s: %s" % (job["name"], job["sha256"]), job)
    except OSError as e:
        log("could not read %s: %s" % (job["name"], e), job)
    job["hashing"] = None
    save_history()


def thumbnail(job_id):
    """Manager page: a small picture of a finished video, made with ffmpeg once and kept in the thumbs
    folder next to history.json. Returns it as a data: url (extension pages cannot read files), or None."""
    if jobs.get(job_id, {}).get("scan") in ("threat", "deleted"):  # never open a file Defender warned about
        return None
    # ---- input: a finished video that is still on disk ----
    job = jobs.get(job_id)
    if not job or job["state"] != "done" or job["private"] or not os.path.isfile(job["path"]):
        return None
    if os.path.splitext(job["path"])[1][1:].lower() not in VIDEO:
        return None
    thumb = os.path.join(DATA_DIR, "thumbs", job_id + ".jpg")

    # ---- process: a frame 3 seconds in (or the first one for a short video), 160 px wide ----
    if not os.path.exists(thumb) or os.path.getmtime(thumb) < os.path.getmtime(job["path"]):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return None
        os.makedirs(os.path.dirname(thumb), exist_ok=True)
        for start in ("3", "0"):
            subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", start, "-i", job["path"],
                            "-frames:v", "1", "-vf", "scale=160:-2", "-q:v", "5", thumb],
                           capture_output=True, stdin=subprocess.DEVNULL, timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            if os.path.exists(thumb) and os.path.getsize(thumb):
                break
        else:
            return None

    # ---- output: the jpg as a data: url ----
    with open(thumb, "rb") as f:
        return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()


def remove_thumbnail(job_id):
    """A job left the list: its thumbnail goes too."""
    thumb = os.path.join(DATA_DIR, "thumbs", job_id + ".jpg")
    if os.path.exists(thumb):
        os.remove(thumb)


def stats_row(job):
    """One finished download for stats.json: day, time, bytes, seconds, and the site (the page's, else the
    file's; "" when the privacy setting says sites are not recorded)."""
    finished = job["finished"] or time.time()
    return {"day": time.strftime("%Y-%m-%d", time.localtime(finished)), "time": round(finished),
            "size": job["total"] or job["done"] or 0, "seconds": round(job["elapsed"] or 0, 1),
            "site": site_of(job) if settings["statsSites"] else ""}


def log_stats(rows):
    """Add finished downloads to stats.json. It is kept apart from the history, so Clear finished
    and Delete do not change the stats page."""
    if not keep_history:
        return
    path = os.path.join(DATA_DIR, "stats.json")
    with disk_lock:
        old = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    old = json.load(f)
            except Exception as e:
                log("could not read %s, starting it again: %s" % (path, e))
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(old + rows, f)
        os.replace(path + ".tmp", path)


def stats_rows():
    """Every line of stats.json."""
    path = os.path.join(DATA_DIR, "stats.json")
    if not os.path.exists(path):
        return []
    with disk_lock, open(path, encoding="utf-8") as f:
        return json.load(f)


def stats():
    """Stats page: total downloaded, number of files, average speed, top sites, files per day (30 days)."""
    # ---- input: stats.json ----
    rows = stats_rows()

    # ---- process: sums, per site, per day ----
    total = sum(r["size"] for r in rows)
    seconds = sum(r["seconds"] for r in rows if r["seconds"] > 0)
    timed = sum(r["size"] for r in rows if r["seconds"] > 0)
    sites = collections.defaultdict(lambda: {"files": 0, "bytes": 0})
    days = collections.defaultdict(lambda: {"files": 0, "bytes": 0})
    for r in rows:
        sites[r["site"] or "(unknown)"]["files"] += 1
        sites[r["site"] or "(unknown)"]["bytes"] += r["size"]
        days[r["day"]]["files"] += 1
        days[r["day"]]["bytes"] += r["size"]
    top = sorted(sites.items(), key=lambda x: (-x[1]["files"], -x[1]["bytes"], x[0]))[:8]
    last30 = [time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400 * n)) for n in range(29, -1, -1)]

    # ---- output ----
    return {"total": total, "files": len(rows), "averageSpeed": timed / seconds if seconds else 0,
            "topSites": [{"site": k, **v} for k, v in top],
            "perDay": [{"day": d, **days.get(d, {"files": 0, "bytes": 0})} for d in last30]}


def import_backup(msg):
    """Settings page, Import: replace the settings, and the history and stats when the backup has them
    (a "settings only" backup has neither). Replacing the history is refused while something downloads.
    Every row is checked first, so a bad file changes nothing. Returns an error message, or None."""
    # ---- input: the backup's rows and settings ----
    rows, stat_rows, new_settings = msg.get("history"), msg.get("stats"), msg.get("settings")
    lists = all(x is None or isinstance(x, list) for x in (rows, stat_rows))
    if not lists or not isinstance(new_settings, dict):
        return "this is not a UDM backup (no settings, or history or stats that are not lists)"
    if rows is not None:
        with lock:
            if any(j["state"] in ACTIVE for j in jobs.values()):
                return "downloads are running or queued. Pause them or let them finish, then import."

    # ---- process: check every row like load_history will read it ----
    seen = set()
    for n, row in enumerate(rows or [], 1):
        try:
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in ("id", "name", "url", "path")):
                raise ValueError("id, name, url and path are needed")
            if row["id"] in seen or not row["id"].isdigit():
                raise ValueError("the id %r is not a new number" % row["id"])
            seen.add(row["id"])
            if row.get("status") not in ("queued", "running", "paused", "stopped", "done", "failed", "canceled"):
                raise ValueError("unknown status %r" % row.get("status"))
            time.strptime(row["added"], DATE)
            if row.get("finished"):
                time.strptime(row["finished"], DATE)
        except Exception as e:
            return "row %d of the backup is broken (%s); nothing was changed" % (n, e)
    for n, row in enumerate(stat_rows or [], 1):
        good = isinstance(row, dict) and re.match(r"^\d{4}-\d\d-\d\d$", str(row.get("day"))) \
            and all(type(row.get(k)) in (int, float) for k in ("size", "seconds")) and isinstance(row.get("site"), str)
        if not good:
            return "stats line %d of the backup is broken; nothing was changed" % n
    # private mode stays as it is now: a backup made while it was on does not switch it on here
    error = change_settings({k: v for k, v in new_settings.items() if k not in ("privateMode", "proxyPassword")})
    if error:
        return error

    # ---- output: the new history.json (read back as at startup) and stats.json ----
    with lock, disk_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        for name, data in (("history.json", rows), ("stats.json", stat_rows)):
            if data is not None:
                path = os.path.join(DATA_DIR, name)
                with open(path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=1 if name == "history.json" else None)
                os.replace(path + ".tmp", path)
    if rows is not None:
        with lock:
            jobs.clear()
            live.clear()
            queue.clear()
            load_history()
    log("imported a backup: %s downloads, %s stat lines" % (len(rows) if rows is not None else "no",
                                                         len(stat_rows) if stat_rows is not None else "no"))
    return None


def clear_finished():
    """Take done and canceled jobs off the list. Their files stay on disk."""
    with lock:
        for job_id in [x for x, job in jobs.items() if job["state"] in ("done", "canceled")]:
            remove_thumbnail(job_id)
            del jobs[job_id], live[job_id]
    save_history()


def in_site(host, site):
    """True when host is the site or one of its subdomains ("cdn.x.com" is in "x.com")."""
    host = (host or "").lower()
    return bool(site) and (host == site or host.endswith("." + site))


def clear_data(start, end, what, files=False, site="", dry=False):
    """Privacy: delete for real what was saved from start to end (clock times): rows of history.json and
    stats.json, files in the thumbs folder, lines of udm.log. what: some of "history", "stats", "thumbs", "log".
    site: only what belongs to that site (Forget this site). files: also delete the downloaded files of the
    removed downloads. Running, queued, paused and stopped downloads are never touched. dry: only count.
    Returns how many of each would go / went: {history, stats, thumbs, log, files}."""
    inside = lambda t: start <= t < end  # noqa: E731
    mine = lambda job: not site or any(in_site(urllib.parse.urlsplit(job[k] or "").hostname, site)  # noqa: E731
                                       for k in ("url", "referrer"))

    # ---- input: each kind with its time (and its site) ----
    with lock:
        gone = [j for j in jobs.values() if "history" in what and j["state"] in ("done", "failed", "canceled")
                and inside(j["finished"] or j["added"]) and mine(j)]
    path = os.path.join(DATA_DIR, "stats.json")
    rows = []
    if os.path.exists(path):
        with disk_lock, open(path, encoding="utf-8") as f:
            rows = json.load(f)
    day_start = lambda r: time.mktime(time.strptime(r["day"], "%Y-%m-%d"))  # noqa: E731  rows from before "time"
    old_stats = {n for n, r in enumerate(rows) if "stats" in what and inside(r.get("time") or day_start(r))
                 and (not site or in_site(r["site"], site))}
    folder = os.path.join(DATA_DIR, "thumbs")
    thumbs = {os.path.join(folder, j["id"] + ".jpg") for j in gone}  # a removed download's thumbnail goes with it
    for name in os.listdir(folder) if "thumbs" in what and os.path.isdir(folder) else []:
        job = jobs.get(name[:-4])
        if inside(os.path.getmtime(os.path.join(folder, name))) and (not site or (job and mine(job))):
            thumbs.add(os.path.join(folder, name))
    thumbs = {x for x in thumbs if os.path.exists(x)}
    with log_lock:
        if sys.stdout:
            sys.stdout.flush()
        lines = []
        if "log" in what and os.path.exists(LOG):
            with open(LOG, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
    # a line without a time (an older log, a traceback) belongs to the line above it; at the top it counts as old
    found = re.compile(r"(^|[^a-z0-9-])" + re.escape(site) + r"($|[^a-z0-9-])", re.I) if site else None
    names = [j["name"] for j in gone]
    when, old_lines = 0, set()
    for n, line in enumerate(lines):
        stamp = STAMP.match(line)
        when = time.mktime(time.strptime(stamp.group(1), DATE)) if stamp else when
        if inside(when) and (not site or found.search(line) or any(x in line for x in names)):
            old_lines.add(n)
    downloaded = []
    for j in gone if files else []:
        if j["state"] == "done":
            stem = os.path.splitext(j["path"])[0]
            ext = "srt" if j["subFormat"] == "srt" else "vtt"
            subs = ["%s.%s.%s" % (stem, safe_name(s.get("language") or s.get("name")) or "subtitles", ext)
                    for s in j["subtitles"] or []]
            downloaded += [x for x in [j["path"]] + subs if os.path.isfile(x)]
    counts = {"history": len(gone), "stats": len(old_stats), "thumbs": len(thumbs), "log": len(old_lines),
              "files": len(downloaded)}
    if dry:
        return counts

    # ---- output: the files rewritten without them, the thumbnails and (if asked) the downloads deleted ----
    with lock:
        for job in gone:
            if job["id"] in jobs and jobs[job["id"]]["state"] in ("done", "failed", "canceled"):
                remove_parts(job)
                del jobs[job["id"]], live[job["id"]]
    save_history()
    if old_stats:
        with disk_lock:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump([r for n, r in enumerate(rows) if n not in old_stats], f)
            os.replace(path + ".tmp", path)
    for x in thumbs | set(downloaded):
        try:
            os.remove(x)
        except OSError as e:
            log("could not delete %s: %s" % (os.path.basename(x), e))
    if old_lines:
        with log_lock:
            if sys.stdout:
                sys.stdout.flush()
            with open(LOG, encoding="utf-8", errors="replace") as f:
                now = f.readlines()  # lines written since we read it are kept
            with open(LOG, "w", encoding="utf-8") as f:
                f.writelines(line for n, line in enumerate(now) if n not in old_lines)
    log("cleared: %(history)d downloads, %(stats)d stat lines, %(thumbs)d thumbnails, %(log)d log lines, "
        "%(files)d files" % counts)
    return counts


def auto_clean():
    """Auto clean (privacy setting): history, stats, thumbnails and log lines older than N days go."""
    days = settings["autoClean"]
    if days:
        clear_data(-1, time.time() - days * 86400, ("history", "stats", "thumbs", "log"))


def all_jobs(action):
    """Scheduler and tray icon: "resume" every paused or stopped job, or "pause" every running or queued job."""
    states = ("paused", "stopped") if action == "resume" else ACTIVE
    for job_id in [x for x, job in list(jobs.items()) if job["state"] in states]:
        error = control({"type": action, "id": job_id})
        if error:
            log("scheduler: %s" % error)


def tray_tip():
    """The tray icon's hover text: active downloads and their total speed."""
    running = [j for j in jobs.values() if j["state"] == "running"]
    queued = sum(j["state"] == "queued" for j in jobs.values())
    lines = ["Uniguri Download Manager"]
    if not running and not queued:
        lines.append("Nothing downloading")
    else:
        speed = sum(j["speed"] or 0 for j in running)
        lines.append("%d downloading, %s" % (len(running), "%.1f MB/s" % (speed / 1048576) if speed >= 1048576
                                                          else "%d KB/s" % (speed / 1024)))
        if queued:
            lines.append("%d queued" % queued)
    return "\n".join(lines)


def open_manager():
    """Tray: open the download manager page in Chrome. The extension's id is in the native host's manifest."""
    try:
        with open(os.path.join(HERE, "..", "host", "com.udm.host.json")) as f:
            page = json.load(f)["allowed_origins"][0] + "manager.html"  # chrome-extension://<id>/manager.html
        chrome = "chrome"
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                chrome = winreg.QueryValue(root, r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
                break
            except OSError:
                pass
        subprocess.Popen([chrome, page])
    except Exception as e:
        log("tray: could not open the download manager: %s" % e)


def quit_downloader(reason="from the tray icon"):
    """Tray: Quit. Also when the tray's hidden window is closed (Setup, taskkill without /F)."""
    log("quit %s" % reason)
    drop_private()
    save_history()
    tray.remove()
    os._exit(0)


TRAY_MENU = [(1, "Open download manager"), (0, ""), (2, "Pause all"), (3, "Start all"), (0, ""), (4, "Quit")]
TRAY_COMMANDS = {1: open_manager, 2: lambda: all_jobs("pause"), 3: lambda: all_jobs("resume"), 4: quit_downloader}


def tick():
    """Background thread: notices changes to settings.json, runs the scheduler once a minute, and auto clean
    at startup, once a day, and when its setting changes."""
    last = ""
    cleaned = None  # (day, setting) of the last auto clean
    while True:
        load_settings()
        if settings["proxyMode"] == "windows" and urllib.request.getproxies_registry() != windows_proxy:
            use_proxy()
            log("network: the Windows proxy changed, now %s" % proxy_text(proxy_conf()))
        if (time.strftime("%Y-%m-%d"), settings["autoClean"]) != cleaned:
            cleaned = (time.strftime("%Y-%m-%d"), settings["autoClean"])
            try:
                auto_clean()
            except Exception as e:
                log("auto clean failed: %s" % e)
        now = time.strftime("%H:%M")
        if now != last:  # a new minute
            last = now
            if settings["scheduleStart"] and now == settings["startTime"]:
                log("scheduler: %s, starting the queue" % now)
                all_jobs("resume")
            if settings["scheduleStop"] and now == settings["stopTime"]:
                log("scheduler: %s, stopping all downloads" % now)
                all_jobs("pause")
        time.sleep(5)


def change_settings(new, save=True):
    """Take over settings from the extension or from settings.json. Returns an error message, or None."""
    global settings_time
    merged = {**settings, **{k: v for k, v in new.items() if k in settings and type(v) is type(settings[k])}}
    if merged["proxyMode"] not in PROXY_MODES:
        return "Proxy must be one of: %s" % ", ".join(PROXY_MODES)
    if merged["proxyType"] not in PROXY_TYPES:
        return "Proxy type must be one of: %s" % ", ".join(PROXY_TYPES)
    if merged["proxyMode"] == "manual" and not (clean_host(merged["proxyHost"]) and 0 < merged["proxyPort"] < 65536):
        return "a manual proxy needs an address and a port from 1 to 65535"
    for key, value in new.items():
        if key not in settings or type(value) is not type(settings[key]):
            continue  # unknown name or wrong kind of value
        if key == "folder" and value:
            try:
                os.makedirs(value, exist_ok=True)
            except OSError as e:
                return "cannot use the folder %s: %s" % (value, e.strerror or e)
        if key == "afterDone" and value not in AFTER_DONE:
            return "After done must be one of: %s" % ", ".join(AFTER_DONE)
        if key in ("startTime", "stopTime", "limitFrom", "limitTo") and not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value):
            return "a time must look like 14:30, not %s" % value
        if key == "autoClean" and value not in AUTO_CLEAN:
            return "Auto clean must be one of: %s days" % ", ".join(map(str, AUTO_CLEAN))
        if key in ("maxJobs", "parts", "siteConnections"):
            value = min(max(value, 1), 32)
        if key == "siteLimits":
            for line in filter(str.strip, value.splitlines()):
                site, equals, number = line.partition("=")
                if not (equals and clean_host(site) and number.strip().isdigit() and int(number) > 0):
                    return "a site limit must look like example.com = 2, not %s" % line.strip()
            value = "\n".join("%s = %d" % (site, n) for site, n in site_limits_of(value).items())
        if key in ("speedLimit", "limitSpeed"):
            value = max(value, 0)
        if key == "proxyHost":
            value = clean_host(value)
        if key == "proxyPassword" and value and not value.startswith("dpapi:"):  # typed into settings.json
            value = protect(value)
            save = True
        settings[key] = value
    if save:
        with disk_lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            path = os.path.join(DATA_DIR, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=1)
            settings_time = os.path.getmtime(path)
    use_proxy()
    pump()  # more downloads at once may be allowed now
    return None


def load_settings():
    """Read settings.json if it is there and newer than what we have. Called at startup and by tick()."""
    global settings_time
    path = os.path.join(DATA_DIR, "settings.json")
    if not os.path.exists(path) or os.path.getmtime(path) == settings_time:
        return
    settings_time = os.path.getmtime(path)
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        error = change_settings(saved, save=False)
        log("settings loaded from %s%s" % (path, ": " + error if error else ""))
        # an older settings.json lacks the newer settings: they keep their defaults, written back to the file
        missing = [key for key in settings if key not in saved]
        if missing:
            change_settings({}, save=True)
            log("defaults added to %s: %s" % (path, ", ".join(missing)))
    except Exception as e:
        log("could not read %s: %s" % (path, e))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # only the native host may talk to us: web pages always send an Origin header
        # and cannot send application/json here without a preflight we never answer
        allowed = self.headers.get("Content-Type") == "application/json" and not self.headers.get("Origin")
        reply = {"ok": allowed}
        if allowed:
            msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            kind = msg.get("type")
            try:
                if kind == "status":
                    reply.update(jobs=jobs, queue=queue, settings=public_settings(), defaultFolder=OUT_DIR, dataFolder=DATA_DIR,
                                 limitNow=current_limit())
                elif kind == "settings":
                    new = dict(msg.get("set") or {})
                    if isinstance(new.get("proxyPassword"), str):  # typed on the settings page: kept only encrypted
                        new["proxyPassword"] = protect(new["proxyPassword"])
                    error = change_settings(new)
                    reply = {"ok": False, "error": error} if error else {"ok": True, "settings": public_settings()}
                elif kind == "proxytest":
                    reply = test_proxy(msg)
                elif kind == "defender":  # settings page: can the scan run?
                    d = check_defender(again=True)
                    reply.update(available=d["available"], reason=d["reason"])
                elif kind == "clear":
                    clear_finished()
                elif kind == "notified":
                    for job_id in msg.get("ids") or []:
                        if job_id in jobs:
                            jobs[job_id]["notified"] = True
                    save_history()
                elif kind in ("pause", "resume", "cancel", "retry", "delete", "open", "move", "limit", "hash", "threat"):
                    error = control(msg)
                    if error:
                        reply = {"ok": False, "error": error}
                elif kind == "thumb":  # manager page: picture of a finished video
                    reply["data"] = thumbnail(str(msg.get("id")))
                elif kind == "stats":
                    reply.update(stats(), sites=settings["statsSites"])
                elif kind == "export":  # settings page, Export: 500 rows at a time (a reply must stay under 1 MB)
                    rows, start = stats_rows() if msg.get("part") == "stats" else history_rows(), int(msg.get("offset") or 0)
                    reply.update(rows=rows[start:start + 500], total=len(rows), settings=public_settings())
                elif kind == "cleardata":  # settings page, Clear data: range in seconds back from now, 0 = all time
                    seconds = int(msg.get("range") or 0)
                    what = [x for x in msg.get("what") or [] if x in ("history", "stats", "thumbs", "log")]
                    reply["counts"] = clear_data(time.time() - seconds if seconds else -1, float("inf"), what,
                                                 files=bool(msg.get("files")), dry=bool(msg.get("dry")))
                elif kind == "forget":  # manager and stats page: Forget this site
                    site = str(msg.get("site") or "").strip().lower()
                    site = site[4:] if site.startswith("www.") else site
                    if not re.match(r"^[a-z0-9.-]+$", site):
                        reply = {"ok": False, "error": "not a site name: %r" % site}
                    else:
                        reply["counts"] = clear_data(-1, float("inf"), ("history", "stats", "thumbs", "log"),
                                                     site=site, dry=bool(msg.get("dry")))
                elif kind == "import":
                    error = import_backup(msg)
                    if error:
                        reply = {"ok": False, "error": error}
                elif kind == "probe":  # the qualities and subtitles of a stream, for the popup
                    headers = request_headers({"userAgent": msg.get("userAgent"), "referrer": msg.get("referrer")},
                                              {"cookies": msg.get("cookies", "")})
                    reply.update(read_master(msg.get("url", ""), headers, bool(msg.get("private")) or settings["privateMode"]))
                elif kind == "refresh":
                    error = refresh(msg)
                    if error:
                        reply = {"ok": False, "error": error}
                elif kind in (None, "download"):
                    added = add(msg)
                    if isinstance(added, dict):  # already downloaded: the extension asks what to do
                        reply = {"ok": False, "duplicate": added, "error": "already downloaded: %s" % added["path"]}
                    else:
                        reply["id"] = added
                else:
                    reply = {"ok": False, "error": "unknown message type: %s" % kind}
            except Exception as e:
                reply = {"ok": False, "error": str(e) or repr(e)}
        with lock:
            body = json.dumps(reply).encode()
        self.send_response(200 if allowed else 403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# ---- one downloader at a time: a second start (autostart, Start menu, Setup) must not add a second tray icon ----
ERROR_ALREADY_EXISTS = 183
MB_ICONERROR, MB_SETFOREGROUND, MB_TOPMOST = 0x10, 0x10000, 0x40000
instance_mutex = None  # held (never closed) while this downloader runs; Windows frees it when the process ends


def first_instance():
    """True when no other downloader for PORT is running: takes a named Windows mutex for this login session."""
    global instance_mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    instance_mutex = kernel32.CreateMutexW(None, False, "Local\\UDM downloader %d" % PORT)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


class Server(ThreadingHTTPServer):
    """The http server, alone on its port. HTTPServer asks for SO_REUSEADDR, and on Windows that lets a second
    program (or a second UDM) bind the same port without an error: exclusive use makes that bind fail instead."""
    allow_reuse_address = False

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def answers_like_udm():
    """True when what listens on PORT is a UDM downloader (an older one, from before the mutex)."""
    try:
        req = urllib.request.Request("http://127.0.0.1:%d" % PORT, b'{"type": "status"}',
                                     {"Content-Type": "application/json"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3) as r:
            return "jobs" in json.load(r)
    except Exception:
        return False


def port_owner():
    """The program listening on PORT, like "nginx.exe (process 1234)", or "" when Windows does not say."""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        # the listening line: local address on PORT, no remote address (the state's name is translated)
        pid = next(cols[-1] for cols in map(str.split, out.splitlines())
                   if len(cols) == 5 and cols[1].endswith(":%d" % PORT) and cols[2].endswith(":0"))
        row = subprocess.run(["tasklist", "/FI", "PID eq %s" % pid, "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW).stdout
        name = row.split('","')[0].strip().strip('"')
        return "%s (process %s)" % (name, pid) if name.lower().endswith(".exe") else "process %s" % pid
    except Exception:
        return ""


def port_taken(error):
    """PORT is used by a program that is not UDM: say so in the log and in a window, since there is no console."""
    owner = port_owner()
    text = ("UDM cannot start: port %d on this PC is already used by another program%s.\n\n"
            "The browser extension talks to UDM on that port, so UDM cannot work while it is taken. "
            "Close that program, then start UDM again." % (PORT, " (%s)" % owner if owner else ""))
    log("%s (%s)" % (text.replace("\n\n", " "), error))
    ctypes.windll.user32.MessageBoxW(None, text, "UDM", MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST)


def main():
    if sys.stdout is None:  # started by pythonw (no console): write the log to a file instead
        os.makedirs(OUT_DIR, exist_ok=True)
        sys.stdout = sys.stderr = open(LOG, "a", buffering=1, encoding="utf-8")
    server = None
    if len(sys.argv) == 1:  # the background downloader (not a one-file test run): one at a time, port first,
        if not first_instance():  # then the tray icon, so a copy that cannot run never shows one
            log("another UDM downloader is already running on port %d, so this one quits" % PORT)
            sys.exit(0)
        try:
            server = Server(("127.0.0.1", PORT), Handler)
        except OSError as e:
            if answers_like_udm():
                log("an older UDM downloader is already running on port %d, so this one quits" % PORT)
                sys.exit(0)
            port_taken(e)
            sys.exit(1)
    if not find_ffmpeg():
        log(NO_FFMPEG)
    load_settings()
    use_proxy()
    if settings["proxyMode"] != "none":
        log("network: %s" % proxy_text(proxy_conf()))
    threading.Thread(target=check_defender, daemon=True).start()
    if len(sys.argv) > 1:
        global keep_history
        keep_history = False
        job = jobs[add({"url": sys.argv[1], "force": True})]
        while job["state"] in ACTIVE:
            time.sleep(0.2)
        sys.exit(0 if job["state"] == "done" else 1)
    else:
        load_history()
        if not os.path.exists(os.path.join(DATA_DIR, "stats.json")):  # first start with stats: begin with the history
            log_stats([stats_row(j) for j in jobs.values() if j["state"] == "done"])
        threading.Thread(target=tick, daemon=True).start()
        closed = lambda: quit_downloader("by another program (Setup or taskkill)")  # noqa: E731
        threading.Thread(target=tray.run, args=("UDM tray %d" % PORT, tray_tip, TRAY_MENU, TRAY_COMMANDS, closed),
                         daemon=True).start()
        atexit.register(tray.remove)  # Ctrl+C: no icon left behind
        atexit.register(drop_private)
        log("downloader listening on port %d, saving to %s" % (PORT, settings["folder"] or OUT_DIR))
        server.serve_forever()


if __name__ == "__main__":
    main()
