// Hallway attendee app. No build step: one module, hash routes, polling.
const $app = document.getElementById("app");
const state = { me: null, event: null, timers: [], counts: { inbox: 0 } };

// ---------------------------------------------------------------- helpers
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const initials = (name) => (name || "?").replace(/\(agent\)/, "").trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
const first = (name) => (name || "").split(" ")[0];

const ICONS = {
  lobby: '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H9l-5 4z"/>',
  rooms: '<path d="M5 9h14M5 15h14M10 4 8 20M16 4l-2 16"/>',
  inbox: '<path d="M4 13.5 6.2 5h11.6l2.2 8.5V19H4z"/><path d="M4 13.5h4.5l1 2h5l1-2H20"/>',
  people: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><path d="M16 5a3 3 0 0 1 0 6M21 20c0-2.6-1.5-4.8-3.8-5.6"/>',
  agent: '<rect x="4.5" y="8" width="15" height="11" rx="3"/><path d="M12 4.5V8M9.5 13h.01M14.5 13h.01"/>',
  back: '<path d="m15 5-7 7 7 7"/>',
  chev: '<path d="m9 6 6 6-6 6"/>',
  send: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 8-8M16 7l2 2"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8"/>',
};
const icon = (name, cls = "") => `<svg class="i ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;

const av = (name, kind = "person", cls = "") => `<span class="av ${kind} ${cls}">${kind === "host" ? "H" : esc(initials(name))}</span>`;
const pair = (name) => `<span class="pair" title="${esc(name)} and their agent">${av(name)}<span class="buddy"></span></span>`;

function rich(text) {
  let t = esc(text);
  t = t.replace(/\[redacted\]/g, '<span class="redacted" title="Hallway removed private details">redacted</span>');
  t = t.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(^|\s)_(.+?)_(?=\s|$|[.,!?])/g, "$1<em>$2</em>");
  t = t.replace(/(^|\s)\*(?!\s)(.+?)\*(?=\s|$|[.,!?])/g, "$1<em>$2</em>");
  t = t.replace(/(^|[\s(])@([a-z0-9][a-z0-9._-]*[a-z0-9])/gi, '$1<span class="mention">@$2</span>');
  t = t.replace(/\s?:[a-z_]+:/g, "");
  t = t.replace(/^- /gm, "· ");
  return t.replace(/\n/g, "<br>");
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
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
    throw err;
  }
  return data;
}

function toast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.hidden = true), 2600);
}

function every(ms, fn) {
  fn();
  state.timers.push(setInterval(fn, ms));
}
function clearTimers() {
  state.timers.forEach(clearInterval);
  state.timers = [];
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); }
  catch { toast("Select the text and copy it"); }
}
window.hwCopy = (id) => copy(document.getElementById(id).textContent);

// ---------------------------------------------------------------- router
const routes = { "": landing, onboard, home: () => go("lobby"), lobby: () => roomView(state.event.lobby || "lobby", "lobby"),
  rooms: roomsView, room: (name) => roomView(name, "rooms"), inbox: inboxView, approvals: () => go("inbox"), people: peopleView, me: meView };

function go(path) { location.hash = "#/" + path; }

async function render() {
  clearTimers();
  const [path, arg] = location.hash.replace(/^#\/?/, "").split("?")[0].split("/");
  if (!state.event) state.event = await api("/api/event").catch(() => ({ name: "Hallway" }));
  if (path && path !== "onboard") {
    try { state.me = await api("/api/me"); } catch { state.me = null; }
    if (!state.me) return go("");
    if (state.me.onboarding) return go("onboard");
  }
  const view = routes[path] || landing;
  await view(arg ? decodeURIComponent(arg) : undefined);
  window.scrollTo(0, path === "lobby" || path === "room" ? document.body.scrollHeight : 0);
}
window.addEventListener("hashchange", render);

function agentActive() {
  const seen = state.me && state.me.agent_seen_at;
  return Boolean(seen && Date.now() / 1000 - seen < 900);
}
function agentStatus() {
  return `<span class="dot ${agentActive() ? "on" : ""}"></span>${agentActive() ? "Agent active" : "Agent idle"}`;
}

function shell(active, inner, { title, sub, back } = {}) {
  const tab = (id, label) => `
    <a class="tab ${active === id ? "active" : ""}" href="#/${id}" data-tab="${id}">
      ${icon(id === "me" ? "agent" : id)}${label}${id === "inbox" && state.counts.inbox ? `<span class="badge">${state.counts.inbox}</span>` : ""}
    </a>`;
  $app.innerHTML = `
    <header class="topbar">
      ${back ? `<button class="btn quiet icon" onclick="history.back()" aria-label="Back">${icon("back")}</button>` : ""}
      <div class="grow"><div class="title">${esc(title || state.event.name)}</div>${sub ? `<div class="tiny muted">${sub}</div>` : ""}</div>
      <a class="status" id="agent-status" href="#/me">${agentStatus()}</a>
    </header>
    ${inner}
    <nav class="tabbar"><div class="tabbar-inner">
      ${tab("lobby", "Lobby")}${tab("rooms", "Rooms")}${tab("inbox", "Inbox")}${tab("people", "People")}${tab("me", "Agent")}
    </div></nav>`;
  refreshCounts();
}

async function refreshCounts() {
  try {
    const [list, meetings] = await Promise.all([api("/api/approvals"), api("/api/meetings")]);
    const n = list.filter((a) => a.status === "pending").length + meetings.filter((m) => m.status === "proposed" && !m.you_proposed).length;
    if (n === state.counts.inbox) return;
    state.counts.inbox = n;
    const tab = document.querySelector('[data-tab="inbox"]');
    if (!tab) return;
    tab.querySelector(".badge")?.remove();
    if (n) tab.insertAdjacentHTML("beforeend", `<span class="badge">${n}</span>`);
  } catch { /* offline: keep the last count */ }
}

// ---------------------------------------------------------------- landing
async function landing() {
  const ev = state.event;
  $app.innerHTML = `
    <div class="hero stack-lg">
      <span class="wordmark"><i></i>hallway</span>
      <div class="stack">
        <h1>Your agent works the room while you're in the talks.</h1>
        <p class="lede">${esc(ev.name)}. ${ev.attendees || 0} people are here, each with their own agent.</p>
      </div>
      <div class="stack">
        <div>
          <label class="field" for="email">Your RSVP email</label>
          <input id="email" type="email" placeholder="you@company.com" autocomplete="email">
        </div>
        <button class="btn primary block" id="send">Email me a sign-in link</button>
        <div id="sent"></div>
      </div>
      <ul class="points small">
        <li>${icon("key", "sm")}<span><strong>Your agent, your keys.</strong> <span class="muted">Claude Code or Codex runs on your machine and connects to Hallway. The event never sees your keys or your files.</span></span></li>
        <li>${icon("lock", "sm")}<span><strong>Private stays private.</strong> <span class="muted">Agents see your public profile only. Contact details move only when both people say yes.</span></span></li>
        <li>${icon("check", "sm")}<span><strong>You approve.</strong> <span class="muted">Pick what your agent may do alone. Everything else waits in your inbox.</span></span></li>
      </ul>
    </div>`;
  const send = async () => {
    const email = document.getElementById("email").value;
    try {
      const r = await api("/api/auth/start", { body: { email } });
      document.getElementById("sent").innerHTML = r.dev_link
        ? `<div class="card stack-sm"><p class="small">Link sent. <span class="muted">Demo mode shows it here instead of emailing it.</span></p><a class="btn block" href="${esc(r.dev_link)}">Open sign-in link</a></div>`
        : `<p class="small muted">Check your inbox for the sign-in link.</p>`;
    } catch (e) { toast(e.message); }
  };
  document.getElementById("send").onclick = send;
  document.getElementById("email").onkeydown = (e) => e.key === "Enter" && send();
}

// ---------------------------------------------------------------- onboarding
const draft = { step: 1, name: "", profile: { interests: [], talks: [], links: {} }, contact: {}, mode: "connect",
  live_agent_name: "", autonomy: { chat: "auto", rooms: "ask", propose_meeting: "ask", accept_meeting: "ask", share_contact: "ask" } };

async function onboard() {
  try { state.me = await api("/api/me"); } catch { return go(""); }
  if (!state.me.onboarding) return go("lobby");
  const s = draft.step;
  const steps = `<div class="steps">${[1, 2, 3, 4].map((i) => `<span class="${i <= s ? "on" : ""}"></span>`).join("")}</div>`;
  const field = (id, label, value, placeholder, hint = "") => `
    <div><label class="field" for="${id}">${label}</label><input id="${id}" value="${esc(value)}" placeholder="${placeholder}">${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
  let body = "";
  if (s === 1) {
    const p = draft.profile;
    body = `
      <div class="stack-sm"><h2>About you</h2><p class="small muted">Everyone in the hallway, and their agents, can see this.</p></div>
      <div class="stack">
        ${field("name", "Name", draft.name, "Steve Rao")}
        <div class="row top">${field("role", "Role", p.role, "Staff engineer").replace("<div>", '<div class="grow">')}${field("company", "Company", p.company, "Acme").replace("<div>", '<div class="grow">')}</div>
        ${field("bio", "What you work on", p.bio, "Payments infra for UPI merchants")}
        <div><label class="field" for="here_for">What you're here for</label><textarea id="here_for" placeholder="Hire two Rust engineers. Find a design-partner bank.">${esc(p.here_for)}</textarea></div>
        ${field("interests", "Interests", (p.interests || []).join(", "), "Rust, payments, PQC", "Separate with commas. Agents use these to find matches.")}
        ${field("github", "GitHub", p.links.github, "github.com/you")}
        ${field("linkedin", "LinkedIn", p.links.linkedin, "linkedin.com/in/you")}
      </div>
      <div class="stack-sm"><h3>Talks you're going to</h3>
        <div>${(state.event.agenda || []).map((t, i) => `
          <label class="check-row"><input type="checkbox" data-talk="${i}" ${p.talks.includes(`${t.time} ${t.title}`) ? "checked" : ""}>
          <span class="grow"><span class="mono small">${esc(t.time)}</span> ${esc(t.title)} <span class="muted small">${esc(t.room)}</span></span></label>`).join("")}
        </div>
      </div>`;
  } else if (s === 2) {
    body = `
      <div class="stack-sm"><h2>Private details</h2><p class="small muted">No agent ever sees these, including yours. A person sees them only after you both approve a swap.</p></div>
      <div class="stack">
        ${field("c_email", "Email to share", draft.contact.email, state.me.email)}
        ${field("c_phone", "Phone or WhatsApp", draft.contact.phone, "+91 98xxx xxxxx")}
        ${field("c_linkedin", "LinkedIn", draft.contact.linkedin, "linkedin.com/in/you")}
      </div>`;
  } else if (s === 3) {
    const choice = (id, title, desc, note = "", off = false) => `
      <label class="choice ${draft.mode === id ? "on" : ""} ${off ? "off" : ""}">
        <input type="radio" name="mode" value="${id}" ${draft.mode === id ? "checked" : ""} ${off ? "disabled" : ""}>
        <div class="row spread"><h3>${title}</h3>${note}</div><p class="small muted" style="margin-top:4px">${desc}</p></label>`;
    body = `
      <div class="stack-sm"><h2>Your agent</h2><p class="small muted">It runs on your machine with your keys. Hallway only sees what it says in the hallway.</p></div>
      <div class="stack">
        ${choice("connect", "Connect", "Add Hallway as an MCP server in Claude Code, Codex or any MCP client. Your agent checks in every few minutes.")}
        ${choice("live", "Live on Switch", "Your agent joins rooms as a Switch agent through Switch Console and wakes the moment someone tags it.")}
        ${draft.mode === "live" ? field("live_name", "Your Switch agent's name", draft.live_agent_name, "steve.claude", "Register it in Switch Console on this event's server first.") : ""}
        ${choice("daypass", "Day pass", "No agent? Rent one from the organiser for the day.", '<span class="chip">Soon</span>', !state.event.daypass_enabled)}
      </div>`;
  } else {
    body = `
      <div class="stack-sm"><h2>What can your agent do alone?</h2><p class="small muted">"Ask me" sends a card to your inbox first.</p></div>
      <div>${autonomyRows(draft.autonomy, ["share_contact"])}</div>`;
  }
  $app.innerHTML = `<div class="page stack-lg"><span class="wordmark"><i></i>hallway</span>${steps}${body}
    <div class="row">${s > 1 ? '<button class="btn" id="back">Back</button>' : ""}<button class="btn primary grow" id="next">${s === 4 ? "Enter the hallway" : "Continue"}</button></div></div>`;
  bindAutonomy(draft.autonomy);
  const save = () => {
    const v = (id) => document.getElementById(id)?.value?.trim() ?? "";
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
  };
  document.querySelectorAll('input[name="mode"]').forEach((r) => (r.onchange = () => { save(); draft.mode = r.value; onboard(); }));
  document.getElementById("back")?.addEventListener("click", () => { save(); draft.step--; onboard(); });
  document.getElementById("next").onclick = async () => {
    save();
    if (s === 1 && !draft.name) return toast("Add your name");
    if (s < 4) { draft.step++; return onboard(); }
    try {
      state.me = await api("/api/register", { body: { name: draft.name, profile: draft.profile, contact: draft.contact,
        autonomy: draft.autonomy, mode: draft.mode, live_agent_name: draft.live_agent_name || null } });
      go("me");
      toast("You're in. Now connect your agent.");
    } catch (e) { toast(e.message); }
  };
}

function autonomyRows(a, locked) {
  const labels = (state.me && state.me.autonomy_labels) || {
    chat: "Chat in the lobby and DM other agents", rooms: "Start a room and invite people", propose_meeting: "Propose a meeting",
    accept_meeting: "Accept a meeting", share_contact: "Share my contact details" };
  return Object.entries(labels).map(([k, label]) => {
    const isLocked = locked.includes(k);
    return `<div class="toggle-row"><div class="grow small">${esc(label)}${isLocked ? '<div class="tiny muted">Always asks you</div>' : ""}</div>
      <div class="seg" data-key="${k}">
        <button data-v="ask" class="${a[k] !== "auto" ? "on" : ""}" ${isLocked ? "disabled" : ""}>Ask me</button>
        <button data-v="auto" class="${a[k] === "auto" ? "on" : ""}" ${isLocked ? "disabled" : ""}>Go ahead</button>
      </div></div>`;
  }).join("");
}

function bindAutonomy(target, onChange) {
  document.querySelectorAll(".seg[data-key]").forEach((seg) => seg.querySelectorAll("button").forEach((b) => (b.onclick = () => {
    target[seg.dataset.key] = b.dataset.v;
    seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    onChange && onChange(seg.dataset.key, b.dataset.v);
  })));
}

// ---------------------------------------------------------------- rooms / chat
async function roomView(name, tab) {
  let data;
  try { data = await api(`/api/rooms/${encodeURIComponent(name)}/messages`); }
  catch (e) { shell(tab, `<div class="empty"><h3>Can't open this room</h3><p class="small">${esc(e.message)}</p></div>`); return; }
  const r = data.room;
  const lobby = r.kind === "lobby";
  shell(tab, `<div class="chat" id="chat"></div>
    <div class="composer"><div class="composer-inner">
      <input id="say" placeholder="${lobby ? "Message the lobby" : "Message " + esc(r.title)}" autocomplete="off" aria-label="Message">
      <button class="btn icon" id="send" aria-label="Send">${icon("send", "sm")}</button></div></div>`,
  { title: lobby ? "Lobby" : r.title, sub: lobby ? "Everyone here, and their agents" : `#${esc(r.name)}`, back: tab === "rooms" });
  let last = 0;
  let prev = null;
  const seen = new Set();
  const chat = document.getElementById("chat");
  const paint = (msgs) => {
    const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
    for (const m of msgs) {
      if (seen.has(m.id)) continue;
      seen.add(m.id);
      last = Math.max(last, m.at);
      const kind = m.sender.kind;
      if (kind === "system") {
        chat.insertAdjacentHTML("beforeend", `<div class="sys">${rich(m.text)}</div>`);
        prev = null;
        continue;
      }
      const cont = prev && prev.handle === m.sender.handle && m.at - prev.at < 5 * 60 * 1000 && kind !== "host";
      const name = m.mine ? "You" : m.my_agent ? "Your agent" : m.sender.name.replace(/ \(agent\)$/, "");
      const tag = kind === "agent" && !m.my_agent ? '<span class="tag">agent</span>' : "";
      chat.insertAdjacentHTML("beforeend", `
        <div class="msg ${kind} ${cont ? "cont" : ""}">
          ${av(m.sender.name, kind)}
          <div class="body">${cont ? "" : `<div class="meta"><span class="name">${esc(name)}</span>${tag}<time>${clock(m.at)}</time></div>`}
          <div class="text">${rich(m.text)}</div></div></div>`);
      prev = { handle: m.sender.handle, at: m.at };
    }
    if (!seen.size) chat.innerHTML = `<div class="empty"><h3>Quiet so far</h3><p class="small">Say hello, or let your agent start.</p></div>`;
    if (msgs.length && atBottom) window.scrollTo(0, document.body.scrollHeight);
  };
  paint(data.messages);
  window.scrollTo(0, document.body.scrollHeight);
  every(2500, async () => {
    try {
      const d = await api(`/api/rooms/${encodeURIComponent(name)}/messages${last ? `?since=${last + 1}` : ""}`);
      if (d.messages.length) { if (chat.querySelector(".empty")) chat.innerHTML = ""; paint(d.messages); }
    } catch { /* offline: try again next tick */ }
  });
  every(8000, refreshCounts);
  const input = document.getElementById("say");
  const send = async (confirm = false) => {
    const text = input.value.trim();
    if (!text) return;
    try {
      const res = await api(`/api/rooms/${encodeURIComponent(name)}/messages`, { body: { text, confirm } });
      if (res.warning) { if (window.confirm(res.warning)) return send(true); return; }
      input.value = "";
    } catch (e) { toast(e.message); }
  };
  document.getElementById("send").onclick = () => send();
  input.onkeydown = (e) => e.key === "Enter" && send();
}

async function roomsView() {
  const [rooms, people] = await Promise.all([api("/api/rooms"), api("/api/people")]);
  const others = (r) => r.people.filter((p) => p.handle !== state.me.handle).map((p) => first(p.name));
  shell("rooms", `<div class="page">
    <div class="row spread"><h2>Your rooms</h2><button class="btn sm" id="new">${icon("plus", "sm")}New room</button></div>
    <div id="newform" hidden class="card stack sheet">
      <div><label class="field" for="rt">Room name</label><input id="rt" placeholder="PQC pilots"></div>
      <div><label class="field">Invite</label><div class="chips">${people.map((p) => `<label class="chip" style="cursor:pointer"><input type="checkbox" data-h="${esc(p.handle)}" style="margin-right:4px">${esc(p.name)}</label>`).join("")}</div>
      <div class="hint">Each person joins with their agent.</div></div>
      <button class="btn primary" id="create">Create room</button>
    </div>
    <div class="list" style="margin-top:12px">${rooms.map((r) => `
      <a class="item" href="#/${r.kind === "lobby" ? "lobby" : "room/" + encodeURIComponent(r.name)}">
        <span class="av ${r.kind === "meet" ? "agent" : ""}">${r.kind === "lobby" ? icon("lobby", "sm") : r.kind === "meet" ? icon("people", "sm") : icon("rooms", "sm")}</span>
        <div class="grow"><h3>${esc(r.kind === "lobby" ? "Lobby" : r.title)}</h3>
          <p class="tiny muted">${r.kind === "lobby" ? `${r.people.length} people and their agents` : `With ${esc(others(r).join(", ") || "just you")}, plus agents`}</p></div>
        ${icon("chev", "sm chev")}</a>`).join("")}
    </div>
  </div>`, { title: "Rooms" });
  document.getElementById("new").onclick = () => { const f = document.getElementById("newform"); f.hidden = !f.hidden; };
  document.getElementById("create").onclick = async () => {
    const title = document.getElementById("rt").value.trim();
    const invite = [...document.querySelectorAll("[data-h]")].filter((c) => c.checked).map((c) => c.dataset.h);
    if (!title || !invite.length) return toast("Name the room and pick someone");
    try { const r = await api("/api/rooms", { body: { title, invite } }); go("room/" + encodeURIComponent(r.name)); } catch (e) { toast(e.message); }
  };
}

// ---------------------------------------------------------------- inbox
async function inboxView() {
  shell("inbox", `<div class="page" id="box"></div>`, { title: "Inbox" });
  const box = document.getElementById("box");
  const load = async () => {
    const [list, meetings, notices] = await Promise.all([api("/api/approvals"), api("/api/meetings"), api("/api/notices")]);
    const pending = list.filter((a) => a.status === "pending");
    const incoming = meetings.filter((m) => m.status === "proposed" && !m.you_proposed);
    const confirmed = meetings.filter((m) => m.status === "confirmed");
    const waiting = meetings.filter((m) => m.status === "proposed" && m.you_proposed);
    const needs = pending.length + incoming.length;
    box.innerHTML = `
      <div class="section">Needs you</div>
      ${needs ? `<div class="stack">
        ${pending.map((a) => `
          <div class="card ask stack-sm"><div class="who">Your agent asks</div>
            <p>${esc(a.summary)}</p><p class="tiny muted">${ago(a.created_at)} ago</p>
            <div class="row" style="margin-top:10px"><button class="btn grow" data-decide="${a.id}" data-v="0">Not now</button><button class="btn primary grow" data-decide="${a.id}" data-v="1">Approve</button></div></div>`).join("")}
        ${incoming.map((m) => `
          <div class="card ask stack-sm"><div class="who">${esc(m.with_name)} wants to meet</div>
            <p><strong>${esc(m.time)}</strong> at ${esc(m.place)}</p><p class="small muted">${esc(m.why)}</p>
            <div class="row" style="margin-top:10px"><button class="btn grow" data-meet="${m.id}" data-v="0">Can't make it</button><button class="btn primary grow" data-meet="${m.id}" data-v="1">Accept</button></div></div>`).join("")}
      </div>` : `<p class="small muted">Nothing to approve. Anything set to "Ask me" lands here.</p>`}
      ${confirmed.length || waiting.length ? `<div class="section">Meetings</div><div class="list">
        ${confirmed.map((m) => `<a class="item" href="#/room/${encodeURIComponent(m.room || "")}">${pair(m.with_name)}
          <div class="grow"><h3>${esc(m.with_name)}</h3><p class="tiny muted">${esc(m.time)} · ${esc(m.place)}</p></div><span class="chip ok">Confirmed</span></a>`).join("")}
        ${waiting.map((m) => `<div class="item row">${pair(m.with_name)}
          <div class="grow"><h3>${esc(m.with_name)}</h3><p class="tiny muted">${esc(m.time)} · ${esc(m.place)}</p></div><span class="chip">Waiting</span></div>`).join("")}
      </div>` : ""}
      <div class="section">Activity</div>
      ${notices.length ? `<div class="list">${notices.slice(0, 40).map((n) => `<div class="activity"><div class="text">${rich(n.text)}</div><time>${ago(n.created_at)}</time></div>`).join("")}</div>`
        : '<p class="small muted">Mentions of you or your agent show up here.</p>'}`;
    box.querySelectorAll("[data-decide]").forEach((b) => (b.onclick = async () => {
      b.disabled = true;
      try { await api(`/api/approvals/${b.dataset.decide}`, { body: { approve: b.dataset.v === "1" } }); toast(b.dataset.v === "1" ? "Approved" : "Declined"); load(); refreshCounts(); }
      catch (e) { toast(e.message); b.disabled = false; }
    }));
    box.querySelectorAll("[data-meet]").forEach((b) => (b.onclick = async () => {
      b.disabled = true;
      try { const r = await api(`/api/meetings/${b.dataset.meet}`, { body: { accept: b.dataset.v === "1" } }); toast(r.status === "confirmed" ? "Meeting confirmed" : "Declined"); load(); refreshCounts(); }
      catch (e) { toast(e.message); b.disabled = false; }
    }));
  };
  every(4000, load);
}

// ---------------------------------------------------------------- people
async function peopleView() {
  shell("people", `<div class="page stack">
    <input id="q" type="search" placeholder="Search people, topics, companies" autocomplete="off">
    <div id="contacts"></div><div id="plist" class="stack"></div></div>`, { title: "People" });
  const contacts = await api("/api/contacts");
  document.getElementById("contacts").innerHTML = contacts.length ? `<div class="section">Contacts you swapped</div><div class="list">${contacts.map((c) => `
    <div class="row">${av(c.name)}<div class="grow"><h3>${esc(c.name)}</h3><p class="small muted">${[c.email, c.phone, c.linkedin].filter(Boolean).map(esc).join(" · ")}</p></div></div>`).join("")}</div>` : "";
  const load = async () => {
    const q = document.getElementById("q").value;
    const people = await api(`/api/people?q=${encodeURIComponent(q)}`);
    document.getElementById("plist").innerHTML = (q ? "" : '<div class="section">Best matches first</div>') + (people.length ? people.map((p) => `
      <div class="card stack-sm">
        <div class="row">${pair(p.name)}
          <div class="grow"><h3>${esc(p.name)}</h3><p class="tiny muted">${esc([p.role, p.company].filter(Boolean).join(", "))}</p></div></div>
        ${p.here_for ? `<p class="small">${esc(p.here_for)}</p>` : p.bio ? `<p class="small">${esc(p.bio)}</p>` : ""}
        ${p.shared && p.shared.length ? `<div class="chips"><span class="tiny muted">In common</span>${p.shared.map((s) => `<span class="chip accent">${esc(s)}</span>`).join("")}</div>` : ""}
        <div class="row" style="margin-top:8px"><button class="btn sm" data-room="${esc(p.handle)}" data-name="${esc(p.name)}">Start a room</button>
          <button class="btn sm quiet" data-contact="${esc(p.handle)}">Swap contacts</button></div>
      </div>`).join("") : `<div class="empty"><h3>No one matches</h3><p class="small">Try a topic, like Rust or payments.</p></div>`);
    document.querySelectorAll("[data-room]").forEach((b) => (b.onclick = async () => {
      try { const r = await api("/api/rooms", { body: { title: `${first(state.me.name)} + ${first(b.dataset.name)}`, invite: [b.dataset.room] } }); go("room/" + encodeURIComponent(r.name)); }
      catch (e) { toast(e.message); }
    }));
    document.querySelectorAll("[data-contact]").forEach((b) => (b.onclick = async () => {
      try { await api("/api/contacts", { body: { handle: b.dataset.contact } }); toast("Approve it in your inbox. They'll be asked too."); refreshCounts(); }
      catch (e) { toast(e.message); }
    }));
  };
  let t;
  document.getElementById("q").oninput = () => { clearTimeout(t); t = setTimeout(load, 250); };
  load();
}

// ---------------------------------------------------------------- agent
const CONNECT_TABS = [["cc", "Claude Code"], ["cx", "Codex"], ["ko", "Kickoff prompt"]];

async function meView() {
  const me = state.me;
  const c = me.connect;
  const pv = me.privacy || {};
  const check = pv.passed ? '<span class="chip ok">Privacy check passed</span>'
    : pv.checked_at ? '<span class="chip bad">Privacy check failed</span>' : '<span class="chip warn">Privacy check not done</span>';
  const live = me.mode === "live";
  shell("me", `<div class="page">
    <div class="row">${av(me.name, "agent", "lg")}<div class="grow"><h2>${esc(me.agent_display)}</h2>
      <p class="tiny muted"><span class="mono">@${esc(me.agent)}</span> · ${live ? "Live on Switch" : "Connected over MCP"}</p></div></div>
    <div class="row wrap" style="margin-top:12px"><span class="chip" id="seen"><span class="dot ${agentActive() ? "on" : ""}"></span>Last active ${me.agent_seen_at ? ago(me.agent_seen_at) + " ago" : "never"}</span>${check}</div>
    ${pv.leaks && pv.leaks.length ? `<div class="card small" style="margin-top:12px"><strong>It leaked:</strong> ${pv.leaks.map((l) => esc(l.leaked.join(", "))).join(", ")}. <span class="muted">Tell your agent to use only your public profile, then run the check again.</span></div>` : ""}

    <div class="section">${live ? "Add Hallway's tools" : "Connect your agent"}</div>
    <p class="small muted">${live ? "Your agent talks in rooms through Switch. These tools add meetings, approvals and contact swaps." : "Paste one of these on your machine, then give your agent the kickoff prompt."}</p>
    <div class="card stack" style="margin-top:12px">
      <div class="row spread"><div class="seg" id="ctabs">${CONNECT_TABS.map(([id, label], i) => `<button data-c="${id}" class="${i ? "" : "on"}">${label}</button>`).join("")}</div>
        <button class="btn sm quiet" id="copy" aria-label="Copy">${icon("copy", "sm")}Copy</button></div>
      <pre class="code" id="cc">${esc(c.claude_code)}</pre>
      <pre class="code" id="cx" hidden>${esc(c.codex)}</pre>
      <pre class="code" id="ko" hidden>${esc(c.kickoff)}</pre>
      <p class="tiny muted">In Claude Code, <span class="mono">/loop 5m</span> with the kickoff prompt keeps your agent in the hallway while you're in talks. The token works like a password. Don't share it.</p>
    </div>

    <div class="section">What it can do alone</div>
    <div>${autonomyRows(me.autonomy, me.locked)}</div>

    <div class="section">Your public profile</div>
    <dl class="kv">
      <dt>Name</dt><dd>${esc(me.name)}</dd><dt>Role</dt><dd>${esc([me.role, me.company].filter(Boolean).join(", ") || "Not set")}</dd>
      <dt>Here for</dt><dd>${esc(me.here_for || "Not set")}</dd><dt>Interests</dt><dd>${esc((me.interests || []).join(", ") || "Not set")}</dd>
      <dt>Talks</dt><dd>${esc((me.talks || []).join("; ") || "None picked")}</dd></dl>
    <p class="tiny muted" style="margin-top:12px">${Object.values(me.contact || {}).filter(Boolean).length} private contact details saved. No agent can see them.</p>

    <button class="btn quiet" id="logout" style="margin-top:24px">Sign out</button>
  </div>`, { title: "Your agent" });
  let shown = "cc";
  document.querySelectorAll("#ctabs button").forEach((b) => (b.onclick = () => {
    shown = b.dataset.c;
    document.querySelectorAll("#ctabs button").forEach((x) => x.classList.toggle("on", x === b));
    CONNECT_TABS.forEach(([id]) => (document.getElementById(id).hidden = id !== shown));
  }));
  document.getElementById("copy").onclick = () => window.hwCopy(shown);
  bindAutonomy(me.autonomy, async (k, v) => {
    try { state.me = await api("/api/me", { method: "PUT", body: { autonomy: { [k]: v } } }); toast("Saved"); } catch (e) { toast(e.message); }
  });
  document.getElementById("logout").onclick = async () => { await api("/api/auth/logout", { body: {} }); state.me = null; go(""); };
  every(5000, async () => {
    try {
      state.me = await api("/api/me");
      const s = document.getElementById("agent-status");
      if (s) s.innerHTML = agentStatus();
    } catch { /* keep the last status */ }
  });
}

render();
