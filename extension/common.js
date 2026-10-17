// Shared by the extension pages: number formats, the buttons of a job, dialogs, and the privacy helpers.

// 126248550 -> "120.4 MB"
function fmtSize(bytes) {
  if (bytes == null) return "?";
  if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(2) + " GB";
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
  return (bytes / 1024).toFixed(0) + " KB";
}

// bytes per second -> "2.3 MB/s" or "640 KB/s"
function fmtSpeed(bps) {
  return bps >= 1048576 ? (bps / 1048576).toFixed(1) + " MB/s" : ((bps || 0) / 1024).toFixed(0) + " KB/s";
}

// 102 -> "1m 42s"
function fmtTime(seconds) {
  const s = Math.round(seconds || 0);
  if (s >= 3600) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
  if (s >= 60) return Math.floor(s / 60) + "m " + (s % 60) + "s";
  return s + "s";
}

const STREAM_NOTE = "This stream cannot be paused. Stop ends the download, and Restart downloads it again from the beginning.";

// Send one control action for a job. It goes background script -> native host -> downloader.
function control(action, id, extra) {
  return chrome.runtime.sendMessage({ type: "control", action, id, ...extra });
}

// What Windows Defender's scan said about a finished download, for the status line ("" when there was no scan).
const SCAN_TEXT = {
  clean: "scanned, clean", trusted: "not scanned (trusted site)", unavailable: "scan not available", failed: "scan failed",
  kept: "threat found, file kept", deleted: "threat found, file deleted",
};
function scanText(job) {
  return job.scan === "threat" ? "threat found: " + (job.threat || "unknown") : SCAN_TEXT[job.scan] || "";
}

// The buttons a job can have right now, as [label, action, tooltip].
function jobButtons(job) {
  // an HLS stream that UDM downloads in segments can pause like a file (job.pausable); one that ffmpeg downloads
  // alone (live, DASH, ...) can only stop
  const stream = job.kind === "stream" && !job.pausable;
  const list = [];
  if (job.scan === "scanning") return list; // the download is finished: nothing to pause or cancel
  if (job.scan === "threat") {
    list.push(["Delete file", "threat:delete", "Windows Defender found " + (job.threat || "a threat") + " in this file. Deletes it from disk."]);
    list.push(["Keep file", "threat:keep", "Keep the file anyway. Only if you trust it."]);
  }
  if (job.state === "running") {
    list.push(stream ? ["Stop", "pause", STREAM_NOTE] : ["Pause", "pause", "Keeps what is downloaded so far"]);
    if (job.kind === "stream" && job.pausable) list[list.length - 1][2] = "Keeps the segments downloaded so far";
  }
  if (job.state === "queued") list.push(["Pause", "pause", "Takes it out of the queue"]);
  if (job.state === "paused") list.push(["Resume", "resume", "Continues from where it paused"]);
  if (job.state === "stopped") list.push(["Restart", "resume", STREAM_NOTE]);
  if (["running", "queued", "paused", "stopped"].includes(job.state)) {
    list.push(["Cancel", "cancel", "Stops the job and deletes its part files"]);
  }
  if (job.state === "failed" && job.refresh) {
    list.push(["Refresh link", "refresh", "The link expired. Opens the download page again: start the download there and UDM continues this one with the new link."]);
  }
  if (job.state === "failed" || job.state === "canceled") list.push(["Retry", "retry", "Tries the same url again"]);
  if (job.state !== "running" && job.state !== "queued") list.push(["Delete", "delete", "Removes the job from the list"]);
  return list;
}

// A small dialog with a question and buttons. choices: [[label, value]]. Resolves to the value, or null.
function choose(question, detail, choices) {
  return new Promise((resolve) => {
    const box = document.createElement("dialog");
    box.className = "ask";
    const text = document.createElement("p");
    text.textContent = question;
    box.append(text);
    if (detail) box.append(Object.assign(document.createElement("div"), { className: "detail", textContent: detail }));
    for (const [label, value] of choices) {
      const button = document.createElement("button");
      button.textContent = label;
      button.dataset.choice = value;
      button.onclick = () => {
        box.remove();
        resolve(value || null);
      };
      box.append(button, " ");
    }
    box.addEventListener("cancel", () => resolve(null)); // Esc
    document.body.append(box);
    box.showModal();
  });
}

// The downloader already has this file. Resolves to "again", "open", or null (Cancel).
function askDuplicate(dup) {
  return choose("Already downloaded: " + dup.name, dup.path + (dup.size ? " (" + fmtSize(dup.size) + ")" : ""),
    [["Download again", "again"], ["Open existing file", "open"], ["Cancel", ""]]);
}

// Ask what Delete should do. Resolves to "list", "file", or null when the user backs out.
function askDelete(job) {
  return new Promise((resolve) => {
    const box = document.createElement("dialog");
    box.className = "ask";
    const text = document.createElement("p");
    text.textContent = "Delete " + job.name + "?";
    box.append(text);
    for (const [label, value] of [["Remove from list only", "list"], ["Also delete the file from disk", "file"], ["Keep", ""]]) {
      const button = document.createElement("button");
      button.textContent = label;
      button.dataset.choice = value;
      button.onclick = () => {
        box.remove();
        resolve(value || null);
      };
      box.append(button, " ");
    }
    document.body.append(box);
    box.showModal();
  });
}

// Build the row of buttons for one job. refresh() is called after an action went through.
function jobActions(job, refresh) {
  const box = document.createElement("span");
  box.className = "actions";
  for (const [label, action, tip] of jobButtons(job)) {
    const button = document.createElement("button");
    button.textContent = label;
    button.title = tip;
    button.dataset.action = action;
    button.onclick = async () => {
      const extra = {};
      if (action === "delete") {
        const choice = await askDelete(job);
        if (!choice) return;
        extra.deleteFile = choice === "file";
      }
      button.disabled = true;
      const [name, pick] = action.split(":"); // "threat:delete" -> the threat action with a choice
      const reply = await control(name, job.id, pick ? { ...extra, choice: pick } : extra);
      if (!reply || !reply.ok) button.title = (reply && reply.error) || "no reply from the downloader";
      if (action === "refresh") button.disabled = false; // the job stays failed until the new link is caught
      refresh();
    };
    box.append(button);
  }
  return box;
}

// The site a download came from, as the downloader counts it: the page's, else the file's, without "www."
function siteOf(job) {
  try {
    const host = new URL(job.referrer || job.url).hostname;
    return host.startsWith("www.") ? host.slice(4) : host;
  } catch (e) {
    return "";
  }
}

// Forget this site (manager and stats page): ask with the counts first, then delete. Resolves to the counts, or null.
async function forgetSite(site) {
  const preview = await chrome.runtime.sendMessage({ type: "forget", site, dry: true });
  if (!preview || !preview.ok) return null;
  const c = preview.counts;
  const choice = await choose("Forget " + site + "?", "Deletes " + countText(c) + ". Files on disk stay. "
    + "Running, queued and paused downloads are kept.", [["Forget", "delete"], ["Cancel", ""]]);
  if (!choice) return null;
  const reply = await chrome.runtime.sendMessage({ type: "forget", site });
  return reply && reply.ok ? reply.counts : null;
}

// { history: 2, stats: 3, thumbs: 0, log: 9, files: 0 } -> "2 downloads, 3 stat lines, 0 thumbnails and 9 log lines"
function countText(c) {
  const n = (count, one, many) => count + " " + (count === 1 ? one : many);
  const list = [n(c.history, "download", "downloads"), n(c.stats, "stat line", "stat lines"),
    n(c.thumbs, "thumbnail", "thumbnails"), n(c.log, "log line", "log lines")];
  if (c.files) list.push(n(c.files, "downloaded file", "downloaded files"));
  return list.slice(0, -1).join(", ") + " and " + list[list.length - 1];
}

// Private mode, shown as a switch button and a "private" badge (popup and manager header).
// settings: from the downloader's status reply (null when it is not running). badge: null when the
// switch shows it itself (popup). refresh: called after a click.
function showPrivate(settings, button, badge, refresh) {
  const on = !!(settings && settings.privateMode);
  if (badge) badge.hidden = !on;
  button.disabled = !settings;
  button.setAttribute("aria-pressed", on);
  button.title = on
    ? "Private mode is on: new downloads are not saved to the history, stats, thumbnails or log. Click to turn it off."
    : "Turn on private mode: new downloads are not saved to the history, stats, thumbnails or log";
  button.onclick = async () => {
    button.disabled = true;
    await chrome.runtime.sendMessage({ type: "control", action: "settings", set: { privateMode: !on } });
    refresh();
  };
}
