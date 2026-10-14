// Tells the extension when this page plays a <video> through a blob: url.
// A blob: url only exists inside the browser; the real stream is the .m3u8/.mpd the player loaded.
let told = false;

function check(event) {
  if (told) return;
  const videos = [...document.querySelectorAll("video")];
  if (event && event.target instanceof HTMLVideoElement) videos.push(event.target);
  if (videos.some((v) => (v.currentSrc || v.src || "").startsWith("blob:"))) {
    told = true;
    chrome.runtime.sendMessage({ type: "blob" }).catch(() => {});
  }
}

check();
// media events do not bubble, so listen in the capture phase to catch players added later
for (const name of ["loadstart", "loadedmetadata", "playing"]) document.addEventListener(name, check, true);

// ---- Alt+click on a link: the browser downloads it this once, without UDM ----
// mousedown comes before the click that starts the download, so the background knows in time
document.addEventListener("mousedown", (e) => {
  const link = e.altKey && e.target.closest && e.target.closest("a[href], area[href]");
  if (link && /^https?:/i.test(link.href)) chrome.runtime.sendMessage({ type: "skip", url: link.href }).catch(() => {});
}, true);

// ---- Download all links: the popup asks the page (top frame) for every link on it ----
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type !== "links" || window !== window.top) return;
  const seen = new Set();
  const links = [];
  for (const a of document.querySelectorAll("a[href], area[href]")) {
    if (!/^https?:/i.test(a.href) || seen.has(a.href)) continue;
    seen.add(a.href);
    links.push({ url: a.href, text: (a.textContent || a.title || "").replace(/\s+/g, " ").trim().slice(0, 200) });
  }
  reply(links);
});
