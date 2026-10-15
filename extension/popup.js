const KINDS = { hls: "HLS stream (m3u8)", dash: "DASH stream (mpd)" };
let incognito = false; // the popup belongs to an incognito window: every download from it is private

function row(nameText, metaText, title) {
  const row = document.createElement("div");
  row.className = "row";
  const info = document.createElement("div");
  info.className = "info";
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = nameText;
  name.title = title || nameText;
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = metaText;
  info.append(name, meta);
  row.append(info);
  return { row, meta };
}

// "1280x720 · 2.3 Mbps · ~41.0 MB"
function qualityText(q) {
  const res = q.height ? q.height + "p" : "Quality " + (q.index + 1);
  const rate = q.bandwidth >= 1e6 ? (q.bandwidth / 1e6).toFixed(1) + " Mbps" : Math.round(q.bandwidth / 1e3) + " kbps";
  return [res, q.bandwidth ? rate : "", q.size ? "~" + fmtSize(q.size) : ""].filter(Boolean).join(" · ");
}

// The quality and subtitle picker for a stream, shown under its row.
// input: what the downloader read from the master playlist.
// output: done({ variant, subtitles, subFormat }), or done(null) for Cancel.
async function picker(info, done) {
  const box = document.createElement("div");
  box.className = "picker";

  // quality: the last choice (stored as a height, or "highest"), else the highest
  const sorted = [...info.variants].sort((a, b) => b.height - a.height || b.bandwidth - a.bandwidth);
  const { quality } = await chrome.storage.local.get("quality");
  const pick = (typeof quality === "number" && (sorted.find((q) => q.height === quality) || sorted.find((q) => q.height <= quality))) || sorted[0];
  const select = document.createElement("select");
  select.id = "quality";
  for (const q of sorted) select.append(new Option(qualityText(q), q.index, false, q === pick));
  const qualityLine = document.createElement("label");
  qualityLine.className = "field";
  qualityLine.append(Object.assign(document.createElement("span"), { textContent: "Quality" }), select);
  if (sorted.length > 1) box.append(qualityLine);

  // subtitles: one checkbox per track, and the file format
  const checks = info.subtitles.map((sub) => {
    const label = document.createElement("label");
    label.className = "check";
    const input = Object.assign(document.createElement("input"), { type: "checkbox" });
    label.append(input, sub.name + (sub.language && sub.language !== sub.name ? " (" + sub.language + ")" : ""));
    return { label, input, sub };
  });
  const format = document.createElement("select");
  format.append(new Option(".vtt", "vtt"), new Option(".srt", "srt"));
  format.title = "File type of the saved subtitles";
  if (checks.length) {
    const subs = document.createElement("div");
    subs.className = "field subs";
    const list = document.createElement("div");
    list.className = "checks";
    list.append(...checks.map((c) => c.label));
    subs.append(Object.assign(document.createElement("span"), { textContent: "Subtitles" }), list, format);
    box.append(subs);
  }

  const go = document.createElement("button");
  go.className = "primary";
  go.textContent = "Download";
  go.onclick = () => {
    const chosen = info.variants[Number(select.value)];
    if (chosen) chrome.storage.local.set({ quality: chosen === sorted[0] ? "highest" : chosen.height });
    done({
      variant: chosen ? chosen.index : undefined,
      subtitles: checks.filter((c) => c.input.checked).map((c) => c.sub),
      subFormat: format.value,
    });
  };
  const back = document.createElement("button");
  back.textContent = "Cancel";
  back.onclick = () => done(null);
  const actions = document.createElement("div");
  actions.className = "picker-actions";
  actions.append(back, go);
  box.append(actions);
  return box;
}

const views = {}; // job id -> the row built for it, updated in place so buttons keep working

// the text under the bar. "~" marks a stream's estimated total size and time left
function describe(job) {
  if (job.state === "failed") return "failed · " + job.detail;
  if (job.scan === "scanning") return "scanning with Windows Defender…";
  const est = job.estimate ? "~" : "";
  const parts = [job.state];
  if (job.state === "done") {
    parts.push(fmtSize(job.total), "took " + fmtTime(job.elapsed));
    if (job.detail) parts.push(job.detail); // "2 subtitles saved"
    if (scanText(job)) parts.push(scanText(job)); // "scanned, clean"
  } else if (job.state === "stopped") {
    parts.push("streams cannot pause: Restart starts from the beginning");
  } else if (job.state === "canceled") {
    // nothing more to say
  } else if (job.percent != null) {
    parts.push(fmtSize(job.done) + " / " + est + fmtSize(job.total));
    if (job.state === "running") parts.push(job.detail === "site asked to slow down" ? job.detail : fmtSpeed(job.speed));
    if (job.state === "running" && job.eta != null) parts.push(est + fmtTime(job.eta) + " left");
  } else if (job.state === "queued") {
    parts.push(job.detail || "waiting for a free place");
  } else if (!job.done) {
    if (job.state === "running") parts.push(job.detail || "starting…"); // detail: "retrying (2/3)"
  } else {
    parts.push(job.kind === "stream" ? fmtTime(job.position) + " downloaded" : fmtSize(job.done), fmtSpeed(job.speed));
  }
  return parts.join(" · ");
}

// the downloader's job list: bar, size, speed, time left, and the buttons for each job
async function showJobs() {
  const reply = await chrome.runtime.sendMessage({ type: "status" });
  // the switch is the badge here when it is on (see popup.html); the separate badge only says "incognito"
  showPrivate(reply && reply.settings, document.getElementById("private"), null, showJobs);
  const jobs = Object.values((reply && reply.jobs) || {}).reverse().slice(0, 4); // the manager page has the full list
  document.getElementById("jobsTitle").hidden = !jobs.length;
  for (const job of jobs) {
    // build the row once
    let v = views[job.id];
    if (!v) {
      v = views[job.id] = row(job.name, "");
      v.row.classList.add("job");
      if (job.private) v.row.querySelector(".name").append(Object.assign(document.createElement("span"), { className: "tag", textContent: "private" }));
      v.bar = document.createElement("div");
      v.bar.className = "bar";
      v.progress = document.createElement("progress");
      v.progress.max = 100;
      v.percent = document.createElement("span");
      v.bar.append(v.progress, v.percent);
      v.meta.before(v.bar);
    }
    // then only change what moved. No bar when the percent is unknown, or the job failed.
    const showBar = job.percent != null && job.state !== "failed" && job.state !== "canceled";
    v.bar.hidden = !showBar;
    if (showBar) {
      v.progress.value = job.percent;
      v.percent.textContent = (job.state === "done" ? 100 : Math.min(job.percent, 99.9).toFixed(1)) + "%";
    }
    const threat = job.scan === "threat" || job.scan === "deleted";
    v.row.dataset.state = threat ? "failed" : job.state; // only for the colors in theme.css
    v.meta.textContent = describe(job);
    v.meta.classList.toggle("failed", job.state === "failed" || threat);
    if (v.state !== job.state + job.scan) { // new state, new buttons
      v.state = job.state + job.scan;
      if (v.actions) v.actions.remove();
      v.actions = jobActions(job, showJobs);
      v.meta.after(v.actions);
    }
  }
  // rows of jobs that left the list go away, the rest keep their order
  for (const id of Object.keys(views)) {
    if (!jobs.some((job) => job.id === id)) {
      views[id].row.remove();
      delete views[id];
    }
  }
  const box = document.getElementById("jobs");
  const wanted = jobs.map((job) => views[job.id].row);
  if (wanted.some((r, i) => box.children[i] !== r)) box.replaceChildren(...wanted);
}

(async () => {
  // input: what the background script and the content script found in the current tab
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  incognito = !!tab.incognito;
  document.getElementById("privateBadge").hidden = !incognito; // an incognito window is always private
  const stored = await chrome.storage.session.get(["tab" + tab.id, "blob" + tab.id]);
  const videos = stored["tab" + tab.id] || [];
  const list = document.getElementById("list");

  if (stored["blob" + tab.id]) {
    const note = document.getElementById("blob");
    note.hidden = false;
    note.textContent = videos.length
      ? "This page uses a blob video, pick the stream from the list. The first stream is usually the full-quality one."
      : "This page uses a blob video, but no stream was seen yet. Reload the page, press play, then open this again.";
  }
  if (!videos.length) {
    list.innerHTML = '<div id="empty">Nothing detected yet. Start playing the video, then open this again.</div>';
  }

  // output: one row per video, each with an MP3 and a Download button
  for (const v of videos) {
    const isStream = v.kind !== "file";
    const size = v.size ? (v.size / 1048576).toFixed(1) + " MB" : "";
    const meta = isStream ? KINDS[v.kind] + " · " + v.name : [v.type || "file", size].filter(Boolean).join(" · ");
    const r = row(isStream ? tab.title || v.name : v.name, meta, v.url);

    // send the download; extra = { variant, subtitles, subFormat } from the picker, or { audioOnly }
    async function send(button, extra) {
      const label = button.textContent;
      button.disabled = true;
      button.textContent = "Sending…";
      // the background script adds cookies and user agent and talks to the native host
      const reply = await chrome.runtime.sendMessage({
        type: "download", url: v.url, referrer: tab.url || "", kind: v.kind, title: isStream ? tab.title || "" : "",
        size: v.size || 0, ask: true, incognito, ...extra,
      });
      if (reply && reply.duplicate) {
        const choice = await askDuplicate(reply.duplicate);
        if (choice === "again") return send(button, { ...extra, force: true });
        if (choice === "open") control("open", reply.duplicate.id);
        button.disabled = false;
        button.textContent = label;
        return;
      }
      button.textContent = reply && reply.ok ? "Sent" : "Failed";
      if (!reply || !reply.ok) {
        button.disabled = false;
        r.meta.textContent = (reply && reply.error) || "no reply from the extension";
        r.meta.classList.add("failed");
      }
      showJobs();
    }

    const mp3 = document.createElement("button");
    mp3.textContent = "MP3";
    mp3.title = "Save only the audio, as an mp3";
    mp3.onclick = () => send(mp3, { audioOnly: true });

    const button = document.createElement("button");
    button.className = "primary";
    button.textContent = "Download";
    button.onclick = async () => {
      if (v.kind === "hls") {
        // a master playlist has qualities and maybe subtitles: ask the downloader, then show the picker
        button.disabled = true;
        button.textContent = "Reading…";
        const info = await chrome.runtime.sendMessage({ type: "probe", url: v.url, referrer: tab.url || "", incognito });
        button.textContent = "Download";
        if (info && info.ok && (info.variants.length > 1 || info.subtitles.length)) {
          // the picker has its own Download button; this one comes back if the picker is canceled
          const box = await picker(info, (extra) => {
            box.remove();
            button.disabled = false;
            if (extra) send(button, extra);
          });
          r.row.after(box);
          return;
        }
      }
      send(button, {});
    };
    r.row.append(mp3, button);
    list.append(r.row);
  }

  document.getElementById("manager").onclick = () => chrome.tabs.create({ url: chrome.runtime.getURL("manager.html") });
  document.getElementById("allLinks").onclick = () => showLinks(tab);
  showJobs();
  setInterval(showJobs, 1000);
})();

// ---- Download all links: every link on the page, with checkboxes, a type filter and a text filter ----
// The categories use the downloader's folder lists when it has a folder of that name.
const CATEGORIES = {
  video: "mp4 mkv avi mov webm flv wmv m4v ts mpg mpeg 3gp",
  music: "mp3 m4a flac wav aac ogg wma opus",
  documents: "pdf doc docx xls xlsx ppt pptx txt rtf epub csv odt",
  compressed: "zip rar 7z tar gz bz2 xz tgz iso",
  programs: "exe msi apk bat",
  images: "jpg jpeg png gif webp bmp svg avif ico tif tiff heic",
};
let links = []; // { url, name, ext, type, row, box } for every link on the page
let linkType = "all";

async function showLinks(tab) {
  document.getElementById("main").hidden = true;
  document.getElementById("links").hidden = false;
  const note = document.getElementById("linksNote");

  // ---- input: the page's links (content.js answers in the top frame) and the folder lists ----
  const reply = await chrome.runtime.sendMessage({ type: "status" });
  const folders = (reply && reply.settings && reply.settings.folders) || {};
  for (const key of Object.keys(CATEGORIES)) if (folders[key]) CATEGORIES[key] = folders[key];
  let found = [];
  try {
    found = (await chrome.tabs.sendMessage(tab.id, { type: "links" }, { frameId: 0 })) || [];
  } catch (e) {
    note.textContent = "This page cannot be read. Reload it and try again.";
  }

  // ---- process: file name, extension and category of each link ----
  links = found.map((link) => {
    const u = new URL(link.url);
    let name = u.pathname.split("/").pop();
    try {
      name = decodeURIComponent(name);
    } catch (e) {}
    const ext = ((/\.([a-z0-9]{1,8})$/i.exec(name) || [])[1] || "").toLowerCase();
    const type = Object.keys(CATEGORIES).find((key) => ext && CATEGORIES[key].toLowerCase().split(/[\s,]+/).includes(ext)) || "";
    return { url: link.url, name: name || link.text || u.hostname, text: link.text, ext, type };
  });

  // ---- output: one row per link (built once; the filters only hide rows) ----
  const list = document.getElementById("linkList");
  for (const link of links) {
    link.row = document.createElement("label");
    link.row.className = "link";
    link.row.title = link.url;
    link.box = Object.assign(document.createElement("input"), { type: "checkbox" });
    link.box.onchange = renderLinks;
    const info = document.createElement("div");
    info.className = "info";
    info.append(Object.assign(document.createElement("div"), { className: "name", textContent: link.name }),
      Object.assign(document.createElement("div"), { className: "meta", textContent: link.text && link.text !== link.name ? link.text : new URL(link.url).hostname }));
    link.row.append(link.box, info, Object.assign(document.createElement("span"), { className: "type", textContent: link.ext || "page" }));
  }
  list.replaceChildren(...links.map((l) => l.row));
  if (!found.length && !note.textContent) list.replaceChildren(Object.assign(document.createElement("div"), { className: "empty", textContent: "No links on this page." }));
  renderLinks();
  document.getElementById("linkFilter").focus();

  for (const chip of document.querySelectorAll("#types button")) {
    chip.onclick = () => {
      linkType = chip.dataset.type;
      renderLinks();
    };
  }
  document.getElementById("linkFilter").oninput = renderLinks;
  document.getElementById("selectAll").onchange = (e) => {
    for (const link of links) if (!link.row.hidden) link.box.checked = e.target.checked;
    renderLinks();
  };
  document.getElementById("linksBack").onclick = () => {
    document.getElementById("links").hidden = true;
    document.getElementById("main").hidden = false;
  };
  document.getElementById("downloadSelected").onclick = () => downloadSelected(tab);
}

// show the links the type and the text filter allow; counts on the chips and the button
function renderLinks() {
  const text = document.getElementById("linkFilter").value.trim().toLowerCase();
  for (const chip of document.querySelectorAll("#types button")) {
    const count = links.filter((l) => chip.dataset.type === "all" || l.type === chip.dataset.type).length;
    chip.textContent = chip.textContent.replace(/ \(\d+\)$/, "") + " (" + count + ")";
    chip.classList.toggle("on", chip.dataset.type === linkType);
    chip.disabled = !count && chip.dataset.type !== "all";
  }
  for (const link of links) {
    link.row.hidden = !((linkType === "all" || link.type === linkType)
      && (!text || (link.name + " " + link.url + " " + link.text).toLowerCase().includes(text)));
  }
  const shown = links.filter((l) => !l.row.hidden);
  const picked = shown.filter((l) => l.box.checked);
  document.getElementById("selectAll").checked = shown.length > 0 && picked.length === shown.length;
  document.getElementById("linkCount").textContent = "(" + picked.length + " of " + shown.length + ")";
  const button = document.getElementById("downloadSelected");
  button.disabled = !picked.length;
  button.textContent = picked.length ? "Download selected (" + picked.length + ")" : "Download selected";
}

// add every checked link that is shown to the queue; duplicates ask with a notification
async function downloadSelected(tab) {
  const button = document.getElementById("downloadSelected");
  const picked = links.filter((l) => !l.row.hidden && l.box.checked);
  button.disabled = true;
  let added = 0, already = 0;
  const failed = [];
  for (const link of picked) {
    const reply = await chrome.runtime.sendMessage({ type: "download", url: link.url, referrer: tab.url || "", incognito, queue: true });
    if (reply && reply.ok) added++, (link.box.checked = false);
    else if (reply && reply.duplicate) already++, (link.box.checked = false);
    else failed.push(link.name + ": " + ((reply && reply.error) || "no reply"));
  }
  document.getElementById("linksNote").textContent = [
    added ? added + " added to the queue" : "",
    already ? already + " already downloaded (see the notification)" : "",
    failed.length ? failed.length + " failed: " + failed[0] : "",
  ].filter(Boolean).join(", ");
  renderLinks();
  showJobs();
}
