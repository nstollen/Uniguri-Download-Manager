// The download manager page: every job in a table, with filters, search and buttons per row.
const FILTERS = {
  all: () => true,
  downloading: (j) => j.state === "running" || j.state === "queued",
  paused: (j) => j.state === "paused" || j.state === "stopped",
  done: (j) => j.state === "done",
  failed: (j) => j.state === "failed",
};
const LABELS = { all: "All", downloading: "Downloading", paused: "Paused", done: "Done", failed: "Failed" };
const COLUMNS = ["name", "size", "progress", "speed", "eta", "status", "added", "buttons"];
let filter = "all";
let selected = null; // id of the row the keyboard works on
const views = {}; // job id -> the table row built for it, updated in place so buttons keep working
const search = document.getElementById("search");

// one more button in a row: runs fn, shows the downloader's error as a tooltip if there is one
function extraButton(label, tip, fn) {
  const button = document.createElement("button");
  button.textContent = label;
  button.title = tip;
  button.onclick = async () => {
    const reply = await fn();
    if (reply && reply.ok === false) button.title = reply.error;
    show();
  };
  return button;
}

// Ask for one number in a small dialog. Resolves to the number, or null when the user backs out.
function askNumber(question, value) {
  return new Promise((resolve) => {
    const box = document.createElement("dialog");
    box.className = "ask";
    const text = document.createElement("p");
    text.textContent = question;
    const input = document.createElement("input");
    input.type = "number";
    input.min = 0;
    input.value = value;
    const ok = document.createElement("button");
    ok.textContent = "OK";
    ok.onclick = () => {
      box.remove();
      resolve(Math.max(0, Math.floor(Number(input.value) || 0)));
    };
    const back = document.createElement("button");
    back.textContent = "Back";
    back.onclick = () => {
      box.remove();
      resolve(null);
    };
    box.append(text, input, " ", ok, " ", back);
    document.body.append(box);
    box.showModal();
  });
}

// Ask for one line of text in a small dialog. Resolves to the text, or null when the user backs out.
function askText(question, placeholder) {
  return new Promise((resolve) => {
    const box = document.createElement("dialog");
    box.className = "ask wide";
    const text = document.createElement("p");
    text.textContent = question;
    const input = Object.assign(document.createElement("input"), { type: "text", placeholder, spellcheck: false });
    const ok = Object.assign(document.createElement("button"), { textContent: "Compare", className: "primary" });
    const back = Object.assign(document.createElement("button"), { textContent: "Back" });
    const end = (value) => {
      box.remove();
      resolve(value);
    };
    ok.onclick = () => end(input.value.trim());
    back.onclick = () => end(null);
    input.onkeydown = (e) => e.key === "Enter" && ok.click();
    box.addEventListener("cancel", () => resolve(null));
    box.append(text, input, document.createElement("br"), ok, " ", back);
    document.body.append(box);
    box.showModal();
  });
}

// the SHA-256 line under a finished job's status, with the result of Verify
function showHash(v, job) {
  v.hash.hidden = !job.sha256;
  const result = v.verified == null ? "" : v.verified ? "matches" : "does not match";
  if (!job.sha256 || v.hash.dataset.shown === job.sha256 + result) return;
  v.hash.dataset.shown = job.sha256 + result;
  const code = Object.assign(document.createElement("code"), { textContent: job.sha256, title: "Click to select" });
  const verdict = Object.assign(document.createElement("b"), { textContent: result, className: v.verified ? "match" : "nomatch" });
  v.hash.replaceChildren("SHA-256 ", code, ...(result ? [" ", verdict] : []));
}

// the queue settings in the toolbar: show the downloader's values, send changes back
function showSettings(settings, limitNow) {
  for (const input of document.querySelectorAll("#queue [data-setting]")) {
    // a setting missing from the reply (an older downloader) keeps what the box shows
    if (document.activeElement !== input && settings) input.value = settings[input.dataset.setting] ?? input.value;
    input.onchange = async () => {
      const value = input.type === "number" ? Math.max(Number(input.min), Math.floor(Number(input.value) || 0)) : input.value;
      await chrome.runtime.sendMessage({ type: "control", action: "settings", set: { [input.dataset.setting]: value } });
      show();
    };
  }
  // the bandwidth schedule (settings page) is limiting right now
  const scheduled = settings && limitNow != null && limitNow !== settings.speedLimit;
  document.getElementById("limitNow").textContent = scheduled ? "now " + (limitNow || "no limit") + (limitNow ? " KB/s" : "") + " (schedule)" : "";
}

async function show() {
  // input: the job list from the downloader
  const reply = await chrome.runtime.sendMessage({ type: "status" });
  document.getElementById("offline").hidden = !!(reply && reply.ok);
  const queue = (reply && reply.queue) || [];
  showSettings(reply && reply.settings, reply && reply.limitNow);
  showPrivate(reply && reply.settings, document.getElementById("private"), document.getElementById("privateBadge"), show);
  const all = Object.values((reply && reply.jobs) || {}).sort((a, b) => b.added - a.added || b.id - a.id);

  // process: count per filter, then keep what the filter and the search box allow
  for (const button of document.querySelectorAll("#filters button")) {
    const name = button.dataset.filter;
    button.textContent = LABELS[name] + " (" + all.filter(FILTERS[name]).length + ")";
    button.classList.toggle("on", name === filter);
  }
  const text = search.value.trim().toLowerCase();
  const jobs = all.filter(FILTERS[filter]).filter((j) => j.name.toLowerCase().includes(text));

  // output: one table row per job
  for (const job of jobs) {
    let v = views[job.id];
    if (!v) { // build the row once
      v = views[job.id] = { tr: document.createElement("tr") };
      for (const name of COLUMNS) {
        v[name] = document.createElement("td");
        v[name].className = name;
        v.tr.append(v[name]);
      }
      v.bar = document.createElement("progress");
      v.bar.max = 100;
      v.percent = document.createElement("span");
      v.progress.append(v.bar, v.percent);
      // status text, and under it the SHA-256 once it is calculated (kept as is so it can be selected)
      // name cell: a thumbnail for finished videos, then the name
      v.thumb = Object.assign(document.createElement("img"), { alt: "", hidden: true });
      v.nameText = Object.assign(document.createElement("span"), { className: "text" });
      const cell = Object.assign(document.createElement("div"), { className: "cell" });
      cell.append(v.thumb, v.nameText);
      if (job.private) cell.append(Object.assign(document.createElement("span"), { className: "tag", textContent: "private", title: "Not saved: it leaves the list once it finishes" }));
      v.name.append(cell);
      v.tr.onclick = (e) => {
        if (!e.target.closest("button")) select(job.id);
      };
      v.statusText = document.createElement("span");
      v.hash = document.createElement("div");
      v.hash.className = "hash";
      v.hash.hidden = true;
      v.status.append(v.statusText, v.hash);
    }
    // then only change what moved
    const est = job.estimate ? "~" : "";
    const running = job.state === "running";
    v.tr.dataset.id = job.id;
    v.tr.dataset.state = job.state; // only for the colors in theme.css
    v.nameText.textContent = job.name;
    v.name.title = job.name + "\n" + job.url;
    v.tr.classList.toggle("selected", job.id === selected);
    if (job.state === "done" && !v.thumbAsked && VIDEO.test(job.name)) { // made by the downloader with ffmpeg, once
      v.thumbAsked = true;
      chrome.runtime.sendMessage({ type: "thumb", id: job.id }).then((r) => {
        if (r && r.data) Object.assign(v.thumb, { src: r.data, hidden: false });
      });
    }
    if (job.state === "done") v.size.textContent = fmtSize(job.total);
    else if (job.total) v.size.textContent = fmtSize(job.done) + " / " + est + fmtSize(job.total);
    else v.size.textContent = job.done ? fmtSize(job.done) : "";
    if (job.state === "canceled") v.size.textContent = "";
    const showBar = job.percent != null && job.state !== "failed" && job.state !== "canceled";
    v.bar.hidden = v.percent.hidden = !showBar;
    if (showBar) {
      v.bar.value = job.percent;
      v.percent.textContent = (job.state === "done" ? 100 : Math.min(job.percent, 99.9).toFixed(1)) + "%";
    }
    v.speed.textContent = running && job.done ? fmtSpeed(job.speed) : "";
    v.eta.textContent = running && job.eta != null ? est + fmtTime(job.eta) : "";
    let status = job.state;
    if (running && job.detail.startsWith("retrying")) status = job.detail; // "retrying (2/3)"
    if (job.state === "failed") status = "failed: " + job.detail;
    if (job.state === "stopped") status = "stopped (Restart starts from the beginning)";
    if (job.state === "done") status = "done in " + fmtTime(job.elapsed) + (job.detail ? " · " + job.detail : "");
    if (job.state === "queued") status = job.detail || "queued, place " + (queue.indexOf(job.id) + 1);
    if (job.limit && job.state !== "done") status += " · limit " + job.limit + " KB/s";
    if (job.hashing != null) status += " · SHA-256 " + Math.floor(job.hashing) + "%";
    v.statusText.textContent = status;
    showHash(v, job);
    v.status.classList.toggle("failed", job.state === "failed");
    v.added.textContent = new Date(job.added * 1000).toLocaleString();
    const key = job.state + (job.refresh ? " refresh" : "") + (job.sha256 ? " hash" : "");
    if (v.state !== key) { // new state, new buttons
      v.state = key;
      const box = jobActions(job, show);
      const extra = [
        extraButton("Open folder", "Shows the file in Explorer", () => control("open", job.id, { folder: true })),
        extraButton("Copy URL", job.url, () => navigator.clipboard.writeText(job.url)),
      ];
      if (job.state === "queued") {
        extra.unshift(
          extraButton("Up", "Earlier in the queue", () => control("move", job.id, { by: -1 })),
          extraButton("Down", "Later in the queue", () => control("move", job.id, { by: 1 })),
        );
      }
      if (job.kind === "file" && ["running", "queued", "paused"].includes(job.state)) {
        extra.unshift(extraButton("Limit", "Speed limit for this download", async () => {
          const now = (((await chrome.runtime.sendMessage({ type: "status" })).jobs || {})[job.id] || {}).limit || 0;
          const limit = await askNumber("Speed limit for " + job.name + " in KB/s (0 = no limit)", now);
          return limit == null ? null : control("limit", job.id, { limit });
        }));
      }
      if (job.state === "done") {
        extra.unshift(extraButton("Open file", job.path, () => control("open", job.id)));
        extra[0].dataset.key = "open"; // the Enter key presses it
        // checksum: calculated only on request, then compared with a pasted one
        extra.push(job.sha256
          ? extraButton("Verify", "Paste the SHA-256 the website gives and compare", async () => {
            const expected = await askText("Paste the SHA-256 for " + job.name, "64 letters and digits");
            if (expected != null) v.verified = expected.replace(/\s+/g, "").toLowerCase() === job.sha256;
            return null;
          })
          : extraButton("Checksum", "Calculates the SHA-256 of the file", () => control("hash", job.id)));
      }
      // every download, stat line, thumbnail and log line of this site (asks first, with the counts)
      const site = siteOf(job);
      if (site && !job.private) {
        extra.push(extraButton("Forget site", "Delete every download, stat line, thumbnail and log line from " + site, () => forgetSite(site)));
      }
      for (const button of extra) box.insertBefore(button, box.querySelector("[data-action=delete]")); // Delete stays last
      v.buttons.replaceChildren(box);
    }
  }
  // rows of jobs that left the list (or the filter) go away, the rest keep their order
  for (const id of Object.keys(views)) {
    if (!jobs.some((job) => job.id === id)) {
      views[id].tr.remove();
      delete views[id];
    }
  }
  const body = document.getElementById("rows");
  const wanted = jobs.map((job) => views[job.id].tr);
  if (wanted.some((tr, i) => body.children[i] !== tr)) body.replaceChildren(...wanted);
  document.getElementById("empty").hidden = jobs.length > 0;
}

for (const button of document.querySelectorAll("#filters button")) {
  button.onclick = () => {
    filter = button.dataset.filter;
    show();
  };
}
search.oninput = show;
document.getElementById("clear").onclick = async () => {
  await chrome.runtime.sendMessage({ type: "control", action: "clear" });
  show();
};

document.getElementById("settings").onclick = () => chrome.runtime.openOptionsPage();
document.getElementById("stats").onclick = () => (location.href = "stats.html");

// ---- keyboard: arrows pick a row, the other keys press that row's own buttons ----
const VIDEO = /\.(mp4|mkv|avi|mov|webm|flv|wmv|m4v|ts|mpg|mpeg|3gp)$/i;
const keys = document.getElementById("keys");
document.getElementById("keysButton").onclick = () => keys.showModal();
document.getElementById("keysClose").onclick = () => keys.close();

function select(id) {
  selected = id;
  for (const tr of document.querySelectorAll("#rows tr")) tr.classList.toggle("selected", tr.dataset.id === id);
}

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.key.toLowerCase() === "f") { // the search box, not the browser's find bar
    e.preventDefault();
    search.focus();
    search.select();
    return;
  }
  // typing in a box, a button with focus (Enter and Space press it), or an open dialog: not ours
  if (e.ctrlKey || e.altKey || e.metaKey || document.querySelector("dialog[open]")) return;
  if (e.target.closest("input, select, textarea, button, a, summary")) return;
  const rows = [...document.querySelectorAll("#rows tr")];
  const at = rows.findIndex((tr) => tr.dataset.id === selected);
  const row = rows[at];
  const press = (query) => {
    const button = row && row.querySelector(query);
    if (button && !button.disabled) button.click();
    return !!button;
  };
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const next = rows[at < 0 ? 0 : Math.min(rows.length - 1, Math.max(0, at + (e.key === "ArrowDown" ? 1 : -1)))];
    if (next) {
      select(next.dataset.id);
      next.scrollIntoView({ block: "nearest" });
    }
  } else if (e.key === " ") {
    e.preventDefault();
    press("[data-action=pause]") || press("[data-action=resume]");
  } else if (e.key === "Delete") {
    press("[data-action=delete]");
  } else if (e.key === "Enter") {
    press("[data-key=open]");
  } else if (e.key === "?") {
    keys.showModal();
  }
});

// theme button: system > light > dark > system
const themeButton = document.getElementById("theme");
const THEME_NAMES = { system: "System", light: "Light", dark: "Dark" };
themeButton.onclick = () => setTheme(THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length]);
onThemeChange((theme) => {
  themeButton.replaceChildren("Theme: ", Object.assign(document.createElement("b"), { textContent: THEME_NAMES[theme] }));
});

// ---- add a link by hand ----
document.getElementById("addUrl").onclick = async () => {
  const input = document.getElementById("url");
  const note = document.getElementById("addNote");
  const url = input.value.trim();
  if (!/^https?:\/\//i.test(url)) {
    note.textContent = "Paste a link that starts with http:// or https://";
    return;
  }
  let reply = await chrome.runtime.sendMessage({ type: "download", url, referrer: "", ask: true });
  if (reply && reply.duplicate) { // already downloaded: ask
    const choice = await askDuplicate(reply.duplicate);
    if (choice === "open") control("open", reply.duplicate.id);
    reply = choice === "again" ? await chrome.runtime.sendMessage({ type: "download", url, referrer: "", force: true }) : { ok: choice === "open" };
  }
  note.textContent = reply && reply.ok ? "" : (reply && reply.error) || "no reply from the extension";
  if (reply && reply.ok) input.value = "";
  show();
};
document.getElementById("url").onkeydown = (e) => {
  if (e.key === "Enter") document.getElementById("addUrl").click();
};

// ---- start all / pause all: one control message per job, so resumes get fresh cookies ----
for (const [button, action, states] of [["startAll", "resume", ["paused", "stopped"]], ["pauseAll", "pause", ["running", "queued"]]]) {
  document.getElementById(button).onclick = async () => {
    const reply = await chrome.runtime.sendMessage({ type: "status" });
    for (const job of Object.values((reply && reply.jobs) || {})) {
      if (states.includes(job.state)) await control(action, job.id);
    }
    show();
  };
}

show();
setInterval(show, 1000);
