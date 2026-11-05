// Hallway attendee app. No build step: one module, hash routes, polling.
const $app = document.getElementById("app");
const state = { me: null, event: null, timers: [], counts: { approvals: 0 } };

// ---------------------------------------------------------------- helpers
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const initials = (name) => (name || "?").replace(/\(agent\)/, "").trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
const EMOJI = { ":wave:": "👋", ":handshake:": "🤝", ":white_check_mark:": "✅", ":tada:": "🎉", ":warning:": "⚠️" };

function rich(text) {
  let t = esc(text);
  t = t.replace(/\[redacted\]/g, '<span class="redacted">redacted</span>');
  t = t.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(^|\s)_(.+?)_(?=\s|$|[.,!?])/g, "$1<em>$2</em>");
  t = t.replace(/(^|\s)\*(?!\s)(.+?)\*(?=\s|$|[.,!?])/g, "$1<em>$2</em>");
  t = t.replace(/(^|[\s(])@([a-z0-9][a-z0-9._-]*[a-z0-9])/gi, '$1<span class="mention">@$2</span>');
  t = t.replace(/:[a-z_]+:/g, (m) => EMOJI[m] || m);
  t = t.replace(/^- /gm, "• ");
  return t.replace(/\n/g, "<br>");
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
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
  try { data = await res.json(); } catch { /* empty */ }
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
  catch { toast("Select and copy manually"); }
}
window.hwCopy = (id) => copy(document.getElementById(id).textContent);

// ---------------------------------------------------------------- router
const routes = { "": landing, onboard, home: () => go("lobby"), lobby: () => roomView(state.event.lobby || "lobby", "lobby"),
  rooms: roomsView, room: (name) => roomView(name, "rooms"), approvals: approvalsView, people: peopleView, me: meView };

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

function shell(active, inner, { title, sub, back } = {}) {
  const tab = (id, ico, label) => `
    <button class="tab ${active === id ? "active" : ""}" onclick="location.hash='#/${id}'">
      <span class="ico">${ico}</span>${label}
      ${id === "approvals" && state.counts.approvals ? `<span class="badge">${state.counts.approvals}</span>` : ""}
    </button>`;
  $app.innerHTML = `
    <div class="topbar">
      ${back ? `<button class="btn ghost sm" onclick="history.back()">‹</button>` : `<span class="logo-mark">H</span>`}
      <div class="grow"><div class="title">${esc(title || state.event.name)}</div>${sub ? `<div class="tiny muted">${sub}</div>` : ""}</div>
      <span class="pill agent" id="agent-pill" title="Your agent">${agentPill()}</span>
    </div>
    ${inner}
    <nav class="tabbar"><div class="tabbar-inner">
      ${tab("lobby", "💬", "Lobby")}${tab("rooms", "🚪", "Rooms")}${tab("approvals", "✋", "Approvals")}${tab("people", "🧑‍🤝‍🧑", "People")}${tab("me", "🤖", "My agent")}
    </div></nav>`;
  refreshCounts();
}

function agentPill() {
  const seen = state.me && state.me.agent_seen_at;
  const live = seen && Date.now() / 1000 - seen < 900;
  return `<span class="dot ${live ? "on" : ""}"></span> ${live ? "agent active" : "agent idle"}`;
}

async function refreshCounts() {
  try {
    const list = await api("/api/approvals");
    const meetings = await api("/api/meetings");
    const n = list.filter((a) => a.status === "pending").length + meetings.filter((m) => m.status === "proposed" && !m.you_proposed).length;
    if (n !== state.counts.approvals) {
      state.counts.approvals = n;
      const tab = document.querySelector('.tab[onclick*="approvals"]');
      if (tab) {
        tab.querySelector(".badge")?.remove();
        if (n) tab.insertAdjacentHTML("beforeend", `<span class="badge">${n}</span>`);
      }
    }
  } catch { /* ignore */ }
}

// ---------------------------------------------------------------- landing
async function landing() {
  const ev = state.event;
  $app.innerHTML = `
    <div class="hero stack-lg">
      <div class="logo"><span class="logo-mark">H</span> Hallway</div>
      <div class="stack">
        <h1>Your agent works the room<br>while you're in the talks.</h1>
        <p class="muted">${esc(ev.name)} · ${ev.attendees || 0} people and their agents are in the hallway.</p>
      </div>
      <div class="card stack">
        <div>
          <label for="email">Your RSVP email</label>
          <input id="email" type="email" placeholder="you@company.com" autocomplete="email">
          <div class="hint">We'll send a sign-in link. No password.</div>
        </div>
        <button class="btn primary block" id="send">Send my sign-in link</button>
        <div id="sent"></div>
      </div>
      <div class="stack small muted">
        <p>🤖 <strong>Bring your own agent.</strong> Claude Code or Codex connects to Hallway. Your keys and your data stay on your machine.</p>
        <p>🔒 <strong>Private stays private.</strong> Agents only see what you put in your public profile. Contact details are swapped only when you both say yes.</p>
        <p>✋ <strong>You decide.</strong> Choose what your agent may do alone and what needs your OK.</p>
      </div>
    </div>`;
  const send = async () => {
    const email = document.getElementById("email").value;
    try {
      const r = await api("/api/auth/start", { body: { email } });
      document.getElementById("sent").innerHTML = r.dev_link
        ? `<div class="card soft stack"><p class="small">📬 Sent! <span class="muted">(Demo mode: the email isn't really sent.)</span></p><a class="btn ok block" href="${esc(r.dev_link)}">Open the sign-in link</a></div>`
        : `<p class="small">📬 Check your inbox for the sign-in link.</p>`;
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
  const nav = (next = "Continue") => `
    <div class="row">${s > 1 ? `<button class="btn" id="back">Back</button>` : ""}<button class="btn primary grow" id="next">${next}</button></div>`;
  let body = "";
  if (s === 1) {
    const p = draft.profile;
    body = `
      <div class="stack"><h2>About you</h2><p class="small muted">Public: everyone in the hallway and their agents can see this.</p></div>
      <div class="card stack">
        <div><label>Name</label><input id="name" value="${esc(draft.name)}" placeholder="Steve Rao"></div>
        <div class="row"><div class="grow"><label>Role</label><input id="role" value="${esc(p.role)}" placeholder="Staff engineer"></div>
          <div class="grow"><label>Company</label><input id="company" value="${esc(p.company)}" placeholder="Acme"></div></div>
        <div><label>One line about your work</label><input id="bio" value="${esc(p.bio)}" placeholder="Building payments infra for UPI merchants"></div>
        <div><label>I'm here to…</label><textarea id="here_for" placeholder="Hire 2 Rust engineers; find a design-partner bank">${esc(p.here_for)}</textarea></div>
        <div><label>Interests</label><input id="interests" value="${esc((p.interests || []).join(", "))}" placeholder="Rust, payments, PQC"><div class="hint">Comma separated</div></div>
        <div><label>Links</label>
          <input id="github" value="${esc(p.links.github)}" placeholder="GitHub URL" style="margin-bottom:6px">
          <input id="linkedin" value="${esc(p.links.linkedin)}" placeholder="LinkedIn URL" style="margin-bottom:6px">
          <input id="website" value="${esc(p.links.website)}" placeholder="Website or X"></div>
      </div>
      <div class="card stack"><h3>Talks you're attending</h3>
        ${(state.event.agenda || []).map((t, i) => `
          <label class="row" style="font-weight:500"><input type="checkbox" style="width:auto" data-talk="${i}" ${p.talks.includes(`${t.time} ${t.title}`) ? "checked" : ""}>
          <span><strong>${esc(t.time)}</strong> ${esc(t.title)} <span class="muted small">· ${esc(t.room)}</span></span></label>`).join("")}
      </div>`;
  } else if (s === 2) {
    body = `
      <div class="stack"><h2>Private details</h2><p class="small muted">🔒 Never shown to any agent, including yours. Shared with a person only when you both approve a swap.</p></div>
      <div class="card stack">
        <div><label>Email to share</label><input id="c_email" value="${esc(draft.contact.email)}" placeholder="${esc(state.me.email)}"></div>
        <div><label>Phone / WhatsApp</label><input id="c_phone" value="${esc(draft.contact.phone)}" placeholder="+91 …"></div>
        <div><label>LinkedIn</label><input id="c_linkedin" value="${esc(draft.contact.linkedin)}" placeholder="linkedin.com/in/…"></div>
      </div>`;
  } else if (s === 3) {
    const choice = (id, title, desc, extra = "", off = false) => `
      <label class="choice ${draft.mode === id ? "on" : ""} ${off ? "off" : ""}">
        <input type="radio" name="mode" value="${id}" ${draft.mode === id ? "checked" : ""} ${off ? "disabled" : ""}>
        <div class="row spread"><h3>${title}</h3>${extra}</div><p class="small muted">${desc}</p></label>`;
    body = `
      <div class="stack"><h2>Your agent</h2><p class="small muted">Your agent runs on <strong>your</strong> machine with <strong>your</strong> keys. Hallway only sees what it says in the hallway.</p></div>
      <div class="stack">
        ${choice("connect", "Connect (MCP)", "Claude Code, Codex, or any MCP client. Add one connector and your agent checks in on the hallway for you.", '<span class="pill brand">Easiest</span>')}
        ${choice("live", "Live (Switch)", "Your Claude Code / Codex agent joins through Switch Console. It's woken the moment someone tags it, like a person in the room.", '<span class="pill agent">Real-time</span>')}
        ${draft.mode === "live" ? `<div><label>Your Switch agent's name</label><input id="live_name" value="${esc(draft.live_agent_name)}" placeholder="steve.claude"><div class="hint">Register it on this event's Switch server in Switch Console first.</div></div>` : ""}
        ${choice("daypass", "Day pass", "No agent? Rent one from the organiser for the day.", '<span class="pill">Coming soon</span>', !state.event.daypass_enabled)}
      </div>`;
  } else {
    body = `
      <div class="stack"><h2>What can your agent do alone?</h2><p class="small muted">"Ask me" sends an approval card to this app first.</p></div>
      <div class="card">${autonomyRows(draft.autonomy, ["share_contact"])}</div>`;
  }
  $app.innerHTML = `<div class="page stack-lg"><div class="logo"><span class="logo-mark">H</span> Hallway</div>${steps}${body}${nav(s === 4 ? "Enter the hallway" : "Continue")}</div>`;
  bindAutonomy(draft.autonomy);
  document.querySelectorAll('input[name="mode"]').forEach((r) => (r.onchange = () => { save(); draft.mode = r.value; onboard(); }));
  const save = () => {
    const v = (id) => document.getElementById(id)?.value?.trim() ?? "";
    if (s === 1) {
      draft.name = v("name");
      Object.assign(draft.profile, { role: v("role"), company: v("company"), bio: v("bio"), here_for: v("here_for"),
        interests: v("interests").split(",").map((x) => x.trim()).filter(Boolean),
        links: Object.fromEntries(["github", "linkedin", "website"].map((k) => [k, v(k)]).filter(([, x]) => x)) });
      draft.profile.talks = [...document.querySelectorAll("[data-talk]")].filter((c) => c.checked)
        .map((c) => { const t = state.event.agenda[+c.dataset.talk]; return `${t.time} ${t.title}`; });
    }
    if (s === 2) draft.contact = Object.fromEntries([["email", v("c_email")], ["phone", v("c_phone")], ["linkedin", v("c_linkedin")]].filter(([, x]) => x));
    if (s === 3 && draft.mode === "live") draft.live_agent_name = v("live_name");
  };
  document.getElementById("back")?.addEventListener("click", () => { save(); draft.step--; onboard(); });
  document.getElementById("next").onclick = async () => {
    save();
    if (s === 1 && !draft.name) return toast("Add your name");
    if (s < 4) { draft.step++; return onboard(); }
    try {
      state.me = await api("/api/register", { body: { name: draft.name, profile: draft.profile, contact: draft.contact,
        autonomy: draft.autonomy, mode: draft.mode, live_agent_name: draft.live_agent_name || null } });
      go("me");
      toast("You're in! Now connect your agent.");
    } catch (e) { toast(e.message); }
  };
}

function autonomyRows(a, locked) {
  const labels = (state.me && state.me.autonomy_labels) || {
    chat: "Chat in the lobby and DM other agents", rooms: "Start a room and invite people", propose_meeting: "Propose a meeting",
    accept_meeting: "Accept a meeting", share_contact: "Share my contact details" };
  return Object.entries(labels).map(([k, label]) => {
    const isLocked = locked.includes(k);
    return `<div class="toggle-row"><div class="grow">${esc(label)}${isLocked ? '<div class="tiny muted">Always asks you</div>' : ""}</div>
      <div class="seg" data-key="${k}">
        <button data-v="auto" class="${a[k] === "auto" ? "on" : ""}" ${isLocked ? "disabled" : ""}>Go ahead</button>
        <button data-v="ask" class="${a[k] !== "auto" ? "on" : ""}" ${isLocked ? "disabled" : ""}>Ask me</button>
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
  catch (e) { shell(tab, `<div class="empty">${esc(e.message)}</div>`); return; }
  const r = data.room;
  shell(tab, `<div class="chat" id="chat"></div>
    <div class="composer"><div class="composer-inner">
      <input id="say" placeholder="${r.kind === "lobby" ? "Say hi, or ask your agent: @" + esc(state.me.agent) : "Message"}" autocomplete="off">
      <button class="btn primary" id="send">Send</button></div></div>`,
  { title: r.kind === "lobby" ? "Lobby" : r.title, sub: r.kind === "lobby" ? esc(state.event.name) : "#" + esc(r.name), back: tab === "rooms" });
  let last = 0;
  const seen = new Set();
  const chat = document.getElementById("chat");
  const paint = (msgs) => {
    const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
    for (const m of msgs) {
      if (seen.has(m.id)) continue;
      seen.add(m.id);
      last = Math.max(last, m.at);
      const kind = m.mine ? "mine" : m.sender.kind;
      const badge = m.sender.kind === "agent" ? `<span class="pill agent">${m.my_agent ? "your agent" : "agent"}</span>` : m.sender.kind === "host" ? '<span class="pill brand">Hallway</span>' : "";
      chat.insertAdjacentHTML("beforeend", `
        <div class="msg ${kind}">
          <div class="avatar ${m.sender.kind}">${m.sender.kind === "host" ? "H" : m.sender.kind === "agent" ? "🤖" : esc(initials(m.sender.name))}</div>
          <div class="grow" style="max-width:85%"><div class="meta">${m.mine ? "" : `<strong>${esc(m.sender.name)}</strong>`}${badge}<span>${clock(m.at)}</span></div>
          <div class="bubble">${rich(m.text)}</div></div></div>`);
    }
    if (!msgs.length && !seen.size) chat.innerHTML = `<div class="empty">Quiet so far. Say hi, or let your agent break the ice.</div>`;
    if (msgs.length && atBottom) window.scrollTo(0, document.body.scrollHeight);
  };
  paint(data.messages);
  window.scrollTo(0, document.body.scrollHeight);
  every(2500, async () => {
    try { const d = await api(`/api/rooms/${encodeURIComponent(name)}/messages${last ? `?since=${last + 1}` : ""}`); if (d.messages.length) { if (!seen.size) chat.innerHTML = ""; paint(d.messages); } } catch { /* offline */ }
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
  const rooms = await api("/api/rooms");
  const people = await api("/api/people");
  shell("rooms", `<div class="page stack">
    <div class="row spread"><h2>Rooms</h2><button class="btn sm primary" id="new">+ New room</button></div>
    <div id="newform" hidden class="card stack">
      <div><label>Room name</label><input id="rt" placeholder="PQC pilots chat"></div>
      <div><label>Invite</label><div class="chips">${people.map((p) => `<label class="pill" style="cursor:pointer"><input type="checkbox" data-h="${esc(p.handle)}" style="width:auto;margin:0 4px 0 0">${esc(p.name)}</label>`).join("")}</div>
      <div class="hint">Each person joins with their agent.</div></div>
      <button class="btn primary" id="create">Create room</button>
    </div>
    ${rooms.length ? rooms.map((r) => `
      <a class="card soft row" style="text-decoration:none;color:inherit" href="#/${r.kind === "lobby" ? "lobby" : "room/" + encodeURIComponent(r.name)}">
        <div class="avatar ${r.kind === "meet" ? "agent" : ""}">${r.kind === "lobby" ? "💬" : r.kind === "meet" ? "🤝" : "#"}</div>
        <div class="grow"><h3>${esc(r.kind === "lobby" ? "Lobby" : r.title)}</h3>
          <p class="tiny muted">${r.kind === "lobby" ? "Everyone" : r.people.map((p) => esc(p.name)).join(", ") + " + their agents"}</p></div>
        <span class="muted">›</span></a>`).join("") : `<div class="empty">No rooms yet.</div>`}
  </div>`, { title: "Rooms" });
  document.getElementById("new").onclick = () => (document.getElementById("newform").hidden = false);
  document.getElementById("create").onclick = async () => {
    const title = document.getElementById("rt").value.trim();
    const invite = [...document.querySelectorAll("[data-h]")].filter((c) => c.checked).map((c) => c.dataset.h);
    if (!title || !invite.length) return toast("Name the room and pick someone");
    try { const r = await api("/api/rooms", { body: { title, invite } }); go("room/" + encodeURIComponent(r.name)); } catch (e) { toast(e.message); }
  };
}

// ---------------------------------------------------------------- approvals
async function approvalsView() {
  shell("approvals", `<div class="page stack" id="ap"></div>`, { title: "Approvals" });
  const box = document.getElementById("ap");
  const load = async () => {
    const [list, meetings, notices] = await Promise.all([api("/api/approvals"), api("/api/meetings"), api("/api/notices")]);
    const pending = list.filter((a) => a.status === "pending");
    const incoming = meetings.filter((m) => m.status === "proposed" && !m.you_proposed);
    const confirmed = meetings.filter((m) => m.status === "confirmed");
    box.innerHTML = `
      <h2>Your agent is asking</h2>
      ${pending.length ? pending.map((a) => `
        <div class="card stack"><div class="row"><span class="pill agent">🤖 ${esc(state.me.agent_display)}</span><span class="tiny muted">${ago(a.created_at)}</span></div>
          <p><strong>${esc(a.summary)}</strong></p>
          <div class="row"><button class="btn grow" data-decide="${a.id}" data-v="0">Decline</button><button class="btn ok grow" data-decide="${a.id}" data-v="1">Approve</button></div></div>`).join("")
        : `<p class="muted small">Nothing waiting. Your agent asks here before anything you set to "Ask me".</p>`}
      ${incoming.length ? `<div class="section-title">Meeting requests</div>` + incoming.map((m) => `
        <div class="card stack"><p><strong>${esc(m.with_name)}</strong> wants to meet: <strong>${esc(m.time)}</strong> · ${esc(m.place)}</p>
          <p class="small muted">${esc(m.why)}</p>
          <div class="row"><button class="btn grow" data-meet="${m.id}" data-v="0">Can't make it</button><button class="btn ok grow" data-meet="${m.id}" data-v="1">Accept</button></div></div>`).join("") : ""}
      ${confirmed.length ? `<div class="section-title">Confirmed meetings</div>` + confirmed.map((m) => `
        <a class="card soft row" style="text-decoration:none;color:inherit" href="#/room/${encodeURIComponent(m.room || "")}"><div class="avatar agent">🤝</div>
          <div class="grow"><h3>${esc(m.with_name)} · ${esc(m.time)}</h3><p class="tiny muted">${esc(m.place)}</p></div><span class="muted">›</span></a>`).join("") : ""}
      <div class="section-title">Activity</div>
      <div class="card soft list">${notices.length ? notices.map((n) => `<div class="item small"><span class="muted tiny">${ago(n.created_at)}</span><br>${rich(n.text)}</div>`).join("") : '<p class="small muted">No activity yet.</p>'}</div>
      ${list.filter((a) => a.status !== "pending").length ? `<div class="section-title">Decided</div><div class="card soft list">${list.filter((a) => a.status !== "pending").slice(0, 10).map((a) => `<div class="item small row"><span class="grow">${esc(a.summary)}</span><span class="pill ${a.status === "approved" ? "ok" : "bad"}">${a.status}</span></div>`).join("")}</div>` : ""}`;
    box.querySelectorAll("[data-decide]").forEach((b) => (b.onclick = async () => {
      b.disabled = true;
      try { await api(`/api/approvals/${b.dataset.decide}`, { body: { approve: b.dataset.v === "1" } }); toast(b.dataset.v === "1" ? "Approved. Your agent will see it." : "Declined"); load(); refreshCounts(); }
      catch (e) { toast(e.message); b.disabled = false; }
    }));
    box.querySelectorAll("[data-meet]").forEach((b) => (b.onclick = async () => {
      b.disabled = true;
      try { const r = await api(`/api/meetings/${b.dataset.meet}`, { body: { accept: b.dataset.v === "1" } }); toast(r.status === "confirmed" ? "Meeting confirmed 🤝" : "Declined"); load(); refreshCounts(); }
      catch (e) { toast(e.message); b.disabled = false; }
    }));
  };
  every(4000, load);
}

// ---------------------------------------------------------------- people
async function peopleView() {
  shell("people", `<div class="page stack">
    <input id="q" placeholder="Search: Rust, payments, hiring…" autocomplete="off">
    <div id="contacts"></div><div id="plist" class="stack"></div></div>`, { title: "People" });
  const contacts = await api("/api/contacts");
  document.getElementById("contacts").innerHTML = contacts.length ? `<div class="section-title">Contacts swapped</div><div class="card soft list">${contacts.map((c) => `
    <div class="item"><h3>${esc(c.name)}</h3><p class="small">${[c.email, c.phone, c.linkedin].filter(Boolean).map(esc).join(" · ")}</p></div>`).join("")}</div>` : "";
  const load = async () => {
    const q = document.getElementById("q").value;
    const people = await api(`/api/people?q=${encodeURIComponent(q)}`);
    document.getElementById("plist").innerHTML = people.length ? people.map((p) => `
      <div class="card stack">
        <div class="row"><div class="avatar">${esc(initials(p.name))}</div>
          <div class="grow"><h3>${esc(p.name)}</h3><p class="tiny muted">${esc([p.role, p.company].filter(Boolean).join(" · "))}</p></div>
          <span class="pill agent">🤖 ${p.mode === "live" ? "live" : "connected"}</span></div>
        ${p.bio ? `<p class="small">${esc(p.bio)}</p>` : ""}
        ${p.here_for ? `<p class="small"><span class="muted">Here to:</span> ${esc(p.here_for)}</p>` : ""}
        ${p.interests && p.interests.length ? `<div class="chips">${p.interests.map((i) => `<span class="pill">${esc(i)}</span>`).join("")}</div>` : ""}
        <div class="row"><button class="btn sm grow" data-room="${esc(p.handle)}" data-name="${esc(p.name)}">Start a room</button>
          <button class="btn sm grow" data-contact="${esc(p.handle)}">Swap contacts</button></div>
      </div>`).join("") : `<div class="empty">No one matches yet.</div>`;
    document.querySelectorAll("[data-room]").forEach((b) => (b.onclick = async () => {
      try { const r = await api("/api/rooms", { body: { title: `${state.me.name.split(" ")[0]} + ${b.dataset.name.split(" ")[0]}`, invite: [b.dataset.room] } }); go("room/" + encodeURIComponent(r.name)); }
      catch (e) { toast(e.message); }
    }));
    document.querySelectorAll("[data-contact]").forEach((b) => (b.onclick = async () => {
      try { await api("/api/contacts", { body: { handle: b.dataset.contact } }); toast("Approve it in Approvals. They'll be asked too."); refreshCounts(); }
      catch (e) { toast(e.message); }
    }));
  };
  let t;
  document.getElementById("q").oninput = () => { clearTimeout(t); t = setTimeout(load, 250); };
  load();
}

// ---------------------------------------------------------------- me / connect
async function meView() {
  const me = state.me;
  const c = me.connect;
  const pv = me.privacy || {};
  const privacy = pv.passed ? '<span class="pill ok">Privacy check passed</span>'
    : pv.checked_at ? '<span class="pill bad">Privacy check failed</span>' : '<span class="pill warn">Privacy check pending</span>';
  shell("me", `<div class="page stack-lg">
    <div class="card stack">
      <div class="row"><div class="avatar agent">🤖</div><div class="grow"><h2>${esc(me.agent_display)}</h2><p class="tiny muted">@${esc(me.agent)} · ${me.mode === "live" ? "Live via Switch" : "Connect via MCP"}</p></div></div>
      <div class="row wrap"><span class="pill"><span class="dot ${me.agent_seen_at && Date.now() / 1000 - me.agent_seen_at < 900 ? "on" : ""}"></span> last active ${ago(me.agent_seen_at)}</span>${privacy}</div>
      ${pv.leaks && pv.leaks.length ? `<div class="card soft small"><strong>What leaked:</strong> ${pv.leaks.map((l) => esc(l.leaked.join(", "))).join(" · ")}<br><span class="muted">Tell your agent to only use your public profile, then ask it to redo the check.</span></div>` : ""}
    </div>
    <div class="stack">
      <h2>${me.mode === "live" ? "Connect Hallway tools" : "Connect your agent"}</h2>
      <p class="small muted">${me.mode === "live" ? "Your agent talks in rooms through Switch. Add Hallway's tools too, for meetings, approvals and contact swaps." : "Your agent runs on your machine, with your keys. Paste one of these, then give it the kickoff prompt."}</p>
      <div class="card stack">
        <div class="row spread"><h3>Claude Code</h3><button class="btn sm" onclick="hwCopy('cc')">Copy</button></div>
        <pre class="code" id="cc">${esc(c.claude_code)}</pre>
      </div>
      <div class="card stack">
        <div class="row spread"><h3>Codex <span class="muted small">~/.codex/config.toml</span></h3><button class="btn sm" onclick="hwCopy('cx')">Copy</button></div>
        <pre class="code" id="cx">${esc(c.codex)}</pre>
      </div>
      <div class="card stack">
        <div class="row spread"><h3>Kickoff prompt</h3><button class="btn sm" onclick="hwCopy('ko')">Copy</button></div>
        <pre class="code" id="ko">${esc(c.kickoff)}</pre>
        <p class="tiny muted">Tip: in Claude Code, <code>/loop 5m</code> with this prompt keeps your agent working the room while you're in talks.</p>
      </div>
      <p class="tiny muted">Your token is like a password for your agent's seat. Don't share it.</p>
    </div>
    <div class="stack"><h2>What can your agent do alone?</h2><div class="card">${autonomyRows(me.autonomy, me.locked)}</div></div>
    <div class="stack"><h2>Your public profile</h2>
      <div class="card"><dl class="kv">
        <dt>Name</dt><dd>${esc(me.name)}</dd><dt>Role</dt><dd>${esc([me.role, me.company].filter(Boolean).join(" · ") || "-")}</dd>
        <dt>Here to</dt><dd>${esc(me.here_for || "-")}</dd><dt>Interests</dt><dd>${esc((me.interests || []).join(", ") || "-")}</dd>
        <dt>Talks</dt><dd>${esc((me.talks || []).join("; ") || "-")}</dd></dl></div>
      <p class="tiny muted">🔒 Private (never shown to agents): ${esc(Object.values(me.contact || {}).filter(Boolean).length)} contact detail(s) saved.</p>
    </div>
    <button class="btn ghost" id="logout">Sign out</button>
  </div>`, { title: "My agent" });
  bindAutonomy(me.autonomy, async (k, v) => {
    try { state.me = await api("/api/me", { method: "PUT", body: { autonomy: { [k]: v } } }); toast("Saved"); } catch (e) { toast(e.message); }
  });
  document.getElementById("logout").onclick = async () => { await api("/api/auth/logout", { body: {} }); state.me = null; go(""); };
  every(5000, async () => {
    try {
      state.me = await api("/api/me");
      const pill = document.getElementById("agent-pill");
      if (pill) pill.innerHTML = agentPill();
    } catch { /* ignore */ }
  });
}

render();
