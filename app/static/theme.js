// Apply a saved theme before first paint (a classic, blocking script on purpose).
// A file, not inline: the Content-Security-Policy allows scripts from 'self' only.
// Light unless the attendee picked dark or system.
try { const t = localStorage.getItem("hw-theme") || "light"; if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch { document.documentElement.dataset.theme = "light"; }
