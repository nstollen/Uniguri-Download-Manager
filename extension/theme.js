// The theme setting: "system" (follow Windows), "light" or "dark".
// chrome.storage.local keeps it. localStorage keeps a copy, because it can be read before the page paints:
// this file loads in <head>, so the page never shows the wrong theme first.
const THEMES = ["system", "light", "dark"];
const themeListeners = [];

// "light" and "dark" set data-theme on <html>, which theme.css follows. "system" removes it.
function applyTheme(theme) {
  if (!THEMES.includes(theme)) theme = "system";
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("theme", theme);
  } catch {}
  for (const fn of themeListeners) fn(theme);
}

function currentTheme() {
  return document.documentElement.dataset.theme || "system";
}

// Save a new theme. Every open extension page hears it through chrome.storage.onChanged.
function setTheme(theme) {
  applyTheme(theme);
  return chrome.storage.local.set({ theme });
}

// fn(theme) runs now, and again each time the theme changes, here or in another tab
function onThemeChange(fn) {
  themeListeners.push(fn);
  fn(currentTheme());
}

try {
  applyTheme(localStorage.getItem("theme"));
} catch {}
chrome.storage.local.get("theme").then((stored) => applyTheme(stored.theme));
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.theme) applyTheme(changes.theme.newValue);
});
