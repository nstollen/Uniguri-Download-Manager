// Clipboard watch: read the clipboard once a second; tell the background about a newly copied http(s) link.
// Runs in an offscreen page that only exists while clipboard watch is on.
const box = document.getElementById("box");
let last = null; // what the clipboard had last time; what was there before watching started is not new

setInterval(() => {
  // input: the clipboard text (paste into a hidden box; needs the clipboardRead permission)
  box.value = "";
  box.focus();
  document.execCommand("paste");
  const text = box.value.trim();
  // process: new since last time, and one http(s) link?
  if (last !== null && text !== last && /^https?:\/\/\S+$/i.test(text)) {
    chrome.runtime.sendMessage({ type: "copied", url: text }); // output: the background decides and asks
  }
  last = text;
}, 1000);
