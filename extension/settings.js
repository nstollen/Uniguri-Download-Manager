// The settings page. The downloader keeps the settings (settings.json); this page shows and changes them.
const TEXT = ["folder", "startTime", "stopTime", "limitFrom", "limitTo", "proxyHost", "proxyUser", "proxyType"];
const NUMBER = ["maxJobs", "parts", "siteConnections", "speedLimit", "limitSpeed", "proxyPort"];
const CHECK = ["ask", "sortFolders", "notify", "scheduleStart", "scheduleStop", "limitSchedule", "statsSites", "scanDownloads"];
const SELECT = ["autoClean"]; // number choices
// private mode is not in these lists: it changes right away (and from the popup and manager too), not with Save
const note = document.getElementById("note");
const folders = document.getElementById("folders");
let loaded = false; // the downloader's settings are in the form (it was running)

// one line of the file type list: folder name + its extensions
function folderLine(name, extensions) {
  const line = document.createElement("div");
  line.className = "line";
  for (const [value, hint] of [[name, "folder name"], [extensions, "extensions, like: mp4 mkv avi"]]) {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value;
    input.placeholder = hint;
    line.append(input);
  }
  folders.append(line);
}

// input: the settings from the downloader -> output: the filled form
async function load() {
  const reply = await chrome.runtime.sendMessage({ type: "status" });
  if (!reply || !reply.ok) {
    note.className = "bad";
    note.textContent = "The downloader is not running, so its settings cannot be shown or saved. Capture rules still can.";
    return;
  }
  loaded = true;
  const s = reply.settings;
  // a setting missing from the reply (an older downloader) shows as empty, never as "undefined"
  for (const id of TEXT.concat(NUMBER)) document.getElementById(id).value = s[id] ?? "";
  for (const id of CHECK) document.getElementById(id).checked = !!s[id];
  for (const id of SELECT) document.getElementById(id).value = String(s[id] ?? 0);
  document.getElementById("privateMode").checked = !!s.privateMode;
  document.getElementById("defaultFolder").textContent = reply.defaultFolder;
  document.getElementById("dataFolder").textContent = reply.dataFolder;
  folders.replaceChildren();
  for (const [name, extensions] of Object.entries(s.folders)) folderLine(name, extensions);
  for (const input of proxyModes) input.checked = input.value === (s.proxyMode || "none");
  document.getElementById("trustedSites").value = words(s.trustedSites).join("\n");
  document.getElementById("siteLimits").value = s.siteLimits || "";
  showPassword(s.proxyPasswordSet);
  showProxy();
  showDefender();
}

// ---- network: the proxy ----
const proxyModes = document.querySelectorAll("input[name=proxyMode]");
const proxyPassword = document.getElementById("proxyPassword");
const proxyForget = document.getElementById("proxyForget");
const proxyResult = document.getElementById("proxyResult");
const proxyMode = () => ([...proxyModes].find((x) => x.checked) || { value: "none" }).value;

// the manual boxes only for Manual, the SOCKS5 note only for SOCKS5, Test only with a proxy.
// An earlier Test result goes away when the proxy changes.
function showProxy() {
  document.getElementById("proxyManual").hidden = proxyMode() !== "manual";
  document.getElementById("proxyWindowsHint").hidden = proxyMode() !== "windows";
  document.getElementById("proxyTestLine").hidden = proxyMode() === "none";
  document.getElementById("socksNote").hidden = document.getElementById("proxyType").value !== "socks5";
  proxyResult.textContent = "";
}
for (const input of proxyModes) input.onchange = showProxy;
document.getElementById("proxyManual").oninput = () => (proxyResult.textContent = "");
document.getElementById("proxyType").onchange = showProxy;

// the password box is always empty: a saved password never comes to this page, only whether there is one
function showPassword(saved) {
  proxyPassword.value = "";
  proxyPassword.placeholder = saved ? "saved (type a new one to change it)" : "optional";
  proxyForget.disabled = !saved;
}
proxyForget.onclick = async (e) => {
  e.preventDefault(); // the button sits in a label
  const reply = await chrome.runtime.sendMessage({ type: "control", action: "settings", set: { proxyPassword: "" } });
  note.className = reply && reply.ok ? "good" : "bad";
  note.textContent = reply && reply.ok ? "The saved proxy password is deleted." : (reply && reply.error) || "no reply from the downloader";
  if (reply && reply.ok) showPassword(false);
};

// the proxy as typed in the form
function readProxy() {
  return {
    proxyMode: proxyMode(),
    proxyType: document.getElementById("proxyType").value,
    proxyHost: document.getElementById("proxyHost").value.trim(),
    proxyPort: Math.floor(Number(document.getElementById("proxyPort").value) || 0),
    proxyUser: document.getElementById("proxyUser").value.trim(),
  };
}

document.getElementById("proxyTest").onclick = async () => {
  const test = document.getElementById("proxyTest");
  test.disabled = true;
  proxyResult.className = "note";
  proxyResult.textContent = "Testing…";
  const reply = await chrome.runtime.sendMessage({ type: "proxytest", ...readProxy(), proxyPassword: proxyPassword.value });
  test.disabled = false;
  const ok = reply && reply.ok;
  proxyResult.className = "note " + (ok ? "good" : "bad");
  proxyResult.textContent = ok ? "OK: the test page loaded through " + reply.via + " in " + reply.ms + " ms."
    : "Failed: " + ((reply && reply.error) || "no reply from the downloader");
};

// ---- security: can Windows Defender scan here? ----
async function showDefender() {
  const state = document.getElementById("defenderState");
  const reply = await chrome.runtime.sendMessage({ type: "defender" });
  if (!reply || !reply.ok) return;
  state.className = "note wide " + (reply.available ? "good" : "warn");
  state.textContent = reply.available ? "Windows Defender is ready to scan." : "Scan not available: " + reply.reason + ". Downloads are not scanned.";
}

// ---- capture rules: kept by the extension (chrome.storage.local), see background.js ----
const words = (text) => (text || "").toLowerCase().split(/[\s,;]+/).filter(Boolean);

async function loadCapture() {
  const c = { ignoreSites: "", types: "", minSize: 0, clipboard: false, ...(await chrome.storage.local.get("capture")).capture };
  document.getElementById("ignoreSites").value = words(c.ignoreSites).join("\n");
  document.getElementById("captureTypes").value = c.types;
  document.getElementById("minSize").value = c.minSize;
  document.getElementById("clipboardWatch").checked = c.clipboard;
}

// input: the form. output: the rules, cleaned up ("https://www.x.com/a" -> "www.x.com", ".ZIP" -> "zip")
function readCapture() {
  return {
    ignoreSites: words(document.getElementById("ignoreSites").value).map((x) => x.replace(/^[a-z]+:\/\//, "").split(/[/?#]/)[0]).join("\n"),
    types: words(document.getElementById("captureTypes").value).map((x) => x.replace(/^\*?\./, "")).join(" "),
    minSize: Math.max(0, Math.floor(Number(document.getElementById("minSize").value) || 0)),
    clipboard: document.getElementById("clipboardWatch").checked,
  };
}

// input: the form -> output: the capture rules to chrome.storage.local, one settings message to the downloader
document.getElementById("save").onclick = async () => {
  await chrome.storage.local.set({ capture: readCapture() });
  loadCapture();
  if (!loaded) { // the downloader's part of the form is empty: do not send it
    note.className = "bad";
    note.textContent = "Capture rules saved. The downloader is not running, so its settings were not saved.";
    return;
  }
  const set = { folders: {} };
  for (const id of TEXT) set[id] = document.getElementById(id).value.trim();
  for (const id of NUMBER) set[id] = Math.floor(Number(document.getElementById(id).value) || 0);
  for (const id of CHECK) set[id] = document.getElementById(id).checked;
  for (const id of SELECT) set[id] = Number(document.getElementById(id).value);
  for (const line of folders.children) {
    const [name, extensions] = [...line.querySelectorAll("input")].map((i) => i.value.trim());
    if (name) set.folders[name] = extensions.toLowerCase();
  }
  Object.assign(set, readProxy());
  if (proxyPassword.value) set.proxyPassword = proxyPassword.value; // empty: keep the saved one
  // "https://www.x.com/a" -> "www.x.com", like the sites to ignore
  set.trustedSites = words(document.getElementById("trustedSites").value).map((x) => x.replace(/^[a-z]+:\/\//, "").split(/[/?#]/)[0]).join("\n");
  set.siteLimits = document.getElementById("siteLimits").value;
  const reply = await chrome.runtime.sendMessage({ type: "control", action: "settings", set });
  const ok = reply && reply.ok;
  note.className = ok ? "good" : "bad";
  note.textContent = ok ? "Saved." : (reply && reply.error) || "no reply from the downloader";
  if (ok) load();
};
document.getElementById("addFolder").onclick = () => folderLine("", "");

// the theme is kept by the extension, not the downloader, so it saves on click and works while the downloader is off
const themeInputs = document.querySelectorAll("input[name=theme]");
for (const input of themeInputs) input.onchange = () => setTheme(input.value);
onThemeChange((theme) => {
  for (const input of themeInputs) input.checked = input.value === theme;
});

// ---- privacy ----
// private mode: saved right away. The popup and the manager can change it too, so look again when the page gets focus
const privateBox = document.getElementById("privateMode");
privateBox.onchange = async () => {
  const reply = await chrome.runtime.sendMessage({ type: "control", action: "settings", set: { privateMode: privateBox.checked } });
  note.className = reply && reply.ok ? "good" : "bad";
  note.textContent = reply && reply.ok ? "Private mode is " + (privateBox.checked ? "on." : "off.") : (reply && reply.error) || "no reply from the downloader";
};
window.addEventListener("focus", async () => {
  const reply = await chrome.runtime.sendMessage({ type: "status" });
  if (reply && reply.ok) privateBox.checked = !!reply.settings.privateMode;
});

// Clear data: a time range and what to clear; the counts come from the downloader (a dry run) as you pick
const clearDialog = document.getElementById("clearDialog");
const clearGo = document.getElementById("clearGo");
const clearFiles = document.getElementById("clearFiles");
const UNITS = { history: ["download", "downloads"], stats: ["line", "lines"], thumbs: ["file", "files"], log: ["line", "lines"], files: ["file", "files"] };

// input: the dialog's choices -> output: the message for the downloader
function clearRequest(dry) {
  const what = [...clearDialog.querySelectorAll("[data-what]:checked")].map((x) => x.dataset.what);
  clearFiles.disabled = !what.includes("history"); // the files of the downloads that leave the history
  if (clearFiles.disabled) clearFiles.checked = false;
  return { type: "cleardata", range: Number(document.getElementById("clearRange").value), what, files: clearFiles.checked, dry };
}

async function clearCounts() {
  const reply = await chrome.runtime.sendMessage(clearRequest(true));
  const c = (reply && reply.ok && reply.counts) || null;
  for (const span of clearDialog.querySelectorAll("[data-count]")) {
    const n = c ? c[span.dataset.count] : null;
    span.textContent = n == null ? "" : n + " " + UNITS[span.dataset.count][n === 1 ? 0 : 1];
  }
  const total = c ? c.history + c.stats + c.thumbs + c.log + c.files : 0;
  clearGo.disabled = !total;
  clearGo.textContent = !c ? "The downloader is not running" : total ? "Clear" : "Nothing to clear";
  return c;
}

document.getElementById("clearData").onclick = () => {
  clearDialog.showModal();
  clearCounts();
};
clearDialog.onchange = clearCounts;
document.getElementById("clearCancel").onclick = () => clearDialog.close();
clearGo.onclick = async () => {
  const request = clearRequest(false);
  const c = await clearCounts(); // the counts right now, for the question
  if (!c) return;
  const sure = await choose("Delete this for good?", "This deletes " + countText(c) + ". It cannot be undone.",
    [["Delete", "delete"], ["Back", ""]]);
  if (!sure) return;
  const reply = await chrome.runtime.sendMessage(request);
  clearDialog.close();
  const clearNote = document.getElementById("clearNote");
  clearNote.textContent = reply && reply.ok ? "Deleted " + countText(reply.counts) + "." : "Not cleared: " + ((reply && reply.error) || "no reply from the downloader");
};

// ---- backup: history, stats, settings and capture rules as one json file ----
const backupNote = document.getElementById("backupNote");

// input: one kind of rows from the downloader ("history" or "stats"), 500 per message -> output: all of them, or null
async function exportRows(part) {
  const rows = [];
  let reply;
  do {
    reply = await chrome.runtime.sendMessage({ type: "export", part, offset: rows.length });
    if (!reply || !reply.ok) return null;
    rows.push(...reply.rows);
  } while (rows.length < reply.total && reply.rows.length);
  return { rows, settings: reply.settings };
}

document.getElementById("export").onclick = async () => {
  // input: what to include, then the downloader's rows and settings, and the capture rules
  const choice = await choose("What should the backup include?",
    "Settings and capture rules are always in it. History and stats list every download and the site it came from.",
    [["History, stats and settings", "all"], ["Only settings", "settings"], ["Cancel", ""]]);
  if (!choice) return;
  const history = choice === "all" ? await exportRows("history") : null;
  const stats = choice === "all" ? await exportRows("stats") : null;
  const reply = choice === "all" ? history && stats && history : await chrome.runtime.sendMessage({ type: "status" });
  if (!reply || reply.ok === false) {
    backupNote.textContent = "Export needs the downloader, and it is not running.";
    return;
  }
  const capture = (await chrome.storage.local.get("capture")).capture || {};
  // output: udm-backup-2026-10-14.json, saved by the browser
  const backup = { udmBackup: 1, exported: new Date().toISOString(), settings: reply.settings, capture };
  if (choice === "all") Object.assign(backup, { history: history.rows, stats: stats.rows });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([JSON.stringify(backup, null, 1)], { type: "application/json" }));
  link.download = "udm-backup-" + new Date().toLocaleDateString("sv") + ".json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  backupNote.textContent = "Exported " + backupText(backup) + ".";
};

// "1200 downloads, 900 stat lines, the settings and the capture rules", or "the settings and the capture rules"
function backupText(backup) {
  const parts = [];
  if (backup.history) parts.push(backup.history.length + " downloads");
  if (backup.stats) parts.push(backup.stats.length + " stat lines");
  return parts.concat("the settings and the capture rules").join(", ");
}

const importFile = document.getElementById("importFile");
document.getElementById("import").onclick = () => importFile.click();
importFile.onchange = async () => {
  // input: the chosen file
  const file = importFile.files[0];
  importFile.value = "";
  if (!file) return;
  let backup;
  try {
    backup = JSON.parse(await file.text());
  } catch (e) {
    backupNote.textContent = file.name + " is not a json file.";
    return;
  }
  // history and stats are left out of a "settings only" backup
  const lists = [backup && backup.history, backup && backup.stats].every((x) => x === undefined || Array.isArray(x));
  if (!backup || backup.udmBackup !== 1 || !lists || !backup.settings || typeof backup.settings !== "object") {
    backupNote.textContent = file.name + " is not a UDM backup.";
    return;
  }
  // process: ask first
  const when = backup.exported ? new Date(backup.exported).toLocaleString() : "an unknown date";
  const what = [backup.history && "history", backup.stats && "stats", "settings"].filter(Boolean).join(", ");
  const choice = await choose("Replace your " + what + " and capture rules?",
    "With the backup from " + when + ": " + (backup.history ? backupText(backup) + ". What is there now is replaced; files on disk are not touched."
      : "settings only. Your history and stats stay as they are."),
    [["Replace", "yes"], ["Cancel", ""]]);
  if (!choice) return;
  // output: the downloader takes the history and settings (all or nothing), then the capture rules
  const reply = await chrome.runtime.sendMessage({ type: "import", history: backup.history, stats: backup.stats, settings: backup.settings });
  if (!reply || !reply.ok) {
    backupNote.textContent = "Not imported: " + ((reply && reply.error) || "no reply from the downloader");
    return;
  }
  await chrome.storage.local.set({ capture: { ...backup.capture } });
  backupNote.textContent = "Imported " + backupText(backup) + ".";
  load();
  loadCapture();
};

load();
loadCapture();
