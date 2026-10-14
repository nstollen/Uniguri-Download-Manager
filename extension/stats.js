// The stats page: totals, downloads per day for the last 30 days, and top sites (from stats.json in the downloader).
const dayName = (day) => new Date(day + "T12:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
const cell = (text, className) => Object.assign(document.createElement("td"), { textContent: text, className: className || "" });

(async () => {
  document.getElementById("downloads").onclick = () => (location.href = "manager.html");

  // ---- input: the downloader's sums ----
  const s = await chrome.runtime.sendMessage({ type: "stats" });
  if (!s || !s.ok) {
    document.getElementById("offline").hidden = false;
    return;
  }

  // ---- output: the three numbers ----
  document.getElementById("total").textContent = fmtSize(s.total);
  document.getElementById("files").textContent = s.files.toLocaleString();
  document.getElementById("speed").textContent = s.files ? fmtSpeed(s.averageSpeed) : "-";

  // ---- downloads per day: a column per day, the y axis in whole files ----
  const top = Math.max(...s.perDay.map((d) => d.files));
  const step = Math.max(1, Math.ceil(top / 4)); // 4 clean steps at most
  const max = Math.max(step, Math.ceil(top / step) * step);
  const plot = document.getElementById("plot");
  const ticks = document.getElementById("ticks");
  for (let v = 0; v <= max; v += step) {
    ticks.append(Object.assign(document.createElement("span"), { textContent: v, style: `bottom: ${(100 * v) / max}%` }));
    if (v) plot.append(Object.assign(document.createElement("div"), { className: "grid", style: `bottom: ${(100 * v) / max}%` }));
  }
  const tip = document.getElementById("tip");
  for (const d of s.perDay) {
    const day = document.createElement("div");
    day.className = "day";
    day.tabIndex = 0; // the tooltip also shows on keyboard focus
    day.setAttribute("aria-label", dayName(d.day) + ": " + d.files + " files, " + fmtSize(d.bytes));
    if (d.files) day.append(Object.assign(document.createElement("div"), { className: "bar", style: `height: ${(100 * d.files) / max}%` }));
    const show = () => {
      tip.replaceChildren(Object.assign(document.createElement("b"), { textContent: dayName(d.day) }),
        d.files + (d.files === 1 ? " file, " : " files, ") + (d.files ? fmtSize(d.bytes) : "0 MB"));
      tip.hidden = false;
      const box = day.getBoundingClientRect(), area = plot.parentElement.getBoundingClientRect();
      const left = box.left - area.left + box.width / 2 - tip.offsetWidth / 2;
      tip.style.left = Math.max(0, Math.min(left, area.width - tip.offsetWidth)) + "px";
      tip.style.top = "-8px";
    };
    day.onmouseenter = day.onfocus = show;
    day.onmouseleave = day.onblur = () => (tip.hidden = true);
    plot.append(day);
  }
  // a date under the first column and every 7th after it
  document.getElementById("dates").append(...s.perDay.map((d, i) =>
    Object.assign(document.createElement("span"), { textContent: i % 7 === 0 ? dayName(d.day) : "" })));
  document.getElementById("dayRows").append(...[...s.perDay].reverse().map((d) => {
    const tr = document.createElement("tr");
    tr.append(cell(dayName(d.day)), cell(d.files, "num"), cell(d.files ? fmtSize(d.bytes) : "", "num"));
    return tr;
  }));

  // ---- top sites (hidden when the privacy setting says sites are not recorded), each with Forget ----
  document.getElementById("sites").hidden = s.sites === false;
  document.getElementById("siteRows").append(...s.topSites.map((x) => {
    const tr = document.createElement("tr");
    const forget = cell("", "forget");
    if (x.site !== "(unknown)") {
      const button = Object.assign(document.createElement("button"), { textContent: "Forget",
        title: "Delete every download, stat line, thumbnail and log line from " + x.site });
      button.onclick = async () => (await forgetSite(x.site)) && location.reload();
      forget.append(button);
    }
    tr.append(cell(x.site), cell(x.files, "num"), cell(fmtSize(x.bytes), "num"), forget);
    return tr;
  }));
  document.getElementById("sitesBox").hidden = !s.topSites.length;
  document.getElementById("empty").hidden = !!s.topSites.length;
})();
