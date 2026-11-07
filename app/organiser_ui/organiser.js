// The organiser dashboard (app/organiser.py). Everything an attendee typed
// (names, handles) goes into the page as text, never as HTML.

const $ = (id) => document.getElementById(id);
let csrf = null;
let data = null;

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids) if (kid != null) n.append(kid);
  return n;
}

const usd = (x) => `$${Number(x || 0).toFixed(2)}`;

function toast(text, bad = false) {
  const t = $("toast");
  t.textContent = text;
  t.className = bad ? "toast bad" : "toast";
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 3500);
}

async function api(method, path, body) {
  const headers = { "content-type": "application/json" };
  if (csrf) headers["x-csrf-token"] = csrf;
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" });
  const out = await r.json().catch(() => ({}));
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Signed out."); }
  if (!r.ok) throw new Error(out.error || out.detail || `HTTP ${r.status}`);
  return out;
}

// ------------------------------------------------------------ session
function showLogin() {
  csrf = null;
  $("dash").hidden = true;
  $("logout").hidden = true;
  $("login").hidden = false;
  $("password").focus();
}

async function showDash() {
  $("login").hidden = true;
  $("dash").hidden = false;
  $("logout").hidden = false;
  await Promise.all([refresh(), loadKeys()]);
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  try {
    const out = await api("POST", "/api/login", { password: $("password").value });
    csrf = out.csrf;
    $("password").value = "";
    await showDash();
  } catch (err) {
    $("login-error").textContent = err.message;
  }
});

$("logout").addEventListener("click", async () => {
  try { await api("POST", "/api/logout"); } finally { showLogin(); }
});

// ------------------------------------------------------------ tabs
for (const tab of document.querySelectorAll("[data-tab]")) {
  tab.addEventListener("click", () => {
    for (const t of document.querySelectorAll("[data-tab]")) t.setAttribute("aria-selected", String(t === tab));
    for (const p of document.querySelectorAll("[data-panel]")) p.hidden = p.dataset.panel !== tab.dataset.tab;
  });
}

// ------------------------------------------------------------ overview
async function refresh() {
  data = await api("GET", "/api/overview");
  $("event-name").textContent = data.event.name;
  renderTiles();
  renderPeople();
  if (!document.activeElement || !$("event-form").contains(document.activeElement)) renderEvent();
  renderInvite();
}

function tile(k, v, s, cls = "", bar = null) {
  return el("div", { class: `tile ${cls}` },
    el("div", { class: "k", text: k }),
    el("div", { class: "v", text: v }),
    el("div", { class: "s", text: s }),
    bar == null ? null : el("div", { class: "bar" }, meter(bar)));
}

function meter(fraction) {
  const i = el("i");
  i.style.width = `${Math.min(100, Math.round(fraction * 100))}%`;  // CSSOM, which the CSP allows (a style attribute isn't)
  return i;
}

function renderTiles() {
  const c = data.counts, d = data.daypass;
  const used = d.budget_usd ? d.spent_usd / d.budget_usd : 0;
  $("tiles").replaceChildren(
    tile("Attendees", c.attendees, `${c.personas} demo personas · ${c.modes.connect} connect, ${c.modes.live} live, ${c.modes.daypass} day pass`),
    tile("Agents online", c.online, `of ${c.attendees + c.personas} in the hallway`),
    tile("Day passes", d.active, `active · ${d.paused} paused · ${d.spent_out} out of budget · ${d.runs} runs`),
    tile("Day pass spend", usd(d.spent_usd), `of ${usd(d.budget_usd)} budgeted (${usd(d.spent_all_time_usd)} all time)`, "signal", used),
  );
}

function agentChip(a) {
  if (a.online) return el("span", { class: "chip go" }, el("span", { class: "dot" }), "online");
  const label = { active: "seen recently", idle: "idle", never: "never checked in" }[a.agent_status] || a.agent_status;
  return el("span", { class: a.agent_status === "never" ? "chip wait" : "chip" }, label);
}

function passChip(p) {
  if (!p) return el("span", { class: "muted", text: "—" });
  const cls = { active: "chip go", paused: "chip wait", spent: "chip stop" }[p.status] || "chip";
  return el("span", { class: cls, text: p.status === "spent" ? "out of budget" : p.status });
}

async function act(fn, done) {
  try { await fn(); toast(done); await refresh(); } catch (err) { toast(err.message, true); }
}

function passActions(a) {
  const h = encodeURIComponent(a.handle);
  if (a.mode === "live") return el("span", { class: "muted small", text: "Runs in Switch" });
  if (!a.pass) {
    return el("button", {
      class: "btn signal sm",
      disabled: !data.event.daypass_enabled,
      title: data.event.daypass_enabled ? "" : "Turn day passes on in Event first",
      onclick: () => act(() => api("POST", `/api/passes/${h}/issue`), `Day pass issued to @${a.handle}`),
    }, "Issue pass");
  }
  const cap = el("input", { class: "cap-input", type: "number", min: "0", max: "1000", step: "0.05", value: a.pass.cap_usd, "aria-label": `Budget cap for @${a.handle}` });
  return el("div", { class: "cell-actions" },
    cap,
    el("button", { class: "btn ghost sm", onclick: () => act(() => api("POST", `/api/passes/${h}`, { cap_usd: Number(cap.value) }), "Cap saved") }, "Set cap"),
    a.pass.status === "spent" ? null : el("button", {
      class: "btn ghost sm",
      onclick: () => act(() => api("POST", `/api/passes/${h}`, { paused: a.pass.status === "active" }), a.pass.status === "active" ? "Paused" : "Resumed"),
    }, a.pass.status === "active" ? "Pause" : "Resume"),
    el("button", {
      class: "btn danger sm",
      onclick: () => {
        if (confirm(`Revoke @${a.handle}'s day pass? Their agent stops now and they're back on their own agent (Connect).`)) {
          act(() => api("POST", `/api/passes/${h}/revoke`), `Day pass revoked from @${a.handle}`);
        }
      },
    }, "Revoke"),
  );
}

function renderPeople() {
  const q = $("filter").value.trim().toLowerCase();
  const rows = data.attendees.filter((a) => !q || a.name.toLowerCase().includes(q) || a.handle.includes(q));
  const modeLabel = { connect: "Connect", live: "Live", daypass: "Day pass" };
  $("people").replaceChildren(...rows.map((a) => el("tr", {},
    el("td", { class: "who" }, el("b", { text: a.name }), el("span", { text: `@${a.handle}${a.persona ? " · persona" : ""}` })),
    el("td", {}, el("span", { class: a.mode === "daypass" ? "chip signal" : "chip", text: modeLabel[a.mode] || a.mode })),
    el("td", {}, agentChip(a)),
    el("td", {}, passChip(a.pass)),
    el("td", { class: "num", text: a.pass ? `${usd(a.pass.spent_usd)} / ${usd(a.pass.cap_usd)}` : "" }),
    el("td", {}, passActions(a)),
  )));
  if (!rows.length) $("people").append(el("tr", {}, el("td", { colspan: "6", class: "muted", text: q ? "Nobody matches." : "Nobody has joined yet." })));
}
$("filter").addEventListener("input", () => data && renderPeople());

// ------------------------------------------------------------ event
function agendaRow(item = { time: "", title: "", room: "" }) {
  const row = el("div", { class: "agenda-row" },
    el("input", { "data-f": "time", value: item.time, placeholder: "10:00", pattern: "([01][0-9]|2[0-3]):[0-5][0-9]", required: true, "aria-label": "Time" }),
    el("input", { "data-f": "title", value: item.title, placeholder: "Session title", maxlength: "100", required: true, "aria-label": "Title" }),
    el("input", { "data-f": "room", value: item.room, placeholder: "Room", maxlength: "60", "aria-label": "Room" }),
  );
  row.append(el("button", { type: "button", class: "btn ghost sm", "aria-label": "Remove session", onclick: () => row.remove() }, "Remove"));
  return row;
}

function renderEvent() {
  $("ev-name").value = data.event.name;
  $("ev-daypass").checked = data.event.daypass_enabled;
  $("ev-cap").value = data.event.daypass_cap_usd;
  $("agenda").replaceChildren(...data.event.agenda.map(agendaRow));
}

$("agenda-add").addEventListener("click", () => $("agenda").append(agendaRow()));

$("event-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const agenda = [...$("agenda").children].map((row) => Object.fromEntries(
    [...row.querySelectorAll("input")].map((i) => [i.dataset.f, i.value.trim()])));
  try {
    data = await api("PUT", "/api/event", {
      name: $("ev-name").value,
      daypass_enabled: $("ev-daypass").checked,
      daypass_cap_usd: Number($("ev-cap").value),
      agenda,
    });
    $("event-name").textContent = data.event.name;
    renderEvent(); renderTiles(); renderPeople();
    toast("Event saved");
  } catch (err) { toast(err.message, true); }
});

function renderInvite() {
  const i = data.invite;
  $("invite").replaceChildren(
    el("h2", { text: "Invites" }),
    el("p", {}, i.invite_only ? "Invite link: " : "No invite code: anyone with the address can join.",
      i.invite_url ? el("code", { text: i.invite_url }) : null),
    el("p", { class: "muted small", text: `${i.joined} joined${i.spots_left == null ? "" : ` · ${i.spots_left} of ${i.invite_limit} spots left`}. Rotate the code with scripts/hallway_admin.py invite --rotate.` }),
  );
}

// ------------------------------------------------------------ keys
async function loadKeys() {
  renderKeys(await api("GET", "/api/providers"));
}

let loadedOptions = {};

function renderKeys(s) {
  loadedOptions = { DAYPASS_MODEL: s.options.DAYPASS_MODEL.value, DAYPASS_PRICE_PER_MTOK: s.options.DAYPASS_PRICE_PER_MTOK.value };
  $("opt-model").value = loadedOptions.DAYPASS_MODEL;
  $("opt-price").value = loadedOptions.DAYPASS_PRICE_PER_MTOK;
  $("keys-file").textContent = `Stored in ${s.file} (readable by its owner only). The day pass worker and research lookups pick changes up without a restart; .env is the fallback.`;
  $("keys").replaceChildren(...Object.entries(s.keys).map(([name, k]) => {
    const input = el("input", { type: "password", autocomplete: "off", "data-key": name, maxlength: "400", placeholder: k.set ? "Leave empty to keep" : "Paste a key", "aria-label": name });
    const result = el("p", { class: "test-result", role: "status" });
    const state = k.set
      ? el("span", { class: "chip go", text: `set · …${k.last4 || "••••"} · ${k.source === "dashboard" ? "dashboard" : ".env"}` })
      : el("span", { class: "chip wait", text: "not set" });
    return el("div", { class: "key-row" },
      el("div", { class: "meta" }, el("b", { text: name }), el("span", { class: "muted", text: k.label }), el("div", {}, state)),
      input,
      el("div", { class: "btns" },
        el("button", {
          type: "button", class: "btn ghost sm", disabled: !k.set,
          onclick: async () => {
            result.className = "test-result"; result.textContent = "Testing…";
            try {
              const r = await api("POST", `/api/providers/${name}/test`);
              result.className = `test-result ${r.ok ? "ok" : "bad"}`; result.textContent = r.detail;
            } catch (err) { result.className = "test-result bad"; result.textContent = err.message; }
          },
        }, name === "EXA_API_KEY" ? "Test (~$0.005)" : "Test"),
        k.source === "dashboard" ? el("button", {
          type: "button", class: "btn danger sm",
          onclick: async () => {
            try { renderKeys(await api("PUT", "/api/providers", { keys: { [name]: "" } })); toast(`${name} removed from the dashboard`); }
            catch (err) { toast(err.message, true); }
          },
        }, "Clear") : null),
      result);
  }));
}

$("keys-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const keys = {};
  for (const i of $("keys").querySelectorAll("input[data-key]")) if (i.value.trim()) keys[i.dataset.key] = i.value.trim();
  // Only what changed: an untouched field keeps its source (.env stays .env). Emptied: back to .env.
  const options = {};
  for (const [name, input] of [["DAYPASS_MODEL", $("opt-model")], ["DAYPASS_PRICE_PER_MTOK", $("opt-price")]]) {
    if (input.value.trim() !== loadedOptions[name]) options[name] = input.value.trim();
  }
  try {
    renderKeys(await api("PUT", "/api/providers", { keys, options }));
    toast("Saved");
  } catch (err) { toast(err.message, true); }
});

// ------------------------------------------------------------ boot
(async () => {
  const s = await api("GET", "/api/session").catch(() => ({ signed_in: false }));
  if (s.signed_in) { csrf = s.csrf; await showDash(); } else showLogin();
  setInterval(() => { if (csrf && !$("dash").hidden) refresh().catch(() => {}); }, 15000);
})();
