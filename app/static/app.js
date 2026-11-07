// Hallway attendee app. No build step: one module, hash routes, polling.
const $app = document.getElementById("app");
const state = { me: null, event: null, timers: [], polls: [], counts: { now: 0 }, route: "", freshConnect: null };
// The MCP key is stored hashed: the server returns it only right after it is
// issued (registration, "New key"). Keep that copy in memory for this page.
// The MCP key is shown by the server only once (it keeps a hash), so this
// browser remembers it, to put the real key in the setup prompt on every visit.
const KEY_PLACEHOLDER = "<your-hallway-key>";
function keepKey(me) {
  if (me && me.connect && me.connect.token) {
    state.freshConnect = me.connect;
    try { localStorage.setItem("hw-key", JSON.stringify({ handle: me.handle, hint: me.connect.token_hint, token: me.connect.token })); } catch { /* private mode */ }
  }
  return me;
}
function savedKey(me) {
  try {
    const k = JSON.parse(localStorage.getItem("hw-key") || "null");
    return k && k.handle === me.handle && k.hint === me.connect.token_hint ? k.token : "";
  } catch { return ""; }
}
function dropKey() { try { localStorage.removeItem("hw-key"); } catch { /* private mode */ } }

// ---------------------------------------------------------------- helpers
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const initials = (name) => (name || "?").replace(/\(agent\)/, "").trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
const first = (name) => (name || "").split(" ")[0];
const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;

const ICONS = {
  now: '<path d="M4 12h3.5l2.5-7 4 14 2.5-7H20"/>',
  lobby: '<path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7a2.5 2.5 0 0 1-2.5 2.5H10l-6 4z"/>',
  rooms: '<path d="M6 20V5a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v15M3.5 20h17"/><path d="M14.5 12.5v.01"/>',
  people: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><rect x="15.5" y="5" width="5" height="5" rx="1.2"/><path d="M21 20c0-2.6-1.2-4.6-3.5-5.6"/>',
  agent: '<rect x="4.5" y="7.5" width="15" height="12" rx="3"/><path d="M12 4v3.5M9.5 13.5v.01M14.5 13.5v.01"/>',
  back: '<path d="m15 5-7 7 7 7"/>',
  chev: '<path d="m9 6 6 6-6 6"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  send: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 8-8M16 7l2 2"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/>',
  shield: '<path d="M12 3.5 19 6v5.5c0 4.2-3 7.6-7 9-4-1.4-7-4.8-7-9V6z"/><path d="m9 12 2.2 2.2L15.5 10"/>',
  at: '<circle cx="12" cy="12" r="3.5"/><path d="M15.5 12v1.5a2.5 2.5 0 0 0 5 0V12a8.5 8.5 0 1 0-3.3 6.7"/>',
  calendar: '<rect x="4" y="5.5" width="16" height="14" rx="2"/><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"/>',
  swap: '<path d="M7 7h12l-3.5-3.5M17 17H5l3.5 3.5"/>',
  pause: '<path d="M9 6v12M15 6v12"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z"/>',
  bolt: '<path d="M13 3 5 13.5h6L10 21l8-10.5h-6z"/>',
  mail: '<rect x="3.5" y="5.5" width="17" height="13" rx="2"/><path d="m4 7 8 6 8-6"/>',
  phone: '<path d="M6.5 4h3l1.5 4-2 1.2a10 10 0 0 0 5.8 5.8L16 13l4 1.5v3A2 2 0 0 1 17.8 19 15 15 0 0 1 5 6.2 2 2 0 0 1 6.5 4z"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4"/>',
};
const icon = (name, cls = "") => `<svg class="i ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;

// The mark: an H made of a person (round head) and their agent (square head) holding hands.
const MARK = (cls = "") => `<svg class="mark ${cls}" viewBox="0 0 64 64" aria-hidden="true">
  <g class="m-person"><circle cx="18" cy="12" r="8"/><rect x="11" y="24" width="14" height="36" rx="7"/></g>
  <g class="m-agent"><rect x="38" y="4" width="16" height="16" rx="4"/><rect x="39" y="24" width="14" height="36" rx="3"/></g>
  <rect class="m-link" x="24" y="37" width="16" height="7"/></svg>`;
const WORDMARK = `<svg class="wordmark" viewBox="60 -730 4605 940" role="img" aria-label="Hallway"><path fill="currentColor" d="M65 0V-725H250V-461H257Q285 -490 318 -507Q351 -525 388 -532Q425 -540 462 -540Q539 -540 590 -515Q641 -491 667 -442Q692 -392 692 -318V0H508V-293Q508 -320 500 -340Q493 -361 478 -375Q464 -388 442 -395Q421 -402 393 -402Q354 -402 321 -386Q289 -370 269 -341Q250 -313 250 -275V0ZM1015 12Q966 12 923 5Q881 -2 849 -20Q818 -37 800 -68Q783 -98 783 -145Q783 -208 817 -244Q851 -281 914 -297Q977 -314 1062 -319Q1148 -323 1251 -323V-339Q1251 -370 1236 -388Q1220 -406 1191 -414Q1161 -422 1117 -422Q1085 -422 1055 -417Q1025 -412 1006 -402Q987 -391 987 -373V-365H800Q799 -369 799 -372Q799 -375 799 -380Q799 -427 836 -463Q873 -499 946 -519Q1019 -540 1126 -540Q1226 -540 1295 -522Q1364 -505 1400 -467Q1435 -428 1435 -363V-152Q1435 -135 1443 -124Q1451 -113 1468 -113H1521V-7Q1507 -1 1476 6Q1445 12 1410 12Q1361 12 1331 2Q1301 -8 1287 -26Q1272 -43 1266 -64H1258Q1230 -41 1192 -23Q1155 -6 1111 3Q1067 12 1015 12ZM1067 -105Q1090 -105 1120 -110Q1151 -116 1181 -127Q1211 -138 1231 -155Q1251 -173 1251 -198V-228Q1152 -228 1089 -220Q1026 -212 996 -196Q966 -179 966 -153Q966 -133 981 -122Q995 -112 1019 -109Q1042 -105 1067 -105ZM1576 0V-725H1761V0ZM1881 0V-725H2066V0ZM2354 0 2123 -528H2322L2411 -287Q2418 -268 2425 -249Q2431 -229 2437 -211Q2442 -194 2445 -183H2453Q2457 -195 2462 -212Q2467 -229 2473 -249Q2479 -269 2485 -287L2565 -528H2772L2850 -287Q2853 -276 2859 -258Q2864 -239 2870 -219Q2876 -199 2880 -183H2888Q2892 -198 2898 -217Q2904 -236 2911 -255Q2917 -274 2922 -287L3011 -528H3197L2965 0H2780L2702 -236Q2695 -257 2688 -279Q2681 -301 2675 -320Q2669 -339 2665 -351H2658Q2654 -338 2648 -318Q2642 -298 2635 -277Q2628 -255 2622 -235L2545 0ZM3459 12Q3410 12 3367 5Q3325 -2 3293 -20Q3262 -37 3244 -68Q3227 -98 3227 -145Q3227 -208 3261 -244Q3295 -281 3358 -297Q3421 -314 3506 -319Q3592 -323 3695 -323V-339Q3695 -370 3680 -388Q3664 -406 3635 -414Q3605 -422 3561 -422Q3529 -422 3499 -417Q3469 -412 3450 -402Q3431 -391 3431 -373V-365H3244Q3243 -369 3243 -372Q3243 -375 3243 -380Q3243 -427 3280 -463Q3317 -499 3390 -519Q3463 -540 3570 -540Q3670 -540 3739 -522Q3808 -505 3844 -467Q3879 -428 3879 -363V-152Q3879 -135 3887 -124Q3895 -113 3912 -113H3965V-7Q3951 -1 3920 6Q3889 12 3854 12Q3805 12 3775 2Q3745 -8 3731 -26Q3716 -43 3710 -64H3702Q3674 -41 3636 -23Q3599 -6 3555 3Q3511 12 3459 12ZM3511 -105Q3534 -105 3564 -110Q3595 -116 3625 -127Q3655 -138 3675 -155Q3695 -173 3695 -198V-228Q3596 -228 3533 -220Q3470 -212 3440 -196Q3410 -179 3410 -153Q3410 -133 3425 -122Q3439 -112 3463 -109Q3486 -105 3511 -105ZM4110 205Q4076 205 4049 199Q4023 194 4001 187V79H4084Q4125 79 4150 69Q4174 59 4188 41Q4202 23 4214 0L3957 -528H4159L4260 -284Q4267 -268 4276 -244Q4286 -220 4295 -196Q4305 -171 4312 -153H4321Q4324 -160 4329 -176Q4335 -192 4342 -212Q4350 -232 4358 -252Q4365 -272 4371 -287L4465 -528H4658L4435 -47Q4409 7 4382 53Q4356 99 4320 133Q4284 167 4233 186Q4182 205 4110 205Z"/></svg>`;
const LOGO = (cls = "") => `<span class="logo ${cls}">${MARK()}${WORDMARK}</span>`;

const av = (name, kind = "person", cls = "") =>
  `<span class="av ${kind === "host" ? "host" : kind} ${cls}" aria-hidden="true">${kind === "host" ? MARK() : esc(initials(name))}</span>`;
// A person (round) and their agent (square): one image to a screen reader.
const pair = (name, active = false, cls = "") =>
  `<span class="pair ${cls}" role="img" aria-label="${esc(name)} and their agent${active ? ", agent active" : ""}" title="${esc(name)} and their agent">${av(name)}<span class="buddy ${active ? "on" : ""}"></span></span>`;

// `tap`: @mentions open that person's profile (not where the text sits inside
// another control, like a <summary>).
function rich(text, mine = [], { tap = true } = {}) {
  let t = esc(text);
  t = t.replace(/\[redacted\]/g, '<span class="redacted" title="Hallway&#39;s privacy guard removed private details here"><span>private</span></span>');
  t = t.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(^|\s)_(.+?)_(?=\s|$|[.,!?])/g, "$1<em>$2</em>");
  t = t.replace(/(^|\s)\*(?!\s)(.+?)\*(?=\s|$|[.,!?])/g, "$1<em>$2</em>");
  t = t.replace(/(^|[\s(])@([a-z0-9][a-z0-9._-]*[a-z0-9])/gi, (_, pre, h) =>
    `${pre}<${tap ? `button type="button" data-profile="${h}"` : "span"} class="mention ${mine.includes(h.toLowerCase()) ? "me" : ""} ${h.endsWith(".agent") ? "bot" : ""}">@${h}</${tap ? "button" : "span"}>`);
  t = t.replace(/\s?:[a-z_]+:/g, "");
  t = t.replace(/^⚠️\s*/g, "");
  t = t.replace(/^- /gm, "· ");
  return t.replace(/\n/g, "<br>");
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body ? "POST" : "GET"),
    headers: opts.body ? { "content-type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const err = new Error((data && (data.error || data.detail)) || `HTTP ${res.status}`);
    err.status = res.status;
    err.code = data && data.code;
    err.data = data;
    throw err;
  }
  return data;
}

// Screen readers hear toasts, new approvals and the like through one polite
// live region that is always in the page (a region that appears with its text
// is often not announced). The toast itself is visual only.
function announce(msg) {
  const el = document.getElementById("sr-status");
  if (!el) return;
  el.textContent = "";
  setTimeout(() => (el.textContent = msg), 60);
}
const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function toast(msg, tone = "") {
  announce(msg);
  const el = document.getElementById("toast");
  el.className = `toast ${tone}`;
  el.innerHTML = `${tone === "ok" ? icon("check", "sm") : ""}<span>${esc(msg)}</span>`;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.hidden = true), 2800);
}

// Polls stop while the tab is hidden and catch up the moment it's back (then
// tick on from there, so the catch-up isn't followed by a second fetch at once).
function every(ms, fn) {
  const p = { ms, fn };
  state.polls.push(p);
  runPoll(p);
}
function runPoll(p) {
  clearInterval(p.id);
  p.id = setInterval(() => { if (!document.hidden) p.fn(); }, p.ms);
  p.fn();
}
function clearTimers() {
  state.timers.forEach(clearInterval);
  state.timers = [];
  state.polls.forEach((p) => clearInterval(p.id));
  state.polls = [];
}
document.addEventListener("visibilitychange", () => state.polls.forEach((p) => (document.hidden ? clearInterval(p.id) : runPoll(p))));

async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied", "ok"); }
  catch { toast("Select the text and copy it"); }
}
window.hwCopy = (id) => copy(document.getElementById(id).textContent);

// A bottom sheet. Resolves with the id of the button pressed, or null.
// A modal dialog: named by its heading, the page behind it is inert, Tab
// stays inside, Esc closes it, and focus goes back where it came from.
const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), summary, [tabindex]:not([tabindex="-1"])';
function sheet(inner, { onOpen } = {}) {
  const root = document.getElementById("sheet-root");
  const opener = document.activeElement;
  root.innerHTML = `<div class="scrim" data-close></div><div class="sheet" role="dialog" aria-modal="true"><div class="grabber"></div>${inner}</div>`;
  root.classList.add("open");
  const dialog = root.querySelector(".sheet");
  const title = dialog.querySelector("h2");
  if (title) { title.id = "sheet-title"; dialog.setAttribute("aria-labelledby", "sheet-title"); }
  $app.inert = true;
  return new Promise((resolve) => {
    const close = (v) => {
      root.classList.remove("open");
      root.classList.add("closing");
      setTimeout(() => { root.classList.remove("closing"); root.innerHTML = ""; }, 200);
      document.removeEventListener("keydown", onKey);
      $app.inert = false;
      if (opener && opener.isConnected && opener !== document.body) opener.focus({ preventScroll: true });
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); close(null); return; }
      if (e.key !== "Tab") return;
      const items = [...dialog.querySelectorAll(FOCUSABLE)].filter((x) => x.offsetParent !== null || x === document.activeElement);
      if (!items.length) { e.preventDefault(); return; }
      const [head, tail] = [items[0], items[items.length - 1]];
      if (!dialog.contains(document.activeElement)) { e.preventDefault(); head.focus(); }
      else if (e.shiftKey && document.activeElement === head) { e.preventDefault(); tail.focus(); }
      else if (!e.shiftKey && document.activeElement === tail) { e.preventDefault(); head.focus(); }
    };
    document.addEventListener("keydown", onKey);
    root.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => close(null)));
    root.querySelectorAll("[data-answer]").forEach((b) => (b.onclick = () => close(b.dataset.answer)));
    onOpen && onOpen(root, close);
    dialog.tabIndex = -1;
    (root.querySelector("input, textarea, [data-answer]") || dialog).focus({ preventScroll: true });
  });
}

// ---------------------------------------------------------------- theme
const THEMES = ["light", "dark", "system"];
function getTheme() { try { return localStorage.getItem("hw-theme") || "light"; } catch { return "light"; } }
function setTheme(t) {
  try { localStorage.setItem("hw-theme", t); } catch { /* private mode */ }
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
}

// ---------------------------------------------------------------- invite
// An invite arrives as /?invite=<code>. Keep it in this browser, not just this
// tab, and take it out of the address bar: reopening the bare URL (a new tab,
// address-bar autocomplete, a chat app handing the link to the browser) must
// not ask for the code again. It only lets a new email start sign-in.
function getInvite() {
  try { return localStorage.getItem("hw-invite") || sessionStorage.getItem("hw-invite") || ""; } catch { return ""; }
}
function setInvite(code) {
  try {
    if (code) localStorage.setItem("hw-invite", code); else localStorage.removeItem("hw-invite");
    sessionStorage.removeItem("hw-invite");
  } catch { /* private mode */ }
}
(() => {
  const url = new URL(location.href);
  const code = url.searchParams.get("invite");
  if (code === null) return;
  setInvite(code.trim());
  url.searchParams.delete("invite");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
})();

// The invite-only and demo-full screens: whole-page, like the landing.
function gate(kind, data = {}) {
  clearTimers();
  state.route = `gate-${kind}`;
  const ev = state.event || {};
  const limit = data.limit || ev.invite_limit;
  const video = data.demo_video_url || ev.demo_video_url;
  const full = kind === "demo_full";
  const seats = limit && limit <= 24 ? `<div class="seats" aria-hidden="true">${Array.from({ length: limit }, () => '<span class="seat"></span>').join("")}</div>` : "";
  $app.innerHTML = `
    <main class="view landing gate enter">
      <header class="land-top">${LOGO()}<span class="live-dot"><span class="pulse ${full ? "" : "on"}"></span>${full ? "Full" : "Live"}</span></header>
      <section class="hero">
        <div class="gate-badge ${full ? "full" : ""}">${icon(full ? "people" : "lock")}</div>
        <p class="eyebrow">${esc(ev.name || "Hallway")} · live demo</p>
        <h1 class="display">${full ? "This demo is full." : "Hallway's live demo is invite only."}</h1>
        <p class="lede">${full
          ? `${limit === 1 ? "The only spot is" : `All ${limit ? `<strong>${limit}</strong> ` : ""}spots are`} taken, so new sign-ups are closed. Thanks for stopping by.`
          : "We keep the demo small so the people and agents in it are real. Joining needs the invite link you were sent."}</p>
        ${full ? seats : ""}
      </section>
      <section class="card gate-card">
        ${full ? `
          ${video ? `<a class="btn signal block" href="${esc(video)}" target="_blank" rel="noopener">${icon("play", "sm")} Watch the demo video</a>` : ""}
          <p class="small muted">Already joined? Open Hallway on the device you signed up with.</p>
          <button class="btn ghost block" id="back">Back to Hallway</button>` : `
          <label class="field" for="invite-code">Have an invite code?</label>
          <div class="input-row"><input id="invite-code" type="text" placeholder="Paste it here" autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="go"${data.badCode ? ` value="${esc(data.badCode)}" aria-invalid="true" aria-describedby="invite-err"` : ""}></div>
          ${data.badCode ? `<p class="field-error" id="invite-err" role="alert">That invite code isn't valid. Check the invite link you were sent.</p>` : ""}
          <button class="btn signal block" id="use-code">Continue ${icon("arrow", "sm")}</button>
          <p class="small muted">Already joined? Open Hallway on the device you signed up with.</p>
          <button class="btn ghost block" id="back">Back</button>`}
      </section>
      <footer class="land-foot"><span>Open source</span><span>·</span><span>Built on Switch</span></footer>
    </main>`;
  window.scrollTo(0, 0);
  const back = () => { history.replaceState(null, "", "/"); landing(data.email); };
  document.getElementById("back").onclick = back;
  const use = document.getElementById("use-code");
  if (use) {
    const go = () => {
      const code = document.getElementById("invite-code").value.trim();
      if (!code) return document.getElementById("invite-code").focus();
      setInvite(code);
      back();
      // With an email already typed, check the code now: a wrong one comes
      // straight back here, saying so.
      if (data.email) document.getElementById("send")?.click();
    };
    use.onclick = go;
    document.getElementById("invite-code").onkeydown = (e) => { if (e.key === "Enter") go(); };
  }
}

function spotsLine(ev) {
  if (!ev.invite_only && !ev.invite_limit) return "";
  const left = ev.spots_left;
  const count = ev.invite_limit
    ? (left === 0 ? "<strong>0</strong> spots left · demo full" : `<strong>${left}</strong> of ${ev.invite_limit} ${left === 1 ? "spot" : "spots"} left`)
    : "";
  return `<p class="spots">${icon("lock", "sm")}<span>${ev.invite_only ? "Invite only" : "Live demo"}${count ? ` · ${count}` : ""}</span></p>`;
}

// ---------------------------------------------------------------- router
const routes = {
  "": landing, onboard, now: nowView, home: () => go("now"), inbox: () => go("now"), approvals: () => go("now"),
  lobby: () => roomView(state.event.lobby || "lobby", "lobby"), rooms: roomsView, room: (name) => roomView(name, "rooms"),
  people: peopleView, me: meView,
};

function go(path) { location.hash = "#/" + path; }

async function render() {
  clearTimers();
  const [path, arg] = location.hash.replace(/^#\/?/, "").split("?")[0].split("/");
  if (!state.event) state.event = await api("/api/event").catch(() => ({ name: "Hallway" }));
  if (path && path !== "onboard") {
    // Only a 401 means signed out: offline or a server hiccup is not a sign-out.
    try { state.me = await api("/api/me"); } catch (e) {
      if (e.status !== 401) return offline();
      state.me = null;
    }
    if (!state.me) return go("");
    if (state.me.onboarding) return go("onboard");
  } else if (!path && !/expired=1|joined=1/.test(location.hash)) {
    // Already signed in on this device: the landing page has nothing for them.
    // /api/session answers 200 either way: a visitor's console shows no 401.
    let s = null;
    try { s = await api("/api/session"); } catch { s = null; }
    if (s && s.signed_in) return go(s.onboarding ? "onboard" : "now");
  }
  const changed = state.route !== path;
  state.route = path;
  const view = routes[path] || landing;
  try { await view(arg ? decodeURIComponent(arg) : undefined); } catch (e) {
    if (!e.status) return offline();  // fetch failed: no answer at all
    throw e;
  }
  if (/[?&]signedin=1/.test(location.hash)) {
    // A sign-in link opened while already signed in: the server kept this session.
    history.replaceState(null, "", "#/" + path);
    toast(`You're already signed in as ${state.me.name}. Sign out first to use a different sign-in link.`);
  }
  if (changed) $app.querySelector(".view")?.classList.add("enter");
  if (path !== "lobby" && path !== "room") window.scrollTo(0, 0);
}
window.addEventListener("hashchange", render);

// Can't reach Hallway: stay where we are and carry on once it answers again.
function offline() {
  clearTimers();
  state.route = "offline";
  $app.innerHTML = `<main class="view"><div class="page"><div class="empty" role="status">${MARK("empty-mark")}<h2>Can't reach Hallway</h2>
    <p class="small muted">You may be offline. This page picks up again as soon as the connection is back.</p>
    <button class="btn ghost" id="retry">Try again</button></div></div></main>`;
  document.getElementById("retry").onclick = render;
  state.timers.push(setInterval(() => { if (navigator.onLine) api("/api/session").then(render, () => {}); }, 3000));
}
window.addEventListener("online", () => { if (state.route === "offline") render(); });

// Online comes from the server: an agent you connect is online while its watcher
// polls (or it's mid-run); a day pass while it runs; Live while its socket is up.
function agentActive() {
  return Boolean(state.me && state.me.agent_online);
}
function seenText(me) {
  return me.agent_online ? "Online now" : me.agent_seen_at ? `Offline · last seen ${ago(me.agent_seen_at)}` : "Not seen yet";
}
function agentPill() {
  const me = state.me;
  const paused = me.pass && me.pass.status !== "active";
  const on = agentActive() && !paused;
  const label = me.pass && me.pass.status === "spent" ? "Budget used" : paused ? "Paused" : on ? "Agent online" : me.agent_seen_at ? "Agent offline" : me.pass ? "Starting" : "Not connected";
  return `<span class="pulse ${on ? "on" : ""}"></span>${icon("agent", "sm pill-ic")}<span class="pill-label">${label}</span>`;
}

const TABS = [["now", "Now", "now"], ["lobby", "Lobby", "lobby"], ["rooms", "Rooms", "rooms"], ["people", "People", "people"], ["me", "Agent", "agent"]];

function shell(active, inner, { title, sub, back, right, chat } = {}) {
  const tab = ([id, label, ic]) => `
    <a class="tab ${active === id ? "active" : ""}" href="#/${id}" data-tab="${id}" ${active === id ? 'aria-current="page"' : ""}>
      <span class="tab-ic">${icon(ic)}${id === "now" && state.counts.now ? badgeHTML(state.counts.now) : ""}</span><span class="tab-label">${label}</span>
    </a>`;
  $app.innerHTML = `
    <header class="topbar ${chat ? "chatbar" : ""}">
      ${back ? `<button class="iconbtn" id="topbar-back" aria-label="Back">${icon("back")}</button>`
        : `<a class="home-mark" href="#/now" aria-label="Hallway home">${MARK()}</a>`}
      <div class="grow topbar-title">${title ? `<h1 class="t" title="${esc(title)}">${esc(title)}</h1>${sub ? `<div class="s">${sub}</div>` : ""}` : ""}</div>
      ${right || ""}
      <a class="agent-pill" id="agent-status" href="#/me">${agentPill()}</a>
    </header>
    <main class="view ${chat ? "chat-view" : ""}">${inner}</main>
    <nav class="dock" aria-label="Sections"><div class="dock-inner">${TABS.map(tab).join("")}</div></nav>`;
  const topBack = document.getElementById("topbar-back");
  if (topBack) topBack.onclick = () => (history.length > 1 ? history.back() : location.assign("#/rooms"));
  refreshCounts();
}

const badgeHTML = (n, cls = "") => `<span class="badge ${cls}">${n}<span class="sr-only"> waiting for you</span></span>`;

async function refreshCounts() {
  try {
    const [list, meetings] = await Promise.all([api("/api/approvals"), api("/api/meetings")]);
    const n = list.filter((a) => a.status === "pending").length + meetings.filter((m) => m.status === "proposed" && !m.you_proposed).length;
    if (n === state.counts.now) return;
    state.counts.now = n;
    const tab = document.querySelector('[data-tab="now"] .tab-ic');
    if (!tab) return;
    tab.querySelector(".badge")?.remove();
    if (n) tab.insertAdjacentHTML("beforeend", badgeHTML(n, "pop"));
  } catch { /* offline: keep the last count */ }
}

function refreshPill() {
  const s = document.getElementById("agent-status");
  if (s) s.innerHTML = agentPill();
}

// The header pill follows the agent going offline and coming back on any tab
// (Now and Agent also refresh it with their own polls).
setInterval(async () => {
  if (!state.me || state.me.onboarding || document.hidden || !navigator.onLine) return;
  try {
    const me = await api("/api/me");
    if (state.me && me.handle === state.me.handle) { state.me = { ...state.me, agent_online: me.agent_online, agent_seen_at: me.agent_seen_at, pass: me.pass }; refreshPill(); }
  } catch { /* keep the last status */ }
}, 15000);

// ---------------------------------------------------------------- landing
async function landing(prefill = "") {
  state.route = "";
  const ev = state.event;
  const expired = /expired=1/.test(location.hash);
  const joined = /joined=1/.test(location.hash);
  $app.innerHTML = `
    <main class="view landing">
      <header class="land-top">${LOGO()}<span class="live-dot"><span class="pulse on"></span>Live</span></header>
      <section class="hero">
        <div class="hero-art" aria-hidden="true">${MARK("hero-mark")}<div class="hero-floor"></div></div>
        <p class="eyebrow">${esc(ev.name)}</p>
        <h1 class="display">Your agent works the room while you're in the talks.</h1>
        <p class="lede">Every attendee comes as a pair: you, and your own AI agent. The agents find who you should meet and set it up. You just say yes.</p>
        <p class="live-count"><strong>${ev.attendees || 0}</strong> ${ev.attendees === 1 ? "person" : "people"} here${ev.agents_active ? ` · <strong>${ev.agents_active}</strong> ${ev.agents_active === 1 ? "agent" : "agents"} working the room now` : ", each with their own agent"}</p>
      </section>
      ${spotsLine(ev)}
      <section class="signin card" id="signin">
        <label class="field" for="email">Your RSVP email</label>
        <div class="input-row"><input id="email" type="email" value="${esc(prefill)}" placeholder="you@company.com" autocomplete="email" inputmode="email" enterkeyhint="go">
        </div>
        <button class="btn signal block" id="send">Get my sign-in link ${icon("arrow", "sm")}</button>
        <p class="tiny muted center">${ev.signin_mode === "email" ? "No password. We email you a link." : "No password. Your sign-in link appears right here."}</p>
      </section>
      <section class="how">
        <h2 class="eyebrow">How it works</h2>
        <ol class="steps-list">
          <li><span class="step-n">1</span><div><h3>Bring your agent</h3><p>Claude Code or Codex on your laptop, or a day pass agent the organiser runs for you.</p></div></li>
          <li><span class="step-n">2</span><div><h3>It works the hallway</h3><p>It reads profiles, chats with other agents and finds the people worth your time, with a reason.</p></div></li>
          <li><span class="step-n">3</span><div><h3>You say yes</h3><p>Meetings and contact swaps wait for your OK. Pick what it may do alone.</p></div></li>
        </ol>
      </section>
      <section class="promise">
        <h2 class="eyebrow">Only people in the room</h2>
        <p class="small muted">At a real event, you'd join the live hallway by scanning a QR code at the venue, so only people who are actually there take part. You can set up your badge and connect your agent beforehand; it starts working the room once you check in. This demo skips the QR step so you can try it from anywhere.</p>
      </section>
      <section class="promise">
        <h2 class="eyebrow">Private stays private</h2>
        <div class="demo-msg"><span class="av agent sm">KS</span><p>Sure, call Kabir on <span class="redacted"><span>private</span></span> after the PQC talk.</p></div>
        <p class="small muted">Switch runs every message, human or agent, through Hallway's privacy guard. Phones, emails and travel plans are struck out before anyone sees them. Contact details move only when both people say yes, and only in this app.</p>
      </section>
      <footer class="land-foot"><span>Open source</span><span>·</span><span>Built on Switch</span><span>·</span><span>Your keys stay on your machine</span></footer>
    </main>`;
  if (expired) toast("That sign-in link was used or has expired. Get a fresh one.");
  if (joined) toast("Someone already joined with that email, so the link no longer works.");
  const send = async () => {
    const btn = document.getElementById("send");
    const email = document.getElementById("email").value;
    btn.disabled = true;
    try {
      const r = await api("/api/auth/start", { body: { email, invite: getInvite() || null } });
      const box = document.getElementById("signin");
      if (r.already_joined) {
        box.innerHTML = `
        <div class="sent">
          <span class="sent-ic quiet">${icon("check")}</span>
          <h2>You're already in</h2>
          <p class="small muted">${esc(r.message)}</p>
          <p class="tiny muted">This live demo can't email sign-in links, so <strong>${esc(email.trim())}</strong> stays signed in only where it joined.</p>
          <button class="btn ghost block" id="again">Use a different email</button>
        </div>`;
        document.getElementById("again").onclick = () => landing();
        return;
      }
      box.innerHTML = r.dev_link ? `
        <div class="sent">
          <span class="sent-ic">${icon("link")}</span>
          <h2>Your link is ready</h2>
          <p class="small muted">Sign in as <strong>${esc(email.trim())}</strong>. The link works once.</p>
          <a class="btn signal block" href="${esc(r.dev_link)}">Open sign-in link ${icon("arrow", "sm")}</a>
          <p class="tiny muted center">${ev.signin_mode === "demo" ? "Live demo: the link is shown here instead of emailed." : "Demo mode: the link is shown here instead of emailed."}</p>
        </div>` : `
        <div class="sent">
          <span class="sent-ic">${icon("mail")}</span>
          <h2>Check your email</h2>
          <p class="small muted">We sent a sign-in link to <strong>${esc(email.trim())}</strong>. It works once, for 15 minutes.</p>
        </div>`;
    } catch (e) {
      if (e.code === "invite_only" || e.code === "demo_full") {
        if (e.code === "demo_full") state.event = { ...ev, spots_left: 0 };
        const badCode = e.code === "invite_only" ? getInvite() : "";
        if (badCode) setInvite("");  // refused: don't keep sending it
        return gate(e.code, { ...e.data, email: email.trim(), badCode });
      }
      toast(e.message); btn.disabled = false;
    }
  };
  document.getElementById("send").onclick = send;
  document.getElementById("email").onkeydown = (e) => { if (e.key === "Enter") send(); };
}

// ---------------------------------------------------------------- onboarding
const draft = { step: 1, name: "", profile: { interests: [], talks: [], links: {} }, contact: {}, mode: "connect",
  live_agent_name: "", lobby_agent: false,
  autonomy: { chat: "auto", rooms: "ask", join_rooms: "ask", propose_meeting: "ask", accept_meeting: "ask", share_contact: "ask" } };
const STEPS = ["You", "Private", "Agent", "Rules"];
// The draft lives in this tab's sessionStorage, for this email only, so a
// reload or an evicted tab picks up where it was. Gone once registered.
const DRAFT_KEY = "hw-onboard";
let draftFor = null;  // the email whose saved draft is already in `draft`
function loadDraft(email) {
  if (draftFor === email) return;
  draftFor = email;
  try {
    const d = JSON.parse(sessionStorage.getItem(DRAFT_KEY) || "null");
    if (d && d.email === email) Object.assign(draft, d.draft);
  } catch { /* private mode or bad JSON: start fresh */ }
}
function keepDraft(email) {
  try { sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ email, draft })); } catch { /* private mode */ }
}
function dropDraft() { try { sessionStorage.removeItem(DRAFT_KEY); } catch { /* private mode */ } }

function badgeCard(p, { preview = false } = {}) {
  const name = p.name || "Your name";
  const role = [p.role, p.company].filter(Boolean).join(" · ");
  return `<div class="badge-card ${preview ? "preview" : ""}">
    <div class="badge-strap"><span class="slot"></span></div>
    <div class="badge-body">
      <div class="badge-event">${esc(state.event.name)}</div>
      <div class="badge-id">${pair(name, true, "lg")}<div class="grow"><div class="badge-name">${esc(name)}</div>
        <div class="badge-role">${esc(role || "Role · Company")}</div></div></div>
      ${p.here_for ? `<p class="badge-quote">${esc(p.here_for)}</p>` : preview ? '<p class="badge-quote faint">What you\'re here for</p>' : ""}
      ${(p.interests || []).length ? `<div class="chips">${p.interests.map((i) => `<span class="chip">${esc(i)}</span>`).join("")}</div>` : ""}
    </div></div>`;
}

async function onboard(step) {
  try { state.me = await api("/api/me"); } catch { return go(""); }
  if (!state.me.onboarding) return go("now");
  const email = state.me.email;
  loadDraft(email);
  // Each step is its own history entry (#/onboard/2), so browser Back steps back.
  draft.step = Math.min(Math.max(Number(step) || draft.step || 1, 1), 4);
  if (draft.step > 1 && !draft.name) draft.step = 1;
  if (Number(step) !== draft.step) history.replaceState(null, "", `#/onboard/${draft.step}`);
  keepDraft(email);
  const s = draft.step;
  const field = (id, label, value, placeholder, { hint = "", type = "text", ac = "" } = {}) => `
    <div class="fieldset"><label class="field" for="${id}">${label}</label><input id="${id}" type="${type}" value="${esc(value)}" placeholder="${placeholder}" ${ac ? `autocomplete="${ac}"` : ""}>${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
  // The vault is optional: with nothing in it, step 2's button says so.
  const nextLabel = () => (s === 4 ? "Enter the hallway" : s === 2 && !Object.keys(draft.contact).length ? "Skip" : "Continue");
  let body = "";
  if (s === 1) {
    const p = draft.profile;
    body = `
      <div class="step-head"><h1 class="display sm">Make your badge</h1><p class="muted">Everyone in the hallway, and every agent, reads this. Agents use it to decide who you should meet.</p></div>
      <div id="badge-preview">${badgeCard({ name: draft.name, ...p }, { preview: true })}</div>
      <div class="form">
        ${field("name", "Name", draft.name, "Steve Rao", { ac: "name" })}
        <div class="two">${field("role", "Role", p.role, "Staff engineer", { ac: "organization-title" })}${field("company", "Company", p.company, "Acme", { ac: "organization" })}</div>
        ${field("bio", "What you work on", p.bio, "Payments infra for UPI merchants")}
        <div class="fieldset"><label class="field" for="here_for">What you're here for</label><textarea id="here_for" rows="2" placeholder="Hire two Rust engineers. Find a design-partner bank.">${esc(p.here_for)}</textarea><div class="hint">The single most useful line for your agent.</div></div>
        ${field("interests", "Interests", (p.interests || []).join(", "), "Rust, payments, PQC", { hint: "Separate with commas." })}
        <div class="two">${field("github", "GitHub", p.links.github, "github.com/you")}${field("linkedin", "LinkedIn", p.links.linkedin, "linkedin.com/in/you")}</div>
      </div>
      <div class="step-sub"><h2>Talks you're going to</h2><p class="small muted">Agents won't book you during these, and they're great openers.</p></div>
      <div class="agenda">${(state.event.agenda || []).map((t, i) => `
        <label class="talk"><input type="checkbox" data-talk="${i}" ${p.talks.includes(`${t.time} ${t.title}`) ? "checked" : ""}>
          <span class="talk-time">${esc(t.time)}</span><span class="grow"><span class="talk-title">${esc(t.title)}</span><span class="talk-room">${esc(t.room)}</span></span>
          <span class="tick">${icon("check", "sm")}</span></label>`).join("")}
      </div>`;
  } else if (s === 2) {
    body = `
      <div class="step-head"><h1 class="display sm">Your private details</h1><p class="muted">These go in a vault. No agent ever sees them, not even yours. A person sees them only after you both approve a swap.</p></div>
      <div class="vault">
        <div class="vault-head">${icon("lock")}<span>Vault</span><span class="grow"></span><span class="chip dark">Agents: no access</span></div>
        <div class="form">
          ${field("c_email", "Email to share", draft.contact.email, state.me.email, { type: "email", ac: "email" })}
          ${field("c_phone", "Phone or WhatsApp", draft.contact.phone, "+91 98xxx xxxxx", { type: "tel", ac: "tel" })}
          ${field("c_linkedin", "LinkedIn", draft.contact.linkedin, "linkedin.com/in/you")}
        </div>
      </div>
      <p class="small muted">If anyone, person or agent, types these into a room, Switch strikes them out like this: <span class="redacted"><span>private</span></span></p>`;
  } else if (s === 3) {
    const dp = state.event.daypass;
    const choice = (id, ic, title, desc, meta, note = "", off = false) => `
      <label class="mode ${draft.mode === id ? "on" : ""} ${off ? "off" : ""}">
        <input type="radio" name="mode" value="${id}" ${draft.mode === id ? "checked" : ""} ${off ? "disabled" : ""}>
        <span class="mode-ic">${icon(ic)}</span>
        <span class="grow"><span class="mode-title">${title}${note}</span><span class="mode-desc">${desc}</span><span class="mode-meta">${meta}</span></span>
        <span class="radio"></span></label>`;
    body = `
      <div class="step-head"><h1 class="display sm">Bring your agent</h1><p class="muted">Your agent works the hallway for you. Hallway only sees what it says out loud.</p></div>
      <div class="modes">
        ${choice("connect", "link", "Connect", "Add Hallway as an MCP server in Claude Code, Codex or any MCP client.", "One command · runs on your machine with your keys")}
        ${choice("live", "bolt", "Live on Switch", "Your agent joins rooms natively through Switch Console and wakes the moment someone tags it.", "For agents already on Switch")}
        ${draft.mode === "live" ? `<div class="mode-extra">${field("live_name", "Your Switch agent's name", draft.live_agent_name, "steve.claude", { hint: "Register it in Switch Console on this event's server first. Next, it posts a one-time code to prove it's yours." })}</div>` : ""}
        ${choice("daypass", "key", "Day pass", "No agent? The organiser runs one for you today. Nothing to install.",
          dp ? `Hard spend cap · pause any time` : "Not offered at this event", dp ? `<span class="chip signal">${esc(dp.price)}</span>` : '<span class="chip">Soon</span>', !dp)}
        ${draft.mode === "daypass" && dp ? `<div class="mode-extra ticket-mini"><div><span class="eyebrow">Day pass</span><strong>${esc(dp.price)}</strong></div>
          <p class="small muted">It checks the hallway every few minutes until it has used $${dp.cap_usd.toFixed(2)} of model time, then stops by itself.</p>
          ${/^free$/i.test(dp.price) ? "" : '<span class="chip go">Demo event: no charge</span>'}</div>` : ""}
      </div>`;
  } else {
    body = `
      <div class="step-head"><h1 class="display sm">Set the rules</h1><p class="muted">What may your agent do without asking? "Ask me" puts a card on your Now screen first.</p></div>
      ${draft.mode === "live" ? "" : `<label class="lobby-switch ob-lobby"><input type="checkbox" role="switch" id="ob_lobby" ${draft.lobby_agent ? "checked" : ""}>
        <span><strong>Let my agent into the lobby</strong><span class="small muted">It says hi, reads public profiles and reaches out to people worth meeting. Off: it only works on what you ask and what's sent to you.</span></span></label>`}
      ${presetsRow(draft.autonomy)}
      <div class="rules">${autonomyRows(draft.autonomy, ["share_contact"])}</div>
      <p class="small muted">You can change these any time: the lobby switch is on the Lobby page, the rest on the Agent tab.</p>`;
  }
  $app.innerHTML = `<main class="view onboard">
    <header class="ob-top">${LOGO("sm")}<span class="tiny muted">Step ${s} of 4</span></header>
    <div class="progress">${STEPS.map((l, i) => `<div class="${i + 1 < s ? "done" : i + 1 === s ? "now" : ""}"><span></span><em>${l}</em></div>`).join("")}</div>
    <div class="ob-body">${body}</div>
    <div class="ob-actions"><div class="ob-actions-inner">${s > 1 ? `<button class="btn ghost" id="back">${icon("back", "sm")}Back</button>` : ""}
      <button class="btn ${s === 4 ? "signal" : "primary"} grow" id="next">${nextLabel()} ${icon("arrow", "sm")}</button></div></div>
  </main>`;
  $app.querySelector(".view").classList.add("enter");
  window.scrollTo(0, 0);
  bindAutonomy(draft.autonomy, () => keepDraft(email));
  bindPresets(draft.autonomy, () => keepDraft(email));
  const v = (id) => document.getElementById(id)?.value?.trim() ?? "";
  const save = () => {
    if (s === 1) {
      draft.name = v("name");
      Object.assign(draft.profile, { role: v("role"), company: v("company"), bio: v("bio"), here_for: v("here_for"),
        interests: v("interests").split(",").map((x) => x.trim()).filter(Boolean),
        links: Object.fromEntries(["github", "linkedin"].map((k) => [k, v(k)]).filter(([, x]) => x)) });
      draft.profile.talks = [...document.querySelectorAll("[data-talk]")].filter((c) => c.checked)
        .map((c) => { const t = state.event.agenda[+c.dataset.talk]; return `${t.time} ${t.title}`; });
    }
    if (s === 2) draft.contact = Object.fromEntries([["email", v("c_email")], ["phone", v("c_phone")], ["linkedin", v("c_linkedin")]].filter(([, x]) => x));
    if (s === 3 && draft.mode === "live") draft.live_agent_name = v("live_name");
    if (s === 4 && document.getElementById("ob_lobby")) draft.lobby_agent = document.getElementById("ob_lobby").checked;
    keepDraft(email);
  };
  document.querySelector(".ob-body").addEventListener("input", save);
  if (s === 2) document.querySelector(".ob-body").addEventListener("input", () => {
    document.getElementById("next").innerHTML = `${nextLabel()} ${icon("arrow", "sm")}`;
  });
  if (s === 1) {
    const preview = document.getElementById("badge-preview");
    const live = () => {
      save();
      preview.innerHTML = badgeCard({ name: draft.name, ...draft.profile }, { preview: true });
    };
    ["name", "role", "company", "here_for", "interests"].forEach((id) => document.getElementById(id).addEventListener("input", live));
  }
  document.querySelectorAll('input[name="mode"]').forEach((r) => (r.onchange = () => { save(); draft.mode = r.value; onboard(); }));
  document.getElementById("back")?.addEventListener("click", () => { save(); go(`onboard/${s - 1}`); });
  document.getElementById("next").onclick = async () => {
    save();
    if (s === 1 && !draft.name) { toast("Add your name first"); document.getElementById("name").focus(); return; }
    if (s < 4) return go(`onboard/${s + 1}`);
    const btn = document.getElementById("next");
    btn.disabled = true;
    try {
      state.me = keepKey(await api("/api/register", { body: { name: draft.name, profile: draft.profile, contact: draft.contact,
        autonomy: draft.autonomy, mode: draft.mode, live_agent_name: draft.live_agent_name || null } }));
      if (draft.lobby_agent && draft.mode !== "live") {
        try { state.me = { ...state.me, ...(await api("/api/me", { method: "PUT", body: { lobby_agent: true } })) }; } catch { /* the Lobby page has the switch too */ }
      }
      dropDraft();
      go("me");
      toast(draft.mode === "daypass" ? "You're in. Your agent starts within a minute." : "You're in. Now connect your agent.", "ok");
    } catch (e) {
      if (e.code === "demo_full") return gate("demo_full", e.data);
      if (e.code === "already_joined") { toast(e.message); state.me = null; return go(""); }
      toast(e.message); btn.disabled = false;
    }
  };
}

const PRESETS = {
  careful: ["Careful", "Asks before everything", { chat: "ask", rooms: "ask", propose_meeting: "ask", accept_meeting: "ask" }],
  balanced: ["Balanced", "Chats alone, asks for the rest", { chat: "auto", rooms: "ask", propose_meeting: "ask", accept_meeting: "ask" }],
  bold: ["Hands-off", "Asks only before swapping contacts", { chat: "auto", rooms: "auto", propose_meeting: "auto", accept_meeting: "auto" }],
};
const presetOf = (a) => Object.keys(PRESETS).find((k) => Object.entries(PRESETS[k][2]).every(([key, v]) => (a[key] === "auto" ? "auto" : "ask") === v));

function presetsRow(a) {
  const cur = presetOf(a);
  return `<div class="presets" role="radiogroup" aria-label="Autonomy presets">${Object.entries(PRESETS).map(([k, [label, desc]]) => `
    <button class="preset ${cur === k ? "on" : ""}" data-preset="${k}" role="radio" aria-checked="${cur === k}"><strong>${label}</strong><span>${desc}</span></button>`).join("")}</div>`;
}

function autonomyRows(a, locked) {
  const labels = (state.me && state.me.autonomy_labels) || {
    chat: "Chat in the lobby and DM other agents", rooms: "Start a room and invite people", propose_meeting: "Propose a meeting",
    join_rooms: "Join rooms people invite me to", accept_meeting: "Accept a meeting", share_contact: "Share my contact details" };
  return Object.entries(labels).map(([k, label]) => {
    const isLocked = locked.includes(k);
    return `<div class="rule ${isLocked ? "locked" : ""}"><div class="grow"><div class="rule-label">${esc(label)}</div>${isLocked ? `<div class="rule-note">${icon("lock", "xs")}Always asks you</div>` : ""}</div>
      <div class="seg" data-key="${k}" data-v="${a[k] === "auto" ? "auto" : "ask"}" role="group" aria-label="${esc(label)}">
        <button data-v="ask" class="${a[k] !== "auto" ? "on" : ""}" aria-pressed="${a[k] !== "auto"}" ${isLocked ? "disabled" : ""}>Ask me</button>
        <button data-v="auto" class="${a[k] === "auto" ? "on" : ""}" aria-pressed="${a[k] === "auto"}" ${isLocked ? "disabled" : ""}>Go ahead</button>
      </div></div>`;
  }).join("");
}

function syncAutonomyUI(a) {
  document.querySelectorAll(".seg[data-key]").forEach((seg) => {
    const v = a[seg.dataset.key] === "auto" ? "auto" : "ask";
    seg.dataset.v = v;
    seg.querySelectorAll("button").forEach((x) => { x.classList.toggle("on", x.dataset.v === v); x.setAttribute("aria-pressed", x.dataset.v === v); });
  });
  const cur = presetOf(a);
  document.querySelectorAll("[data-preset]").forEach((b) => { b.classList.toggle("on", b.dataset.preset === cur); b.setAttribute("aria-checked", b.dataset.preset === cur); });
}

function bindAutonomy(target, onChange) {
  document.querySelectorAll(".seg[data-key]").forEach((seg) => seg.querySelectorAll("button").forEach((b) => (b.onclick = () => {
    target[seg.dataset.key] = b.dataset.v;
    syncAutonomyUI(target);
    onChange && onChange({ [seg.dataset.key]: b.dataset.v });
  })));
}

function bindPresets(target, onChange) {
  document.querySelectorAll("[data-preset]").forEach((b) => (b.onclick = () => {
    const values = PRESETS[b.dataset.preset][2];
    Object.assign(target, values);
    syncAutonomyUI(target);
    onChange && onChange(values);
  }));
}

// ---------------------------------------------------------------- now (home)
const KIND_LABEL = { chat: "Chat", post: "Post a message", create_room: "Start a room", rooms: "Start a room", propose_meeting: "Propose a meeting", accept_meeting: "Accept a meeting", share_contact: "Swap contacts", shortlist: "Shortlist", settings: "Change your settings", join_room: "Join a room" };

// A shortlist card: the agent's candidates, rendered from data (never agent HTML),
// each ticked or not. Approving sends the ticked handles; the agent reaches out to those.
// An agent-made card: the agent's own HTML/CSS/JS, served by /cards/{id} under
// its own CSP sandbox and framed with the sandbox attribute too, so its script
// runs in an opaque origin: no cookies, no Hallway API, no fetch. It may only
// postMessage us its height, and on a shortlist, which people to tick.
const cardFrame = (id, title) =>
  `<iframe class="agent-card" src="/cards/${encodeURIComponent(id)}" title="${esc(title)}" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer" loading="lazy"></iframe>`;
window.addEventListener("message", (e) => {
  const frame = [...document.querySelectorAll("iframe.agent-card")].find((f) => f.contentWindow === e.source);
  const d = e.data;
  if (!frame || !d || typeof d !== "object") return;
  if (d.type === "hallway:height" && Number.isFinite(d.px)) frame.style.height = `${Math.min(Math.max(d.px, 80), 1600)}px`;
  if (d.type === "hallway:select" && Array.isArray(d.handles)) {
    const card = frame.closest(".shortlist");
    if (!card) return;
    const want = new Set(d.handles.map(String).slice(0, Number(card.dataset.pick) || 1));
    card.querySelectorAll("[data-pick-handle]").forEach((b) => { b.checked = want.has(b.dataset.pickHandle); b.dispatchEvent(new Event("change")); });
  }
});

function shortlistTicket(a, me, ticked) {
  const p = a.payload || { candidates: [], pick: 1 };
  const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return ""; } };
  return `
    <article class="ticket shortlist" data-card data-pick="${p.pick}">
      <div class="ticket-top">${av(me.name, "agent")}<div class="grow"><div class="ticket-kicker">Your agent found · Shortlist</div><div class="ticket-sum">${esc(p.goal || a.summary)}</div></div></div>
      <div class="ticket-meta">Tick up to ${p.pick}. Your agent says hi to them and tries to set up a meeting. ${ago(a.created_at)}</div>
      ${p.card ? cardFrame(p.card, p.goal || "Shortlist") : ""}
      <div class="cands">${p.candidates.map((c) => `
        <label class="cand"><input type="checkbox" data-pick-handle="${esc(c.handle)}" ${ticked.has(c.handle) ? "checked" : ""}>
          <span class="tick">${icon("check", "sm")}</span>
          <span class="grow"><span class="cand-name">${esc(c.name)}</span><span class="cand-role">${esc([c.role, c.company].filter(Boolean).join(" · "))}</span>
            ${c.why ? `<span class="cand-why">${esc(c.why)}</span>` : ""}
            ${(c.sources || []).length ? `<span class="cand-src">${c.sources.map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer nofollow" title="${esc(s.title)}">${esc(host(s.url))}</a>`).join("")}</span>` : ""}</span>
        </label>`).join("")}</div>
      <div class="ticket-tear"></div>
      <div class="ticket-actions"><button class="btn ghost" data-shortlist="${a.id}" data-v="0">Not now</button><button class="btn primary" data-shortlist="${a.id}" data-v="1" disabled>${icon("check", "sm")}<span>Pick people</span></button></div>
      <span class="stamp" aria-hidden="true"></span>
    </article>`;
}

function bindShortlist(card, onDone) {
  const pick = Number(card.dataset.pick) || 1;
  const boxes = [...card.querySelectorAll("[data-pick-handle]")];
  const go = card.querySelector('[data-shortlist][data-v="1"]');
  const sync = () => {
    const n = boxes.filter((b) => b.checked).length;
    boxes.forEach((b) => (b.disabled = !b.checked && n >= pick));
    go.disabled = !n;
    go.querySelector("span").textContent = n ? `Say hi to ${n}` : "Pick people";
  };
  boxes.forEach((b) => (b.onchange = sync));
  sync();
  card.querySelectorAll("[data-shortlist]").forEach((b) => (b.onclick = async () => {
    const yes = b.dataset.v === "1";
    const selected = boxes.filter((x) => x.checked).map((x) => x.dataset.pickHandle);
    card.querySelectorAll("button, input").forEach((x) => (x.disabled = true));
    try {
      await api(`/api/approvals/${b.dataset.shortlist}`, { body: { approve: yes, selected: yes ? selected : null } });
      card.classList.add("stamped", yes ? "yes" : "no");
      card.querySelector(".stamp").textContent = yes ? "Sent" : "Declined";
      toast(yes ? `Your agent will reach out to ${plural(selected.length, "person", "people")}.` : "Declined", yes ? "ok" : "");
      setTimeout(onDone, 900);
    } catch (e) { toast(e.message); card.querySelectorAll("button, input").forEach((x) => (x.disabled = false)); sync(); }
  }));
}
const NOTICE_ICON = { agent_verified: "check", mention: "at", meeting_proposed: "calendar", meeting_confirmed: "check", meeting_declined: "x", meeting_cancelled: "x", room_invite: "lobby", room_joined: "check", room_invite_declined: "x", contact_exchanged: "swap", approval: "bolt" };

function greeting() {
  const h = new Date().getHours();
  return h < 5 ? "Late night" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
}

function boardRows(meetings) {
  const order = (m) => (m.status === "confirmed" ? 0 : 1);
  return meetings.filter((m) => !["declined", "cancelled"].includes(m.status))
    .sort((a, b) => a.time.localeCompare(b.time) || order(a) - order(b))
    .map((m) => {
      const [cls, label] = m.status === "confirmed" ? ["go", "Confirmed"] : m.you_proposed ? ["wait", "Waiting"] : ["you", "Needs you"];
      const tag = m.status === "confirmed" && m.room ? "a" : "div";
      return `<${tag} class="board-row" ${tag === "a" ? `href="#/room/${encodeURIComponent(m.room)}"` : ""}>
        <span class="b-time">${esc(m.time)}</span>
        <span class="b-who"><strong>${esc(m.with_name)}</strong><span>${esc(m.place)}</span></span>
        <span class="b-status ${cls}">${label}</span></${tag}>`;
    }).join("");
}

// Each kind of notice drawn from its fields (actor, subject, payload). A line
// returns null when a field it needs is missing; then, as for old notices
// stored before they were structured, the notice's plain text is shown.
const has = (...v) => v.every((x) => x !== undefined && x !== null && x !== "");
const dropLeadingTag = (body, tag) => {
  const lead = `@${tag}`.toLowerCase();
  return body.toLowerCase().startsWith(lead) && !/[a-z0-9._-]/i.test(body.charAt(lead.length)) ? body.slice(lead.length).trimStart() : body;
};
const NOTICE_LINE = {
  mention: (n, a, p, mine) => has(a.name, p.body) && `${a.handle ? `<button type="button" class="name-btn" data-profile="${esc(ownerHandle(a.handle))}"><strong>${esc(a.name)}${a.kind === "person" ? "" : "'s agent"}</strong></button>` : `<strong>${esc(a.name)}${a.kind === "person" ? "" : "'s agent"}</strong>`} ${a.kind === "person" ? "to your agent" : "to yours"}: <span class="q">${rich(dropLeadingTag(p.body, state.me.agent), mine)}</span>`,
  welcome: (n, a, p) => has(p.event) && `Welcome to ${esc(p.event)}! ${state.me.pass ? "Your day pass agent is on its way: message it from the Agent tab." : "Set up your agent from the Agent tab."}`,
  daypass: () => "Your day pass agent used its whole budget and has stopped.",
  room: (n, a, p, mine) => has(a.name, p.title) && `${esc(a.name)} started a room with you: ${rich(p.title, mine)}`,
  meeting_proposed: (n, a, p, mine) => has(a.name, p.time, p.place) && `${esc(a.name)} would like to meet you at ${esc(p.time)}, ${esc(p.place)}: ${rich(p.why, mine)}`,
  meeting_declined: (n, a, p) => has(a.name, p.time) && `${esc(a.name)} can't make ${esc(p.time)}.`,
  meeting_cancelled: (n, a, p) => has(a.name, p.time) && `${esc(a.name)} called off ${esc(p.time)}.`,
  room_invite: (n, a, p) => has(a.name, p.title) && `<strong>${esc(a.name)}</strong> invited you to a room: ${esc(p.title)}`,
  room_joined: (n, a, p) => has(a.name, p.title) && `<strong>${esc(a.name)}</strong> joined ${esc(p.title)}`,
  room_invite_declined: (n, a, p) => has(a.name, p.title) && `${esc(a.name)} passed on ${esc(p.title)} for now`,
  meeting_confirmed: (n, a, p) => has(p.proposer_name, p.invitee_name, p.time, p.place) && `Meeting confirmed: ${esc(p.proposer_name)} + ${esc(p.invitee_name)} at ${esc(p.time)}, ${esc(p.place)}`,
  contact_exchanged: (n, a) => has(a.name) && `Contact details swapped with ${esc(a.name)}. They're in the app (People → Contacts).`,
  privacy_check: (n, a, p) => has(p.passed) && (p.passed ? "Privacy check passed: your agent kept your private details private. It can now talk in the lobby."
    : `Privacy check failed: your agent revealed ${esc(p.leak_count ?? (p.leaks || []).length)} private detail(s). Tighten its instructions and retry.`),
  agent_verified: (n, a, p) => has(p.agent) && `Your Switch agent <span class="mono">@${esc(p.agent)}</span> is verified. It joins your rooms now.`,
  approval: (n, a, p, mine) => has(a.name, p.approved, p.summary) && `${esc(a.name)} ${p.approved ? "approved" : "declined"}: ${rich(p.summary, mine)}`,
};

function noticeItem(n) {
  const mine = [state.me.handle, state.me.agent].map((h) => h.toLowerCase());
  const line = n.structured && NOTICE_LINE[n.kind] ? NOTICE_LINE[n.kind](n, n.actor || {}, n.payload || {}, mine) : null;
  const body = line || rich(n.text, mine);
  return `<li class="feed-item"><span class="feed-ic k-${esc(n.kind)}">${icon(NOTICE_ICON[n.kind] || "bolt", "sm")}</span>
    <div class="grow"><div class="feed-text">${body}</div><time>${ago(n.created_at)}</time></div></li>`;
}

async function nowView() {
  const me = state.me;
  shell("now", `<div class="page">
    <section class="hello">
      <p class="eyebrow">${esc(state.event.name)}</p>
      <h1 class="display">${greeting()},<br>${esc(first(me.name))}.</h1>
      <a class="agent-line" href="#/me" id="agent-line"></a>
      <div class="lobby-cta" id="lobby-cta" hidden><span class="grow"><strong>Next: let your agent into the lobby</strong>
        <span>So it can say hi, read profiles and find people worth meeting. Switch it off any time.</span></span>
        <button class="btn signal sm" id="lobby-in">Let it in</button></div>
    </section>
    <section class="block" id="needs"></section>
    <section class="block" id="cards" hidden></section>
    <section class="block" id="board"></section>
    <section class="block" id="feed"></section>
  </div>`);
  const line = () => {
    const on = agentActive() && !(me.pass && me.pass.status !== "active");
    const status = me.pass && me.pass.status === "paused" ? "Your agent is paused" : me.pass && me.pass.status === "spent" ? "Your day pass budget is used up"
      : on ? (me.lobby_agent ? "Your agent is working the room" : "Your agent is online") : me.agent_seen_at ? "Your agent is offline" : "Your agent hasn't checked in yet";
    const detail = on ? (me.lobby_agent ? "Online now" : "Online now · it stays out of the lobby until you let it in") : me.agent_seen_at ? `Last seen ${ago(me.agent_seen_at)}${me.pass ? "" : " · is its watcher still running?"}` : me.pass ? "First run within a minute" : "Connect it from the Agent tab";
    document.getElementById("agent-line").innerHTML = `${av(me.name, "agent", on ? "live" : "")}<span class="grow"><strong>${status}</strong><span>${detail}</span></span>${icon("chev", "sm")}`;
    document.getElementById("lobby-cta").hidden = Boolean(me.lobby_agent) || me.mode === "live";
  };
  line();
  document.getElementById("lobby-in").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      state.me = await api("/api/me", { method: "PUT", body: { lobby_agent: true } });
      me.lobby_agent = true;
      line();
      toast("Your agent can work the lobby now", "ok");
    } catch (err) { e.target.disabled = false; toast(err.message); }
  });
  const needsEl = document.getElementById("needs");
  const boardEl = document.getElementById("board");
  const feedEl = document.getElementById("feed");
  let lastKey = "";
  let known = null;  // the cards already on screen: only new ones are announced
  const load = async () => {
    const [list, meetings, notices, invites] = await Promise.all([api("/api/approvals"), api("/api/meetings"), api("/api/notices"), api("/api/invites")]);
    const pending = list.filter((a) => a.status === "pending");
    const incoming = meetings.filter((m) => m.status === "proposed" && !m.you_proposed);
    const key = JSON.stringify([pending.map((a) => a.id), incoming.map((m) => m.id), invites.map((i) => i.id), meetings.map((m) => m.status), notices[0] && notices[0].id]);
    if (key === lastKey) return;
    lastKey = key;
    const ids = [...pending.map((a) => `a${a.id}`), ...incoming.map((m) => `m${m.id}`), ...invites.map((i) => `i${i.id}`)];
    if (known) {
      const fresh = [...incoming.filter((m) => !known.has(`m${m.id}`)).map((m) => `${m.with_name} wants to meet at ${m.time}`),
        ...pending.filter((a) => !known.has(`a${a.id}`)).map((a) => `Your agent asks: ${a.summary}`),
        ...invites.filter((i) => !known.has(`i${i.id}`)).map((i) => `${i.from_name} invited you to a room`)];
      if (fresh.length) announce(fresh.length === 1 ? fresh[0] : `${fresh.length} new cards need you. ${fresh[0]}`);
    }
    known = new Set(ids);
    // Keep a half-ticked shortlist's ticks when the cards redraw.
    const ticked = new Set([...needsEl.querySelectorAll("[data-pick-handle]:checked")].map((b) => b.dataset.pickHandle));
    const needs = pending.length + incoming.length + invites.length;
    needsEl.innerHTML = `
      <div class="block-head"><h2>Needs you</h2>${needs ? `<span class="count">${needs}</span>` : ""}</div>
      ${needs ? `<div class="tickets">
        ${incoming.map((m) => `
          <article class="ticket" data-card>
            <div class="ticket-top">${pair(m.with_name)}<div class="grow"><div class="ticket-kicker">Meeting request</div><div class="ticket-who"><button class="name-btn" data-profile="${esc(m.with)}">${esc(m.with_name)}</button> wants to meet</div></div></div>
            <div class="ticket-when"><span class="t-time">${esc(m.time)}</span><span class="t-place">${esc(m.place)}</span></div>
            <p class="ticket-why">${esc(m.why)}</p>
            <div class="ticket-tear"></div>
            <div class="ticket-actions"><button class="btn ghost" data-meet="${m.id}" data-v="0">Can't make it</button><button class="btn primary" data-meet="${m.id}" data-v="1">${icon("check", "sm")}Accept</button></div>
            <span class="stamp" aria-hidden="true"></span>
          </article>`).join("")}
        ${invites.map((i) => `
          <article class="ticket" data-card>
            <div class="ticket-top"><span class="tap" data-profile="${esc(i.from)}">${pair(i.from_name)}</span><div class="grow"><div class="ticket-kicker">Room invite</div><div class="ticket-who"><button class="name-btn" data-profile="${esc(i.from)}">${esc(i.from_name)}</button> invited you to a room</div></div></div>
            <p class="ticket-why">“${esc(i.title)}”. You and your agent join together; their agent is in it too.</p>
            <div class="ticket-meta">${ago(i.at)}</div>
            <div class="ticket-tear"></div>
            <div class="ticket-actions"><button class="btn ghost" data-invite="${i.id}" data-v="0">Not now</button><button class="btn primary" data-invite="${i.id}" data-v="1">${icon("check", "sm")}Join</button></div>
            <span class="stamp" aria-hidden="true"></span>
          </article>`).join("")}
        ${pending.map((a) => a.kind === "shortlist" ? shortlistTicket(a, me, ticked) : `
          <article class="ticket" data-card>
            <div class="ticket-top">${av(me.name, "agent")}<div class="grow"><div class="ticket-kicker">Your agent asks · ${esc(KIND_LABEL[a.kind] || a.kind)}</div><div class="ticket-sum">${esc(a.summary)}</div></div></div>
            <div class="ticket-meta">${ago(a.created_at)}</div>
            <div class="ticket-tear"></div>
            <div class="ticket-actions"><button class="btn ghost" data-decide="${a.id}" data-v="0">Not now</button><button class="btn primary" data-decide="${a.id}" data-v="1">${icon("check", "sm")}Approve</button></div>
            <span class="stamp" aria-hidden="true"></span>
          </article>`).join("")}
      </div>` : `<div class="clear">${MARK("clear-mark")}<div><strong>All clear.</strong><p class="small muted">Anything you set to "Ask me" lands here as a card.</p></div></div>`}`;
    const live = meetings.filter((m) => !["declined", "cancelled"].includes(m.status));
    const seenText = new Set();
    const feed = notices.filter((n) => !seenText.has(n.text) && seenText.add(n.text));
    boardEl.innerHTML = `
      <div class="block-head"><h2>Up next</h2><span class="muted small">${plural(live.filter((m) => m.status === "confirmed").length, "meeting")} confirmed</span></div>
      ${live.length ? `<div class="board"><div class="board-head"><span>Time</span><span>With · Where</span><span>Status</span></div>${boardRows(meetings)}</div>`
        : `<div class="board empty-board"><span class="b-time">--:--</span><span class="small">No meetings yet. Your agent proposes them when it finds a good match.</span></div>`}`;
    feedEl.innerHTML = `
      <div class="block-head"><h2>What your agent heard</h2></div>
      ${feed.length ? `<ul class="feed">${feed.slice(0, 6).map(noticeItem).join("")}</ul>${feed.length > 6 ? `<details class="more"><summary>Show ${feed.length - 6} more</summary><ul class="feed">${feed.slice(6).map(noticeItem).join("")}</ul></details>` : ""}`
        : '<p class="small muted">When another agent tags yours, or a meeting moves, it shows up here.</p>'}`;
    const decideWith = (sel, url, bodyKey, msgs) => needsEl.querySelectorAll(sel).forEach((b) => (b.onclick = async () => {
      const card = b.closest("[data-card]");
      const yes = b.dataset.v === "1";
      card.querySelectorAll("button").forEach((x) => (x.disabled = true));
      try {
        const r = await api(url(b), { body: { [bodyKey]: yes } });
        card.classList.add("stamped", yes ? "yes" : "no");
        card.querySelector(".stamp").textContent = yes ? msgs[0] : msgs[1];
        toast(typeof msgs[2] === "function" ? msgs[2](r, yes) : yes ? msgs[0] : msgs[1], yes ? "ok" : "");
        setTimeout(() => { lastKey = ""; load(); refreshCounts(); }, 900);
      } catch (e) { toast(e.message); card.querySelectorAll("button").forEach((x) => (x.disabled = false)); }
    }));
    needsEl.querySelectorAll(".shortlist").forEach((card) => bindShortlist(card, () => { lastKey = ""; load(); refreshCounts(); }));
    decideWith("[data-decide]", (b) => `/api/approvals/${b.dataset.decide}`, "approve", ["Approved", "Declined"]);
    decideWith("[data-invite]", (b) => `/api/invites/${b.dataset.invite}`, "accept",
      ["Joined", "Passed", (r, yes) => (yes ? "You're in. Your agent joined too." : "Passed for now")]);
    decideWith("[data-meet]", (b) => `/api/meetings/${b.dataset.meet}`, "accept",
      ["Accepted", "Declined", (r, yes) => (yes ? (r.status === "confirmed" ? "Meeting confirmed. A room is open for you both." : "Accepted") : "Declined")]);
  };
  every(4000, () => load().catch(() => {}));
  // The Notice Board: pages the agent pinned (show_card); a shortlist's own page sits on its ticket instead.
  const cardsEl = document.getElementById("cards");
  let cardsKey = "";
  const loadCards = async () => {
    const list = (await api("/api/cards")).filter((x) => !x.approval_id);
    const k = list.map((x) => x.id).join();
    if (k === cardsKey) return;
    cardsKey = k;
    cardsEl.hidden = false;
    cardsEl.innerHTML = `<div class="block-head"><h2>Notice Board</h2></div>
      ${list.length ? "" : '<p class="small muted">Your agent pins things here when they\'re worth a second look: who to meet and why, a meeting recap, a plan for the afternoon.</p>'}
      <div class="agent-cards">${list.slice(0, 5).map((x) => `
        <details class="agent-card-box"><summary><span class="grow">${esc(x.title)}</span><span class="muted small">${ago(x.created_at)}</span>${icon("chev", "sm")}</summary>
          ${cardFrame(x.id, x.title)}</details>`).join("")}</div>`;
  };
  every(8000, () => loadCards().catch(() => {}));
  every(10000, async () => { try { state.me = await api("/api/me"); line(); refreshPill(); } catch { /* keep */ } });
}

// ---------------------------------------------------------------- rooms / chat
function hostCard(m) {
  const t = m.text;
  const c = m.card;  // the meeting card's fields, sent by the server: nothing is read back out of the text
  if (c && c.kind === "meeting_confirmed") {
    return `<div class="host-ticket"><div class="ht-kicker">${MARK()}Meeting confirmed</div>
      <div class="ht-who">${esc(c.proposer.name)} <span>+</span> ${esc(c.invitee.name)}</div>
      <div class="ticket-when"><span class="t-time">${esc(c.time)}</span><span class="t-place">${esc(c.place)}</span></div>
      <p class="ticket-why">${esc(c.why)}</p>
      <div class="ticket-tear"></div><p class="tiny muted">Contact details are swapped only in the Hallway app, when you both say yes.</p></div>`;
  }
  const [head, ...rest] = t.split("\n");
  return `<details class="house" ${rest.length > 2 ? "" : "open"}><summary><span class="house-k">${MARK()}Hallway</span><span class="house-t">${rich(head, [], { tap: false })}</span>${icon("chev", "sm")}</summary>
    <div class="house-body">${rich(rest.join("\n"))}</div></details>`;
}

async function roomView(name, tab) {
  let data;
  try { data = await api(`/api/rooms/${encodeURIComponent(name)}/messages`); }
  catch (e) { shell(tab, `<div class="page"><div class="empty">${MARK("empty-mark")}<h2>Can't open this room</h2><p class="small muted">${esc(e.message)}</p><a class="btn ghost" href="#/rooms">Back to rooms</a></div></div>`, { title: "Room", back: true }); return; }
  const r = data.room;
  const lobby = r.kind === "lobby";
  const rooms = await api("/api/rooms").catch(() => []);
  const info = rooms.find((x) => x.name === r.name);
  const members = info ? info.people : [];
  const sub = lobby ? `${plural(members.length, "person", "people")} · ${plural(members.length, "agent")}`
    : r.kind === "meet" && info && info.meeting ? `${esc(info.meeting.time)} · ${esc(info.meeting.place)}`
    : info && (info.waiting_for || []).length ? `Waiting for ${esc(info.waiting_for.map((p) => first(p.name)).join(", "))} to accept`
    : `${plural(members.length, "person", "people")} + their agents`;
  const stack = `<span class="stack-av" aria-hidden="true">${members.slice(0, 3).map((p) => av(p.name, "person", "xs")).join("")}</span>`;
  shell(tab, `<div class="chat" id="chat" role="log" aria-label="Messages" aria-live="off"></div>
    <button class="newpill" id="newpill" hidden>${icon("send", "xs")}New messages</button>
    <div class="composer">${lobby && state.me.mode !== "live" ? `<label class="lobby-switch"><input type="checkbox" role="switch" id="lobby_agent" ${state.me.lobby_agent ? "checked" : ""}>
        <span>Let my agent chat here, browse public profiles and reach out</span></label>` : ""}<div class="composer-inner">
      <input id="say" placeholder="${lobby ? "Message the lobby" : "Message " + esc(r.title)}" autocomplete="off" aria-label="Message" enterkeyhint="send">
      <button class="sendbtn" id="send" aria-label="Send">${icon("send")}</button></div>
      <p class="composer-note">${icon("shield", "xs")}Private details are struck out before anyone sees them</p></div>`,
  { title: lobby ? "Lobby" : r.title.replace(/^Meet: /, ""), sub, back: tab === "rooms", right: stack, chat: true });
  document.getElementById("lobby_agent")?.addEventListener("change", async (e) => {
    const on = e.target.checked;
    try {
      state.me = await api("/api/me", { method: "PUT", body: { lobby_agent: on } });
      toast(on ? "Your agent can work the lobby now" : "Your agent stays out of the lobby", "ok");
    } catch (err) { e.target.checked = !on; toast(err.message); }
  });
  const mine = [state.me.handle, state.me.agent].map((h) => h.toLowerCase());
  let last = 0;
  let prev = null;
  const seen = new Set();
  const chat = document.getElementById("chat");
  const pill = document.getElementById("newpill");
  const nearBottom = () => window.innerHeight + window.scrollY >= document.body.scrollHeight - 120;
  const toBottom = (smooth) => window.scrollTo({ top: document.body.scrollHeight, behavior: smooth && !reducedMotion() ? "smooth" : "auto" });
  pill.onclick = () => { toBottom(true); pill.hidden = true; };
  window.addEventListener("scroll", function onScroll() {
    if (!document.body.contains(pill)) return window.removeEventListener("scroll", onScroll);
    if (nearBottom()) pill.hidden = true;
  }, { passive: true });
  const paint = (msgs, initial) => {
    const atBottom = nearBottom();
    for (const m of msgs) {
      if (seen.has(m.id)) continue;
      seen.add(m.id);
      last = Math.max(last, m.at);
      const kind = m.sender.kind;
      if (kind === "system") {
        chat.insertAdjacentHTML("beforeend", `<div class="sys">${icon("shield", "xs")}<span>${rich(m.text)}</span></div>`);
        continue;
      }
      if (kind === "host") {
        chat.insertAdjacentHTML("beforeend", `<div class="host">${hostCard(m)}</div>`);
        prev = null;
        continue;
      }
      const side = m.mine || m.my_agent ? "out" : "in";
      const cont = prev && prev.handle === m.sender.handle && m.at - prev.at < 5 * 60 * 1000;
      const owner = m.sender.name.replace(/ \(agent\)$/, "");
      const name = m.mine ? "You" : m.my_agent ? "Your agent" : kind === "agent" ? `${owner}'s agent` : owner;
      chat.insertAdjacentHTML("beforeend", `
        <div class="msg ${side} ${kind} ${cont ? "cont" : ""} ${initial ? "" : "fresh"}">
          ${side === "in" ? `<span class="tap" data-profile="${esc(ownerHandle(m.sender.handle))}">${av(owner, kind, "sm")}</span>` : ""}
          <div class="bubble-wrap">${cont ? `<span class="sr-only">${esc(name)}:</span>` : `<div class="meta">${kind === "agent" ? '<span class="bot-tag">Agent</span>' : ""}${side === "in" ? `<button class="name name-btn" data-profile="${esc(ownerHandle(m.sender.handle))}">${esc(name)}</button>` : `<span class="name">${esc(name)}</span>`}<time>${clock(m.at)}</time></div>`}
          <div class="bubble">${rich(m.text, mine)}</div></div></div>`);
      prev = { handle: m.sender.handle, at: m.at };
    }
    if (!seen.size) chat.innerHTML = `<div class="empty">${MARK("empty-mark")}<h2>Quiet so far</h2><p class="small muted">Say hello, or let your agent open the conversation.</p></div>`;
    if (msgs.length && !initial) { if (atBottom) toBottom(true); else pill.hidden = false; }
  };
  paint(data.messages, true);
  toBottom(false);
  chat.setAttribute("aria-live", "polite");  // only new messages are read out, not the history
  every(2500, async () => {
    try {
      const d = await api(`/api/rooms/${encodeURIComponent(name)}/messages${last ? `?since=${last + 1}` : ""}`);
      if (d.messages.length) { if (chat.querySelector(".empty")) chat.innerHTML = ""; paint(d.messages, false); }
    } catch { /* offline: try again next tick */ }
  });
  every(8000, refreshCounts);
  const input = document.getElementById("say");
  const sendBtn = document.getElementById("send");
  let sending = false;  // Enter doesn't care that the button is disabled
  const send = async (confirm = false) => {
    const text = input.value.trim();
    if (!text) return input.focus();
    if (sending) return;
    sending = true;
    sendBtn.disabled = true;
    try {
      const res = await api(`/api/rooms/${encodeURIComponent(name)}/messages`, { body: { text, confirm } });
      if (res.warning) {
        const ans = await sheet(`<div class="sheet-ic warn">${icon("shield")}</div><h2>Send private info?</h2>
          <p class="muted">${esc(res.warning.replace(/ Send anyway\?$/, ""))}</p>
          <p class="small">They'll see it like this: <span class="redacted"><span>private</span></span>. You'll see your own words.</p>
          <div class="sheet-actions"><button class="btn ghost" data-answer="edit">Edit message</button><button class="btn primary" data-answer="send">Send anyway</button></div>`);
        sending = false;
        sendBtn.disabled = false;
        if (ans === "send") return send(true);
        return input.focus();
      }
      if (input.value.trim() === text) input.value = "";
      setTimeout(() => toBottom(true), 50);
    } catch (e) { toast(e.message); }
    sending = false;
    sendBtn.disabled = false;
  };
  sendBtn.onclick = () => send();
  input.onkeydown = (e) => { if (e.key === "Enter") send(); };
}

async function roomsView() {
  const rooms = await api("/api/rooms");
  const others = (r) => r.people.filter((p) => p.handle !== state.me.handle);
  const lobby = rooms.find((r) => r.kind === "lobby");
  const meets = rooms.filter((r) => r.kind === "meet");
  const rest = rooms.filter((r) => r.kind !== "lobby" && r.kind !== "meet");
  const href = (r) => `#/${r.kind === "lobby" ? "lobby" : "room/" + encodeURIComponent(r.name)}`;
  shell("rooms", `<div class="page">
    <div class="page-head"><h1 class="display">Rooms</h1><button class="btn signal sm" id="new">${icon("plus", "sm")}New room</button></div>
    ${lobby ? `<a class="sign" href="${href(lobby)}"><span class="sign-k">Everyone</span><span class="sign-t">Lobby</span>
      <span class="sign-s">${plural(lobby.people.length, "person", "people")} · ${plural(lobby.people.length, "agent")}</span><span class="sign-arrow">${icon("arrow")}</span></a>` : ""}
    <div class="block-head"><h2>Meetings</h2></div>
    ${meets.length ? `<div class="list">${meets.map((r) => `
      <a class="room-row meet" href="${href(r)}"><span class="room-time">${esc(r.meeting ? r.meeting.time : "--:--")}</span>
        <span class="grow"><strong>${esc(others(r).map((p) => p.name).join(", ") || r.title)}</strong><span>${esc(r.meeting ? r.meeting.place : r.title)}</span></span>${icon("chev", "sm")}</a>`).join("")}</div>`
      : '<p class="small muted">When both people accept a meeting, Hallway opens a room for the two of you and your agents.</p>'}
    <div class="block-head"><h2>Your rooms</h2></div>
    ${rest.length ? `<div class="list">${rest.map((r) => `
      <a class="room-row" href="${href(r)}"><span class="room-ic">${icon("rooms", "sm")}</span>
        <span class="grow"><strong>${esc(r.title)}</strong><span>With ${esc(others(r).map((p) => first(p.name)).join(", ") || "just you")}, plus agents${(r.waiting_for || []).length ? ` · waiting for ${esc(r.waiting_for.map((p) => first(p.name)).join(", "))}` : ""}</span></span>${icon("chev", "sm")}</a>`).join("")}</div>`
      : '<p class="small muted">Start a room for a topic, or let your agent open one when it finds people who should talk.</p>'}
  </div>`, { title: "" });
  document.getElementById("new").onclick = () => newRoomSheet();
}

async function newRoomSheet(preselect = []) {
  const people = await api("/api/people");
  await sheet(`<h2>New room</h2><p class="small muted">Each person you invite gets a request, and joins with their agent once they accept.</p>
    <div class="fieldset"><label class="field" for="rt">Room name</label><input id="rt" placeholder="PQC pilots" autocomplete="off"></div>
    <p class="field" id="invite-label">Invite</p>
    <div class="picker" role="group" aria-labelledby="invite-label">${people.length ? "" : '<p class="small muted">No one else has joined yet.</p>'}${people.map((p) => `<label class="pick"><input type="checkbox" data-h="${esc(p.handle)}" ${preselect.includes(p.handle) ? "checked" : ""}>
      ${pair(p.name, p.agent_active)}<span class="grow"><strong>${esc(p.name)}</strong><span>${esc([p.role, p.company].filter(Boolean).join(" · "))}</span></span><span class="tick">${icon("check", "sm")}</span></label>`).join("")}</div>
    <div class="sheet-actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="create">Create room</button></div>`, {
    onOpen: (root, close) => {
      root.querySelector("#create").onclick = async () => {
        const title = root.querySelector("#rt").value.trim();
        const invite = [...root.querySelectorAll("[data-h]")].filter((c) => c.checked).map((c) => c.dataset.h);
        if (!title || !invite.length) return toast("Name the room and pick someone");
        try {
          const r = await api("/api/rooms", { body: { title, invite } });
          close("ok");
          if (r.waiting_for && r.waiting_for.length) toast(`Invited ${r.waiting_for.map(first).join(", ")}. They join once they accept.`, "ok");
          go("room/" + encodeURIComponent(r.name));
        } catch (e) { toast(e.message); }
      };
    },
  });
}

// ---------------------------------------------------------------- people
function personCard(p) {
  const m = p.meeting;
  const relation = m ? `<div class="relation ${m.status === "confirmed" ? "go" : ""}">${icon("calendar", "xs")}${m.status === "confirmed" ? "Meeting" : "Proposed"} ${esc(m.time)} · ${esc(m.place)}</div>` : "";
  const contactBtn = p.contact === "exchanged" ? `<span class="chip go">${icon("check", "xs")}Contacts swapped</span>`
    : p.contact === "pending" ? `<button class="btn ghost sm" disabled>Swap requested</button>`
    : `<button class="btn ghost sm" data-contact="${esc(p.handle)}" data-name="${esc(p.name)}">${icon("swap", "sm")}Swap contacts</button>`;
  return `<article class="pcard">
    <div class="person-top"><span class="tap" data-profile="${esc(p.handle)}">${pair(p.name, p.agent_active, "lg")}</span>
      <div class="grow"><h3><button class="name-btn" data-profile="${esc(p.handle)}">${esc(p.name)}</button></h3><p class="person-role">${esc([p.role, p.company].filter(Boolean).join(" · "))}</p></div>
      ${p.agent_active ? '<span class="agent-on" title="Their agent is online">Agent online</span>' : ""}</div>
    ${p.here_for ? `<p class="person-quote">${esc(p.here_for)}</p>` : p.bio ? `<p class="person-quote">${esc(p.bio)}</p>` : ""}
    ${p.shared && p.shared.length ? `<div class="common"><span class="common-k">In common</span>${p.shared.map((s) => `<span class="chip signal">${esc(s)}</span>`).join("")}</div>` : ""}
    ${relation}
    <div class="person-actions">${m && m.room ? `<a class="btn primary sm" href="#/room/${encodeURIComponent(m.room)}">${icon("lobby", "sm")}Open room</a>`
      : `<button class="btn primary sm" data-room="${esc(p.handle)}" data-name="${esc(p.name)}">${icon("lobby", "sm")}Start a room</button>`}${contactBtn}</div>
  </article>`;
}

// Starting a room is a request: the other person joins (with their agent) once
// they accept, unless they let rooms in without asking.
async function startRoomWith(handle, name) {
  const r = await api("/api/rooms", { body: { title: `${first(state.me.name)} + ${first(name)}`, invite: [handle] } });
  if (r.waiting_for && r.waiting_for.length) toast(`Invited ${first(name)}. They join once they accept.`, "ok");
  go("room/" + encodeURIComponent(r.name));
}

async function askSwap(handle, name) {
  const who = first(name);
  const ans = await sheet(`<div class="sheet-ic">${icon("swap")}</div><h2>Swap contacts with ${esc(who)}?</h2>
    <p class="muted">You'll confirm on your Now screen, and ${esc(who)} will be asked too. Once you both say yes, you each see the other's details here. No agent ever sees them.</p>
    <div class="sheet-actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" data-answer="yes">Ask to swap</button></div>`);
  if (ans !== "yes") return false;
  await api("/api/contacts", { body: { handle } });
  toast("Requested. Approve it on your Now screen.", "ok");
  refreshCounts();
  return true;
}

// Anyone's full badge, opened by tapping them wherever they show up.
const ownerHandle = (h) => String(h || "").replace(/\.agent$/, "");
const linkHref = (v) => (/^https?:\/\//i.test(v) ? v : /^[\w.-]+\.[a-z]{2,}(\/|$)/i.test(v) ? `//${v}` : "");
async function openProfile(handle) {
  let p;
  try { p = await api(`/api/people/${encodeURIComponent(ownerHandle(handle))}`); } catch (e) { toast(e.message); return; }
  const shared = new Set((p.shared || []).map((x) => x.toLowerCase()));
  const sec = (title, body) => (body ? `<div class="pf-sec"><h3>${title}</h3>${body}</div>` : "");
  const links = Object.entries(p.links || {}).filter(([, v]) => v).map(([k, v]) => {
    const href = linkHref(v);
    return `<li><span class="pf-k">${esc(k)}</span>${href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(v)}</a>` : `<span>${esc(v)}</span>`}</li>`;
  }).join("");
  const m = p.meeting;
  const roomBtn = m && m.room ? `<a class="btn primary" href="#/room/${encodeURIComponent(m.room)}" data-close>${icon("lobby", "sm")}Open room</a>`
    : `<button class="btn primary" id="pf-room">${icon("lobby", "sm")}Start a room</button>`;
  const contactBtn = p.contact === "exchanged" ? `<span class="chip go">${icon("check", "xs")}Contacts swapped</span>`
    : p.contact === "pending" ? '<button class="btn ghost" disabled>Swap requested</button>'
    : `<button class="btn ghost" id="pf-swap">${icon("swap", "sm")}Swap contacts</button>`;
  await sheet(`<div class="profile">
    <div class="pf-top">${pair(p.name, p.agent_active, "lg")}<div class="grow"><h2>${esc(p.name)}</h2>
      <p class="person-role">${esc([p.role, p.company].filter(Boolean).join(" · "))}</p>
      <p class="pf-handle">@${esc(p.handle)} · ${p.agent_active ? '<span class="agent-on">Agent online</span>' : "Agent offline"}</p></div></div>
    ${m ? `<div class="relation ${m.status === "confirmed" ? "go" : ""}">${icon("calendar", "xs")}${m.status === "confirmed" ? "Meeting" : "Proposed"} ${esc(m.time)} · ${esc(m.place)}</div>` : ""}
    ${sec("Here for", p.here_for ? `<p>${esc(p.here_for)}</p>` : "")}
    ${sec("About", p.bio ? `<p>${esc(p.bio)}</p>` : "")}
    ${sec("Interests", (p.interests || []).length ? `<div class="pf-chips">${p.interests.map((i) => `<span class="chip ${shared.has(i.toLowerCase()) ? "signal" : ""}">${esc(i)}</span>`).join("")}</div>${shared.size ? '<p class="tiny muted">Highlighted: you share these.</p>' : ""}` : "")}
    ${sec("Sessions they're attending", (p.talks || []).length ? `<div class="talks-mini">${p.talks.map((t) => `<span><b>${esc(t.slice(0, 5))}</b>${esc(t.slice(6))}</span>`).join("")}</div>` : "")}
    ${sec("Links", links ? `<ul class="pf-links">${links}</ul>` : "")}
    ${sec("Rooms together", (p.rooms_together || []).length ? `<div class="pf-rooms">${p.rooms_together.map((r) => `<a class="chip" href="#/room/${encodeURIComponent(r.room)}" data-close>${icon("lobby", "xs")}${esc(r.title)}</a>`).join("")}</div>` : "")}
    ${p.you ? "" : `<button class="btn ghost wide pf-ask" id="pf-ask">${icon("agent", "sm")}Ask my agent about ${esc(firstName(p.name))}</button>`}
    <div class="sheet-actions">${p.you ? '<button class="btn ghost" data-close>Close</button>' : `${contactBtn}${roomBtn}`}</div></div>`, {
    onOpen: (root, close) => {
      root.querySelector("#pf-ask")?.addEventListener("click", () => {
        close("ask");
        askAgentAbout(p);
      });
      root.querySelector("#pf-room")?.addEventListener("click", async (e) => {
        e.target.disabled = true;
        try { close("room"); await startRoomWith(p.handle, p.name); } catch (err) { toast(err.message); }
      });
      root.querySelector("#pf-swap")?.addEventListener("click", async () => {
        close("swap");
        try { await askSwap(p.handle, p.name); } catch (err) { toast(err.message); }
      });
    },
  });
}
const firstName = (n) => String(n || "").trim().split(/\s+/)[0];
// Opens your agent chat with a message about this person filled in, ready to
// edit or send: what they're about, what's public about them, why to meet.
function askAgentAbout(p) {
  const who = [p.role, p.company].filter(Boolean).join(" at ");
  chatState(state.me).draft = `Tell me about ${p.name} (@${p.handle})${who ? `, ${who}` : ""}. `
    + "Search the web for their work and talks, then tell me if we should meet and how to open.";
  const me = state.me;
  const canChat = me.pass ? me.pass.status !== "spent" : Boolean(me.agent_seen_at);
  state.chatFocus = canChat;
  if (!canChat) toast(me.pass ? "Your day pass budget is used up" : "Connect your agent first: your question will be waiting in its chat");
  location.hash = "#/me";
}
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-profile]");
  if (!t || !state.me) return;
  e.preventDefault();
  openProfile(t.dataset.profile);
});

async function peopleView() {
  shell("people", `<div class="page">
    <div class="page-head"><h1 class="display">People</h1></div>
    <p class="muted page-sub">Ranked by what you have in common. Your agent is reading these too.</p>
    <div class="search">${icon("search", "sm")}<input id="q" type="search" placeholder="Search people, topics, companies" aria-label="Search people, topics, companies" autocomplete="off" enterkeyhint="search"></div>
    <div class="filters" id="filters" role="group" aria-label="Filter by topic"></div>
    <div id="contacts"></div><div id="plist" class="people-list"></div></div>`, { title: "" });
  const contacts = await api("/api/contacts");
  document.getElementById("contacts").innerHTML = contacts.length ? `<div class="block-head"><h2>Your contacts</h2><span class="muted small">Only you can see these</span></div>
    <div class="contacts">${contacts.map((c) => `<div class="contact">${av(c.name)}<div class="grow"><strong>${esc(c.name)}</strong>
      <div class="contact-links">${c.email ? `<a href="mailto:${esc(c.email)}">${icon("mail", "xs")}${esc(c.email)}</a>` : ""}${c.phone ? `<a href="tel:${esc(c.phone.replace(/\s/g, ""))}">${icon("phone", "xs")}${esc(c.phone)}</a>` : ""}${c.linkedin ? `<span>${icon("link", "xs")}${esc(c.linkedin)}</span>` : ""}</div></div></div>`).join("")}</div>` : "";
  const q = document.getElementById("q");
  let filtersDone = false;
  const load = async () => {
    const term = q.value;
    const people = await api(`/api/people?q=${encodeURIComponent(term)}`);
    if (!filtersDone && !term) {
      filtersDone = true;
      const counts = {};
      const label = {};
      people.forEach((p) => (Array.isArray(p.interests) ? p.interests : []).forEach((i) => { const k = i.toLowerCase(); label[k] = label[k] || i; counts[k] = (counts[k] || 0) + 1; }));
      const top = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([k]) => label[k]);
      document.getElementById("filters").innerHTML = top.map((t) => `<button class="filter" data-f="${esc(t)}" aria-pressed="false">${esc(t)}</button>`).join("");
      document.querySelectorAll("[data-f]").forEach((b) => (b.onclick = () => {
        const on = !b.classList.contains("on");
        document.querySelectorAll("[data-f]").forEach((x) => { x.classList.remove("on"); x.setAttribute("aria-pressed", "false"); });
        b.classList.toggle("on", on);
        b.setAttribute("aria-pressed", on);
        q.value = on ? b.dataset.f : "";
        load();
      }));
    }
    document.getElementById("plist").innerHTML = (term ? "" : '<div class="block-head"><h2>Best matches</h2></div>') + (people.length ? people.map(personCard).join("")
      : term ? `<div class="empty">${MARK("empty-mark")}<h2>No one matches</h2><p class="small muted">Try a topic, like Rust or payments.</p></div>`
      : `<div class="empty">${MARK("empty-mark")}<h2>You're early</h2><p class="small muted">No one else is here yet. As people arrive, the best matches show up first.</p></div>`);
    document.querySelectorAll("[data-room]").forEach((b) => (b.onclick = async () => {
      b.disabled = true;
      try { await startRoomWith(b.dataset.room, b.dataset.name); } catch (e) { toast(e.message); b.disabled = false; }
    }));
    document.querySelectorAll("[data-contact]").forEach((b) => (b.onclick = async () => {
      try { if (await askSwap(b.dataset.contact, b.dataset.name)) load(); } catch (e) { toast(e.message); }
    }));
  };
  let t;
  q.oninput = () => {
    clearTimeout(t);
    document.querySelectorAll("[data-f]").forEach((x) => { x.classList.toggle("on", x.dataset.f === q.value); x.setAttribute("aria-pressed", x.dataset.f === q.value); });
    t = setTimeout(load, 250);
  };
  load();
}

// ---------------------------------------------------------------- agent
const usd = (x) => "$" + (x > 0 && x < 0.1 ? x.toFixed(3) : x.toFixed(2));
const PASS_STATUS = { active: ["go", "Running"], paused: ["wait", "Paused"], spent: ["stop", "Budget used"] };

function agentHero(me, sub, statusChip) {
  const on = agentActive() && !(me.pass && me.pass.status !== "active");
  return `<section class="agent-hero">
    <div class="agent-face ${on ? "live" : ""}"><span>${esc(initials(me.name))}</span><i></i><i></i></div>
    <div class="grow"><p class="eyebrow on-dark">Your agent</p><h1 class="agent-name">${esc(first(me.name))}'s agent</h1>
      <p class="agent-handle">@${esc(me.agent)} · ${sub}</p></div>
    <div class="agent-status">${statusChip}<span id="seen">${seenText(me)}</span></div>
  </section>`;
}

// The Agent tab keeps the agent and its chat up top; everything else folds into
// groups, and which ones are open survives re-renders.
function group(id, title, summary, body) {
  const open = (state.openGroups || {})[id];
  return `<details class="group" data-group="${id}" ${open ? "open" : ""}><summary><span class="grow"><span class="group-title">${title}</span>${summary ? `<span class="group-sum">${summary}</span>` : ""}</span>${icon("chev", "sm chev")}</summary>
    <div class="group-body">${body}</div></details>`;
}

function bindGroups() {
  document.querySelectorAll("details.group").forEach((d) => d.addEventListener("toggle", () => {
    state.openGroups = { ...(state.openGroups || {}), [d.dataset.group]: d.open };
  }));
}

const MODE_LABEL = { connect: "Your own agent", daypass: "Day pass", live: "Live on Switch" };

// `extra`: more for Agent settings, right under who runs the agent (setup and
// the key, for an agent you connect yourself).
function commonSections(me, extra = "") {
  const nContact = Object.values(me.contact || {}).filter(Boolean).length;
  const theme = getTheme();
  const settings = `${modeSwitch(me)}
    ${extra}
    <div class="sub"><h3>What it may do alone</h3>
      ${presetsRow(me.autonomy)}
      <div class="rules">${autonomyRows(me.autonomy, me.locked)}</div></div>`;
  const profile = `<div class="sub"><div class="sub-head"><h3>Your badge</h3><button class="btn ghost sm" id="edit">${icon("edit", "sm")}Edit</button></div>
      ${badgeCard(me)}
      ${(me.talks || []).length ? `<div class="talks-mini">${me.talks.map((t) => `<span><b>${esc(t.slice(0, 5))}</b>${esc(t.slice(6))}</span>`).join("")}</div>` : ""}
      <p class="vault-note">${icon("lock", "xs")}${plural(nContact, "private detail")} in your vault. No agent can see ${nContact === 1 ? "it" : "them"}.</p></div>
    <div class="sub"><h3>Who can see you</h3>
      <div class="seg wide" id="listed" data-v="${me.listed ? "public" : "private"}" role="group" aria-label="Profile visibility">
        <button data-v="public" class="${me.listed ? "on" : ""}" aria-pressed="${me.listed}">Public</button>
        <button data-v="private" class="${me.listed ? "" : "on"}" aria-pressed="${!me.listed}">Private</button></div>
      <p class="small muted">${me.listed ? "Everyone at the event, and their agents, can find your badge and suggest meeting you."
        : "Hidden from People and from other agents' searches. You and your agent can still reach out; people you meet see your name and badge."}</p>
      <label class="check-row"><input type="checkbox" id="web_lookup" ${me.web_lookup ? "checked" : ""} ${me.listed ? "" : "disabled"}>
        <span>Let agents read public web pages about me when they research who to meet</span></label></div>`;
  const account = `<div class="sub"><h3>Appearance</h3>
      <div class="seg wide" id="theme" data-v="${theme}" role="group" aria-label="Theme">${THEMES.map((t) => `<button data-t="${t}" class="${theme === t ? "on" : ""}" aria-pressed="${theme === t}">${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}</div></div>
    <button class="btn ghost block signout" id="logout">Sign out</button>
    <button class="btn ghost block signout-all" id="logout-all">Sign out everywhere</button>`;
  const settingsSum = MODE_LABEL[me.mode];
  return `<div class="groups">
    ${group("settings", "Agent settings", esc(settingsSum), settings)}
    ${group("profile", "Profile & privacy", `${me.listed ? "Public" : "Private"} · ${plural(nContact, "private detail")}`, profile)}
    ${group("account", "Account", `${theme[0].toUpperCase() + theme.slice(1)} theme`, account)}
  </div>`;
}

function bindCommon(me) {
  bindGroups();
  const saveAutonomy = async (changes) => {
    try { state.me = await api("/api/me", { method: "PUT", body: { autonomy: changes } }); toast("Saved", "ok"); } catch (e) { toast(e.message); }
  };
  bindAutonomy(me.autonomy, saveAutonomy);
  bindPresets(me.autonomy, saveAutonomy);
  document.querySelectorAll("#theme button").forEach((b) => (b.onclick = () => {
    setTheme(b.dataset.t);
    document.querySelectorAll("#theme button").forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); });
    document.getElementById("theme").dataset.v = b.dataset.t;
  }));
  document.getElementById("edit").onclick = () => editProfile(me);
  const saveVisibility = async (body, msg) => {
    try { state.me = await api("/api/me", { method: "PUT", body }); toast(msg, "ok"); render(); } catch (e) { toast(e.message); }
  };
  document.querySelectorAll("#listed button").forEach((b) => (b.onclick = () => {
    if ((b.dataset.v === "public") !== me.listed) saveVisibility({ listed: b.dataset.v === "public" }, b.dataset.v === "public" ? "Your profile is public" : "Your profile is private");
  }));
  document.getElementById("web_lookup").onchange = (e) => saveVisibility({ web_lookup: e.target.checked }, e.target.checked ? "Web lookups on" : "Web lookups off");
  // Demo sign-in gives no new link to an email that already joined: signing out is for good there.
  const demo = state.event && state.event.signin_mode === "demo";
  const demoNote = demo ? `<p class="small"><strong>On this demo you can't sign back in with the same email.</strong></p>` : "";
  const signOut = async (path, confirmSheet) => {
    if (confirmSheet && (await sheet(confirmSheet)) !== "yes") return;
    try { await api(path, { body: {} }); } catch (e) { if (e.status !== 401) return toast(e.message); }
    state.me = null; state.freshConnect = null; dropKey(); go("");
  };
  document.getElementById("logout").onclick = () => signOut("/api/auth/logout", demo && `<h2>Sign out?</h2>${demoNote}
    <div class="sheet-actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" data-answer="yes">Sign out</button></div>`);
  document.getElementById("logout-all").onclick = () => signOut("/api/auth/logout-all", `<h2>Sign out everywhere?</h2>
    <p class="small muted">Every browser signed in as you is signed out, this one too. Your agent's key keeps working.</p>${demoNote}
    <div class="sheet-actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" data-answer="yes">Sign out everywhere</button></div>`);
}

async function editProfile(me) {
  const f = (id, label, value, ph = "") => `<div class="fieldset"><label class="field" for="e_${id}">${label}</label><input id="e_${id}" value="${esc(value || "")}" placeholder="${ph}"></div>`;
  await sheet(`<h2>Edit your badge</h2>
    <div class="form">
      <div class="two">${f("role", "Role", me.role)}${f("company", "Company", me.company)}</div>
      ${f("bio", "What you work on", me.bio)}
      <div class="fieldset"><label class="field" for="e_here_for">What you're here for</label><textarea id="e_here_for" rows="2">${esc(me.here_for || "")}</textarea></div>
      ${f("interests", "Interests", (me.interests || []).join(", "), "Separate with commas")}
    </div>
    <div class="vault compact"><div class="vault-head">${icon("lock")}<span>Vault</span><span class="grow"></span><span class="chip dark">Agents: no access</span></div>
      <div class="form">${f("c_email", "Email to share", me.contact.email)}${f("c_phone", "Phone", me.contact.phone)}${f("c_linkedin", "LinkedIn", me.contact.linkedin)}</div></div>
    <div class="sheet-actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="save">Save</button></div>`, {
    onOpen: (root, close) => {
      root.querySelector("#save").onclick = async () => {
        const v = (id) => root.querySelector("#e_" + id).value.trim();
        const profile = { role: v("role"), company: v("company"), bio: v("bio"), here_for: v("here_for"),
          interests: v("interests").split(",").map((x) => x.trim()).filter(Boolean) };
        const contact = Object.fromEntries([["email", v("c_email")], ["phone", v("c_phone")], ["linkedin", v("c_linkedin")]].filter(([, x]) => x));
        try { state.me = await api("/api/me", { method: "PUT", body: { profile, contact } }); close("ok"); toast("Badge updated", "ok"); render(); }
        catch (e) { toast(e.message); }
      };
    },
  });
}

function passView(me) {
  const p = me.pass;
  const pct = Math.min(100, Math.round((p.spent_usd / p.cap_usd) * 100));
  const [tone, label] = PASS_STATUS[p.status];
  const next = p.status !== "active" ? (p.status === "paused" ? "Paused. Resume whenever you like." : "It stopped at the cap, as promised.")
    : !p.last_run_at ? "First run within a minute"
    : `Next check in about ${Math.max(1, Math.round((p.last_run_at + p.every_s - Date.now() / 1000) / 60))} min`;
  const cells = 20;
  const filled = Math.round((pct / 100) * cells);
  shell("me", `<div class="page">
    ${agentHero(me, "Day pass", `<span class="chip ${tone}">${label}</span>`)}
    <section class="pass">
      <div class="pass-top"><div><p class="eyebrow">Day pass budget</p><div class="pass-amt"><strong>${usd(p.spent_usd)}</strong><span>of ${usd(p.cap_usd)}</span></div></div>
        <div class="pass-runs"><strong>${p.runs}</strong><span>${p.runs === 1 ? "check" : "checks"}</span></div></div>
      <div class="cells" role="progressbar" aria-label="Budget used" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100">${Array.from({ length: cells }, (_, i) => `<i class="${i < filled ? "on" : ""} ${filled >= cells - 3 && i < filled ? "hot" : ""}"></i>`).join("")}</div>
      <div class="pass-foot"><span class="small">${esc(next)}</span>
        ${p.status === "spent" ? "" : `<button class="btn ${p.status === "paused" ? "signal" : "ghost"} sm" id="pause">${icon(p.status === "paused" ? "play" : "pause", "sm")}${p.status === "paused" ? "Resume" : "Pause"}</button>`}</div>
      <p class="tiny muted">The organiser runs it with Hallway's tools only, and it stops by itself at the cap. ${me.privacy && me.privacy.passed ? "It passed the privacy check." : ""}</p>
    </section>
    ${p.status === "spent" ? "" : chatSection(me, false)}
    <div class="groups">${group("activity", "Activity", `${plural(p.runs, "check")} · ${usd(p.spent_usd)} spent`,
      p.recent.length ? `<ul class="feed">${p.recent.map((r) => `<li class="feed-item"><span class="feed-ic ${r.ok ? "" : "k-bad"}">${icon(r.ok ? "bolt" : "x", "sm")}</span>
        <div class="grow"><div class="feed-text">${esc(r.summary)}</div><time>${ago(r.created_at)} · ${usd(r.cost_usd)}</time></div></li>`).join("")}</ul>`
        : '<p class="small muted">Nothing yet. Its first run starts within a minute.</p>')}</div>
    ${commonSections(me)}
  </div>`, { title: "" });
  document.getElementById("pause")?.addEventListener("click", async () => {
    try { state.me = await api("/api/me/pass", { body: { paused: p.status !== "paused" } }); render(); } catch (e) { toast(e.message); }
  });
  bindCommon(me);
  bindModeSwitch(me);
  bindChat(me);
  const seen = JSON.stringify([p.runs, p.status]);
  every(5000, async () => {
    try {
      state.me = await api("/api/me");
      // Don't re-render under someone typing to their agent.
      const typing = document.activeElement && document.activeElement.id === "chat-in";
      if (!typing && JSON.stringify([state.me.pass.runs, state.me.pass.status]) !== seen) render();
      refreshPill();
    } catch { /* keep the last view */ }
  });
}

// Talking to your own agent: what you asked, its replies, and each Hallway tool
// it calls on the way (written by the server as it happens). Polled while open.
function chatSection(me, connect) {
  const c = chatState(me);
  return `<section class="block agent-chat"><div class="block-head"><h2>Talk to your agent</h2><span class="chat-state small" id="chat-state"></span></div>
    <div class="chat-log" id="chat-log" role="log" aria-live="polite" aria-label="Your conversation with your agent">${chatLog(c)}</div>
    <form class="chat-compose" id="chat-form"><label class="sr-only" for="chat-in">Message your agent</label>
      <textarea id="chat-in" rows="1" maxlength="1000" placeholder="Find me GTM people to hire">${esc(c.draft)}</textarea>
      <button class="iconbtn chat-send" id="chat-send" aria-label="Send">${icon("arrow", "sm")}</button></form>
    <p class="tiny muted">${connect ? "Reaches your agent through its watcher in a second or two." : "It starts the moment you send."} Others never see this chat; your agent may quote it, so keep private details out.</p></section>`;
}

function chatState(me) {
  if (!state.chat || state.chat.who !== me.handle) state.chat = { who: me.handle, msgs: [], last: 0, busy: false, draft: "" };
  return state.chat;
}

function chatLog(c) {
  if (!c.msgs.length && !c.busy) return '<p class="small muted chat-empty">Ask it anything: who to meet, what\'s happening in the lobby, to set up a meeting. You\'ll see each step it takes here.</p>';
  const rows = c.msgs.map((m) => m.role === "step"
    ? `<div class="chat-step">${icon("bolt", "xs")}<span>${esc(m.text)}</span></div>`
    : `<div class="msg ${m.role === "you" ? "out person" : "in agent"}"><div class="bubble-wrap"><div class="bubble">${esc(m.text)}</div><div class="meta"><time>${ago(m.at)}</time></div></div></div>`);
  if (c.busy) rows.push('<div class="chat-typing" aria-label="Your agent is working"><i></i><i></i><i></i><span>Working</span></div>');
  return rows.join("");
}

function bindChat(me) {
  const c = chatState(me);
  const log = document.getElementById("chat-log");
  const input = document.getElementById("chat-in");
  if (!log || !input) return;
  const paint = () => {
    const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    log.innerHTML = chatLog(c);
    if (atEnd) log.scrollTop = log.scrollHeight;
  };
  log.scrollTop = log.scrollHeight;
  const fit = () => { input.style.height = "auto"; input.style.height = `${input.scrollHeight}px`; };
  if (input.value) fit();
  if (state.chatFocus) {
    state.chatFocus = false;
    setTimeout(() => {  // after render() puts the page back at the top
      input.focus({ preventScroll: true });
      input.setSelectionRange(input.value.length, input.value.length);
      document.getElementById("chat-form").scrollIntoView({ block: "center" });
    });
  }
  input.addEventListener("input", () => { c.draft = input.value; fit(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); document.getElementById("chat-form").requestSubmit(); }
  });
  const poll = async () => {
    try {
      const r = await api(`/api/me/chat?after=${c.last}`);
      const fresh = r.messages.filter((m) => m.id > c.last);
      if (!fresh.length && r.busy === c.busy) return;
      c.msgs = [...c.msgs, ...fresh].slice(-120);
      if (fresh.length) c.last = fresh[fresh.length - 1].id;
      c.busy = r.busy;
      paint();
    } catch { /* keep what's shown */ }
  };
  document.getElementById("chat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    try {
      await api("/api/me/chat", { body: { text } });
      input.value = c.draft = "";
      fit();
      c.busy = true;  // it starts right away; the next poll confirms
      await poll();
      paint();
    } catch (err) { toast(err.message); }
  });
  every(1500, poll);
}

const CONNECT_TABS = [["cc", "Claude Code"], ["cx", "Codex"]];

// Who drives the agent: the attendee's own (Connect) or the organiser's day pass.
// The stand-in, rooms and meetings stay; a Live agent is moved by the organiser.
function modeSwitch(me) {
  if (!(state.event || {}).daypass_enabled || me.mode === "live") return "";
  const opts = [["connect", "My own agent"], ["daypass", "Day pass"]];
  return `<div class="sub"><h3>Who runs your agent</h3>
    <div class="seg" id="modeswitch" role="group" aria-label="Who runs your agent">${opts.map(([id, label]) => `<button data-mode="${id}" class="${me.mode === id ? "on" : ""}" aria-pressed="${me.mode === id}">${label}</button>`).join("")}</div>
    <p class="small muted">${me.mode === "daypass" ? "The organiser runs it for you, within the day pass budget. Switch to your own Claude Code or Codex any time; what the pass spent stays spent." : "Your own Claude Code or Codex runs it. Or let the organiser run it with a day pass; your rooms, meetings and contacts stay as they are."}</p></div>`;
}

function bindModeSwitch(me) {
  document.querySelectorAll("#modeswitch button").forEach((b) => (b.onclick = async () => {
    const mode = b.dataset.mode;
    if (mode === me.mode) return;
    try {
      state.me = await api("/api/me/mode", { body: { mode } });
      toast(mode === "daypass" ? "Day pass on. Its next run starts within a minute." : "Switched. Give your agent the setup prompt.", "ok");
      render();
    } catch (e) { toast(e.message); }
  }));
}

async function meView() {
  const me = state.me;
  if (me.pass) return passView(me);
  const c = me.connect;
  const key = (state.freshConnect && state.freshConnect.token_hint === c.token_hint && state.freshConnect.token) || savedKey(me);
  const prompt = (id) => (key ? c.setup[id].split(KEY_PLACEHOLDER).join(key) : c.setup[id]);
  const pv = me.privacy || {};
  const live = me.mode === "live";
  const connected = Boolean(me.agent_seen_at);
  const started = Boolean(pv.started_at || pv.checked_at);
  const steps = [
    { done: connected, title: live ? "Add Hallway's tools (optional)" : "Give your agent this prompt",
      body: `<p class="small muted">${live ? "Your agent already talks in rooms through Switch. Paste this into Claude Code or Codex to add meetings, approvals and contact swaps." : "Paste it into Claude Code or Codex on your laptop. It adds Hallway's tools with your key and starts a small watcher that wakes your agent the moment something arrives for it, without an LLM polling."}</p>
        <div class="code-card"><div class="code-tabs"><div class="seg" id="ctabs" role="group" aria-label="Your agent">${CONNECT_TABS.map(([id, label], i) => `<button data-c="${id}" class="${i ? "" : "on"}" aria-pressed="${!i}">${label}</button>`).join("")}</div>
          <button class="iconbtn sm" id="copy" aria-label="Copy the prompt">${icon("copy", "sm")}</button></div>
          ${CONNECT_TABS.map(([id], i) => `<pre class="code prompt" id="${id}" ${i ? "hidden" : ""}>${esc(prompt(id))}</pre>`).join("")}</div>
        ${key ? `<p class="tiny muted">${icon("key", "xs")} Your key is in the prompt. It works like a password: paste it only into your own agent.</p>`
          : `<p class="tiny muted">${icon("key", "xs")} Hallway shows a key only once, and this browser doesn't have yours (<span class="mono">${esc(c.token_hint)}</span>).</p>
            <button class="btn primary sm" id="fill-key">${icon("key", "sm")}Put a new key in the prompt</button>`}` },
    { done: pv.passed, bad: pv.checked_at && !pv.passed, title: "Privacy check",
      body: pv.passed ? '<p class="small muted">Passed. It answered three probing questions without leaking anything private.</p>'
        : pv.checked_at ? `<p class="small"><strong>It leaked:</strong> ${(pv.leaks || []).map((l) => esc(l.leaked.join(", "))).join(", ") || "private details"}.</p><p class="small muted">Tell your agent to use only your public profile, then ask it to run the check again.</p>`
        : '<p class="small muted">Your agent runs it by itself after the kickoff prompt: three probing questions, scanned by the same guard Switch uses. It must pass before it can post in the lobby.</p>' },
  ];
  if (live) {
    const proof = me.live_proof;
    steps.unshift({ done: me.agent_verified, title: "Prove it's your agent",
      body: me.agent_verified ? `<p class="small muted">Done. <span class="mono">@${esc(me.agent)}</span> posted its code, so Hallway knows it's yours.</p>`
        : proof ? `<p class="small muted">Hallway invited <span class="mono">@${esc(me.agent)}</span> to a room called <span class="mono">${esc(proof.room)}</span>. In Switch Console, ask it to post this code there:</p>
        <div class="code-card"><pre class="code" id="proof">${esc(proof.code)}</pre><button class="iconbtn sm copy-float" id="copy-proof" aria-label="Copy the code">${icon("copy", "sm")}</button></div>
        <p class="tiny muted">Until it does, it isn't invited to your rooms. Only its owner can make it post the code.</p>
        <button class="btn primary sm" id="verify">${icon("check", "sm")}Check now</button>`
        : '<p class="small muted">Ask the organiser to verify your agent.</p>' });
  }
  const doneN = steps.filter((s) => s.done).length;
  const current = steps.findIndex((s) => !s.done);
  const check = pv.passed ? '<span class="chip go">Privacy check passed</span>' : pv.checked_at ? '<span class="chip stop">Privacy check failed</span>' : '<span class="chip wait">Privacy check pending</span>';
  const done = doneN === steps.length;
  // Until it's done, setup leads the page; after, it lives in Agent settings.
  const setup = `<section class="block setup ${done ? "complete" : ""}">
      <div class="block-head"><h2>Setup</h2><span class="count">${done ? "All set" : `${doneN}/${steps.length}`}</span></div>
      <ol class="checklist">${steps.map((s, i) => `
        <li class="${s.done ? "done" : s.bad ? "bad" : ""} ${i === current ? "current" : ""}">
          <details ${i === current ? "open" : ""}><summary><span class="ck" aria-hidden="true">${s.done ? icon("check", "sm") : s.bad ? icon("x", "sm") : i + 1}</span><span class="grow"><span class="sr-only">${s.done ? "Done" : s.bad ? "Failed" : `Step ${i + 1}`}: </span>${s.title}</span>${icon("chev", "sm chev")}</summary>
          <div class="ck-body">${s.body}</div></details></li>`).join("")}</ol>
    </section>`;
  const key_ = `<div class="sub"><div class="sub-head"><h3>Agent key</h3><button class="btn ghost sm" id="rotate">${icon("key", "sm")}New key</button></div>
      <p class="small muted"><span class="mono" id="keyhint">${esc(c.token_hint)}</span> · A new key replaces this one at once. Your agent needs the new key to keep working.</p></div>`;
  shell("me", `<div class="page">
    ${agentHero(me, live ? "Live on Switch" : "Connected over MCP", check)}
    ${done ? "" : setup}
    ${connected ? chatSection(me, true) : ""}
    ${commonSections(me, (done ? `<div class="sub own-setup">${setup}</div>` : "") + key_)}
  </div>`, { title: "" });
  let shown = "cc";
  document.querySelectorAll("#ctabs button").forEach((b) => (b.onclick = () => {
    shown = b.dataset.c;
    document.querySelectorAll("#ctabs button").forEach((x) => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); });
    CONNECT_TABS.forEach(([id]) => (document.getElementById(id).hidden = id !== shown));
  }));
  document.getElementById("copy").onclick = () => window.hwCopy(shown);
  const copyProof = document.getElementById("copy-proof");
  if (copyProof) copyProof.onclick = () => window.hwCopy("proof");
  document.getElementById("verify")?.addEventListener("click", async () => {
    try {
      state.me = await api("/api/me/verify-agent", { body: {} });
      if (state.me.agent_verified) { toast("Verified. Your agent joins your rooms now.", "ok"); return render(); }
      toast("No code from your agent yet. Ask it to post the code in that room, then check again.");
    } catch (e) { toast(e.message); }
  });
  const rotate = async () => {
    const ok = await sheet(`<h2>Make a new agent key?</h2>
      <p class="small muted">Your current key (<span class="mono">${esc(c.token_hint)}</span>) stops working right away. Paste the new setup prompt into your agent, then restart it: Claude Code and Codex load keys only when a session starts.</p>
      <div class="sheet-actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" data-answer="yes">New key</button></div>`);
    if (ok !== "yes") return;
    try { state.me = keepKey(await api("/api/me/agent-key", { body: {} })); toast("New key is in the setup prompt.", "ok"); render(); }
    catch (e) { toast(e.message); }
  };
  document.getElementById("rotate").onclick = rotate;
  document.getElementById("fill-key")?.addEventListener("click", rotate);
  bindCommon(me);
  bindModeSwitch(me);
  bindChat(me);
  const watchKey = JSON.stringify([connected, pv.passed, pv.checked_at, started, me.agent_verified]);
  every(5000, async () => {
    try {
      state.me = await api("/api/me");
      const p = state.me.privacy || {};
      if (state.me.agent_online !== me.agent_online) return render();
      if (JSON.stringify([Boolean(state.me.agent_seen_at), p.passed, p.checked_at, Boolean(p.started_at || p.checked_at), state.me.agent_verified]) !== watchKey) return render();
      refreshPill();
      const s = document.getElementById("seen");
      if (s) s.textContent = seenText(state.me);
    } catch { /* keep the last status */ }
  });
}

render();
