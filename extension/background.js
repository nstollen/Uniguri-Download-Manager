const HOST = "com.udm.host";
const MIN_SIZE = 500 * 1024; // ignore media files smaller than this
const MAX_LIST = 30; // per tab
const ICON = chrome.runtime.getURL("icons/icon128.png");
const handedBack = new Set(); // urls given back to the browser after a failed hand-off
let queue = Promise.resolve(); // video list updates run one at a time

// Send one message to the native host. Resolves to its reply: { ok, error, ... }.
async function ask(message) {
  try {
    return (await chrome.runtime.sendNativeMessage(HOST, message)) || { ok: false, error: "empty reply" };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// The Cookie header the browser would send to this url. An incognito window has its own cookie store.
async function cookieHeader(url, incognito) {
  const store = incognito && (await chrome.cookies.getAllCookieStores()).find((s) => s.id !== "0");
  if (incognito && !store) return "";
  return (await chrome.cookies.getAll({ url, ...(store ? { storeId: store.id } : {}) })).map((c) => `${c.name}=${c.value}`).join("; ");
}

// Send one download job: url, referrer, cookies, user agent (+ kind and title for streams).
// extra.private: it comes from an incognito window, so the downloader keeps nothing of it.
// When the file was already downloaded the downloader refuses it with reply.duplicate. A page that
// asks the user itself passes pageAsks; everything else asks with a notification.
async function handOff(url, referrer, extra, pageAsks) {
  const cookies = await cookieHeader(url, extra && extra.private);
  const reply = await ask({ url, referrer: referrer || "", cookies, userAgent: navigator.userAgent, ...extra });
  if (reply.ok && reply.id) watch();
  if (reply.duplicate && !pageAsks) askDuplicate(reply.duplicate, url, referrer, extra);
  return reply;
}

function notify(title, message, extra) {
  chrome.notifications.create(extra && extra.id, { type: "basic", iconUrl: ICON, title, message, ...(extra && extra.options) });
}

// The file name at the end of a url: "https://x/a/My%20File.zip?t=1" -> "My File.zip"
function fileName(url) {
  try {
    return decodeURIComponent(new URL(url).pathname.split("/").pop());
  } catch (e) {
    return "";
  }
}

// ---- duplicate check: "already downloaded" notification with two buttons; closing it cancels ----
async function askDuplicate(dup, url, referrer, extra) {
  const id = "dup" + Date.now() + Math.random();
  await chrome.storage.session.set({ [id]: { url, referrer, extra, existing: dup.id } });
  notify("UDM: already downloaded", dup.name + "\n" + dup.path, {
    id, options: { buttons: [{ title: "Download again" }, { title: "Open existing file" }], requireInteraction: true },
  });
}
async function notificationButton(id, index) {
  if (id.startsWith("threat:")) { // Windows Defender found a threat: 0 = delete the file, 1 = keep it
    chrome.notifications.clear(id);
    const reply = await ask({ type: "threat", id: id.slice(7), choice: index === 0 ? "delete" : "keep" });
    if (!reply.ok) notify("UDM: not done", reply.error);
    return;
  }
  const pending = (await chrome.storage.session.get(id))[id];
  chrome.notifications.clear(id);
  if (!pending) return;
  await chrome.storage.session.remove(id);
  if (pending.existing) {
    if (index === 0) handOff(pending.url, pending.referrer, { ...pending.extra, force: true });
    else ask({ type: "open", id: pending.existing });
  } else if (pending.copied && index === 0) {
    handOff(pending.copied, "");
  }
}
chrome.notifications.onButtonClicked.addListener(notificationButton);
chrome.notifications.onClosed.addListener((id) => chrome.storage.session.remove(id)); // closed = Cancel

// ---- refresh link: a direct download failed with 403, 404 or 410 after it started ----
// Its page is opened again. The next response for the same file (same name or same size), in any
// tab, gives the downloader the new url, and the download continues from its saved parts.
let refreshing = null; // { id, name, names, size, until } while a refresh waits for its file
const caught = new Set(); // urls handed to a refresh: the browser's own download of them is dropped
chrome.storage.session.get("refresh").then((s) => (refreshing = refreshing || s.refresh || null));

async function startRefresh(id) {
  const job = ((await ask({ type: "status" })).jobs || {})[id];
  if (!job || !job.refresh) return { ok: false, error: "this download has no link to refresh" };
  if (!/^https?:/i.test(job.referrer || "")) {
    return { ok: false, error: "the page this download came from is not known. Open it, and start the download there again." };
  }
  refreshing = { id, name: job.name, names: [job.name, fileName(job.url)].filter(Boolean), size: job.total, until: Date.now() + 10 * 60000 };
  await chrome.storage.session.set({ refresh: refreshing });
  await chrome.tabs.create({ url: job.referrer });
  return { ok: true };
}

// input: a response or a new browser download. process: is it the file a refresh waits for?
// output: true when it was; the new url went to the downloader, which checks the size
function catchRefresh(url, name, size) {
  const r = refreshing;
  if (!r || !/^https?:/i.test(url)) return false;
  if (Date.now() > r.until) {
    refreshing = null;
    chrome.storage.session.remove("refresh");
    return false;
  }
  if (!r.names.includes(name) && !(size && size === r.size)) return false;
  refreshing = null; // taken right away, so the same file is not caught twice
  caught.add(url);
  chrome.storage.session.remove("refresh");
  (async () => {
    const reply = await ask({ type: "refresh", id: r.id, url, cookies: await cookieHeader(url), userAgent: navigator.userAgent });
    if (reply.ok) watch();
    notify(reply.ok ? "UDM: link refreshed" : "UDM: link not refreshed",
      reply.ok ? r.name + " continues from where it stopped." : r.name + "\n" + reply.error);
  })();
  return true;
}

// Pass one control action (pause, resume, cancel, retry, delete) to the downloader.
// The downloader never saves cookies, so resume and retry carry fresh ones for the job's url.
async function control(msg) {
  if (msg.action === "refresh") return startRefresh(msg.id);
  const out = { ...msg, type: msg.action };
  delete out.action;
  const again = msg.action === "resume" || msg.action === "retry";
  if (again) {
    const job = ((await ask({ type: "status" })).jobs || {})[msg.id];
    if (job) Object.assign(out, { cookies: await cookieHeader(job.url), userAgent: navigator.userAgent });
  }
  const reply = await ask(out);
  if (reply.ok && again) watch();
  return reply;
}

// While downloads are active, look at the job list every few seconds and raise one Windows
// notification for each job that finished or failed. The downloader remembers which were announced.
let watching = false;
async function watch() {
  if (watching) return;
  watching = true;
  try {
    for (;;) {
      const reply = await ask({ type: "status" });
      const jobs = Object.values(reply.jobs || {});
      const news = jobs.filter((j) => (j.state === "done" || j.state === "failed") && !j.notified);
      if (news.length) {
        await ask({ type: "notified", ids: news.map((j) => j.id) });
        // a threat is always announced, also with notifications off; a private one without its name
        for (const job of news.filter((j) => j.scan === "threat")) {
          notify("UDM: threat found", (job.private ? "In a private download" : job.name) + "\n"
            + (job.threat || "Windows Defender found a threat") + ". Delete the file, or keep it?", {
            id: "threat:" + job.id, options: { buttons: [{ title: "Delete file" }, { title: "Keep file" }], requireInteraction: true },
          });
        }
        // no notification for a private download: Windows keeps old notifications in its list
        for (const job of reply.settings.notify ? news.filter((j) => !j.private && j.scan !== "threat") : []) {
          chrome.notifications.create({
            type: "basic", iconUrl: ICON,
            title: job.state === "done" ? "UDM: download finished" : "UDM: download failed",
            message: job.state === "done" ? job.name : job.name + "\n" + job.detail,
          });
        }
      }
      if (!jobs.some((j) => j.state === "running" || j.state === "queued")) return;
      await new Promise((r) => setTimeout(r, 3000));
    }
  } finally {
    watching = false;
  }
}
// also look once a minute, for downloads the scheduler started or that were resumed from elsewhere
chrome.alarms.create("watch", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(watch);
watch();

// ---- right-click menu on links, images and videos ----
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "udm", title: "Download with UDM", contexts: ["link", "image", "video", "audio"] });
  });
});
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  // on a picture or a video take the media itself, otherwise the link
  const url = (info.mediaType ? info.srcUrl : info.linkUrl) || info.srcUrl || info.linkUrl || "";
  let error = "";
  if (!/^https?:/i.test(url)) {
    error = "This address only exists inside the browser (" + url.split(":")[0] + ":). Click the UDM icon to pick the real stream.";
  } else {
    const reply = await handOff(url, info.pageUrl || (tab && tab.url) || "", { private: !!(tab && tab.incognito) });
    if (!reply.ok && !reply.duplicate) error = reply.error; // a duplicate has its own notification
  }
  if (error) chrome.notifications.create({ type: "basic", iconUrl: ICON, title: "UDM: could not start the download", message: error });
});

// ---- capture rules (settings page): which browser downloads UDM takes; the rest stay in the browser ----
// Kept in chrome.storage.local, so they work while the downloader is off.
//   ignoreSites: "example.com other.org" (the download's site or the page's site)
//   types: "zip exe mp4" (file extensions; empty = every type), minSize: KB (0 = any size)
//   clipboard: clipboard watch on/off
const RULES = { ignoreSites: "", types: "", minSize: 0, clipboard: false };
async function captureRules() {
  return { ...RULES, ...(await chrome.storage.local.get("capture")).capture };
}
const words = (text) => (text || "").toLowerCase().split(/[\s,;]+/).filter(Boolean);
const extension = (name) => ((/\.([a-z0-9]{1,8})$/i.exec(name || "") || [])[1] || "").toLowerCase();

// input: the rules and one download. output: true when UDM should take it
function wanted(rules, url, referrer, name, size) {
  const hosts = [url, referrer].map((x) => {
    try {
      return new URL(x).hostname.toLowerCase();
    } catch (e) {
      return "";
    }
  });
  const sites = words(rules.ignoreSites);
  if (hosts.some((h) => h && sites.some((site) => h === site || h.endsWith("." + site)))) return false;
  const types = words(rules.types).map((t) => t.replace(/^\./, ""));
  if (types.length && !types.includes(extension(name))) return false;
  if (rules.minSize > 0 && size > 0 && size < rules.minSize * 1024) return false; // unknown size: taken
  return true;
}

// ---- Alt+click: content.js tells us the link first; that url goes to the browser once ----
async function skipped(urls) {
  const keys = urls.map((u) => "skip " + u);
  const found = await chrome.storage.session.get(keys);
  const hit = keys.find((k) => found[k] && found[k] > Date.now());
  if (hit) await chrome.storage.session.remove(hit);
  return !!hit;
}

// ---- browser downloads: take the ones the rules allow, cancel the browser's copy, hand it off ----
// onDeterminingFilename comes once the browser knows the file name (also from Content-Disposition)
// and the size; the download waits for us until suggest() or cancel.
chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  capture(item).then((taken) => taken || suggest(), (e) => {
    console.warn("udm capture:", e);
    suggest();
  });
  return true; // suggest() comes later
});

async function capture(item) {
  // ---- input: the browser's new download ----
  const url = item.finalUrl || item.url;
  if (!/^https?:/i.test(url) || item.state !== "in_progress") return false; // blob:, data: only exist in the browser
  if (handedBack.delete(url)) return false; // our own fallback after a failed hand-off
  const name = (item.filename || "").split(/[\\/]/).pop() || fileName(url);
  const size = item.totalBytes > 0 ? item.totalBytes : 0;
  const drop = () => chrome.downloads.cancel(item.id, () => chrome.downloads.erase({ id: item.id }));

  // ---- process: a Refresh link waits for it? Alt+click? the capture rules? ----
  // the file a Refresh link waits for: its new url goes to that download, not to a new one
  if (caught.has(url) || catchRefresh(url, name, size)) return drop(), true;
  if (await skipped([item.url, url])) return false;
  if (!wanted(await captureRules(), url, item.referrer, name, size)) return false;

  // ---- output: the downloader gets it (a duplicate asks with a notification) ----
  // an incognito download is private: the downloader keeps nothing of it
  drop();
  const reply = await handOff(url, item.referrer, { size, private: !!item.incognito });
  if (!reply.ok && !reply.duplicate && item.incognito) {
    // the browser's own copy would land in the normal window's download list, so say it instead
    notify("UDM: could not start the download", fileName(url) + "\n" + reply.error);
  } else if (!reply.ok && !reply.duplicate) {
    console.warn("udm hand-off failed, letting the browser download:", reply.error);
    handedBack.add(url);
    chrome.downloads.download({ url });
  }
  return true;
}

// ---- clipboard watch (settings page, off by default) ----
// An offscreen page reads the clipboard every second and reports a newly copied http(s) link.
// A link whose file type is in the capture rules (or, with no types there, in the downloader's
// folder lists) gets a notification with a Download button. Web page links get nothing.
async function clipboardWatch() {
  const on = (await captureRules()).clipboard;
  const open = await chrome.offscreen.hasDocument();
  if (on && !open) {
    await chrome.offscreen.createDocument({
      url: "clipboard.html", reasons: ["CLIPBOARD"], justification: "Clipboard watch: offer to download copied file links",
    });
  } else if (!on && open) {
    await chrome.offscreen.closeDocument();
  }
}
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.capture) clipboardWatch().catch((e) => console.warn("udm clipboard:", e));
});
clipboardWatch().catch((e) => console.warn("udm clipboard:", e));

async function copied(url) {
  // input: a link that was just copied. process: a file type we catch, on a site not ignored?
  const rules = await captureRules();
  if (!rules.clipboard || !/^https?:\/\/\S+$/i.test(url)) return;
  let types = words(rules.types);
  if (!types.length) {
    const folders = ((await ask({ type: "status" })).settings || {}).folders || {};
    types = words(Object.values(folders).join(" "));
  }
  const ext = extension(fileName(url));
  if (!ext || !types.includes(ext) || !wanted({ ...rules, types: "" }, url, "", fileName(url), 0)) return;
  // output: a notification that asks
  const id = "clip" + Date.now();
  await chrome.storage.session.set({ [id]: { copied: url } });
  notify("UDM: download the copied link?", fileName(url) + "\n" + url, { id, options: { buttons: [{ title: "Download" }] } });
}

// ---- video detection: watch every response in every tab ----
chrome.webRequest.onHeadersReceived.addListener(
  (d) => {
    const newPage = d.type === "main_frame"; // the tab navigated: start a fresh list

    // input: response headers
    const h = {};
    for (const x of d.responseHeaders || []) h[x.name.toLowerCase()] = x.value || "";
    const type = (h["content-type"] || "").split(";")[0].trim().toLowerCase();
    const u = new URL(d.url);
    const path = u.pathname.toLowerCase();

    // a Refresh link waits for this file? (same name, or same size). Not the html page itself,
    // whose address often ends in the file name too.
    if (refreshing && (d.statusCode === 200 || d.statusCode === 206) && type !== "text/html") {
      const named = /filename\*?=(?:UTF-8'')?"?([^";]+)/i.exec(h["content-disposition"] || "");
      const total = d.statusCode === 206 ? Number((h["content-range"] || "").split("/")[1]) || 0 : Number(h["content-length"]) || 0;
      let name = fileName(d.url);
      try { if (named) name = decodeURIComponent(named[1]); } catch (e) {}
      catchRefresh(d.url, name, total);
    }
    if (d.tabId < 0) return;

    // process: a stream playlist (any size), or a media file of 500 KB or more
    let kind = "";
    if (path.endsWith(".m3u8") || type.includes("mpegurl")) kind = "hls";
    else if (path.endsWith(".mpd") || type === "application/dash+xml") kind = "dash";
    else if (/^(video|audio)\//.test(type) || /\.(mp4|webm)$/.test(path)) kind = "file";
    const segment = type === "video/mp2t" || /\.(ts|m4s)$/.test(path); // pieces of a stream, not files
    // a 206 reply only carries a slice, so the full size is the number after "/" in Content-Range
    const size = d.statusCode === 206
      ? Number((h["content-range"] || "").split("/")[1]) || 0
      : Number(h["content-length"]) || 0;
    const ok = d.statusCode === 200 || d.statusCode === 206;
    const wanted = kind && !segment && ok && (kind !== "file" || size >= MIN_SIZE);
    if (!wanted && !newPage) return;

    // output: update this tab's list and the badge
    const key = "tab" + d.tabId;
    const same = u.origin + u.pathname; // the same stream is often requested again with a new token
    queue = queue
      .then(async () => {
        if (newPage) await chrome.storage.session.remove("blob" + d.tabId);
        const list = newPage ? [] : (await chrome.storage.session.get(key))[key] || [];
        if (wanted && list.length < MAX_LIST && !list.some((v) => v.same === same)) {
          let name = u.pathname.split("/").pop();
          try { name = decodeURIComponent(name); } catch (e) {}
          list.push({ url: d.url, same, name: name || u.hostname, kind, size, type });
        }
        await chrome.storage.session.set({ [key]: list });
        await chrome.action.setBadgeText({ tabId: d.tabId, text: list.length ? String(list.length) : "" });
      })
      .catch((e) => console.warn("udm video list:", e));
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders"]
);

// keep the badge right after a page finishes loading, and forget closed tabs
chrome.tabs.onUpdated.addListener(async (tabId, change) => {
  if (change.status !== "complete") return;
  const key = "tab" + tabId;
  const list = (await chrome.storage.session.get(key))[key] || [];
  chrome.action.setBadgeText({ tabId, text: list.length ? String(list.length) : "" }).catch(() => {});
});
chrome.tabs.onRemoved.addListener((tabId) => chrome.storage.session.remove(["tab" + tabId, "blob" + tabId]));

// ---- messages from the content script and the popup ----
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "blob" && sender.tab) {
    chrome.storage.session.set({ ["blob" + sender.tab.id]: true }); // content.js saw a blob: video
  } else if (msg.type === "skip" && sender.tab) {
    chrome.storage.session.set({ ["skip " + msg.url]: Date.now() + 30000 }); // Alt+click: 30 seconds to start
    return;
  }
  // everything else only from the extension's own pages (popup, manager, settings, stats, clipboard), never a web page
  if (!(sender.url || "").startsWith(chrome.runtime.getURL(""))) return;
  if (msg.type === "copied" && sender.url === chrome.runtime.getURL("clipboard.html")) {
    copied(msg.url);
  } else if (msg.type === "download") {
    // variant, subtitles, subFormat: picked in the popup for a stream. audioOnly: the MP3 button.
    // force: "Download again" after the duplicate check. ask: the page asks about duplicates itself.
    // incognito: the popup's tab is in an incognito window, so the download is private
    const { kind, title, variant, subtitles, subFormat, audioOnly, force } = msg;
    const extra = { kind, title, variant, subtitles, subFormat, audioOnly, force, private: !!msg.incognito };
    handOff(msg.url, msg.referrer, extra, msg.ask).then(sendResponse);
    return true; // reply comes later
  } else if (msg.type === "probe") {
    // the qualities and subtitle tracks of a stream, read by the downloader with the browser's cookies
    cookieHeader(msg.url, msg.incognito)
      .then((cookies) => ask({ type: "probe", url: msg.url, referrer: msg.referrer || "", cookies, userAgent: navigator.userAgent, private: !!msg.incognito }))
      .then(sendResponse);
    return true;
  } else if (msg.type === "control") {
    control(msg).then(sendResponse);
    return true;
  } else if (["stats", "thumb", "export", "import", "cleardata", "forget"].includes(msg.type)) {
    // stats page, thumbnails, backup, and the privacy tools (Clear data, Forget this site)
    const { type, id, offset, part, history, stats, settings, range, what, files, site, dry } = msg;
    ask({ type, id, offset, part, history, stats, settings, range, what, files, site, dry }).then(sendResponse);
    return true;
  } else if (msg.type === "proxytest") {
    // settings page, Network: Test with what is typed there (an empty password means the saved one)
    const { proxyMode, proxyType, proxyHost, proxyPort, proxyUser, proxyPassword } = msg;
    ask({ type: "proxytest", proxyMode, proxyType, proxyHost, proxyPort, proxyUser, proxyPassword }).then(sendResponse);
    return true;
  } else if (msg.type === "defender") {
    ask({ type: "defender" }).then(sendResponse); // settings page, Security: can Windows Defender scan?
    return true;
  } else if (msg.type === "status") {
    ask({ type: "status" }).then(sendResponse);
    return true;
  }
});
