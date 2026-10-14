"""Run by the uninstaller, through the installed exe (it has Python inside):
udm-downloader.exe -c "exec(open(r'<this file>', encoding='utf-8').read())" <data folder> [data] [files]

data:  delete what UDM saved: history.json, settings.json, stats.json, the thumbs folder and udm.log.
files: delete what UDM downloaded: finished files (with their subtitles) and the part files of
       unfinished downloads, as listed in history.json. Files UDM does not know about stay.
"""
import glob
import json
import os
import re
import shutil
import sys

folder, what = sys.argv[1], sys.argv[2:]

# ---- input: the downloads UDM knows about ----
try:
    with open(os.path.join(folder, "history.json"), encoding="utf-8") as f:
        rows = json.load(f)
except Exception:
    rows = []

# ---- process: the list of what goes ----
gone = []
for row in rows if "files" in what else []:
    path = row.get("path") or ""
    if not path:
        continue
    if row.get("status") == "done":  # the file and its subtitles, named as the downloader names them
        stem, ext = os.path.splitext(path)[0], "srt" if row.get("subFormat") == "srt" else "vtt"
        for sub in row.get("subtitles") or []:
            name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", sub.get("language") or sub.get("name") or "").strip(". ")[:120]
            gone.append("%s.%s.%s" % (stem, name or "subtitles", ext))
        gone.append(path)
    else:  # unfinished: its part files and progress file (an mp3 is downloaded to "name.mp3.video" first)
        for start in (path, path + ".video"):
            gone += glob.glob(glob.escape(start) + ".part*") + glob.glob(glob.escape(start) + ".progress.json*")
if "data" in what:
    gone += [os.path.join(folder, x) for x in ("history.json", "settings.json", "stats.json", "udm.log")]
    gone += glob.glob(os.path.join(glob.escape(folder), "*.json.tmp"))

# ---- output: deleted; then the folders they were in (like "compressed"), and the data folder, once empty ----
for path in gone:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
if "data" in what:
    shutil.rmtree(os.path.join(folder, "thumbs"), ignore_errors=True)
for path in sorted({os.path.dirname(x) for x in gone}, key=len, reverse=True) + [folder]:
    try:
        os.rmdir(path)  # only works when nothing is left in it
    except OSError:
        pass
