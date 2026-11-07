// Apply a saved theme before first paint (a classic, blocking script on purpose).
// A file, not inline: the Content-Security-Policy allows scripts from 'self' only.
// Light unless the attendee picked dark or system.
// The browser bar follows the app's theme too, and the OS only on "system".
function themeColor(t) {
  document.querySelectorAll('meta[name="theme-color"]').forEach((m) => (m.content = (t === "system" ? m.media.includes("dark") : t === "dark") ? "#11110E" : "#F2EEE4"));
}
try { const t = localStorage.getItem("hw-theme") || "light"; if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; themeColor(t); } catch { document.documentElement.dataset.theme = "light"; themeColor("light"); }
