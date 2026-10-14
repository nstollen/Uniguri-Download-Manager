"""Small download manager. Direct files are fetched in parallel range requests,
streams (.m3u8, .mpd) are handed to ffmpeg.

python downloader.py          -> wait for messages from the native host on http://127.0.0.1:8765
python downloader.py <url>    -> download one file or stream right now (for testing)
"""
import atexit
import base64
import collections
import glob
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import winreg
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
MIN_SPLIT = 1024 * 1024  # dynamic segmentation: a part is only split when at least this much is left
EXPIRED_CODES = (403, 404, 410)  # a direct download that fails with one of these can get a refreshed link
ACTIVE = ("running", "queued")  # states of a job that is downloading or waiting for its turn
# what the user can change on the settings page; kept in settings.json.
# Speed limits are in KB per second, 0 = no limit.
settings_time = 0  # when settings.json was last written, to notice edits made by hand
settings = {
    "maxJobs": 3, "parts": 8, "speedLimit": 0,
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
         "notified", "variant", "subtitles", "subFormat", "audioOnly", "refresh", "sha256")


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
            # the part files on disk are the truth about how much we already have
            job["done"] = sum(os.path.getsize(x) for x in glob.glob(glob.escape(source(job)) + ".part[0-9]*"))
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
        job.update(refresh=bool(job["refresh"]), sha256=job["sha256"] or "", hashing=None, private=False)
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


def fetch_part(job, headers, path, part, ctl, guard):
    """Append bytes to one part file until its range is complete or the job is told to stop.
    The end of the range can move closer while this runs (dynamic segmentation): guard is held
    while it changes, and while bytes are counted, so no byte past the new end is ever written."""
    left = lambda: part["end"] - part["start"] + 1 - part["done"]  # noqa: E731
    for attempt in range(1 + RETRIES):
        if left() <= 0 or ctl["stop"]:
            return
        try:
            rng = "bytes=%d-%d" % (part["start"] + part["done"], part["end"])
            req = urllib.request.Request(job["url"], headers={**headers, "Range": rng})
            with urllib.request.urlopen(req, timeout=30) as r, open(path, "ab") as f:
                if r.status != 206:
                    raise IOError("server ignored the range request (status %d)" % r.status)
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
                    throttle(job, ctl, len(data))
        except Exception as e:
            ctl["error"] = str(e) or repr(e)
            ctl["code"] = getattr(e, "code", None)  # the http status, for Refresh link
            log("  part at byte %d failed (try %d of %d): %s" % (part["start"], attempt + 1, 1 + RETRIES, e), job)
            time.sleep(1)


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

    # ---- check range support and size: ask for the first byte only ----
    # A 206 reply proves ranges work (same meaning as Accept-Ranges: bytes),
    # and Content-Range carries the full length.
    size = 0
    try:
        probe = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-0"})
        with urllib.request.urlopen(probe, timeout=30) as r:
            if r.status == 206:
                size = int(r.headers["Content-Range"].split("/")[1])
            else:
                log("no range support (Accept-Ranges: %s, Content-Length: %s)"
                    % (r.headers.get("Accept-Ranges"), r.headers.get("Content-Length")), job)
    except urllib.error.HTTPError as e:
        ctl["code"] = e.code
        if job["done"]:  # already started: the saved parts are worth more than a fresh single download
            return "%s (the link may have expired)" % e
        log("server refused the range check (HTTP %d), trying a single download" % e.code, job)
    except Exception as e:
        # network trouble, not a "no ranges" answer: stop here so saved parts stay resumable
        return "cannot reach server: %s. Resume to try again." % e

    def no_room(need):
        """An error message when the disk cannot take `need` more bytes, else None."""
        free = shutil.disk_usage(os.path.dirname(target)).free
        if need > free:
            return ("not enough disk space: %.1f MB needed, %.1f MB free on %s"
                    % (need / 1048576, free / 1048576, os.path.splitdrive(target)[0] or os.path.dirname(target)))

    # ---- no ranges: normal single download (pause works, but resume starts it again) ----
    if size < parts:
        log("single download: %s" % name, job)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r, open(target + ".part", "wb") as f:
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
                    throttle(job, ctl, len(data))
                    if time.time() - last >= 0.5:
                        report(done, total)
                        last = time.time()
        except Exception as e:
            ctl["code"] = getattr(e, "code", None)
            return str(e)
        if ctl["stop"]:
            return "stopped"
        os.replace(target + ".part", target)
        log("done: %s (%d bytes, %.1fs)" % (name, os.path.getsize(target), time.time() - started), job)
        return None

    # ---- ranges: load saved progress or plan the parts ----
    state = None
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
        if state.get("url") != url or state.get("size") != size:
            state = None
    resumed = state is not None
    if not resumed:
        step = size // parts
        state = {"url": url, "size": size, "parts": [
            {"start": i * step, "end": size - 1 if i == parts - 1 else (i + 1) * step - 1, "done": 0}
            for i in range(parts)]}

    paths = ["%s.part%d" % (target, i) for i in range(len(state["parts"]))]
    for path, part in zip(paths, state["parts"]):
        # the part file on disk is the truth about how much we already have
        have = os.path.getsize(path) if os.path.exists(path) else 0
        if have and (not resumed or have > part["end"] - part["start"] + 1):
            os.remove(path)
            have = 0
        part["done"] = have
    already = sum(p["done"] for p in state["parts"])
    log("%s: %s, %d bytes in %d parts (%d bytes already on disk)"
        % ("resuming" if resumed else "starting", name, size, len(paths), already), job)
    job["total"] = size
    save_history()  # the size is known now, keep it for after a restart
    # room for what is still missing, plus one part: the merge deletes each part once it is copied
    largest = max(p["end"] - p["start"] + 1 for p in state["parts"])
    if no_room(size - already + largest):
        return no_room(size - already + largest)

    # ---- process: parallel range requests, progress saved to json ----
    guard = threading.Lock()  # held while a part's range or byte count changes
    threads = [threading.Thread(target=fetch_part, args=(job, headers, path, part, ctl, guard), daemon=True)
               for path, part in zip(paths, state["parts"])]
    for t in threads:
        t.start()
    speeds = [[] for _ in threads]  # samples per part, for the speed of each connection
    last_print = 0
    while True:
        running = [i for i, t in enumerate(threads) if t.is_alive()]
        alive = bool(running)
        # Dynamic segmentation: a connection finished its part early, so the part that will take longest
        # is split in half and a new connection takes the second half. At most `parts` connections run,
        # and a part that ended with an error stops the splitting (the job is going to fail anyway).
        now = time.time()
        rates = [rate(speeds[i], now, p["done"]) for i, p in enumerate(state["parts"])]
        failed_part = any(not t.is_alive() and p["done"] < p["end"] - p["start"] + 1
                          for t, p in zip(threads, state["parts"]))
        if running and len(running) < parts and not ctl["stop"] and not failed_part:
            with guard:
                left = {i: state["parts"][i]["end"] - state["parts"][i]["start"] + 1 - state["parts"][i]["done"]
                        for i in running}
                slow = max(left, key=lambda i: left[i] / max(rates[i], 1.0))  # most time left
                if left[slow] >= MIN_SPLIT:
                    old = state["parts"][slow]
                    mid = old["start"] + old["done"] + left[slow] // 2
                    state["parts"].append({"start": mid, "end": old["end"], "done": 0})
                    old["end"] = mid - 1
                    paths.append("%s.part%d" % (target, len(paths)))
                    speeds.append([])
                    threads.append(threading.Thread(target=fetch_part, daemon=True, args=(
                        job, headers, paths[-1], state["parts"][-1], ctl, guard)))
                    threads[-1].start()
                    log("  split part %d at byte %d: %d connections, %d parts"
                        % (slow, mid, len(running) + 1, len(paths)), job)
        with guard:
            with open(state_path + ".tmp", "w") as f:
                json.dump(state, f)
        os.replace(state_path + ".tmp", state_path)
        done = sum(p["done"] for p in state["parts"])
        report(done, size)
        job["detail"] = "%.1f%%" % job["percent"]
        if time.time() - last_print > 2 or not alive:
            log("  %s  %5.1f%%  %d / %d" % (name, 100.0 * done / size, done, size), job)
            last_print = time.time()
        if not alive:
            break
        time.sleep(0.5)

    if done != size:  # paused, canceled, or a part that kept failing: the part files stay for a resume
        return ("a part still failed after %d retries (%s). %d of %d bytes are saved, Retry continues from there."
                % (RETRIES, ctl.get("error") or "connection closed", done, size))

    # ---- output: merge parts, clean up ----
    with open(target, "wb") as out:
        for path, _ in sorted(zip(paths, state["parts"]), key=lambda x: x[1]["start"]):
            with open(path, "rb") as f:
                shutil.copyfileobj(f, out, CHUNK)
            os.remove(path)  # free its space right away
    remove_parts(job)
    log("done: %s (%d bytes, %.1fs)" % (name, os.path.getsize(target), time.time() - started), job)
    return None


def stream_duration(ffmpeg, url, headers, job=None):
    """Total length of a stream in seconds, or 0 when it cannot be found (for example a live stream).
    job: for the log only (a private one is not logged)."""
    # ---- ffprobe, when it sits next to ffmpeg ----
    probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe")
    if os.path.exists(probe):
        try:
            cmd = [probe, "-v", "error", "-user_agent", headers["User-Agent"]]
            extra = "".join("%s: %s\r\n" % kv for kv in headers.items() if kv[0] != "User-Agent")
            if extra:
                cmd += ["-headers", extra]
            cmd += ["-show_entries", "format=duration", "-of", "csv=p=0", url]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW).stdout
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
                                          creationflags=subprocess.CREATE_NO_WINDOW)
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
    job.update(detail="", speed=0, eta=None, finished=None, notified=False, refresh=False)
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
        if job and job["private"] and job["state"] in ("done", "canceled"):
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
                        "refresh": False, "sha256": "", "hashing": None, "private": private}
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
        if action == "open":
            # open the finished file, or show where it is (or will be) in Explorer
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
    error = change_settings({k: v for k, v in new_settings.items() if k != "privateMode"})
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


def quit_downloader():
    """Tray: Quit."""
    log("quit from the tray icon")
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
        if key in ("maxJobs", "parts"):
            value = min(max(value, 1), 32)
        if key in ("speedLimit", "limitSpeed"):
            value = max(value, 0)
        settings[key] = value
    if save:
        with disk_lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            path = os.path.join(DATA_DIR, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=1)
            settings_time = os.path.getmtime(path)
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
                    reply.update(jobs=jobs, queue=queue, settings=settings, defaultFolder=OUT_DIR, dataFolder=DATA_DIR,
                                 limitNow=current_limit())
                elif kind == "settings":
                    error = change_settings(msg.get("set") or {})
                    reply = {"ok": False, "error": error} if error else {"ok": True, "settings": settings}
                elif kind == "clear":
                    clear_finished()
                elif kind == "notified":
                    for job_id in msg.get("ids") or []:
                        if job_id in jobs:
                            jobs[job_id]["notified"] = True
                    save_history()
                elif kind in ("pause", "resume", "cancel", "retry", "delete", "open", "move", "limit", "hash"):
                    error = control(msg)
                    if error:
                        reply = {"ok": False, "error": error}
                elif kind == "thumb":  # manager page: picture of a finished video
                    reply["data"] = thumbnail(str(msg.get("id")))
                elif kind == "stats":
                    reply.update(stats(), sites=settings["statsSites"])
                elif kind == "export":  # settings page, Export: 500 rows at a time (a reply must stay under 1 MB)
                    rows, start = stats_rows() if msg.get("part") == "stats" else history_rows(), int(msg.get("offset") or 0)
                    reply.update(rows=rows[start:start + 500], total=len(rows), settings=settings)
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


def main():
    if sys.stdout is None:  # started by pythonw (no console): write the log to a file instead
        os.makedirs(OUT_DIR, exist_ok=True)
        sys.stdout = sys.stderr = open(LOG, "a", buffering=1, encoding="utf-8")
    if not find_ffmpeg():
        log(NO_FFMPEG)
    load_settings()
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
        threading.Thread(target=tray.run, args=("UDM tray %d" % PORT, tray_tip, TRAY_MENU, TRAY_COMMANDS),
                         daemon=True).start()
        atexit.register(tray.remove)  # Ctrl+C: no icon left behind
        atexit.register(drop_private)
        log("downloader listening on port %d, saving to %s" % (PORT, settings["folder"] or OUT_DIR))
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
