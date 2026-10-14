"""Native messaging host: Chrome -> stdin (4-byte length + json) -> downloader on localhost."""
import json
import os
import struct
import sys
import urllib.request

DOWNLOADER = "http://127.0.0.1:%s/" % (os.environ.get("UDM_PORT") or 8765)  # UDM_PORT: only for testing
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never go through a proxy

while True:
    # input: one length-prefixed json message from the extension
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        break
    job = sys.stdin.buffer.read(struct.unpack("<I", raw)[0])

    # process: hand the job to the downloader
    try:
        req = urllib.request.Request(DOWNLOADER, data=job, headers={"Content-Type": "application/json"})
        reply = opener.open(req, timeout=60).read()
    except Exception as e:
        reply = json.dumps({"ok": False, "error": "downloader not reachable: %s" % e}).encode()

    # output: length-prefixed json reply back to the extension
    sys.stdout.buffer.write(struct.pack("<I", len(reply)) + reply)
    sys.stdout.buffer.flush()
