// Splitter frontend — talks to the Flask REST API.
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// Any API call that comes back 401 means the session expired — bounce to login.
const _fetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const r = await _fetch(...args);
  if (r.status === 401) window.location.href = "/login";
  return r;
};

let CFG = {};
let IFACES = [];
let SETTINGS = { subinterface_enabled: false };  // tool-wide settings
let ME = null;        // current user {username, role}
let EDITING = null;   // domain currently being edited, or null
let EDIT_HAS_CERT = false;   // does the mapping being edited terminate TLS?

const isAdmin = () => ME && ME.role === "admin";

// A mapping is identified by domain + listen port (a domain can map on several
// ports). This composite key is used for selection, health/traffic cells and to
// address a specific mapping in the API.
const mkey = (m) => `${m.domain}:${m.listen_port || 443}`;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --- page navigation -------------------------------------------------------
const PAGES = ["mappings", "form", "users", "activity", "monitoring", "livemap", "interfaces", "docker", "access", "tools", "ssl", "backup", "waf", "firewall"];
// Pages whose data is loaded lazily on first visit (see showPage).
const PAGE_LOADED = new Set();
function showPage(name) {
  if (!PAGES.includes(name)) name = "mappings";
  history.replaceState(null, "", "#" + name);
  PAGES.forEach((p) => {
    const sec = $("#page-" + p);
    if (sec) sec.classList.toggle("hidden", p !== name);
  });
  if (name === "activity") loadActivity();
  if (name === "backup") loadBackups();
  if (name === "waf") loadWaf();
  if (name === "ssl") loadSslCerts();
  if (name === "access") loadAccessLists();
  if (name === "firewall") loadFirewall();
  if (name === "docker") loadDocker();
  // Heavy list pages: load their data on first access (not eagerly on refresh),
  // then keep it fresh via the pollers / the page's Refresh button.
  if (name === "mappings" && !PAGE_LOADED.has("mappings")) {
    PAGE_LOADED.add("mappings");
    loadMappings();
    startHealthPolling();
  }
  if (name === "users" && !PAGE_LOADED.has("users")) {
    PAGE_LOADED.add("users");
    loadUsers();
  }
  // Poll host metrics + per-interface throughput only while Monitoring is open.
  if (name === "monitoring") startMonitoring();
  else stopMonitoring();
  // Live routing map only while the Live Map page is open.
  if (name === "livemap") startLivemap();
  else stopLivemap();
  // The Interfaces page renders its sub-interface overview.
  if (name === "interfaces") startInterfaces();
  else stopInterfaces();
  // Tools page: initialise tab strip on first visit.
  if (name === "tools") startTools();
  // Live traffic sparklines only matter while the mappings table is visible.
  if (name === "mappings") startTrafficPolling();
  else stopTrafficPolling();
  $$(".nav-link").forEach((b) => b.classList.toggle("active", b.dataset.page === name));
  // restart the fade-in animation on the now-visible page
  const active = $("#page-" + name);
  if (active) { active.classList.remove("page"); void active.offsetWidth; active.classList.add("page"); }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function toast(message, ok = true) {
  const t = $("#toast");
  t.textContent = message;
  t.className =
    "fixed bottom-6 right-6 max-w-sm rounded-lg px-4 py-3 text-sm shadow-lg " +
    (ok ? "bg-emerald-600 text-white" : "bg-red-600 text-white");
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 5000);
}

// --- action status: loading → success / error (centered) -------------------
// A small inline spinner for buttons, plus a centered status card any async
// action can drive: showStatus("loading" | "success" | "error", title, msg).
// Success auto-dismisses; an error stays up (with the reason) until closed.
const BTN_SPINNER =
  '<span class="inline-block h-4 w-4 mr-2 align-[-3px] rounded-full border-2 border-white/40 border-t-white animate-spin"></span>';

function setBtnLoading(btn, loading, loadingHtml) {
  if (!btn) return;
  if (loading) {
    if (btn.dataset._html == null) btn.dataset._html = btn.innerHTML;
    btn.disabled = true;
    btn.classList.add("opacity-70", "cursor-not-allowed");
    btn.innerHTML = BTN_SPINNER + (loadingHtml || "Working…");
  } else {
    btn.disabled = false;
    btn.classList.remove("opacity-70", "cursor-not-allowed");
    if (btn.dataset._html != null) { btn.innerHTML = btn.dataset._html; delete btn.dataset._html; }
  }
}

function _statusEl() {
  let el = $("#action-status");
  if (!el) {
    el = document.createElement("div");
    el.id = "action-status";
    el.className = "fixed inset-0 z-[9998] hidden items-center justify-center bg-slate-900/40 backdrop-blur-[2px] p-4";
    el.innerHTML = `
      <div class="w-full max-w-sm rounded-2xl bg-white shadow-2xl border border-slate-200 p-6 text-center">
        <div id="action-status-icon" class="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full"></div>
        <h3 id="action-status-title" class="text-base font-semibold text-slate-800"></h3>
        <p id="action-status-msg" class="mt-1.5 text-sm text-slate-500 whitespace-pre-wrap break-words"></p>
        <button id="action-status-close" class="mt-5 hidden rounded-lg bg-slate-200 hover:bg-slate-300 text-slate-700 text-sm font-semibold px-4 py-2">Close</button>
      </div>`;
    document.body.appendChild(el);
    // Dismiss an error by clicking the backdrop or Close (loading can't be closed).
    el.addEventListener("click", (e) => {
      if ((e.target === el || e.target.id === "action-status-close") &&
          el.dataset.state !== "loading") hideStatus();
    });
  }
  return el;
}

function showStatus(state, title, message = "") {
  clearTimeout(showStatus._t);
  const el = _statusEl();
  el.dataset.state = state;
  const icon = $("#action-status-icon");
  const close = $("#action-status-close");
  $("#action-status-title").textContent = title;
  const msg = $("#action-status-msg");
  msg.textContent = message;
  msg.classList.toggle("hidden", !message);
  el.classList.remove("hidden");
  el.classList.add("flex");
  if (state === "loading") {
    icon.className = "mx-auto mb-4 flex h-14 w-14 items-center justify-center";
    icon.innerHTML = '<span class="h-11 w-11 rounded-full border-4 border-slate-200 border-t-emerald-500 animate-spin"></span>';
    close.classList.add("hidden");
  } else if (state === "success") {
    icon.className = "mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-emerald-100 text-emerald-600 text-2xl font-bold";
    icon.textContent = "✓";
    close.classList.add("hidden");
    showStatus._t = setTimeout(hideStatus, 1800);
  } else { // error
    icon.className = "mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-red-100 text-red-600 text-2xl font-bold";
    icon.textContent = "✗";
    close.classList.remove("hidden");
  }
}

function hideStatus() {
  clearTimeout(showStatus._t);
  const el = $("#action-status");
  if (el) { el.classList.add("hidden"); el.classList.remove("flex"); el.dataset.state = ""; }
}

// --- config + interfaces ---------------------------------------------------
async function loadConfig() {
  CFG = (await (await fetch("/api/config")).json()).config || {};
  $("#bind_prefix").value = CFG.bind_prefix || "24";
  const badge = $("#mode-badge");
  const base = "text-[10px] font-semibold inline-block mt-0.5 px-2 py-0.5 rounded-full ";
  if (CFG.simulate) {
    badge.textContent = "SIMULATION";
    badge.className = base + "bg-amber-500 text-slate-900";
  } else {
    badge.textContent = "● LIVE";
    badge.className = base + "bg-emerald-600 text-white";
  }
}

async function loadInterfaces() {
  IFACES = (await (await fetch("/api/interfaces")).json()).interfaces || [];
  const sel = $("#interface");
  sel.innerHTML = "";
  if (!IFACES.length) {
    sel.innerHTML = '<option value="">no interfaces found</option>';
    return;
  }
  for (const i of IFACES) {
    const o = document.createElement("option");
    o.value = i.name;
    const ips = (i.addresses || []).map((a) => a.ip).join(", ");
    o.textContent = `${i.name}${i.up ? "" : " (down)"}${ips ? " · " + ips : ""}`;
    if (i.name === CFG.nic) o.selected = true;
    sel.appendChild(o);
  }
  updateIfaceInfo();
}

function updateIfaceInfo() {
  const name = $("#interface").value;
  const i = IFACES.find((x) => x.name === name);
  const info = $("#iface-info");
  if (!i) { info.textContent = ""; return; }
  const a = (i.addresses || [])[0];
  info.textContent = `state ${i.state}` + (i.mac ? ` · ${i.mac}` : "") +
    (a ? ` · ${a.ip}/${a.prefix}` : "");
  if (a && a.prefix) $("#bind_prefix").value = a.prefix;
}

async function generateMac() {
  try {
    const j = await (await fetch("/api/random-mac")).json();
    if (j.ok) $("#mac").value = j.mac;
  } catch (_) { /* ignore */ }
}

// --- allocation method toggle ---------------------------------------------
function onMethodChange() {
  const m = document.querySelector('input[name="alloc_method"]:checked').value;
  const isDhcp = m === "dhcp";
  $("#static-row").classList.toggle("hidden", isDhcp);
  $("#dhcp-note").classList.toggle("hidden", !isDhcp);
  // bind_ip is no longer typed on the mapping form — never leave it required
  // (it would block submit while hidden).
  $("#bind_ip").required = false;
}

// --- sub-interface toggle: adapt the mapping form --------------------------
// A mapping never creates a device now. OFF → bind the interface's existing IP
// directly; ON → pick a managed sub-interface from the dropdown. The old
// per-mapping create fields (#subiface-fields) are always hidden.
function applyFormMode() {
  const on = !!SETTINGS.subinterface_enabled;
  const fields = $("#subiface-fields");
  if (fields) fields.classList.add("hidden");
  const pick = $("#subiface-pick");
  if (pick) pick.classList.toggle("hidden", !on);
  // ON → bind a managed sub-interface (the picker implies its parent), so hide
  // the Parent Interface select; OFF → direct-bind needs it.
  const parent = $("#parent-iface-block");
  if (parent) parent.classList.toggle("hidden", on);
  const note = $("#direct-bind-note");
  if (note) note.classList.toggle("hidden", on);
  const bi = $("#bind_ip");
  if (bi) bi.required = false;
}

async function loadSettings() {
  try {
    const j = await (await fetch("/api/settings")).json();
    if (j.ok && j.settings) SETTINGS = j.settings;
  } catch (_) { /* keep defaults */ }
  applyFormMode();
}

async function saveSubifaceSetting(enabled) {
  const msg = $("#subiface-toggle-msg");
  const fd = new FormData();
  fd.append("subinterface_enabled", enabled ? "1" : "0");
  try {
    const j = await (await fetch("/api/settings", { method: "POST", body: fd })).json();
    if (!j.ok) throw new Error(j.error || "Failed to save");
    SETTINGS = j.settings;
    applyFormMode();
    applySubifaceManagerVisibility();
    if (SETTINGS.subinterface_enabled) loadSubinterfaces();
    if (msg) {
      msg.textContent = enabled
        ? "On — new mappings provision a macvlan sub-interface."
        : "Off — new mappings bind to the interface's existing IP.";
      msg.className = "text-xs mt-3 text-slate-500";
      msg.classList.remove("hidden");
    }
  } catch (e) {
    $("#subiface-toggle").checked = !!SETTINGS.subinterface_enabled;  // revert
    if (msg) { msg.textContent = String(e.message || e); msg.className = "text-xs mt-3 text-red-600"; msg.classList.remove("hidden"); }
    toast(String(e.message || e), false);
  }
}

// --- dynamic backend rows (with per-server params) -------------------------
// Split a stored "host:port" backend into its host and port parts. The host may
// itself be an IPv6 literal, so split on the LAST colon.
function splitHostPort(server) {
  const s = (server || "").trim();
  const i = s.lastIndexOf(":");
  if (i === -1) return { host: s, port: "" };
  return { host: s.slice(0, i), port: s.slice(i + 1) };
}

// ==========================================================================
// Docker page — discover containers and build a mapping (auto-reconciling
// container backends). See docker_detect.py / docker_reconcile.py.
// ==========================================================================
let DOCKER_CONTAINERS = [];       // containers (standalone) OR services (swarm)
let DOCKER_SWARM = false;         // swarm-manager mode?
const DOCKER_SEL = new Set();     // selected container/service names -> pool
const DOCKER_PORTS = {};          // name -> chosen backend port (editable)

// First usable port for a container (exposed port) or service (published port).
function dockerFirstPort(name) {
  const c = DOCKER_CONTAINERS.find((x) => x.name === name);
  if (!c || !c.ports || !c.ports.length) return "";
  const p = c.ports[0];
  return (p && typeof p === "object") ? (p.published || "") : p;
}

// Reveal the Docker nav item only when the daemon socket is reachable.
async function refreshDockerNav() {
  const nav = $("#nav-docker");
  if (!nav) return;
  try {
    const j = await (await fetch("/api/docker/status")).json();
    nav.classList.toggle("hidden", !j.available);
    const c = $("#nav-docker-count");
    if (c) c.textContent = j.count || 0;
  } catch (_e) {
    nav.classList.add("hidden");
  }
}

async function loadDocker() {
  const wrap = $("#docker-containers");
  const unavail = $("#docker-unavailable");
  const empty = $("#docker-empty");
  DOCKER_SEL.clear();
  Object.keys(DOCKER_PORTS).forEach((k) => delete DOCKER_PORTS[k]);
  syncDockerPoolBar();
  // Populate the bind-target dropdown to match the current mode.
  await populateDockerBind();
  try {
    const st = await (await fetch("/api/docker/status")).json();
    if (!st.available) throw new Error("Docker unavailable");
    DOCKER_SWARM = !!st.swarm;
    // Swarm managers list SERVICES (by name, routing-mesh published port);
    // standalone hosts list CONTAINERS.
    const url = DOCKER_SWARM ? "/api/docker/services" : "/api/docker/containers";
    const j = await (await fetch(url)).json();
    if (!j.ok) throw new Error(j.error || "Docker query failed");
    unavail.classList.add("hidden");
    DOCKER_CONTAINERS = DOCKER_SWARM ? (j.services || []) : (j.containers || []);
    const hdr = $("#docker-mode-note");
    if (hdr) hdr.textContent = DOCKER_SWARM
      ? "Swarm manager — showing services (routing-mesh published port; the swarm load-balances replicas)."
      : "Standalone Docker — showing running containers.";
    const c = $("#nav-docker-count"); if (c) c.textContent = DOCKER_CONTAINERS.length;
    empty.classList.toggle("hidden", DOCKER_CONTAINERS.length > 0);
    renderDockerCards();
  } catch (e) {
    DOCKER_CONTAINERS = [];
    wrap.innerHTML = "";
    empty.classList.add("hidden");
    unavail.classList.remove("hidden");
  }
}

async function populateDockerBind() {
  const sel = $("#docker-bind");
  if (!sel) return;
  let opts = "";
  try {
    if (SETTINGS.subinterface_enabled) {
      const j = await (await fetch("/api/subinterfaces")).json();
      (j.subinterfaces || []).forEach((s) => {
        opts += `<option value="${escapeHtml(s.name)}">${escapeHtml(s.name)} · ${escapeHtml(s.bind_ip || "")}</option>`;
      });
    } else {
      const j = await (await fetch("/api/interfaces")).json();
      (j.interfaces || []).forEach((i) => {
        const ip = (i.addresses && i.addresses[0] && i.addresses[0].ip) || "";
        if (ip) opts += `<option value="${escapeHtml(i.name)}">${escapeHtml(i.name)} · ${escapeHtml(ip)}</option>`;
      });
    }
  } catch (_e) { /* leave empty */ }
  sel.innerHTML = opts || `<option value="">(no bind target found)</option>`;
}

function renderDockerCards() {
  const wrap = $("#docker-containers");
  wrap.innerHTML = DOCKER_CONTAINERS.map((c) => {
    const sel = DOCKER_SEL.has(c.name);
    // Port chips: container exposed ports, or service published ports.
    const portVals = DOCKER_SWARM
      ? (c.ports || []).map((p) => p.published)
      : (c.ports || []);
    const chips = portVals.map((p) =>
      `<button type="button" data-port="${p}" class="docker-port text-[11px] font-mono px-1.5 py-0.5 rounded bg-slate-100 hover:bg-emerald-100 text-slate-600">${p}</button>`).join(" ")
      || `<span class="text-xs text-slate-400">${DOCKER_SWARM ? "no published port" : "none exposed"}</span>`;
    // Selectable when: container running, or service has a reachable published port.
    const ok = DOCKER_SWARM ? !!c.reachable : (c.state === "running");
    let meta, badge;
    if (DOCKER_SWARM) {
      const rep = c.replicas === "global" ? "global" : (c.replicas != null ? c.replicas + " replica(s)" : "");
      meta = `<div class="mt-2 text-xs text-slate-600"><span class="text-slate-400">swarm service</span> ${escapeHtml(rep)}${c.reachable ? "" : ' · <span class="text-amber-600">unreachable (no published port)</span>'}</div>`;
      badge = `<span class="text-[10px] uppercase tracking-wide px-2 py-0.5 rounded-full bg-sky-100 text-sky-700">service</span>`;
    } else {
      const ip = (c.ips && c.ips[0] && c.ips[0].ip) || "—";
      const net = (c.ips && c.ips[0] && c.ips[0].network) || "";
      meta = `<div class="mt-2 text-xs text-slate-600"><span class="text-slate-400">IP</span> <span class="font-mono">${escapeHtml(ip)}</span> ${net ? `<span class="text-slate-400">· ${escapeHtml(net)}</span>` : ""}</div>`;
      badge = `<span class="text-[10px] uppercase tracking-wide px-2 py-0.5 rounded-full ${ok ? "bg-emerald-100 text-emerald-700" : "bg-slate-100 text-slate-500"}">${escapeHtml(c.state || "?")}</span>`;
    }
    return `
      <div class="card bg-white/80 rounded-2xl border ${sel ? "border-emerald-400 ring-1 ring-emerald-300" : "border-slate-200/70"} shadow-sm p-4" data-cname="${escapeHtml(c.name)}">
        <div class="flex items-start justify-between gap-2">
          <label class="flex items-center gap-2 min-w-0">
            <input type="checkbox" class="docker-check accent-emerald-600" data-name="${escapeHtml(c.name)}" ${sel ? "checked" : ""} ${ok ? "" : "disabled"}>
            <span class="font-semibold text-slate-800 truncate">${escapeHtml(c.name)}</span>
          </label>
          ${badge}
        </div>
        <div class="mt-2 text-xs text-slate-500 truncate" title="${escapeHtml(c.image || "")}">${escapeHtml(c.image || "")}</div>
        ${meta}
        <div class="mt-1 text-xs text-slate-600 flex items-center gap-1 flex-wrap"><span class="text-slate-400">ports</span> ${chips}</div>
      </div>`;
  }).join("");
  // Wire checkboxes: selecting seeds a default port from the container's first
  // exposed port; a port chip sets that container's backend port.
  $$("#docker-containers .docker-check").forEach((cb) => cb.addEventListener("change", () => {
    const name = cb.dataset.name;
    if (cb.checked) { DOCKER_SEL.add(name); if (!DOCKER_PORTS[name]) DOCKER_PORTS[name] = dockerFirstPort(name); }
    else { DOCKER_SEL.delete(name); delete DOCKER_PORTS[name]; }
    renderDockerCards();
    syncDockerPoolBar();
  }));
  $$("#docker-containers .docker-port").forEach((b) => b.addEventListener("click", () => {
    const name = b.closest("[data-cname]").dataset.cname;
    DOCKER_SEL.add(name);
    DOCKER_PORTS[name] = b.dataset.port;
    renderDockerCards();
    syncDockerPoolBar();
  }));
}

// One editable backend row per selected container (name + IP + its own port).
function renderDockerPoolList() {
  const list = $("#docker-pool-list");
  if (!list) return;
  const names = [...DOCKER_SEL];
  if (names.length === 0) { list.innerHTML = '<div class="text-xs text-slate-400">Select containers below to add them as backends.</div>'; return; }
  list.innerHTML = names.map((name) => {
    const c = DOCKER_CONTAINERS.find((x) => x.name === name) || {};
    const ip = (c.ips && c.ips[0] && c.ips[0].ip) || "—";
    return `
      <div class="be-docker-row flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2" data-name="${escapeHtml(name)}">
        <span class="font-semibold text-sm text-slate-800 truncate flex-1">${escapeHtml(name)}</span>
        <span class="text-xs text-slate-400 font-mono hidden sm:inline">${escapeHtml(ip)}</span>
        <span class="text-slate-300">:</span>
        <input type="number" min="1" max="65535" value="${escapeHtml(String(DOCKER_PORTS[name] || ""))}" placeholder="port"
               class="be-docker-port w-24 rounded-lg border border-slate-200 px-2 py-1 text-sm" data-name="${escapeHtml(name)}">
        <button type="button" class="be-docker-del text-slate-400 hover:text-red-500 text-lg leading-none px-1" data-name="${escapeHtml(name)}" title="Remove">&times;</button>
      </div>`;
  }).join("");
  $$("#docker-pool-list .be-docker-port").forEach((inp) => inp.addEventListener("input", () => {
    DOCKER_PORTS[inp.dataset.name] = inp.value.trim();
  }));
  $$("#docker-pool-list .be-docker-del").forEach((btn) => btn.addEventListener("click", () => {
    DOCKER_SEL.delete(btn.dataset.name); delete DOCKER_PORTS[btn.dataset.name];
    renderDockerCards(); syncDockerPoolBar();
  }));
}

function syncDockerPoolBar() {
  const bar = $("#docker-pool-bar");
  if (bar) bar.classList.toggle("hidden", DOCKER_SEL.size === 0);
  const n = $("#docker-pool-count"); if (n) n.textContent = DOCKER_SEL.size;
  renderDockerPoolList();
}

async function dockerCreateMapping(e) {
  e.preventDefault();
  if (DOCKER_SEL.size === 0) { toast("Select at least one container first.", false); return; }
  const f = e.target;
  const domain = f.domain.value.trim();
  const bind = f.bind.value;
  if (!domain) { toast("Enter a domain.", false); return; }
  if (!bind) { toast("No bind target available.", false); return; }
  const missing = [...DOCKER_SEL].filter((n) => !String(DOCKER_PORTS[n] || "").trim());
  if (missing.length) { toast(`Set a port for: ${missing.join(", ")}`, false); return; }
  const backends = [...DOCKER_SEL].map((name) => ({
    docker_container: name,
    docker_port: Number(DOCKER_PORTS[name]),
  }));
  const fd = new FormData();
  fd.append("domain", domain);
  fd.append("listen_port", f.listen_port.value || "443");
  fd.append("ssl_mode", "none");
  fd.append("lb_method", "round_robin");
  fd.append(SETTINGS.subinterface_enabled ? "subiface" : "interface", bind);
  fd.append("backends_json", JSON.stringify(backends));
  try {
    const r = await fetch("/api/mappings", { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok || !j.ok) { toast(j.error || "Create failed.", false); return; }
    toast(`Mapping ${domain} created from ${backends.length} container(s).`);
    DOCKER_SEL.clear();
    await loadMappings();
    showPage("mappings");
  } catch (err) {
    toast(String(err.message || err), false);
  }
}

function addBackendRow(b) {
  b = b || {};
  const inp = "rounded-md border border-slate-300 px-2 py-1 text-xs font-mono outline-none focus:ring-2 focus:ring-emerald-500";
  const isDocker = !!b.docker_container;
  let { host, port } = splitHostPort(b.server);
  if (isDocker) {                       // show the container NAME, not the cached IP
    host = b.docker_container;
    if (b.docker_port != null && b.docker_port !== "") port = String(b.docker_port);
  }
  const wrap = document.createElement("div");
  wrap.className = "be-row rounded-lg border border-slate-200 p-2";
  if (isDocker) wrap.dataset.dockerContainer = b.docker_container;
  wrap.innerHTML = `
    <div class="flex gap-2 items-center">
      ${isDocker ? '<span class="shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-sky-100 text-sky-700" title="Docker container backend — Splitter stores the name and keeps its IP current on restart">🐳 docker</span>' : ''}
      <div class="be-hostport flex flex-1 items-stretch rounded-lg border border-slate-300 bg-white overflow-hidden focus-within:ring-2 focus-within:ring-emerald-500 focus-within:border-emerald-500">
        <input class="be-host flex-1 min-w-0 px-3 py-2 text-sm font-mono outline-none bg-transparent ${isDocker ? "text-sky-700" : ""}"
          placeholder="192.168.10.10 or host.example.com" value="${escapeHtml(host)}" ${isDocker ? "readonly title='Docker container name (managed on the Docker page)'" : ""} />
        <span class="w-px bg-slate-300 shrink-0"></span>
        <input class="be-port w-20 shrink-0 px-3 py-2 text-sm font-mono outline-none bg-transparent text-center"
          placeholder="443" inputmode="numeric" value="${escapeHtml(port)}" />
      </div>
      <button type="button" class="be-cog px-2 text-slate-400 hover:text-slate-700" title="Per-server options">⚙</button>
      <button type="button" class="rm-backend px-2 text-slate-400 hover:text-red-600" title="Remove">✕</button>
    </div>
    <div class="be-prio-row hidden mt-2 flex items-center gap-2">
      <label class="text-[11px] font-medium text-slate-500 shrink-0">Failover role</label>
      <select class="be-prio flex-1 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-xs outline-none focus:ring-2 focus:ring-emerald-500"
        data-prio="${b.priority ?? 1}" title="Which tier this backend serves in (Primary runs first)"></select>
    </div>
    <div class="be-adv hidden mt-2 grid grid-cols-2 gap-2">
      <label class="text-[11px] text-slate-500">weight
        <input class="be-weight ${inp} w-full mt-0.5" placeholder="1" value="${b.weight ?? ""}"></label>
      <label class="text-[11px] text-slate-500">max_fails
        <input class="be-maxfails ${inp} w-full mt-0.5" placeholder="1" value="${b.max_fails ?? ""}"></label>
      <label class="text-[11px] text-slate-500">fail_timeout
        <input class="be-failtimeout ${inp} w-full mt-0.5" placeholder="10s" value="${escapeHtml(b.fail_timeout || "")}"></label>
      <label class="text-[11px] text-slate-500">max_conns
        <input class="be-maxconns ${inp} w-full mt-0.5" placeholder="0" value="${b.max_conns ?? ""}"></label>
      <label class="be-backup-wrap flex items-center gap-1.5 text-[11px] text-slate-600">
        <input type="checkbox" class="be-backup rounded" ${b.backup ? "checked" : ""}> backup</label>
      <label class="flex items-center gap-1.5 text-[11px] text-slate-600">
        <input type="checkbox" class="be-down rounded" ${b.down ? "checked" : ""}> down</label>
    </div>`;
  wrap.querySelector(".be-cog").addEventListener("click", () =>
    wrap.querySelector(".be-adv").classList.toggle("hidden"));
  wrap.querySelector(".rm-backend").addEventListener("click", () => {
    if ($$("#backends .be-row").length > 1) { wrap.remove(); syncLbAuto(); syncFailoverUI(); }
  });
  // Live failover-order preview reacts to role changes and address edits.
  wrap.querySelector(".be-prio").addEventListener("change", renderFailoverPreview);
  wrap.querySelector(".be-host").addEventListener("input", renderFailoverPreview);
  wrap.querySelector(".be-port").addEventListener("input", renderFailoverPreview);
  $("#backends").appendChild(wrap);
  syncLbAuto();   // auto-open LB settings once there are 2+ backends
  syncFailoverUI();   // reveal the failover role picker if failover is on
}

// Rebuild every backend's "Failover role" dropdown. Options run Tier 1 (Primary)
// through one tier per backend — you never need more tiers than backends — while
// preserving any higher saved value. Called whenever backends are added/removed.
function refreshPrioOptions() {
  const rows = $$("#backends .be-row");
  rows.forEach((row) => {
    const sel = row.querySelector(".be-prio");
    if (!sel) return;
    const cur = parseInt(sel.dataset.prio || sel.value || "1", 10) || 1;
    const max = Math.max(rows.length, cur, 2);   // always offer a 2nd tier so failover is usable
    let html = "";
    for (let i = 1; i <= max; i++) {
      const label = i === 1 ? "Tier 1 · Primary" : `Tier ${i} · Backup`;
      html += `<option value="${i}"${i === cur ? " selected" : ""}>${label}</option>`;
    }
    sel.innerHTML = html;
    sel.value = String(Math.min(cur, max));
    delete sel.dataset.prio;   // consumed; subsequent reads come from sel.value
  });
}

// Group the current backends into tiers and render the "Failover order" preview:
// the lowest tier serves traffic, higher tiers stand by. Warns when everything
// sits in one tier (failover would be a no-op).
function renderFailoverPreview() {
  const box = $("#failover-preview");
  if (!box) return;
  const warn = $("#failover-warn");
  const tiers = {};
  $$("#backends .be-row").forEach((row) => {
    const host = row.querySelector(".be-host").value.trim();
    if (!host) return;
    const port = row.querySelector(".be-port").value.trim();
    const p = parseInt(row.querySelector(".be-prio").value || "1", 10) || 1;
    (tiers[p] = tiers[p] || []).push(host + (port ? ":" + port : ""));
  });
  const keys = Object.keys(tiers).map(Number).sort((a, b) => a - b);
  if (warn) warn.classList.toggle("hidden", keys.length >= 2);
  if (!keys.length) {
    box.innerHTML = '<div class="text-xs text-slate-400">Add a backend address to see the failover order.</div>';
    return;
  }
  box.innerHTML = keys.map((p, idx) => {
    const primary = idx === 0;
    const badge = primary
      ? '<span class="shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-emerald-100 text-emerald-700">Tier ' + p + ' · serves traffic</span>'
      : '<span class="shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-slate-200 text-slate-600">Tier ' + p + ' · standby</span>';
    const servers = tiers[p].map(escapeHtml).join(", ");
    return `<div class="flex items-start gap-2">${badge}<span class="text-xs font-mono text-slate-600 break-all">${servers}</span></div>`;
  }).join("");
}

// Reveal the per-backend failover role pickers + order preview with the toggle.
// The nginx `backup` flag is meaningless under managed failover, so hide it too.
function syncFailoverUI() {
  const on = $("#failover") && $("#failover").checked;
  const sec = $("#failover-section");
  if (sec) sec.classList.toggle("hidden", !on);
  refreshPrioOptions();
  $$("#backends .be-prio-row").forEach((el) => el.classList.toggle("hidden", !on));
  $$("#backends .be-backup-wrap").forEach((el) => el.classList.toggle("hidden", on));
  if (on) renderFailoverPreview();
}

function serializeBackends() {
  // Priority tiers only mean something under failover; skip them otherwise so a
  // plain load-balanced pool doesn't accumulate stray priority=1 on every server.
  const failoverOn = $("#failover") && $("#failover").checked;
  return $$("#backends .be-row").map((row) => {
    const g = (s) => row.querySelector(s);
    const host = g(".be-host").value.trim();
    const port = g(".be-port").value.trim();
    const e = {
      weight: g(".be-weight").value.trim(),
      priority: failoverOn ? g(".be-prio").value.trim() : "",
      max_fails: g(".be-maxfails").value.trim(),
      fail_timeout: g(".be-failtimeout").value.trim(),
      max_conns: g(".be-maxconns").value.trim(),
      backup: g(".be-backup").checked,
      down: g(".be-down").checked,
    };
    if (row.dataset.dockerContainer) {
      // Docker backend: keep it managed by NAME (server is re-resolved server-
      // side and refreshed by the reconciler) — never save the cached IP.
      e.docker_container = row.dataset.dockerContainer;
      e.docker_port = port;
    } else {
      // Recombine into the "host:port" the backend expects; keep host-only so
      // the server-side validator reports a clear "must be host:port" error.
      e.server = host && port ? `${host}:${port}` : host;
    }
    return e;
  }).filter((e) => e.server || e.docker_container);
}

// --- load-balancing method panels -----------------------------------------
function onLbChange() {
  const m = $("#lb_method").value;
  $("#lb-hash").classList.toggle("hidden", m !== "hash");
  $("#lb-random").classList.toggle("hidden", m !== "random");
}

function backendCount() { return $$("#backends .be-row").length; }

function toggleLbSection() {
  $("#lb-section").classList.toggle("hidden", !$("#lb_enabled").checked);
}

// Load balancing only matters with multiple backends, so the settings are
// driven by the backend count: 2+ opens them, 1 collapses to round-robin.
function syncLbAuto() {
  const multi = backendCount() >= 2;
  const hint = $("#lb-auto-hint"); if (hint) hint.classList.toggle("hidden", !multi);
  const en = $("#lb_enabled"); if (!en) return;
  en.checked = multi;
  if (!multi) { $("#lb_method").value = "round_robin"; onLbChange(); }
  toggleLbSection();
}

function toggleRateSection() {
  $("#rate-section").classList.toggle("hidden", !$("#rate_limit_enabled").checked);
}

// --- SSL tabs --------------------------------------------------------------
function selectSsl(mode) {
  $("#ssl_mode").value = mode;
  $("#ssl-keep").classList.add("hidden");   // a real choice cancels "keep"
  $$(".ssl-tab").forEach((b) => {
    const on = b.dataset.ssl === mode;
    b.className = "ssl-tab rounded-md px-2 py-1.5 border " +
      (on ? "border-emerald-500 bg-emerald-50 text-emerald-700" : "border-slate-300 text-slate-600");
  });
  $("#ssl-existing").classList.toggle("hidden", mode !== "existing");
  $("#ssl-terminate").classList.toggle("hidden", mode !== "existing");
  if (mode === "existing") loadCerts();
  updateSniAvailability();
}

// Does the current SSL choice terminate TLS on this listener (listen … ssl)?
// A managed cert terminates; "keep" terminates iff the edited mapping had a cert.
function sslTerminates() {
  const mode = $("#ssl_mode").value;
  if (mode === "existing") return true;
  if (mode === "keep") return EDIT_HAS_CERT;
  return false;
}

// SNI hostname restriction uses ssl_preread, which needs TLS *passthrough* and
// TCP — so it's incompatible with TLS termination (a managed cert) or UDP.
// Hide and clear the checkbox in those cases instead of erroring on submit.
function updateSniAvailability() {
  const incompatible = currentTransport() === "udp" || sslTerminates();
  const sni = $("#sni-section");
  if (sni) sni.classList.toggle("hidden", incompatible);
  if (incompatible) $("#sni_guard").checked = false;
}

// Keep the existing cert untouched (edit mode). No option selected; ssl_mode=keep.
function setSslKeep(currentMode) {
  $("#ssl_mode").value = "keep";
  $$(".ssl-tab").forEach((b) => {
    b.className = "ssl-tab rounded-md px-2 py-1.5 border border-slate-300 text-slate-600";
  });
  $("#ssl-existing").classList.add("hidden");
  $("#ssl-terminate").classList.add("hidden");
  $("#ssl-keep-mode").textContent = currentMode || "cert";
  $("#ssl-keep").classList.remove("hidden");
  updateSniAvailability();
}

async function loadCerts() {
  const sel = $("#ssl_existing");
  const certs = (await (await fetch("/api/certs")).json()).certs || [];
  const cur = sel.value;
  sel.innerHTML = "";
  for (const c of certs) {
    const o = document.createElement("option");
    o.value = c.domain;
    o.textContent = `${c.domain}  (${c.ssl_mode})`;
    sel.appendChild(o);
  }
  if (cur) sel.value = cur;
  $("#ssl-existing-empty").classList.toggle("hidden", certs.length > 0);
  sel.classList.toggle("hidden", certs.length === 0);
}

function lbLabel(m) {
  switch (m.lb_method) {
    case "least_conn": return "least_conn";
    case "hash": return "hash " + (m.hash_key || "$remote_addr") + (m.hash_consistent ? " consistent" : "");
    case "random": return m.random_two ? "random two" : "random";
    default: return "round-robin";
  }
}

// Display identity for a backend: the Docker container/service NAME (not the
// resolved IP) when it's a docker-managed backend, else the host:port.
function backendName(b) {
  if (typeof b === "string") return b;
  if (b && b.docker_container) return "🐳 " + b.docker_container + (b.docker_port ? ":" + b.docker_port : "");
  return (b && b.server) || "";
}

// server "ip:port" -> docker display name, so views that only carry the probed
// server address (health rollup, diagnose) can still show the name.
const DOCKER_BE_NAME = {};
function rebuildBackendNames() {
  for (const k in DOCKER_BE_NAME) delete DOCKER_BE_NAME[k];
  MAPPINGS.forEach((m) => (m.backends || []).forEach((b) => {
    if (b && b.docker_container && b.server) {
      DOCKER_BE_NAME[b.server] = "🐳 " + b.docker_container + (b.docker_port ? ":" + b.docker_port : "");
    }
  }));
}
function beDisplay(server) { return DOCKER_BE_NAME[server] || server; }

function backendLabel(b) {
  if (typeof b === "string") return b;
  let s = backendName(b);
  if (b.weight) s += " w" + b.weight;
  if (b.backup) s += " backup";
  if (b.down) s += " down";
  return s;
}

// --- backend health --------------------------------------------------------
let HEALTH = {};           // domain -> {status, up, total, backends:[...]}
let HEALTH_TIMER = null;

// Live per-mapping traffic (animated sparkline column on the mappings page).
const TRAFFIC_BARS = 20;   // sparkline width in samples
let TRAFFIC = {};          // domain -> {connections}
let TRAFFIC_HIST = {};     // domain -> number[] (rolling history)
let TRAFFIC_TIMER = null;

const HEALTH_DOT = {
  green:  "bg-emerald-500 shadow-[0_0_8px] shadow-emerald-400",
  yellow: "bg-amber-500 shadow-[0_0_8px] shadow-amber-400",
  red:    "bg-red-500 shadow-[0_0_8px] shadow-red-400 animate-pulse",
  gray:   "bg-slate-300",
};

// Compact "since" duration, e.g. 45s / 7m / 3h / 2d.
function fmtDur(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return Math.floor(s) + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}

function healthHtml(h) {
  if (!h) {
    return '<span class="inline-flex items-center gap-2 text-xs text-slate-400">'
      + '<span class="h-2.5 w-2.5 rounded-full shimmer"></span>checking…</span>';
  }
  const enabled = (h.backends || []).filter((b) => b.enabled);
  // UDP backends aren't TCP-probed — show that instead of a misleading "—".
  const isUdp = (h.backends || []).some((b) => (b.error || "").includes("udp"));
  // UDP can't be health-probed; give it a visible violet dot (matching the
  // traffic column) rather than the near-invisible gray "unknown" dot.
  const dot = isUdp
    ? "bg-violet-400 shadow-[0_0_8px] shadow-violet-300"
    : (HEALTH_DOT[h.status] || HEALTH_DOT.gray);
  // The current rollup is as old as its most recently-changed backend.
  const since = enabled.map((b) => b.since).filter(Boolean).sort().pop();
  let label;
  if (isUdp)                      label = "udp";
  else if (h.status === "green")  label = "up " + fmtDur(since);
  else if (h.status === "red")    label = "down " + fmtDur(since);
  else if (h.status === "yellow") label = `${h.up}/${h.total} up`;
  else                            label = "—";
  // Failover: show which priority tier is currently serving.
  const foBadge = h.failover
    ? `<span class="ml-1 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700" title="Failover tier currently serving traffic">⇅ Tier ${h.active_priority}</span>`
    : "";
  const lines = (h.backends || []).map((b) => {
    const role = h.failover ? ` [Tier ${b.priority} · ${b.active ? "active" : "standby"}]` : "";
    const tag = !b.enabled ? "disabled"
      : b.up ? `up${b.latency_ms != null ? " " + b.latency_ms + "ms" : ""}`
             : `down${b.error ? " (" + b.error + ")" : ""}`;
    return `${beDisplay(b.server)}${role} — ${tag}`;
  }).join("\n");
  return `<span class="inline-flex items-center gap-2 text-xs text-slate-600" title="${escapeHtml(lines)}">`
    + `<span class="h-2.5 w-2.5 rounded-full ${dot}"></span>${escapeHtml(label)}${foBadge}</span>`;
}

// Patch only the health cells in place — avoids re-rendering (and re-animating)
// the whole table on every poll.
function applyHealth() {
  $$("[data-health]").forEach((cell) => {
    cell.innerHTML = healthHtml(HEALTH[cell.dataset.health]);
  });
}

async function loadHealth(force = false) {
  try {
    const j = await (await fetch("/api/health" + (force ? "?force=1" : ""))).json();
    if (j.ok) {
      HEALTH = j.mappings || {};
      applyHealth();
      if (livemapVisible()) updateRouteHealth();   // recolour the routing map
    }
  } catch (_) { /* leave the last known state on a transient error */ }
}

function startHealthPolling() {
  if (HEALTH_TIMER) return;
  HEALTH_TIMER = setInterval(() => {
    if (!document.hidden) loadHealth();
  }, 15000);
}

// --- live traffic sparkline ------------------------------------------------
// Build the bar track + footer once per cell so subsequent updates only change
// bar heights — that lets the CSS height transition animate each tick smoothly.
function trafficCellInit(cell) {
  if (cell.dataset.init) return;
  const bars = Array.from({ length: TRAFFIC_BARS }, () =>
    '<span class="flex-1 rounded-sm bg-slate-200 transition-all duration-500" style="height:2px"></span>').join("");
  cell.innerHTML =
    `<div class="trf-track flex items-end gap-px h-7 w-24">${bars}</div>`
    + `<div class="mt-1 flex items-center gap-1 text-xs text-slate-500">`
    +   `<span class="trf-dot h-1.5 w-1.5 rounded-full bg-slate-300"></span>`
    +   `<span class="trf-num font-mono tabular-nums">—</span>`
    +   `<span class="text-slate-400">conn</span>`
    + `</div>`;
  cell.dataset.init = "1";
}

function applyTraffic() {
  $$("[data-traffic]").forEach((cell) => {
    trafficCellInit(cell);
    const domain = cell.dataset.traffic;
    const t = TRAFFIC[domain] || {};
    // UDP is connectionless — there's no live connection count to chart.
    if (t.udp) {
      for (const span of cell.querySelector(".trf-track").children) {
        span.style.height = "2px";
        span.className = "flex-1 rounded-sm bg-slate-200";
      }
      cell.querySelector(".trf-num").textContent = "UDP";
      cell.querySelector(".trf-dot").className = "trf-dot h-1.5 w-1.5 rounded-full bg-violet-400";
      return;
    }
    const hist = TRAFFIC_HIST[domain] || [];
    // Right-align the history into a fixed-width window, zero-padded on the left.
    const peak = Math.max(1, ...hist);
    const spans = cell.querySelector(".trf-track").children;
    for (let i = 0; i < TRAFFIC_BARS; i++) {
      const idx = hist.length - TRAFFIC_BARS + i;
      const v = idx >= 0 ? hist[idx] : 0;
      const span = spans[i];
      span.style.height = Math.max(2, Math.round((v / peak) * 26)) + "px";
      span.className = "flex-1 rounded-sm transition-all duration-500 " + (v > 0 ? "bg-sky-400" : "bg-slate-200");
    }
    const cur = (TRAFFIC[domain] || {}).connections ?? 0;
    cell.querySelector(".trf-num").textContent = cur;
    cell.querySelector(".trf-dot").className =
      "trf-dot h-1.5 w-1.5 rounded-full " + (cur > 0 ? "bg-sky-500 animate-pulse" : "bg-slate-300");
  });
}

async function loadTraffic() {
  try {
    const j = await (await fetch("/api/traffic")).json();
    if (!j.ok) return;
    TRAFFIC = j.traffic || {};
    for (const [domain, t] of Object.entries(TRAFFIC)) {
      const h = TRAFFIC_HIST[domain] || (TRAFFIC_HIST[domain] = []);
      h.push(Math.max(0, t.connections || 0));
      if (h.length > TRAFFIC_BARS) h.shift();
    }
    applyTraffic();
  } catch (_) { /* keep last waveform on a transient error */ }
}

function startTrafficPolling() {
  loadTraffic();
  if (TRAFFIC_TIMER) return;
  TRAFFIC_TIMER = setInterval(() => { if (!document.hidden) loadTraffic(); }, 2000);
}
function stopTrafficPolling() { clearInterval(TRAFFIC_TIMER); TRAFFIC_TIMER = null; }

// --- table -----------------------------------------------------------------
let MAPPINGS = [];
async function loadMappings() {
  MAPPINGS = (await (await fetch("/api/mappings")).json()).mappings || [];
  rebuildBackendNames();   // server IP -> docker name, for health/diagnose views
  updateStats(MAPPINGS);
  loadSslCount();   // TLS Certs stat comes from the cert registry, not mappings
  renderMappings();
  if (livemapVisible()) renderRouteMap();   // keep the routing map in sync
  loadHealth();   // fill in the health column (non-blocking)
}

// Animate a number from its current value up/down to the target — adds life
// to the dashboard stats without changing what's displayed at rest.
function animateCount(el, to) {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const from = parseInt(el.textContent, 10) || 0;
  if (reduce || from === to) { el.textContent = to; return; }
  const start = performance.now(), dur = 600;
  const step = (now) => {
    const p = Math.min(1, (now - start) / dur);
    const eased = 1 - Math.pow(1 - p, 3);  // easeOutCubic
    el.textContent = Math.round(from + (to - from) * eased);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function updateStats(list) {
  const backends = list.reduce((n, m) =>
    n + (m.backends || (m.backend ? [m.backend] : [])).length, 0);
  const set = (id, v) => { const e = $("#" + id); if (e) animateCount(e, v); };
  set("stat-total", list.length);
  set("stat-backends", backends);
  set("nav-count", list.length);
}

// The TLS Certs stat reflects the managed certificate registry (the SSL page),
// not how many mappings happen to reference a cert.
async function loadSslCount() {
  try {
    const j = await (await fetch("/api/ssl/certs")).json();
    const n = (j.certs || []).length;
    const e = $("#stat-ssl");
    if (e) animateCount(e, n);
  } catch (_) { /* leave the last value on a transient error */ }
}

// Reusable numeric pager. Renders "Showing a–b of N" + page buttons into the
// given elements; calls onGo(page) when a page is clicked. Hidden when it fits.
function renderPager(containerId, infoId, navId, total, page, size, onGo) {
  const cont = $("#" + containerId);
  if (!cont) return;
  const pages = Math.max(1, Math.ceil(total / size));
  const many = total > size;
  cont.classList.toggle("hidden", !many);
  cont.classList.toggle("flex", many);
  if (!many) return;
  const from = (page - 1) * size + 1, to = Math.min(total, page * size);
  const info = $("#" + infoId); if (info) info.textContent = `Showing ${from}–${to} of ${total}`;
  const nav = $("#" + navId); if (!nav) return;
  const btn = (label, target, o = {}) =>
    `<button data-p="${target}" class="pager-btn min-w-[2rem] px-2 py-1 rounded-md border border-slate-200 text-xs font-medium ${o.active ? "bg-emerald-600 text-white border-emerald-600" : "text-slate-600 hover:bg-slate-100"} ${o.disabled ? "opacity-40 cursor-default" : "cursor-pointer"}" ${o.disabled ? "disabled" : ""}>${label}</button>`;
  const span = 2;
  let lo = Math.max(1, page - span), hi = Math.min(pages, page + span);
  if (page <= span) hi = Math.min(pages, 1 + span * 2);
  if (page > pages - span) lo = Math.max(1, pages - span * 2);
  let html = btn("‹", page - 1, { disabled: page === 1 });
  if (lo > 1) { html += btn("1", 1); if (lo > 2) html += '<span class="px-1 text-slate-400">…</span>'; }
  for (let p = lo; p <= hi; p++) html += btn(String(p), p, { active: p === page });
  if (hi < pages) { if (hi < pages - 1) html += '<span class="px-1 text-slate-400">…</span>'; html += btn(String(pages), pages); }
  html += btn("›", page + 1, { disabled: page === pages });
  nav.innerHTML = html;
  nav.querySelectorAll(".pager-btn").forEach((b) => {
    if (!b.disabled) b.addEventListener("click", () => onGo(Number(b.dataset.p)));
  });
}

const MAP_PAGE_SIZE = 10;
let MAP_PAGE = 1;

function renderMappings() {
  const q = ($("#search")?.value || "").trim().toLowerCase();
  const full = q ? MAPPINGS.filter((m) => (m.domain || "").toLowerCase().includes(q)) : MAPPINGS;
  // Clamp the current page to the available range (e.g. after deletes/filtering).
  const pages = Math.max(1, Math.ceil(full.length / MAP_PAGE_SIZE));
  if (MAP_PAGE > pages) MAP_PAGE = pages;
  const start = (MAP_PAGE - 1) * MAP_PAGE_SIZE;
  const list = full.slice(start, start + MAP_PAGE_SIZE);
  renderPager("map-pagination", "map-page-info", "map-page-nav", full.length, MAP_PAGE, MAP_PAGE_SIZE,
    (p) => { MAP_PAGE = p; renderMappings(); });
  const rows = $("#rows");
  rows.innerHTML = "";
  $("#empty").classList.toggle("hidden", full.length > 0);
  list.forEach((m, idx) => {
    const backends = (m.backends || (m.backend ? [m.backend] : [])).map(backendLabel).join(", ");
    const lb = lbLabel(m);
    const ip = m.bind_ip || "(dhcp)";
    const disabled = m.enabled === false;
    const key = mkey(m);
    const port = m.listen_port || 443;
    const tr = document.createElement("tr");
    tr.className = "hover:bg-slate-50 align-top" + (disabled ? " opacity-50" : "");
    tr.style.setProperty("--i", Math.min(idx, 12));   // staggered entrance
    tr.innerHTML = `
      <td class="px-4 py-3"><input type="checkbox" class="row-check rounded border-slate-300 cursor-pointer align-middle" data-key="${escapeHtml(key)}" ${SELECTED.has(key) ? "checked" : ""} /></td>
      <td class="px-6 py-3 font-mono">${escapeHtml(m.domain)}${disabled ? ' <span class="inline-block px-2 py-0.5 rounded-full bg-slate-200 text-slate-500 text-[10px] font-sans align-middle uppercase tracking-wide">disabled</span>' : ""}</td>
      <td class="px-6 py-3" data-health="${escapeHtml(key)}">${healthHtml(HEALTH[key])}</td>
      <td class="px-6 py-3" data-traffic="${escapeHtml(key)}"></td>
      <td class="px-6 py-3 font-mono">${escapeHtml(ip)}<span class="text-slate-400">:${escapeHtml(m.listen_port || 443)}</span><div class="text-xs text-slate-400">${escapeHtml(m.alloc_method || "static")}${m.protocol ? " · " + escapeHtml(m.protocol) : ""}</div></td>
      <td class="px-6 py-3 font-mono text-slate-600">${m.subiface
        ? '<span class="inline-block px-2 py-0.5 rounded bg-slate-100 text-slate-700 text-xs">' + escapeHtml(m.subiface) + '</span>'
          + '<div class="text-xs text-slate-400">' + escapeHtml(m.mac || "") + '</div>'
          + '<div class="text-xs text-slate-400">on ' + escapeHtml(m.interface || "")
          + (m.vlan_id ? ' · VLAN ' + escapeHtml(m.vlan_id) : '') + '</div>'
        : '<span class="text-slate-400 text-xs">—</span>'}</td>
      <td class="px-6 py-3 font-mono text-slate-600 text-xs">${escapeHtml(backends)}<div class="text-slate-400">⚖ ${escapeHtml(lb)}</div></td>
      <td class="px-6 py-3">${m.has_cert
        ? '<span class="inline-block px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700 text-xs">' + escapeHtml(m.ssl_mode || "cert") + '</span>'
          + (m.cert_domain && m.cert_domain !== m.domain
              ? '<div class="text-xs text-slate-400">↳ ' + escapeHtml(m.cert_domain) + '</div>' : '')
        : '<span class="text-slate-400 text-xs">—</span>'}</td>
      <td class="px-6 py-3 text-right whitespace-nowrap">
        <button data-domain="${escapeHtml(m.domain)}" data-port="${port}" class="edit-btn text-xs font-medium text-emerald-700 hover:text-emerald-900 mr-3">Edit</button>
        ${isAdmin() ? `<label class="relative inline-flex items-center cursor-pointer align-middle mr-3" title="${disabled ? "Enable" : "Disable"} this mapping">
          <input type="checkbox" class="toggle-input sr-only peer" data-domain="${escapeHtml(m.domain)}" data-port="${port}" data-enabled="${disabled ? "false" : "true"}" ${disabled ? "" : "checked"} />
          <div class="w-9 h-5 bg-slate-300 rounded-full peer-checked:bg-emerald-500 transition-colors"></div>
          <div class="absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform peer-checked:translate-x-4"></div>
        </label>` : ""}
        ${isAdmin() ? `<button data-domain="${escapeHtml(m.domain)}" data-port="${port}" class="debug-btn text-xs font-medium text-sky-600 hover:text-sky-800 mr-3">Debug</button>` : ""}
        ${isAdmin() ? `<button data-domain="${escapeHtml(m.domain)}" data-port="${port}" class="del-btn text-xs font-medium text-red-600 hover:text-red-800">Delete</button>` : ""}
      </td>`;
    rows.appendChild(tr);
  });
  rows.querySelectorAll(".del-btn").forEach((b) =>
    b.addEventListener("click", () => del(b.dataset.domain, b.dataset.port)));
  rows.querySelectorAll(".toggle-input").forEach((c) =>
    c.addEventListener("change", () => toggleMapping(c.dataset.domain, c.dataset.port, c.dataset.enabled === "true")));
  rows.querySelectorAll(".debug-btn").forEach((b) =>
    b.addEventListener("click", () => openDiagnose(b.dataset.domain, b.dataset.port)));
  rows.querySelectorAll(".edit-btn").forEach((b) =>
    b.addEventListener("click", () => editMapping(b.dataset.domain, b.dataset.port)));
  rows.querySelectorAll(".row-check").forEach((c) =>
    c.addEventListener("change", () => {
      c.checked ? SELECTED.add(c.dataset.key) : SELECTED.delete(c.dataset.key);
      updateBulkBar();
    }));
  updateBulkBar();
  applyTraffic();   // repaint sparklines into the freshly-rendered cells
}

// --- bulk selection on the mappings table ----------------------------------
const SELECTED = new Set();

function visibleKeys() {
  return $$("#rows .row-check").map((c) => c.dataset.key);
}

function updateBulkBar() {
  // Drop selections for mappings no longer present (deleted/filtered out of data).
  const present = new Set(MAPPINGS.map(mkey));
  [...SELECTED].forEach((d) => { if (!present.has(d)) SELECTED.delete(d); });
  const n = SELECTED.size;
  $("#bulk-bar").classList.toggle("hidden", n === 0);
  $("#bulk-count").textContent = `${n} selected`;
  $("#bulk-delete").classList.toggle("hidden", !isAdmin());
  // Reflect the header "select all" state against the visible rows.
  const vis = visibleKeys();
  const all = vis.length > 0 && vis.every((d) => SELECTED.has(d));
  const some = vis.some((d) => SELECTED.has(d));
  const head = $("#check-all");
  if (head) { head.checked = all; head.indeterminate = some && !all; }
}

function toggleSelectAll(on) {
  visibleKeys().forEach((d) => on ? SELECTED.add(d) : SELECTED.delete(d));
  $$("#rows .row-check").forEach((c) => { c.checked = on; });
  updateBulkBar();
}

function clearSelection() {
  SELECTED.clear();
  $$("#rows .row-check").forEach((c) => { c.checked = false; });
  updateBulkBar();
}

function exportSelected() {
  const chosen = MAPPINGS.filter((m) => SELECTED.has(mkey(m)));
  if (!chosen.length) return;
  const payload = {
    splitter_backup: true, version: 1, exported: new Date().toISOString(),
    mappings: Object.fromEntries(chosen.map((m) => [mkey(m), m])),
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const stamp = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `splitter-selected-${stamp}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
  toast(`Exported ${chosen.length} mapping(s).`);
}

async function deleteSelected() {
  const chosen = MAPPINGS.filter((m) => SELECTED.has(mkey(m)));
  if (!chosen.length) return;
  const labels = chosen.map(mkey);
  if (!confirm(`Delete ${chosen.length} mapping(s)?\n\n${labels.join(", ")}\n\nEach is deprovisioned (config removed, sub-interface left intact) and Nginx reloaded.`)) return;
  let ok = 0; const fails = [];
  for (const m of chosen) {
    const label = mkey(m);
    try {
      const url = `/api/mappings/${encodeURIComponent(m.domain)}?port=${encodeURIComponent(m.listen_port || 443)}`;
      const j = await (await fetch(url, { method: "DELETE" })).json();
      j.ok ? ok++ : fails.push(`${label}: ${j.error || "failed"}`);
    } catch (e) { fails.push(`${label}: ${e.message}`); }
  }
  SELECTED.clear();
  await loadMappings();
  toast(fails.length ? `Deleted ${ok}, ${fails.length} failed: ${fails[0]}` : `Deleted ${ok} mapping(s).`, !fails.length);
}

function renderSteps(steps) {
  const card = $("#log-card");
  const hint = $("#log-hint");
  const ol = $("#steps");
  ol.innerHTML = "";
  if (!steps || !steps.length) {
    card.classList.add("hidden");
    if (hint) hint.classList.remove("hidden");
    return;
  }
  card.classList.remove("hidden");
  if (hint) hint.classList.add("hidden");
  for (const s of steps) {
    const li = document.createElement("li");
    li.className = "flex gap-2";
    const icon = s.ok ? '<span class="text-emerald-600 font-bold">✓</span>'
                      : '<span class="text-red-600 font-bold">✗</span>';
    const sim = s.simulated ? ' <span class="text-amber-600">(sim)</span>' : "";
    li.innerHTML = `${icon}
      <div class="min-w-0">
        <div class="font-medium">${escapeHtml(s.name)}${sim}</div>
        <pre class="text-xs text-slate-500 whitespace-pre-wrap break-all">${escapeHtml(s.detail || "")}</pre>
      </div>`;
    ol.appendChild(li);
  }
}

// --- actions ---------------------------------------------------------------
function formData() {
  const fd = new FormData($("#map-form"));
  fd.set("backends_json", JSON.stringify(serializeBackends()));
  return fd;
}

async function apply(e) {
  e.preventDefault();
  const btn = $("#apply-btn");
  const editing = EDITING;
  setBtnLoading(btn, true, editing ? "Updating…" : "Applying…");
  showStatus("loading", editing ? "Updating mapping…" : "Applying mapping…",
             "Provisioning the bind IP, writing the Nginx config and reloading.");
  try {
    const fd = formData();
    const r = await fetch("/api/mappings", { method: "POST", body: fd });
    const j = await r.json();
    renderSteps(j.steps);
    if (j.ok) {
      showStatus("success", editing ? "Mapping updated" : "Mapping applied",
                 `${j.mapping.domain} is live.`);
      resetForm();
      await loadMappings();
      showPage("mappings");
    } else {
      showStatus("error", "Couldn't apply mapping",
                 j.error || "The host rejected the request. Check the step log for the failing command.");
    }
  } catch (err) {
    showStatus("error", "Request failed", err.message || String(err));
  } finally {
    setBtnLoading(btn, false);
    btn.textContent = EDITING ? "Update" : "Save / Apply";
  }
}

async function del(domain, port) {
  const label = port ? `${domain}:${port}` : domain;
  if (!confirm(`Delete mapping for ${label}? Removes its config, tears down the sub-interface and reloads Nginx.`)) return;
  const url = `/api/mappings/${encodeURIComponent(domain)}${port ? `?port=${encodeURIComponent(port)}` : ""}`;
  const r = await fetch(url, { method: "DELETE" });
  const j = await r.json();
  renderSteps(j.steps);
  if (j.ok) { toast(`Mapping for ${label} removed.`); await loadMappings(); }
  else toast(j.error || "Failed to delete.", false);
}

async function toggleMapping(domain, port, currentlyEnabled) {
  const label = port ? `${domain}:${port}` : domain;
  // The switch has already flipped visually; renderMappings() restores it from
  // MAPPINGS whenever we bail out so it never lies about the real state.
  if (currentlyEnabled &&
      !confirm(`Disable ${label}?\n\nNginx stops serving it (config removed + reload). The bind IP, sub-interface and certificate are kept, so you can re-enable it anytime.`)) {
    renderMappings();
    return;
  }
  const url = `/api/mappings/${encodeURIComponent(domain)}/toggle${port ? `?port=${encodeURIComponent(port)}` : ""}`;
  const r = await fetch(url, { method: "POST" });
  const j = await r.json();
  renderSteps(j.steps);
  if (j.ok) { toast(`${label} ${j.enabled ? "enabled" : "disabled"}.`); await loadMappings(); }
  else { toast(j.error || `Failed to ${currentlyEnabled ? "disable" : "enable"}.`, false); renderMappings(); }
}

async function preview() {
  const fd = formData();
  fd.delete("cert"); fd.delete("key");
  const r = await fetch("/api/preview", { method: "POST", body: fd });
  const j = await r.json();
  if (!j.ok) return toast(j.error || "Cannot preview.", false);
  renderSteps([{ name: j.path, ok: true, detail: j.conf, simulated: false }]);
}

// --- backup: export / import ----------------------------------------------
function exportBackup() {
  // Let the browser download the file straight from the endpoint.
  window.location.href = "/api/backup";
}

async function importBackup(file) {
  if (!file) return;
  if (!confirm(`Import mappings from "${file.name}"?\n\nExisting mappings with the same domain will be overwritten. This restores the data only — Edit → Save/Apply a mapping to re-provision it on the host.`)) return;
  try {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch("/api/import", { method: "POST", body: fd });
    const j = await r.json();
    if (j.ok) { toast(`Imported ${j.imported} mapping(s).`); await loadMappings(); }
    else toast(j.error || "Import failed.", false);
  } catch (err) {
    toast("Import failed: " + err.message, false);
  }
}

async function reapplyAll() {
  if (!MAPPINGS.length) return toast("No mappings to re-apply.", false);
  if (!confirm(`Re-apply all ${MAPPINGS.length} mapping(s)?\n\nThis re-creates IPs / sub-interfaces, rewrites the Nginx configs and reloads. Existing certs on disk are reused.`)) return;
  const btn = $("#reapply-btn");
  btn.disabled = true; btn.textContent = "Re-applying…";
  try {
    const r = await fetch("/api/reapply", { method: "POST" });
    const j = await r.json();
    // Flatten per-mapping results into the provisioning log.
    const steps = [];
    for (const res of (j.results || [])) {
      steps.push({ name: res.domain, ok: res.ok, detail: res.ok ? "applied" : (res.error || "failed") });
      for (const s of (res.steps || [])) steps.push(s);
    }
    renderSteps(steps);
    if (j.failed) toast(`Re-applied ${j.applied}, ${j.failed} failed — see the log.`, false);
    else toast(`Re-applied ${j.applied} mapping(s).`);
    await loadMappings();
  } catch (err) {
    toast("Re-apply failed: " + err.message, false);
  } finally {
    btn.disabled = false; btn.textContent = "Re-apply all";
  }
}

// --- transport / protocol / listen port ------------------------------------
// Two-step: pick TCP or UDP, then the protocol list shows only that transport's
// presets (+ Custom). Picking a protocol fills the port; typing a port reflects
// back to the matching preset (or Custom).
function currentTransport() {
  const r = document.querySelector('input[name="transport"]:checked');
  return r && r.value === "udp" ? "udp" : "tcp";
}

function setTransport(t) {
  const r = document.querySelector(`input[name="transport"][value="${t === "udp" ? "udp" : "tcp"}"]`);
  if (r) r.checked = true;
}

// Show only the protocols for the active transport; if the current selection is
// now hidden, jump to that transport's first protocol.
function filterProtocols() {
  const t = currentTransport();
  const sel = $("#protocol");
  let firstVisible = null;
  for (const opt of sel.options) {
    const match = (opt.dataset.transport || "tcp") === t;
    opt.hidden = !match;
    if (match && !firstVisible) firstVisible = opt;
  }
  if ((sel.selectedOptions[0]?.dataset.transport || "tcp") !== t && firstVisible) {
    sel.value = firstVisible.value;
  }
}

// Match the current port (within the active transport) to a preset, else Custom.
function reflectProtocol() {
  const port = parseInt($("#listen_port").value, 10);
  const t = currentTransport();
  let match = t === "udp" ? "custom-udp" : "custom-tcp";
  for (const opt of $("#protocol").options) {
    if ((opt.dataset.transport || "tcp") === t && opt.dataset.port && parseInt(opt.dataset.port, 10) === port) {
      match = opt.value; break;
    }
  }
  $("#protocol").value = match;

  const hint = $("#port-ssl-hint");
  if (hint) {
    if (t === "udp") hint.textContent = "UDP datagram passthrough — no TLS/SNI on UDP.";
    else if (port === 443) hint.textContent = "Port 443 → TLS termination (configure a certificate below).";
    else hint.textContent = "Plain TCP passthrough — no TLS on this port.";
  }
}

// Transport radio changed: re-filter protocols, adopt the new pick's port.
function onTransportChange() {
  filterProtocols();
  const opt = $("#protocol").selectedOptions[0];
  if (opt && opt.dataset.port) $("#listen_port").value = opt.dataset.port;
  applyTransportRules();
  reflectProtocol();
}

function onProtocolChange() {
  const opt = $("#protocol").selectedOptions[0];
  if (opt && opt.dataset.port) $("#listen_port").value = opt.dataset.port;
  applyTransportRules();
  reflectProtocol();
}

function onListenPortChange() {
  reflectProtocol();
  applyTransportRules();
}

// Your rule + UDP reality: UDP and non-443 TCP ports are plain passthrough, so
// switch SSL off (UDP can't do TLS at all). Don't clobber a kept cert on edit.
function applyTransportRules() {
  const port = parseInt($("#listen_port").value, 10);
  const udp = currentTransport() === "udp";
  if ((udp || port !== 443) && !EDITING) selectSsl("none");
  // TLS termination is impossible on UDP — hide the SSL picker entirely.
  const ssl = $("#ssl-section");
  if (ssl) ssl.classList.toggle("hidden", udp);
  // SNI availability depends on both transport and SSL termination.
  updateSniAvailability();
}

function resetForm() {
  EDITING = null;
  EDIT_HAS_CERT = false;
  $("#map-form").reset();
  setVal("orig_domain", "");   // clear edit identity so a save is treated as a create
  setVal("orig_port", "");
  $("#backends").innerHTML = "";
  addBackendRow();   // empty — placeholder shows the example address (also syncs LB)
  selectSsl("none");
  onMethodChange();
  onLbChange();
  toggleRateSection();   // collapse rate-limit panel (reset() unchecked the toggle)
  $("#bind_prefix").value = CFG.bind_prefix || "24";
  setTransport("tcp");
  filterProtocols();
  reflectProtocol();   // reset() restored port 443 / HTTPS — sync list + hint
  applyTransportRules();
  $("#domain").readOnly = false;
  $("#domain").classList.remove("bg-slate-100");
  $("#edit-banner").classList.add("hidden");
  $("#apply-btn").textContent = "Save / Apply";
  const ft = $("#form-title"); if (ft) ft.textContent = "Add Mapping";
  const fl = $("#nav-form-label"); if (fl) fl.textContent = "Add Mapping";
}

// --- edit an existing mapping ---------------------------------------------
function setVal(id, v) { const e = $("#" + id); if (e) e.value = v ?? ""; }

function editMapping(domain, port) {
  const p = port != null ? String(port) : null;
  const m = MAPPINGS.find((x) => x.domain === domain &&
    (p == null || String(x.listen_port || 443) === p));
  if (!m) return;
  EDITING = mkey(m);
  // Remember the exact mapping being edited so the backend overwrites it (and
  // detects a rename if the domain/port changes on save).
  setVal("orig_domain", m.domain);
  setVal("orig_port", m.listen_port || 443);

  setVal("domain", m.domain);
  $("#domain").readOnly = true;                 // domain is fixed on edit (change the port to remap)
  $("#domain").classList.add("bg-slate-100");
  setTransport(m.transport || "tcp");
  filterProtocols();
  setVal("listen_port", m.listen_port || 443);
  reflectProtocol();                            // sync the protocol dropdown + hint
  applyTransportRules();
  if ([...$("#interface").options].some((o) => o.value === m.interface)) {
    $("#interface").value = m.interface;
    updateIfaceInfo();
  }
  setVal("vlan_id", m.vlan_id);
  setVal("mac", m.mac);

  // allocation method
  const radio = document.querySelector(`input[name="alloc_method"][value="${m.alloc_method || "static"}"]`);
  if (radio) radio.checked = true;
  onMethodChange();
  setVal("bind_ip", m.alloc_method === "dhcp" ? "" : (m.bind_ip || ""));
  setVal("bind_prefix", m.bind_prefix || CFG.bind_prefix || "24");

  // Preselect what this mapping binds: a managed sub-interface (by name) or a
  // physical interface (by interface name) — both live in the same dropdown.
  applyFormMode();
  const sub = $("#subiface-select");
  if (sub && SETTINGS.subinterface_enabled) {
    const want = m.subiface_kind === "managed" ? m.subiface : m.interface;
    if (want && ![...sub.options].some((o) => o.value === want)) {
      const o = document.createElement("option");
      o.value = want; o.dataset.ip = m.bind_ip || "";
      o.textContent = `${want} · ${m.bind_ip || ""}`;
      sub.appendChild(o);
    }
    if (want) sub.value = want;
    syncSubifaceBindIp();
  }

  // backend pool
  $("#backends").innerHTML = "";
  const pool = (m.backends && m.backends.length ? m.backends
    : (m.backend ? [{ server: m.backend }] : [{ server: "" }]));
  pool.forEach((b) => addBackendRow(typeof b === "string" ? { server: b } : b));

  // load balancing — syncLbAuto already opened it if 2+ backends; restore the
  // saved method on top, and reveal options for a non-default 1-backend method.
  setVal("lb_method", m.lb_method || "round_robin");
  setVal("hash_key", m.hash_key || "$remote_addr");
  $("#hash_consistent").checked = !!m.hash_consistent;
  $("#random_two").checked = !!m.random_two;
  if (m.lb_method && m.lb_method !== "round_robin") { $("#lb_enabled").checked = true; toggleLbSection(); }
  onLbChange();

  // failover
  $("#failover").checked = !!m.failover;
  syncFailoverUI();

  // rate limit
  $("#rate_limit_enabled").checked = !!m.rate_limit;
  setVal("limit_conn", m.limit_conn || "");
  setVal("proxy_download_rate", m.proxy_download_rate || "");
  setVal("proxy_upload_rate", m.proxy_upload_rate || "");
  toggleRateSection();

  // timeouts (open the panel if customised)
  setVal("proxy_timeout", m.proxy_timeout || "");
  setVal("proxy_connect_timeout", m.proxy_connect_timeout || "");
  if (m.proxy_timeout || m.proxy_connect_timeout) {
    const d = document.querySelector("details"); if (d) d.open = true;
  }

  $("#sni_guard").checked = !!m.sni_guard;

  // Access list — fall back to the global default for pre-feature mappings.
  populateAccessDropdown();
  $("#access_list").value = (m.access_list !== undefined && m.access_list !== null)
    ? m.access_list : "__default__";
  if (![...$("#access_list").options].some((o) => o.value === $("#access_list").value))
    $("#access_list").value = "__default__";

  // SSL — keep the current cert untouched by default
  EDIT_HAS_CERT = !!m.has_cert;
  if (m.has_cert) {
    setSslKeep(m.ssl_mode);
    $("#proxy_ssl").checked = m.proxy_ssl !== false;
    if (m.ssl_mode === "existing" && m.cert_domain) {
      loadCerts().then(() => setVal("ssl_existing", m.cert_domain));
    }
  } else {
    selectSsl("none");
  }
  updateSniAvailability();

  $("#edit-domain").textContent = m.domain;
  $("#edit-banner").classList.remove("hidden");
  $("#apply-btn").textContent = "Update";
  const ft = $("#form-title"); if (ft) ft.textContent = "Update Mapping";
  const fl = $("#nav-form-label"); if (fl) fl.textContent = "Edit Mapping";
  showPage("form");
}

// --- auth / current user ---------------------------------------------------
// --- host monitoring -------------------------------------------------------
let MON_TIMER = null;

function fmtBytes(n) {
  if (n === null || n === undefined) return "—";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}

function fmtRate(bytesPerSec) {
  if (bytesPerSec === null || bytesPerSec === undefined) return "…";
  return fmtBytes(bytesPerSec) + "/s";
}

// progress-bar colour by load: green < 70 < amber < 90 < red
function usageColor(pct) {
  if (pct == null) return "bg-slate-300";
  if (pct >= 90) return "bg-red-500";
  if (pct >= 70) return "bg-amber-500";
  return "bg-emerald-500";
}

function setBar(barId, pctId, pct, defaultColor) {
  const bar = $("#" + barId), lbl = $("#" + pctId);
  if (pct == null) { bar.style.width = "0%"; lbl.textContent = "—"; return; }
  bar.style.width = Math.max(0, Math.min(100, pct)) + "%";
  bar.className = `h-full rounded-full transition-all duration-500 ${defaultColor || usageColor(pct)}`;
  lbl.textContent = pct.toFixed(1) + "%";
}

function startMonitoring() {
  loadMetrics();
  loadIfaceTraffic();
  renderIfaceTree();
  clearInterval(MON_TIMER);
  MON_TIMER = setInterval(() => { loadMetrics(); loadIfaceTraffic(); }, 2000);
}
function stopMonitoring() { clearInterval(MON_TIMER); MON_TIMER = null; }

function startLivemap() {
  renderRouteMap(true);   // draw with whatever we have…
  loadMappings();         // …then refresh from the API (re-renders if changed)
}
function stopLivemap() { /* nothing to tear down */ }

// --- live routing map (built from real mappings) ---------------------------
let ROUTE_SIG = null;
let ROUTE_EXPANDED = new Set();   // domains whose backend pool is expanded in-graph
let ROUTE_BANDS = {};             // row index -> {i, m, beList, expanded, top, h}
let ROUTE_LINK_BY_ID = {};        // link id -> link object (for packet gating)

// Normalize a mapping's pool to a list of backend objects ({server, ...}).
function backendsOf(m) {
  const raw = (m.backends || (m.backend ? [m.backend] : []));
  return raw.map((b) => (typeof b === "object" ? b : { server: b }));
}

// The live health probe row for one backend of a mapping, or null if unprobed.
// `key` is the composite domain:port health key.
function backendHealthRow(key, server) {
  const rows = (HEALTH[key] && HEALTH[key].backends) || [];
  return rows.find((r) => r.server === server) || null;
}

// Should traffic animate to this backend? Confirmed-down or disabled backends
// carry no flow; up (or not-yet-probed / UDP) backends do.
function backendFlowing(domain, be) {
  if (be && be.down) return false;                 // config-disabled
  const r = backendHealthRow(domain, be.server);
  if (!r) return true;                             // no probe yet → assume flowing
  if (r.enabled === false) return false;
  return r.up !== false;                           // false = down (stop); true/null flow
}

// Colour + status text for a backend endpoint node from its live health.
function beHealthVisual(node) {
  const be = node.be;
  const r = backendHealthRow(mkey(node.data), node.server);
  const disabled = !!(be && be.down);
  const up = r ? r.up : null;                       // true | false | null(udp/unprobed)
  const bad = disabled || up === false;
  const color = disabled ? "#94A3B8"
    : up === false ? "#ef4444"
    : up === true ? "#10B981"
    : "#8B5CF6";
  const status = disabled ? "disabled"
    : up === false ? ("down" + (r && r.error ? " · " + r.error : ""))
    : up === true ? ("up" + (r && r.latency_ms != null ? " · " + r.latency_ms + "ms" : ""))
    : "not probed";
  return { color, status, bad };
}

function toggleRouteExpand(domain) {
  if (ROUTE_EXPANDED.has(domain)) ROUTE_EXPANDED.delete(domain);
  else ROUTE_EXPANDED.add(domain);
  renderRouteMap(true, true);   // relayout for the new node count, keep pan/zoom
}

function livemapVisible() {
  const p = $("#page-livemap");
  return p && !p.classList.contains("hidden");
}

// One inbound node (bind IP + its interface/sub-interface) → splitter → one
// upstream node per mapping (the backend pool collapsed; click to expand).
function routeSig() {
  return JSON.stringify(MAPPINGS.map((m) => [
    m.domain, m.bind_ip, m.listen_port, m.subiface, m.interface,
    (m.backends || (m.backend ? [m.backend] : [])).length,
  ]));
}

function subLabel(m) {
  if (m.subiface_kind === "managed" && m.subiface)
    return `${m.subiface} · ${m.alloc_method || "static"}${m.vlan_id ? " · vlan " + m.vlan_id : ""}`;
  return `${m.interface || "—"} · direct`;
}

// --- node-graph model (n8n-style: draggable nodes, curved links) -----------
let RNODES = {};   // id -> {id, kind, x, y, w, h, data}
let RLINKS = [];   // [{id, s, t}] s=source node (output side), t=target (input side)
let ROUTE_W = 0, ROUTE_H = 0;   // canvas size in viewBox units (incl. pan margins)

// n8n connector: leave/enter horizontally (control points offset on x).
function routeLinkPath(L) {
  const s = RNODES[L.s], t = RNODES[L.t];
  if (!s || !t) return "";
  const sx = s.x + s.w, sy = s.y + s.h / 2;      // source output port (right edge)
  const tx = t.x, ty = t.y + t.h / 2;            // target input port (left edge)
  const off = Math.max(Math.abs(tx - sx) * 0.5, 60);
  return `M${sx} ${sy} C ${sx + off} ${sy}, ${tx - off} ${ty}, ${tx} ${ty}`;
}

function renderRouteMap(force, keepView) {
  const svg = $("#route-map");
  if (!svg) return;
  const sig = routeSig();
  if (!force && sig === ROUTE_SIG) return;   // avoid resetting positions/animations
  ROUTE_SIG = sig;

  const all = MAPPINGS, CAP = 12, rows = all.slice(0, CAP);
  const statusText = $("#route-status-text");
  if (statusText) statusText.textContent = all.length ? "TRAFFIC FLOWING" : "IDLE";

  RNODES = {}; RLINKS = []; ROUTE_BANDS = {};
  if (!rows.length) {
    ROUTE_W = 0; svg.style.width = "100%";
    svg.setAttribute("viewBox", "0 0 1000 220");
    svg.innerHTML = `${routeDefs()}
      <text x="500" y="115" text-anchor="middle" fill="#94A3B8" font-family="monospace" font-size="15">No mappings yet — add one to see live routing.</text>`;
    return;
  }

  // --- default layout (rebuilt on every forced render → resets positions) ---
  // Big PAD margins around the content make the canvas pannable in every
  // direction (up / left / right / down) into the infinite dotted grid. Each
  // mapping occupies a vertical "band"; an expanded pool grows the band to fit
  // one sub-row per backend endpoint (others shift down to make room).
  const PAD = 6000, contentW = 1280, top = 60;
  const NH = 58, BE_H = 44, BE_GAP = 10, GAP = 28;
  const inW = 200, outW = 196, sW = 148, sH = 92;
  const inX = PAD + 80, outX = PAD + contentW - 80 - outW, sX = PAD + (contentW - sW) / 2;

  let cursor = 0;
  rows.forEach((m, i) => {
    const beList = backendsOf(m);
    const expanded = ROUTE_EXPANDED.has(m.domain) && beList.length >= 1;
    const bandH = expanded
      ? Math.max(NH, beList.length * BE_H + (beList.length - 1) * BE_GAP)
      : NH;
    ROUTE_BANDS[i] = { i, m, beList, expanded, top: PAD + top + cursor, h: bandH };
    cursor += bandH + GAP;
  });
  const contentH = Math.max(NH, cursor - GAP);
  const cH = top + contentH + 40;
  const sMid = PAD + top + contentH / 2;
  ROUTE_W = contentW + PAD * 2;
  ROUTE_H = cH + PAD * 2;

  RNODES.splitter = { id: "splitter", kind: "hub", x: sX, y: sMid - sH / 2, w: sW, h: sH };
  rows.forEach((m, i) => {
    const band = ROUTE_BANDS[i];
    const inY = band.top + (band.h - NH) / 2;
    RNODES["in" + i] = { id: "in" + i, kind: "in", x: inX, y: inY, w: inW, h: NH, data: m, idx: i };
    RLINKS.push({ id: "lin" + i, s: "in" + i, t: "splitter", dir: "in", i });
    if (band.expanded) {
      band.beList.forEach((be, j) => {
        const id = "be" + i + "_" + j;
        RNODES[id] = { id, kind: "be", x: outX, y: band.top + j * (BE_H + BE_GAP),
                       w: outW, h: BE_H, data: m, idx: i, be, server: be.server, label: backendName(be) };
        RLINKS.push({ id: "l" + id, s: "splitter", t: id, dir: "be", i, be, server: be.server });
      });
    } else {
      const outY = band.top + (band.h - NH) / 2;
      RNODES["out" + i] = { id: "out" + i, kind: "out", x: outX, y: outY, w: outW, h: NH, data: m, idx: i };
      RLINKS.push({ id: "lout" + i, s: "splitter", t: "out" + i, dir: "out", i });
    }
  });

  const more = all.length > CAP
    ? `<text x="${PAD + contentW / 2}" y="${PAD + cH - 6}" text-anchor="middle" fill="#94A3B8" font-family="monospace" font-size="11">+ ${all.length - CAP} more mapping(s)…</text>` : "";

  svg.setAttribute("viewBox", `0 0 ${ROUTE_W} ${ROUTE_H}`);
  svg.innerHTML = `${routeDefs()}
    <g id="route-links"></g><g id="route-packets"></g><g id="route-nodes"></g>${more}`;
  drawRouteLinks();
  drawRouteNodes();
  applyRouteZoom();
  if (!keepView) centerRouteOnContent(PAD, PAD, contentW, cH);
}

const ROUTE_PKT_PER_LINK = 3;   // dots streaming along each link for a "flow" feel

function drawRouteLinks() {
  // Neutral lines at rest — colour is reserved for health (red/amber). Collapsed
  // rows stay calm (packets appear on hover); an expanded pool streams traffic
  // continuously to each *healthy* backend so you can see the live flow.
  ROUTE_LINK_BY_ID = {};
  const links = RLINKS.map((L) => {
    ROUTE_LINK_BY_ID[L.id] = L;
    return `<path id="${L.id}" class="rlink" data-i="${L.i}" d="${routeLinkPath(L)}" fill="none" stroke="#C3CAD4" stroke-width="2"/>`;
  }).join("");
  const packets = RLINKS.map((L) => {
    const dur = 1.9 + (L.i % 4) * 0.13;
    const fill = L.dir === "in" ? "#10B981" : "#0EA5E9";
    let out = "";
    for (let k = 0; k < ROUTE_PKT_PER_LINK; k++) {
      const begin = (L.i * 0.28 + (L.dir !== "in" ? 0.4 : 0) + (k * dur) / ROUTE_PKT_PER_LINK).toFixed(2);
      out += `<circle class="rpacket" data-lid="${L.id}" data-i="${L.i}" r="4" fill="${fill}" opacity="0">`
        + `<animateMotion dur="${dur.toFixed(2)}s" repeatCount="indefinite" begin="${begin}s"><mpath href="#${L.id}"/></animateMotion></circle>`;
    }
    return out;
  }).join("");
  $("#route-links").innerHTML = links;
  $("#route-packets").innerHTML = packets;
  refreshRoutePackets();
}

// Does the mapping as a whole carry traffic? (at least one reachable backend)
function mappingFlowing(domain) {
  const h = HEALTH[domain];
  if (!h) return true;            // not probed yet → assume flowing
  return h.status !== "red";      // red = every backend down → no flow
}

// Baseline packet visibility — traffic streams continuously along every *healthy*
// path (no hover needed); a down backend / all-down mapping shows a static red
// link with no packets.
function routePacketBase(L) {
  if (!L) return "0";
  const band = ROUTE_BANDS[L.i];
  if (!band) return "0";
  const dom = mkey(band.m);
  if (L.dir === "be") return backendFlowing(dom, L.be) ? "1" : "0";
  return mappingFlowing(dom) ? "1" : "0";   // inbound + collapsed outbound
}

function refreshRoutePackets() {
  $$("#route-packets .rpacket").forEach((c) => {
    c.setAttribute("opacity", routePacketBase(ROUTE_LINK_BY_ID[c.dataset.lid]));
  });
}

// Hovering a node lights up its whole path (in → splitter → out), starts that
// row's packet animation and dims every other row. Pass null to clear.
function setRouteHighlight(i) {
  RLINKS.forEach((L) => {
    const el = document.getElementById(L.id);
    if (!el) return;
    const active = i == null || L.i === i;
    el.setAttribute("stroke-opacity", active ? "1" : "0.08");
    el.setAttribute("stroke-width", (i != null && L.i === i) ? "3.6" : "2");
  });
  // Packets always follow the health baseline (down links never animate). Hover
  // just dims the other rows' flow so the focused row stands out.
  $$("#route-packets .rpacket").forEach((c) => {
    const base = routePacketBase(ROUTE_LINK_BY_ID[c.dataset.lid]);
    const dim = (i != null && Number(c.dataset.i) !== i);
    c.setAttribute("opacity", base === "0" ? "0" : (dim ? "0.12" : "1"));
  });
  Object.values(RNODES).forEach((node) => {
    const g = document.getElementById("rn-" + node.id);
    if (!g) return;
    const related = i == null || node.kind === "hub" || node.idx === i;
    g.setAttribute("opacity", related ? "1" : "0.22");
  });
  if (i == null) updateRouteHealth();   // restore neutral/health colours + widths
}

function updateRouteLinks() {
  RLINKS.forEach((L) => { const el = document.getElementById(L.id); if (el) el.setAttribute("d", routeLinkPath(L)); });
}

function routePort(x, y, c) { return `<circle cx="${x}" cy="${y}" r="4" fill="#fff" stroke="${c}" stroke-width="2"/>`; }

function routeNodeMarkup(node) {
  const esc = escapeHtml, m = node.data, w = node.w, h = node.h, my = h / 2;
  const wrap = (inner) => `<g id="rn-${node.id}" class="rnode" data-id="${node.id}" transform="translate(${node.x},${node.y})" style="cursor:grab" font-family="ui-monospace, monospace">${inner}</g>`;
  if (node.kind === "hub") {
    return wrap(`
      <rect width="${w}" height="${h}" rx="16" fill="url(#route-host)" stroke="#10B981" stroke-width="3" stroke-opacity="0.55"/>
      <circle cx="${w / 2}" cy="24" r="6" fill="#10B981"/>
      <text x="${w / 2}" y="${my + 2}" text-anchor="middle" fill="#0F172A" font-weight="700" font-size="22" font-family="ui-sans-serif, system-ui">splitter</text>
      <text x="${w / 2}" y="${my + 22}" text-anchor="middle" fill="#94A3B8" font-size="11">proxy host</text>
      ${routePort(0, my, "#10B981")}${routePort(w, my, "#10B981")}`);
  }
  if (node.kind === "in") {
    const proto = (m.transport || "tcp").toUpperCase();
    return wrap(`
      <rect width="${w}" height="${h}" rx="10" fill="#F8FAFC" stroke="#E2E8F0"/>
      <circle cx="18" cy="18" r="4" fill="#10B981"/>
      <text x="30" y="22" fill="#0F172A" font-weight="700" font-size="12.5">${esc(m.domain)}</text>
      <text x="14" y="38" fill="#1E293B" font-size="11">${esc((m.bind_ip || "—") + ":" + (m.listen_port || 443))} <tspan fill="#94A3B8">${esc(proto)}</tspan></text>
      <text x="14" y="52" fill="#94A3B8" font-size="10">↳ ${esc(subLabel(m))}</text>
      ${routePort(w, my, "#94A3B8")}`);
  }
  // A single backend endpoint (shown when its pool is expanded). Health-coloured
  // dot + live status; a red ✗ when down/disabled. Click collapses the pool.
  if (node.kind === "be") {
    const v = beHealthVisual(node);
    return `<g id="rn-${node.id}" class="rnode route-be" data-id="${node.id}" data-i="${node.idx}" transform="translate(${node.x},${node.y})" style="cursor:pointer" font-family="ui-monospace, monospace">
      <rect id="rr-${node.id}" width="${w}" height="${h}" rx="9" fill="#F8FAFC" stroke="${v.color}" stroke-opacity="${v.bad ? "0.9" : "0.5"}" stroke-width="${v.bad ? "2" : "1.5"}"/>
      <circle id="rd-${node.id}" cx="16" cy="${my}" r="4.5" fill="${v.color}"/>
      <text x="30" y="${my - 3}" fill="#1E293B" font-size="12">${esc(node.label || node.server)}</text>
      <text id="rt-${node.id}" x="30" y="${my + 12}" fill="${v.color}" font-size="9.5">${esc(v.status)}</text>
      <text id="rx-${node.id}" x="${w - 14}" y="${my + 5}" text-anchor="middle" fill="#ef4444" font-size="15" font-weight="700" style="display:${v.bad ? "" : "none"}">✗</text>
      ${routePort(0, my, "#94A3B8")}</g>`;
  }
  // upstream pool node (click to expand into per-backend endpoints)
  const backs = (m.backends || (m.backend ? [m.backend] : []));
  const lbl = backs.length === 1 ? "1 backend" : `${backs.length} backends`;
  return `<g id="rn-${node.id}" class="rnode route-pool" data-id="${node.id}" data-i="${node.idx}" transform="translate(${node.x},${node.y})" style="cursor:pointer" font-family="ui-monospace, monospace">
      <rect id="rr-${node.id}" width="${w}" height="${h}" rx="10" fill="#F8FAFC" stroke="#10B981" stroke-opacity="0.45"/>
      <text x="16" y="${my - 3}" fill="#1E293B" font-size="12.5">${esc(m.domain)}</text>
      <text x="16" y="${my + 13}" fill="#059669" font-size="10">${esc(lbl)} · ${esc(lbLabel(m))}</text>
      <text id="rh-${node.id}" x="${w - 13}" y="18" text-anchor="middle" fill="#ef4444" font-size="14" font-weight="700" style="display:none">✗</text>
      ${routePort(0, my, "#94A3B8")}</g>`;
}

// Repaint one expanded backend endpoint node from its live health.
function paintBackendNode(node) {
  const v = beHealthVisual(node);
  const rect = document.getElementById("rr-" + node.id);
  if (rect) {
    rect.setAttribute("stroke", v.color);
    rect.setAttribute("stroke-opacity", v.bad ? "0.9" : "0.5");
    rect.setAttribute("stroke-width", v.bad ? "2" : "1.5");
  }
  const dot = document.getElementById("rd-" + node.id);
  if (dot) dot.setAttribute("fill", v.color);
  const txt = document.getElementById("rt-" + node.id);
  if (txt) { txt.setAttribute("fill", v.color); txt.textContent = v.status; }
  const x = document.getElementById("rx-" + node.id);
  if (x) x.style.display = v.bad ? "" : "none";
}

// Style one connector line (colour + width + dashed when unhealthy).
function paintRouteLink(id, color, bad) {
  const p = document.getElementById(id);
  if (!p) return;
  p.setAttribute("stroke", color);
  p.setAttribute("stroke-width", bad ? "2.6" : "2");
  if (bad) p.setAttribute("stroke-dasharray", "7 5"); else p.removeAttribute("stroke-dasharray");
}

// Reflect live health across the whole map: connector colours, the pool node's
// red ✗ / border, each expanded backend endpoint, and which links carry flow.
function updateRouteHealth() {
  Object.values(ROUTE_BANDS).forEach((band) => {
    const dom = mkey(band.m), h = HEALTH[dom];
    const bad = h && (h.status === "red" || h.status === "yellow");
    const red = h && h.status === "red";
    const lineColor = red ? "#ef4444" : (bad ? "#f59e0b" : "#C3CAD4");

    paintRouteLink("lin" + band.i, lineColor, bad);   // client → splitter

    if (band.expanded) {
      band.beList.forEach((be, j) => {
        const node = RNODES["be" + band.i + "_" + j];
        if (node) paintBackendNode(node);
        // splitter → this backend: red + dashed when it can't take traffic.
        const flow = backendFlowing(dom, be);
        const link = document.getElementById("lbe" + band.i + "_" + j);
        if (link) {
          link.setAttribute("stroke", flow ? "#C3CAD4" : "#ef4444");
          link.setAttribute("stroke-width", flow ? "2" : "2.4");
          if (flow) link.removeAttribute("stroke-dasharray"); else link.setAttribute("stroke-dasharray", "7 5");
        }
      });
    } else {
      const mark = document.getElementById("rh-out" + band.i);
      if (mark) mark.style.display = bad ? "" : "none";
      const rect = document.getElementById("rr-out" + band.i);
      if (rect) {
        rect.setAttribute("stroke", red ? "#ef4444" : "#10B981");
        rect.setAttribute("stroke-opacity", red ? "0.9" : "0.45");
      }
      paintRouteLink("lout" + band.i, lineColor, bad);   // splitter → pool
    }
  });
  refreshRoutePackets();   // keep flow in sync with the latest health
}

function drawRouteNodes() {
  $("#route-nodes").innerHTML = Object.values(RNODES).map(routeNodeMarkup).join("");
  updateRouteHealth();   // reflect current health on the upstream nodes
  $$("#route-nodes .rnode").forEach((g) => {
    g.addEventListener("mousedown", onRouteNodeDown);
    const node = RNODES[g.dataset.id];
    if (node && node.kind !== "hub") {   // light up this mapping's full path on hover
      g.addEventListener("mouseenter", () => { if (!ROUTE_DRAG) setRouteHighlight(node.idx); });
      g.addEventListener("mouseleave", () => setRouteHighlight(null));
    }
  });
  $$("#route-nodes .route-pool").forEach((g) => {
    const m = RNODES[g.dataset.id].data;
    // Expansion is click-only: at rest the node just shows the mapping's
    // endpoint(s). A click opens the pool inline — each backend becomes its own
    // endpoint node with a live health check and animated traffic (down backends
    // stay static). Hover does nothing.
    g.addEventListener("click", (e) => {
      e.stopPropagation();
      if (ROUTE_DRAG_MOVED) return;          // a drag, not a click → don't toggle
      toggleRouteExpand(m.domain);
    });
  });
  // Clicking any expanded backend endpoint collapses the pool back to one node.
  $$("#route-nodes .route-be").forEach((g) => {
    const m = RNODES[g.dataset.id].data;
    g.addEventListener("click", (e) => {
      e.stopPropagation();
      if (ROUTE_DRAG_MOVED) return;
      toggleRouteExpand(m.domain);
    });
  });
}

// --- node dragging ---------------------------------------------------------
let ROUTE_DRAG = null, ROUTE_DRAG_MOVED = false, ROUTE_DRAG_START = null, ROUTE_NODE_START = null;

function svgPointRoute(clientX, clientY) {
  const svg = $("#route-map");
  const p = svg.createSVGPoint(); p.x = clientX; p.y = clientY;
  const m = svg.getScreenCTM();
  return m ? p.matrixTransform(m.inverse()) : { x: clientX, y: clientY };
}

function onRouteNodeDown(e) {
  if (e.button !== 0) return;
  const g = e.currentTarget, id = g.dataset.id;
  ROUTE_DRAG = RNODES[id]; ROUTE_DRAG_MOVED = false;
  ROUTE_DRAG_START = svgPointRoute(e.clientX, e.clientY);
  ROUTE_NODE_START = { x: ROUTE_DRAG.x, y: ROUTE_DRAG.y };
  // NB: don't re-parent the node here — moving it in the DOM on mousedown would
  // cancel the click event and break click-to-expand. Raise it to front only
  // once an actual drag begins (see onRouteNodeMove).
  g.style.cursor = "grabbing";
  e.stopPropagation();   // don't start a canvas pan
  e.preventDefault();
}

function onRouteNodeMove(e) {
  if (!ROUTE_DRAG) return;
  const p = svgPointRoute(e.clientX, e.clientY);
  if (!ROUTE_DRAG_MOVED && Math.hypot(p.x - ROUTE_DRAG_START.x, p.y - ROUTE_DRAG_START.y) > 3) {
    ROUTE_DRAG_MOVED = true;
    const g = document.getElementById("rn-" + ROUTE_DRAG.id);
    if (g) g.parentNode.appendChild(g);   // raise to front now the drag has really started
  }
  ROUTE_DRAG.x = ROUTE_NODE_START.x + (p.x - ROUTE_DRAG_START.x);
  ROUTE_DRAG.y = ROUTE_NODE_START.y + (p.y - ROUTE_DRAG_START.y);
  const g = document.getElementById("rn-" + ROUTE_DRAG.id);
  if (g) g.setAttribute("transform", `translate(${ROUTE_DRAG.x},${ROUTE_DRAG.y})`);
  updateRouteLinks();
}

function onRouteNodeUp() {
  if (!ROUTE_DRAG) return;
  const g = document.getElementById("rn-" + ROUTE_DRAG.id);
  if (g) g.style.cursor = "grab";
  ROUTE_DRAG = null;
}

function routeDefs() {
  return `<defs>
    <linearGradient id="route-host" x1="0%" y1="0%" x2="0%" y2="100%"><stop offset="0%" stop-color="#FFFFFF"/><stop offset="100%" stop-color="#ECFDF5"/></linearGradient>
  </defs>`;
}

// Scroll-to-scale: the map lives in a windowed, scrollable container; the wheel
// zooms it (up = larger), and the scrollbars pan a zoomed-in map.
let ROUTE_ZOOM = 1;
function applyRouteZoom() {
  const svg = $("#route-map");
  // Pixel-based sizing (1 viewBox unit = 1px × zoom) so the big pan-margins
  // don't shrink the content — the canvas just becomes scrollable in every
  // direction. Empty state falls back to fitting the width.
  if (svg) svg.style.width = ROUTE_W ? Math.round(ROUTE_W * ROUTE_ZOOM) + "px" : "100%";
  // the dotted canvas grid scales with zoom too (infinite-canvas feel)
  const box = $("#route-scroll");
  if (box) { const s = (22 * ROUTE_ZOOM).toFixed(1) + "px"; box.style.backgroundSize = s + " " + s; }
  const lbl = $("#route-zoom-reset");
  if (lbl) lbl.textContent = Math.round(ROUTE_ZOOM * 100) + "%";
}

// Centre the scroll window on the actual content, leaving the big margins to
// pan into on every side.
function centerRouteOnContent(ox, oy, cw, ch) {
  const box = $("#route-scroll");
  if (!box) return;
  requestAnimationFrame(() => {
    box.scrollLeft = Math.max(0, (ox + cw / 2) * ROUTE_ZOOM - box.clientWidth / 2);
    box.scrollTop = Math.max(0, (oy + ch / 2) * ROUTE_ZOOM - box.clientHeight / 2);
  });
}
function setRouteZoom(z) {
  ROUTE_ZOOM = Math.min(3, Math.max(0.6, Math.round(z * 100) / 100));
  applyRouteZoom();
}

// Zoom toward a point (Google-Maps style): keep the content under the cursor
// fixed while the diagram scales. Falls back to centre when no point is given.
function zoomRouteAt(z, clientX, clientY) {
  const box = $("#route-scroll");
  if (!box) return setRouteZoom(z);
  const rect = box.getBoundingClientRect();
  const cx = clientX != null ? clientX - rect.left : rect.width / 2;
  const cy = clientY != null ? clientY - rect.top : rect.height / 2;
  const relX = (box.scrollLeft + cx) / Math.max(1, box.scrollWidth);
  const relY = (box.scrollTop + cy) / Math.max(1, box.scrollHeight);
  setRouteZoom(z);                       // changes the SVG size (forces reflow on read below)
  box.scrollLeft = relX * box.scrollWidth - cx;
  box.scrollTop = relY * box.scrollHeight - cy;
}

async function loadMetrics() {
  try {
    const j = await (await fetch("/api/metrics")).json();
    if (!j.ok || !j.metrics) return;
    renderMetrics(j.metrics);
  } catch (_) { /* non-fatal — keep last values */ }
}

function renderMetrics(m) {
  const cpu = m.cpu || {}, mem = m.memory || {}, disk = m.disk || {}, net = m.network || {};

  setBar("mon-cpu-bar", "mon-cpu-pct", cpu.pct);
  $("#mon-cpu-meta").textContent =
    `${cpu.cores || "?"} cores` + (cpu.load ? ` · load ${cpu.load.join(" / ")}` : "");

  setBar("mon-mem-bar", "mon-mem-pct", mem.pct, "bg-indigo-500");
  $("#mon-mem-meta").textContent = `${fmtBytes(mem.used)} used of ${fmtBytes(mem.total)} · ${fmtBytes(mem.free)} free`;

  setBar("mon-disk-bar", "mon-disk-pct", disk.pct, "bg-amber-500");
  $("#mon-disk-meta").textContent = `${fmtBytes(disk.used)} used of ${fmtBytes(disk.total)} · ${fmtBytes(disk.free)} free${disk.path ? " · " + disk.path : ""}`;

  $("#mon-net-rx").textContent = fmtRate(net.rx_rate);
  $("#mon-net-tx").textContent = fmtRate(net.tx_rate);
  $("#mon-net-meta").textContent =
    (net.rx_bytes ? `total ↓ ${fmtBytes(net.rx_bytes)} · ↑ ${fmtBytes(net.tx_bytes)}` : "since boot")
    + (m.uptime ? ` · uptime ${fmtUptime(m.uptime)}` : "");

  $("#mon-updated").textContent = (m.simulated ? "simulated · " : "") + new Date().toLocaleTimeString();
}

function fmtUptime(sec) {
  sec = Math.floor(sec);
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), mn = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${mn}m`;
  return `${mn}m`;
}

// --- Interfaces page -------------------------------------------------------
let IFACE_TIMER = null;
let IFACE_RATES = {};   // name -> {rx_rate, tx_rate, up} from /api/interfaces/traffic

function startInterfaces() {
  // Reflect the persisted toggle and render the overview immediately.
  $("#subiface-toggle").checked = !!SETTINGS.subinterface_enabled;
  applySubifaceManagerVisibility();
  if (isAdmin()) loadNetworkSettings();
  fillSiInterfaceSelect();
  loadSubinterfaces();
}

// The sub-interface manager only applies when the toggle is on (and to admins).
function applySubifaceManagerVisibility() {
  const show = isAdmin() && !!SETTINGS.subinterface_enabled;
  const card = $("#subiface-manager");
  if (card) card.classList.toggle("hidden", !show);
}
function stopInterfaces() {}

// --- host network settings (DNS + /etc/hosts) ------------------------------
function dnsRowHtml(ip) {
  return `<div class="dns-row flex gap-2">
    <input class="dns-ip flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm font-mono focus:ring-2 focus:ring-emerald-500 outline-none" placeholder="8.8.8.8" value="${escapeHtml(ip || "")}" />
    <button type="button" class="dns-rm px-3 rounded-lg bg-slate-200 hover:bg-red-100 hover:text-red-600 text-slate-500">✕</button>
  </div>`;
}

function addDnsRow(ip) {
  const host = $("#dns-rows");
  host.insertAdjacentHTML("beforeend", dnsRowHtml(ip));
  host.lastElementChild.querySelector(".dns-rm").addEventListener("click", (e) => e.target.closest(".dns-row").remove());
}

async function loadNetworkSettings() {
  try {
    const dns = await (await fetch("/api/network/dns")).json();
    if (dns.ok) {
      $("#dns-rows").innerHTML = "";
      (dns.servers && dns.servers.length ? dns.servers : [""]).forEach(addDnsRow);
    }
    const hosts = await (await fetch("/api/network/hosts")).json();
    if (hosts.ok) $("#hosts-text").value = hosts.text || "";
  } catch (_) { /* non-fatal */ }
}

async function saveDns() {
  const ips = $$("#dns-rows .dns-ip").map((i) => i.value.trim()).filter(Boolean);
  const fd = new FormData();
  ips.forEach((ip) => fd.append("servers", ip));
  const msg = $("#dns-msg");
  try {
    const j = await (await fetch("/api/network/dns", { method: "POST", body: fd })).json();
    if (!j.ok) throw new Error(j.error || "Failed");
    $("#dns-rows").innerHTML = "";
    (j.servers.length ? j.servers : [""]).forEach(addDnsRow);
    msg.textContent = "Saved ✓"; msg.className = "text-xs text-emerald-600";
    toast("DNS servers saved.");
  } catch (e) {
    msg.textContent = String(e.message || e); msg.className = "text-xs text-red-600";
    toast(String(e.message || e), false);
  }
}

async function saveHosts() {
  const fd = new FormData();
  fd.append("text", $("#hosts-text").value);
  const msg = $("#hosts-msg");
  try {
    const j = await (await fetch("/api/network/hosts", { method: "POST", body: fd })).json();
    if (!j.ok) throw new Error(j.error || "Failed");
    $("#hosts-text").value = j.text || "";
    msg.textContent = "Saved ✓"; msg.className = "text-xs text-emerald-600";
    toast("/etc/hosts saved.");
  } catch (e) {
    msg.textContent = String(e.message || e); msg.className = "text-xs text-red-600";
    toast(String(e.message || e), false);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// One lightweight reachability probe against a public endpoint. Resolves true
// only if the host answered within the timeout.
async function hostReachable(timeoutMs = 3500) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch("/api/auth/status", { cache: "no-store", signal: ctrl.signal });
    return r.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(t);
  }
}

// Full-screen "rebooting" takeover shown while the host is down. Watches the
// host go down and come back, then reloads — the URL hash means the reload
// lands the user back on the page they were on.
function showRebootOverlay() {
  let ov = $("#reboot-overlay");
  if (!ov) {
    ov = document.createElement("div");
    ov.id = "reboot-overlay";
    ov.className = "fixed inset-0 z-[9999] flex items-center justify-center bg-slate-900/95 backdrop-blur-sm";
    ov.innerHTML = `
      <div class="text-center px-6 max-w-md">
        <div class="mx-auto mb-6 h-14 w-14 rounded-full border-4 border-slate-600 border-t-red-500 animate-spin"></div>
        <h2 class="text-xl font-semibold text-white">Rebooting host…</h2>
        <p id="reboot-overlay-status" class="mt-2 text-sm text-slate-400">The host is shutting down…</p>
        <p class="mt-6 text-xs text-slate-500">This page reloads automatically once the host is back online.</p>
      </div>`;
    document.body.appendChild(ov);
  }
  ov.classList.remove("hidden");
  return $("#reboot-overlay-status");
}

async function watchRebootAndReload() {
  const status = showRebootOverlay();
  const setStatus = (t) => { if (status) status.textContent = t; };

  // Phase 1 — wait for the host to actually go down (bounded, in case a probe
  // races ahead of the scheduled shutdown). We break as soon as it's unreachable.
  const downDeadline = Date.now() + 45000;
  while (Date.now() < downDeadline) {
    await sleep(2500);
    if (!(await hostReachable())) break;
  }

  // Phase 2 — wait for it to come back. Require two consecutive successes so a
  // brief blip while services start doesn't reload into a half-up host.
  setStatus("Waiting for the host to come back online…");
  while (true) {
    await sleep(3000);
    if ((await hostReachable()) && (await hostReachable())) break;
  }

  setStatus("Back online — reloading…");
  await sleep(900);
  location.reload();
}

async function rebootHost() {
  if (!confirm("Reboot the host now?\n\nThe operating system restarts: every proxied connection drops and this dashboard is unreachable until the host comes back up (usually a minute or two). Mappings and settings are preserved.")) return;
  const btn = $("#reboot-host");
  const msg = $("#reboot-msg");
  btn.disabled = true;
  btn.classList.add("opacity-60", "cursor-not-allowed");
  try {
    const j = await (await fetch("/api/system/reboot", { method: "POST" })).json();
    if (!j.ok) throw new Error(j.error || "Failed");
    if (j.simulate) {
      msg.classList.remove("hidden");
      msg.textContent = j.message || "Reboot scheduled.";
      msg.className = "text-xs mt-3 text-slate-500";
      toast("Simulation: reboot not actually run.", true);
      btn.disabled = false;
      btn.classList.remove("opacity-60", "cursor-not-allowed");
      return;
    }
    // Real reboot: take over the screen and wait for the host to return.
    toast("Host is rebooting…", true);
    watchRebootAndReload();
  } catch (e) {
    btn.disabled = false;
    btn.classList.remove("opacity-60", "cursor-not-allowed");
    msg.classList.remove("hidden");
    msg.textContent = String(e.message || e);
    msg.className = "text-xs mt-3 text-red-600";
    toast(String(e.message || e), false);
  }
}

// --- managed sub-interfaces (registry CRUD on the Interfaces page) ----------
let SUBIFACES = [];

async function loadSubinterfaces() {
  try {
    const j = await (await fetch("/api/subinterfaces")).json();
    if (j.ok) SUBIFACES = j.subinterfaces || [];
  } catch (_) { /* keep last */ }
  renderSubifaceTable();
  fillSubifaceSelect();
}

// Populate the mapping form's bind dropdown: physical interfaces (bind their
// existing IP directly) AND managed sub-interfaces (name · ip).
function fillSubifaceSelect() {
  const sel = $("#subiface-select");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = "";

  const physical = IFACES.filter((i) => (i.addresses || []).length);
  if (physical.length) {
    const g = document.createElement("optgroup");
    g.label = "Physical interfaces";
    physical.forEach((i) => {
      const o = document.createElement("option");
      o.value = i.name;
      o.dataset.ip = i.addresses[0].ip || "";
      o.textContent = `${i.name} · ${i.addresses[0].ip} (interface)`;
      g.appendChild(o);
    });
    sel.appendChild(g);
  }
  if (SUBIFACES.length) {
    const g = document.createElement("optgroup");
    g.label = "Sub-interfaces";
    SUBIFACES.forEach((s) => {
      const o = document.createElement("option");
      o.value = s.name;
      o.dataset.ip = s.bind_ip || "";
      o.textContent = `${s.name} · ${s.bind_ip || "(no ip)"}${s.vlan_id ? " · vlan " + s.vlan_id : ""}`;
      g.appendChild(o);
    });
    sel.appendChild(g);
  }
  if (!sel.options.length) {
    sel.innerHTML = '<option value="">no interfaces or sub-interfaces available</option>';
  } else if ([...sel.options].some((o) => o.value === cur)) {
    sel.value = cur;
  }
  syncSubifaceBindIp();
}

// Mirror the chosen sub-interface's IP into the hidden bind_ip so Preview shows
// the right listener (the backend recomputes it authoritatively on apply).
function syncSubifaceBindIp() {
  const sel = $("#subiface-select");
  const bi = $("#bind_ip");
  if (!sel || !bi || !SETTINGS.subinterface_enabled) return;
  const opt = sel.selectedOptions[0];
  if (opt && opt.dataset.ip) bi.value = opt.dataset.ip;
}

function fillSiInterfaceSelect() {
  const sel = $("#si-interface");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = "";
  IFACES.forEach((i) => {
    const o = document.createElement("option");
    o.value = i.name;
    const ips = (i.addresses || []).map((a) => a.ip).join(", ");
    o.textContent = `${i.name}${ips ? " · " + ips : ""}`;
    sel.appendChild(o);
  });
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

function renderSubifaceTable() {
  const tb = $("#subiface-rows");
  if (!tb) return;
  tb.innerHTML = "";
  $("#subiface-empty").classList.toggle("hidden", SUBIFACES.length > 0);
  SUBIFACES.forEach((s) => {
    const tr = document.createElement("tr");
    tr.className = "border-b border-slate-100 hover:bg-slate-50";
    const used = s.in_use
      ? `<span class="text-xs text-emerald-700">● ${escapeHtml(s.in_use)}</span>`
      : '<span class="text-xs text-slate-400">unused</span>';
    tr.innerHTML = `
      <td class="py-2 pr-3"><input type="checkbox" class="si-check rounded border-slate-300 cursor-pointer align-middle" data-name="${escapeHtml(s.name)}" ${SI_SELECTED.has(s.name) ? "checked" : ""} /></td>
      <td class="py-2 pr-4 font-mono font-medium text-slate-800">${escapeHtml(s.name)}</td>
      <td class="py-2 pr-4 font-mono text-slate-600">${escapeHtml(s.interface || "")}</td>
      <td class="py-2 pr-4 text-slate-600">${s.vlan_id ? escapeHtml(s.vlan_id) : "—"}</td>
      <td class="py-2 pr-4 text-slate-600">${escapeHtml(s.alloc_method || "static")}</td>
      <td class="py-2 pr-4 font-mono text-slate-600">${escapeHtml(s.bind_ip || "—")}${s.bind_prefix ? "/" + escapeHtml(s.bind_prefix) : ""}</td>
      <td class="py-2 pr-4">${used}</td>
      <td class="py-2 text-right whitespace-nowrap">
        <button data-name="${escapeHtml(s.name)}" class="si-edit text-xs font-medium text-sky-600 hover:text-sky-800 mr-3">Edit</button>
        <button data-name="${escapeHtml(s.name)}" class="si-del text-xs font-medium text-red-600 hover:text-red-800">Delete</button>
      </td>`;
    tb.appendChild(tr);
  });
  $$(".si-edit").forEach((b) => b.addEventListener("click", () => editSubiface(b.dataset.name)));
  $$(".si-del").forEach((b) => b.addEventListener("click", () => deleteSubiface(b.dataset.name)));
  $$(".si-check").forEach((c) => c.addEventListener("change", () => {
    c.checked ? SI_SELECTED.add(c.dataset.name) : SI_SELECTED.delete(c.dataset.name);
    updateSiBulkBar();
  }));
  updateSiBulkBar();
}

// --- bulk selection on the sub-interfaces table ----------------------------
const SI_SELECTED = new Set();

function updateSiBulkBar() {
  const present = new Set(SUBIFACES.map((s) => s.name));
  [...SI_SELECTED].forEach((n) => { if (!present.has(n)) SI_SELECTED.delete(n); });
  $("#si-bulk-count").textContent = SI_SELECTED.size;
  $("#si-bulk-delete").classList.toggle("hidden", SI_SELECTED.size === 0);
  const names = SUBIFACES.map((s) => s.name);
  const all = names.length > 0 && names.every((n) => SI_SELECTED.has(n));
  const head = $("#si-check-all");
  if (head) { head.checked = all; head.indeterminate = !all && names.some((n) => SI_SELECTED.has(n)); }
}

function toggleSiSelectAll(on) {
  SUBIFACES.forEach((s) => on ? SI_SELECTED.add(s.name) : SI_SELECTED.delete(s.name));
  $$(".si-check").forEach((c) => { c.checked = on; });
  updateSiBulkBar();
}

async function deleteSelectedSubifaces() {
  const names = [...SI_SELECTED];
  if (!names.length) return;
  if (!confirm(`Delete ${names.length} sub-interface(s)?\n\n${names.join(", ")}\n\nAn in-use sub-interface is skipped (delete its mapping first).`)) return;
  let ok = 0; const fails = [];
  for (const n of names) {
    try {
      const j = await (await fetch(`/api/subinterfaces/${encodeURIComponent(n)}`, { method: "DELETE" })).json();
      j.ok ? ok++ : fails.push(`${n}: ${j.error || "failed"}`);
    } catch (e) { fails.push(`${n}: ${e.message}`); }
  }
  SI_SELECTED.clear();
  await loadSubinterfaces();
  toast(fails.length ? `Deleted ${ok}, ${fails.length} skipped: ${fails[0]}` : `Deleted ${ok} sub-interface(s).`, !fails.length);
}

function resetSubifaceForm() {
  $("#si-edit-name").value = "";
  $("#si-label").value = "";
  $("#si-vlan").value = "";
  $("#si-mac").value = "";
  $("#si-bind-ip").value = "";
  $("#si-bind-prefix").value = "";
  $("#si-save").textContent = "Create";
  $("#si-cancel").classList.add("hidden");
  $("#subiface-form-title").textContent = "";
  $("#si-label").disabled = false;
}

function editSubiface(name) {
  const s = SUBIFACES.find((x) => x.name === name);
  if (!s) return;
  $("#si-edit-name").value = s.name;
  if ([...$("#si-interface").options].some((o) => o.value === s.interface)) $("#si-interface").value = s.interface;
  $("#si-label").value = "";
  $("#si-label").disabled = true;   // name is fixed on edit
  $("#si-vlan").value = s.vlan_id || "";
  $("#si-mac").value = s.mac || "";
  $("#si-bind-ip").value = s.bind_ip || "";
  $("#si-bind-prefix").value = s.bind_prefix || "";
  $("#si-save").textContent = "Save changes";
  $("#si-cancel").classList.remove("hidden");
  $("#subiface-form-title").textContent = `editing ${s.name}`;
  $("#subiface-manager").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function submitSubiface(e) {
  e.preventDefault();
  const editName = $("#si-edit-name").value;
  const fd = new FormData();
  fd.append("interface", $("#si-interface").value);
  if (!editName) fd.append("label", $("#si-label").value);
  fd.append("vlan_id", $("#si-vlan").value);
  fd.append("mac", $("#si-mac").value);
  fd.append("alloc_method", "static");   // sub-interfaces are always static now
  fd.append("bind_ip", $("#si-bind-ip").value);
  fd.append("bind_prefix", $("#si-bind-prefix").value);
  const url = editName ? `/api/subinterfaces/${encodeURIComponent(editName)}` : "/api/subinterfaces";
  const btn = $("#si-save");
  setBtnLoading(btn, true, editName ? "Saving…" : "Creating…");
  showStatus("loading", editName ? "Saving sub-interface…" : "Creating sub-interface…",
             "Bringing up the macvlan device and assigning its address on the host.");
  try {
    const j = await (await fetch(url, { method: "POST", body: fd })).json();
    if (!j.ok) throw new Error(j.error || "The host rejected the request.");
    showStatus("success", editName ? "Sub-interface saved" : "Sub-interface created",
               editName ? `${editName} updated.` : `${j.subinterface.name} is ready to bind.`);
    resetSubifaceForm();
    await loadSubinterfaces();
  } catch (err) {
    showStatus("error", editName ? "Couldn't save sub-interface" : "Couldn't create sub-interface",
               String(err.message || err));
  } finally {
    setBtnLoading(btn, false);
    // resetSubifaceForm() (on success) flips the form back to create mode; keep
    // the label in sync with whatever mode we ended in.
    btn.textContent = $("#si-edit-name").value ? "Save changes" : "Create";
  }
}

async function deleteSubiface(name) {
  if (!confirm(`Delete sub-interface ${name}? This tears down the device on the host.`)) return;
  try {
    const j = await (await fetch(`/api/subinterfaces/${encodeURIComponent(name)}`, { method: "DELETE" })).json();
    if (!j.ok) throw new Error(j.error || "Failed");
    toast(`Sub-interface ${name} removed.`);
    await loadSubinterfaces();
  } catch (err) {
    toast(String(err.message || err), false);
  }
}

async function loadIfaceTraffic() {
  // Per-interface live throughput (Phase 3 endpoint). Tolerate its absence so
  // the overview still renders before the endpoint exists.
  try {
    const r = await fetch("/api/interfaces/traffic");
    if (!r.ok) return;
    const j = await r.json();
    if (!j.ok || !Array.isArray(j.interfaces)) return;
    IFACE_RATES = {};
    const walk = (nodes) => (nodes || []).forEach((n) => {
      IFACE_RATES[n.name] = n;
      walk(n.children);
    });
    walk(j.interfaces);
    $("#iface-tree-updated").textContent =
      (j.simulated ? "simulated · " : "") + new Date().toLocaleTimeString();
    renderIfaceTree();
  } catch (_) { /* keep last */ }
}

// Build a physical-at-top, sub-interfaces-nested tree from the detected
// interfaces, the mappings bound to each, and (when available) live rates.
function buildIfaceTree() {
  const byName = {};
  IFACES.forEach((i) => { byName[i.name] = { ...i, mappings: [], children: [] }; });
  // Macvlan sub-interfaces this tool created live in the mappings, not in
  // /api/interfaces — surface them as children of their parent interface.
  (MAPPINGS || []).forEach((m) => {
    const parent = byName[m.interface];
    if (parent) parent.mappings.push(m);
    if (m.subiface && m.subiface_kind !== "direct") {
      const node = byName[m.subiface] || (byName[m.subiface] = {
        name: m.subiface, kind: "macvlan", up: true,
        addresses: m.bind_ip ? [{ ip: m.bind_ip, prefix: m.bind_prefix }] : [],
        mappings: [], children: [], _parent: m.interface,
      });
      node.mappings.push(m);
    }
  });
  const roots = [];
  Object.values(byName).forEach((n) => {
    const parentName = n._parent || (n.kind === "vlan" || n.kind === "macvlan" ? n.parent : null);
    if (parentName && byName[parentName]) byName[parentName].children.push(n);
    else roots.push(n);
  });
  return roots.sort((a, b) => a.name.localeCompare(b.name));
}

function ifaceRowHtml(node, depth) {
  const rate = IFACE_RATES[node.name] || {};
  const ips = (node.addresses || []).map((a) => a.ip + (a.prefix ? "/" + a.prefix : "")).join(", ");
  const isChild = depth > 0;
  const down = node.up === false;
  const dot = down ? "bg-slate-300" : "bg-emerald-500";
  const kindBadge = node.kind && node.kind !== "physical"
    ? `<span class="ml-2 px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 text-[10px] uppercase tracking-wide">${escapeHtml(node.kind)}</span>` : "";
  const maps = (node.mappings || []).length
    ? `<span class="text-xs text-slate-400">· ${node.mappings.length} mapping${node.mappings.length > 1 ? "s" : ""}</span>` : "";
  return `
    <div class="flex items-center gap-3 py-2.5" style="padding-left:${depth * 1.5}rem">
      <span class="h-2 w-2 rounded-full ${dot} shrink-0"></span>
      <div class="min-w-0 flex-1">
        <div class="flex items-center">
          <span class="font-mono ${isChild ? "text-slate-600" : "font-semibold text-slate-800"}">${isChild ? "└ " : ""}${escapeHtml(node.name)}</span>
          ${kindBadge}
        </div>
        <div class="text-xs text-slate-400 truncate">${escapeHtml(ips || "no address")} ${maps}</div>
      </div>
      <div class="text-right tabular-nums shrink-0">
        <div class="text-xs text-sky-700">↓ ${fmtRate(rate.rx_rate)}</div>
        <div class="text-xs text-emerald-700">↑ ${fmtRate(rate.tx_rate)}</div>
      </div>
    </div>`;
}

function renderIfaceTree() {
  const host = $("#iface-tree");
  if (!host) return;
  const roots = buildIfaceTree();
  if (!roots.length) {
    host.innerHTML = '<div class="py-6 text-center text-slate-400">No interfaces detected.</div>';
    return;
  }
  const rows = [];
  const emit = (node, depth) => {
    rows.push(ifaceRowHtml(node, depth));
    (node.children || []).sort((a, b) => a.name.localeCompare(b.name))
      .forEach((c) => emit(c, depth + 1));
  };
  roots.forEach((r) => emit(r, 0));
  host.innerHTML = rows.join("");
}

// --- SSL management (certificate registry) ---------------------------------
async function loadSslCerts() {
  try {
    const j = await (await fetch("/api/ssl/certs")).json();
    if (!j.ok) return;
    if (j.ssl_dir) $("#ssl-dir-label").textContent = j.ssl_dir;
    const certs = j.certs || [];
    const tb = $("#ssl-rows");
    tb.innerHTML = "";
    const statEl = $("#stat-ssl");
    if (statEl) animateCount(statEl, certs.length);  // keep the dashboard stat in sync
    $("#ssl-empty").classList.toggle("hidden", certs.length > 0);
    certs.forEach((c, idx) => {
      const tr = document.createElement("tr");
      tr.className = "hover:bg-slate-50 align-top";
      tr.style.setProperty("--i", Math.min(idx, 12));
      const srcBadge = c.source === "upload"
        ? '<span class="inline-block px-2 py-0.5 rounded-full bg-slate-100 text-slate-600 text-xs">uploaded</span>'
        : '<span class="inline-block px-2 py-0.5 rounded-full bg-indigo-100 text-indigo-700 text-xs">self-signed</span>';
      const inUse = c.in_use
        ? '<span class="text-xs text-emerald-700">● in use</span>'
        : '<span class="text-xs text-slate-400">unused</span>';
      tr.innerHTML = `
        <td class="px-6 py-3 font-mono">${escapeHtml(c.name)}${c.subject ? `<div class="text-xs text-slate-400 font-sans">${escapeHtml(c.subject)}</div>` : ""}</td>
        <td class="px-6 py-3">${srcBadge}</td>
        <td class="px-6 py-3 text-xs text-slate-500">${c.not_after ? escapeHtml(c.not_after) : "—"}</td>
        <td class="px-6 py-3">${inUse}</td>
        <td class="px-6 py-3 text-right">
          <button data-cert="${escapeHtml(c.name)}" class="ssl-del-btn text-xs font-medium ${c.in_use ? "text-slate-300 cursor-not-allowed" : "text-red-600 hover:text-red-800"}" ${c.in_use ? "disabled" : ""}
            ${c.in_use ? 'title="In use by a mapping — detach it first"' : ""}>Delete</button>
        </td>`;
      tb.appendChild(tr);
    });
    tb.querySelectorAll(".ssl-del-btn:not([disabled])").forEach((b) =>
      b.addEventListener("click", () => deleteCert(b.dataset.cert)));
  } catch (_) { /* non-fatal */ }
}

async function createCert(mode, form) {
  const fd = new FormData(form);
  fd.append("mode", mode);
  const btn = form.querySelector('button[type="submit"]');
  setBtnLoading(btn, true, mode === "selfsigned" ? "Generating…" : "Adding…");
  showStatus("loading", mode === "selfsigned" ? "Generating certificate…" : "Adding certificate…",
             mode === "selfsigned" ? "Creating a self-signed cert + key on the host."
                                   : "Validating and storing the uploaded cert + key.");
  try {
    const j = await (await fetch("/api/ssl/certs", { method: "POST", body: fd })).json();
    if (j.ok) {
      showStatus("success", "Certificate added", `${j.cert.name} is ready to use.`);
      form.reset();
      await loadSslCerts();
    } else {
      showStatus("error", "Couldn't add certificate", j.error || "The host rejected the request.");
    }
  } catch (err) {
    showStatus("error", "Request failed", err.message || String(err));
  } finally {
    setBtnLoading(btn, false);
  }
}

async function deleteCert(name) {
  if (!confirm(`Delete certificate ${name}? Its cert and key files will be removed.`)) return;
  const j = await (await fetch(`/api/ssl/certs/${encodeURIComponent(name)}`, { method: "DELETE" })).json();
  if (j.ok) { toast(`Certificate ${name} deleted.`); await loadSslCerts(); }
  else toast(j.error || "Could not delete.", false);
}

// --- access lists (allow/deny) ---------------------------------------------
let ACCESS_LISTS = [];        // cached for the mapping-form dropdown
let ACCESS_DEFAULT = "";      // current global default list name
let ACL_EDITING = null;       // list name being edited, or null

function aclTypeBadge(a) {
  if (a.builtin)
    return '<span class="inline-block px-2 py-0.5 rounded-full bg-indigo-100 text-indigo-700 text-xs">built-in</span>';
  if (a.source_url)
    return '<span class="inline-block px-2 py-0.5 rounded-full bg-sky-100 text-sky-700 text-xs">auto · url</span>';
  return '<span class="inline-block px-2 py-0.5 rounded-full bg-slate-100 text-slate-600 text-xs">manual</span>';
}

// Fill the mapping form's #access_list dropdown, preserving the current value.
function populateAccessDropdown() {
  const sel = $("#access_list");
  if (!sel) return;
  const cur = sel.value || "__default__";
  sel.innerHTML = "";
  const opts = [["__default__", "Use global default"], ["", "None — allow all"]];
  for (const a of ACCESS_LISTS) opts.push([a.name, `${a.label || a.name} (${a.count})`]);
  for (const [v, label] of opts) {
    const o = document.createElement("option");
    o.value = v; o.textContent = label;
    sel.appendChild(o);
  }
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "__default__";
}

async function loadAccessLists() {
  try {
    const j = await (await fetch("/api/access-lists")).json();
    if (!j.ok) return;
    ACCESS_LISTS = j.lists || [];
    ACCESS_DEFAULT = j.default || "";
    if (j.acl_dir) { const l = $("#acl-dir-label"); if (l) l.textContent = j.acl_dir; }
    const cnt = $("#nav-access-count"); if (cnt) cnt.textContent = ACCESS_LISTS.length;

    // global-default selector
    const dsel = $("#acl-default-select");
    if (dsel) {
      dsel.innerHTML = "";
      const none = document.createElement("option");
      none.value = ""; none.textContent = "— none (mappings stay open) —";
      dsel.appendChild(none);
      for (const a of ACCESS_LISTS) {
        const o = document.createElement("option");
        o.value = a.name; o.textContent = `${a.label || a.name} (${a.count})`;
        dsel.appendChild(o);
      }
      dsel.value = ACCESS_DEFAULT;
    }

    // table
    const tb = $("#acl-rows");
    if (tb) {
      tb.innerHTML = "";
      $("#acl-empty").classList.toggle("hidden", ACCESS_LISTS.length > 0);
      ACCESS_LISTS.forEach((a, idx) => {
        const tr = document.createElement("tr");
        tr.className = "hover:bg-slate-50 align-top";
        tr.style.setProperty("--i", Math.min(idx, 12));
        const isDefault = a.name === ACCESS_DEFAULT
          ? '<span class="ml-2 inline-block px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700 text-[11px]">default</span>' : "";
        const refreshBtn = a.source_url
          ? `<button data-acl-refresh="${escapeHtml(a.name)}" class="text-xs font-medium text-sky-600 hover:text-sky-800">Refresh</button>` : "";
        const delDisabled = a.builtin || a.in_use;
        const delTitle = a.builtin ? "Built-in list — can't delete"
          : (a.in_use ? "In use by a mapping — detach it first" : "");
        tr.innerHTML = `
          <td class="px-6 py-3 font-mono">${escapeHtml(a.name)}.conf${isDefault}
            <div class="text-xs text-slate-400 font-sans">${escapeHtml(a.label || "")}${a.in_use ? " · in use" : ""}</div></td>
          <td class="px-6 py-3">${aclTypeBadge(a)}</td>
          <td class="px-6 py-3 text-xs text-slate-600">${a.count}${a.include_private ? " <span class='text-slate-400'>+ private</span>" : ""}</td>
          <td class="px-6 py-3 text-xs text-slate-500">${a.last_refresh ? fmtWhen(a.last_refresh) : "—"}</td>
          <td class="px-6 py-3 text-right whitespace-nowrap space-x-3">
            ${refreshBtn}
            <button data-acl-edit="${escapeHtml(a.name)}" class="text-xs font-medium text-slate-600 hover:text-slate-900">Edit</button>
            <button data-acl-del="${escapeHtml(a.name)}" class="text-xs font-medium ${delDisabled ? "text-slate-300 cursor-not-allowed" : "text-red-600 hover:text-red-800"}" ${delDisabled ? "disabled" : ""} ${delTitle ? `title="${escapeHtml(delTitle)}"` : ""}>Delete</button>
          </td>`;
        tb.appendChild(tr);
      });
      tb.querySelectorAll("[data-acl-refresh]").forEach((b) =>
        b.addEventListener("click", () => refreshAccessList(b.dataset.aclRefresh)));
      tb.querySelectorAll("[data-acl-edit]").forEach((b) =>
        b.addEventListener("click", () => editAccessList(b.dataset.aclEdit)));
      tb.querySelectorAll("[data-acl-del]:not([disabled])").forEach((b) =>
        b.addEventListener("click", () => deleteAccessList(b.dataset.aclDel)));
    }

    populateAccessDropdown();
  } catch (_) { /* non-fatal */ }
}

function resetAccessForm() {
  ACL_EDITING = null;
  $("#acl-form").reset();
  $("#acl-name").readOnly = false;
  $("#acl-name").classList.remove("bg-slate-100");
  $("#acl-form-title").textContent = "New access list";
  $("#acl-cancel-edit").classList.add("hidden");
  $("#acl-save-btn").textContent = "Save list";
}

async function editAccessList(name) {
  try {
    const j = await (await fetch(`/api/access-lists/${encodeURIComponent(name)}`)).json();
    if (!j.ok) { toast(j.error || "Could not load list.", false); return; }
    const a = j.list;
    ACL_EDITING = a.name;
    setVal("acl-name", a.name);
    $("#acl-name").readOnly = true;
    $("#acl-name").classList.add("bg-slate-100");
    setVal("acl-label", a.label || "");
    setVal("acl-entries", (a.entries || []).join("\n"));
    setVal("acl-source-url", a.source_url || "");
    setVal("acl-refresh-hours", a.refresh_hours || 24);
    $("#acl-include-private").checked = a.include_private !== false;
    $("#acl-form-title").textContent = `Edit ${a.name}`;
    $("#acl-cancel-edit").classList.remove("hidden");
    $("#acl-save-btn").textContent = "Update list";
    $("#acl-name").scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (err) { toast("Failed: " + err.message, false); }
}

async function submitAccessList(e) {
  e.preventDefault();
  const fd = new FormData($("#acl-form"));
  if (!$("#acl-include-private").checked) fd.set("include_private", "0");
  const btn = $("#acl-save-btn");
  const editing = ACL_EDITING;
  setBtnLoading(btn, true, editing ? "Saving…" : "Creating…");
  showStatus("loading", editing ? "Saving access list…" : "Creating access list…",
             "Writing the .conf snippet and reloading Nginx.");
  try {
    const j = await (await fetch("/api/access-lists", { method: "POST", body: fd })).json();
    if (j.ok) {
      showStatus("success", editing ? "Access list saved" : "Access list created",
                 `${j.list.name} — ${j.list.count} network(s).`);
      resetAccessForm();
      await loadAccessLists();
    } else {
      showStatus("error", "Couldn't save access list", j.error || "The host rejected the request.");
    }
  } catch (err) {
    showStatus("error", "Request failed", err.message || String(err));
  } finally {
    setBtnLoading(btn, false);
    // resetAccessForm() (on success) flips back to create mode; keep the label in sync.
    btn.textContent = ACL_EDITING ? "Update list" : "Save list";
  }
}

async function refreshAccessList(name) {
  toast(`Refreshing ${name}…`);
  try {
    const j = await (await fetch(`/api/access-lists/${encodeURIComponent(name)}/refresh`, { method: "POST" })).json();
    if (j.ok) { toast(j.message || "Refreshed."); await loadAccessLists(); }
    else toast(j.error || "Refresh failed.", false);
  } catch (err) { toast("Failed: " + err.message, false); }
}

async function deleteAccessList(name) {
  if (!confirm(`Delete access list ${name}? Its ${name}.conf snippet will be removed.`)) return;
  try {
    const j = await (await fetch(`/api/access-lists/${encodeURIComponent(name)}`, { method: "DELETE" })).json();
    if (j.ok) { toast(`Access list ${name} deleted.`); if (ACL_EDITING === name) resetAccessForm(); await loadAccessLists(); }
    else toast(j.error || "Could not delete.", false);
  } catch (err) { toast("Failed: " + err.message, false); }
}

async function setDefaultAccessList(name) {
  const fd = new FormData();
  fd.append("default_access_list", name);
  try {
    const j = await (await fetch("/api/settings", { method: "POST", body: fd })).json();
    if (j.ok) { toast(name ? `Default access list set to ${name}.` : "Default access list cleared."); await loadAccessLists(); }
    else { toast(j.error || "Could not set default.", false); await loadAccessLists(); }
  } catch (err) { toast("Failed: " + err.message, false); }
}

// --- firewall (per-interface iptables rules) --------------------------------
let FW_ENABLED = false;
let FW_INTERFACES = [];
let FW_SELECTED = null;   // currently selected interface name, or null
let FW_RULES = [];        // rules for FW_SELECTED
let FW_RULE_EDITING = null;
let FW_DASH_PORT = 8088;  // this dashboard's port; reported by /overview

function fwStepsSummary(steps) {
  if (!steps || !steps.length) return "";
  const failed = steps.filter((s) => !s.ok);
  return failed.length ? `${failed.length} of ${steps.length} step(s) failed: ${failed[0].detail}` : "";
}

async function loadFirewall() {
  try {
    const j = await (await fetch("/api/firewall/overview")).json();
    if (!j.ok) return;
    FW_ENABLED = !!j.enabled;
    FW_INTERFACES = j.interfaces || [];
    FW_DASH_PORT = j.dashboard_port || FW_DASH_PORT;
    renderFwPresets();
    $("#fw-simulate-notice").classList.toggle("hidden", !j.simulate);

    const badge = $("#nav-firewall-badge");
    if (badge) {
      badge.classList.remove("hidden");
      badge.textContent = FW_ENABLED ? "on" : "off";
      badge.className = "ml-auto text-[11px] font-semibold px-2 py-0.5 rounded-full " +
        (FW_ENABLED ? "bg-emerald-500/20 text-emerald-300" : "bg-white/10 text-slate-200");
    }
    $("#fw-master-toggle").checked = FW_ENABLED;

    renderFwIfaceList();
    if (FW_SELECTED && FW_INTERFACES.some((i) => i.interface === FW_SELECTED)) {
      await selectFwInterface(FW_SELECTED);
    } else {
      $("#fw-iface-detail").classList.add("hidden");
      $("#fw-iface-empty").classList.remove("hidden");
    }
  } catch (_) { /* non-fatal */ }
}

function renderFwIfaceList() {
  const box = $("#fw-iface-list");
  if (!box) return;
  box.innerHTML = "";
  $("#fw-iface-list-empty").classList.toggle("hidden", FW_INTERFACES.length > 0);
  FW_INTERFACES.forEach((i) => {
    const row = document.createElement("button");
    row.type = "button";
    const active = i.interface === FW_SELECTED;
    row.className = "w-full text-left px-5 py-3 flex items-center gap-3 hover:bg-slate-50 transition " +
      (active ? "bg-emerald-50/70" : "");
    const dot = i.enabled
      ? '<span class="h-2 w-2 rounded-full bg-emerald-500 shrink-0" title="enforcing"></span>'
      : '<span class="h-2 w-2 rounded-full bg-slate-300 shrink-0" title="not enforcing"></span>';
    row.innerHTML = `
      ${dot}
      <span class="font-mono text-sm text-slate-800 flex-1 truncate">${escapeHtml(i.interface)}</span>
      <span class="text-[11px] font-semibold px-2 py-0.5 rounded-full bg-slate-100 text-slate-500">${i.rule_count}</span>`;
    row.addEventListener("click", () => selectFwInterface(i.interface));
    box.appendChild(row);
  });
}

async function selectFwInterface(name) {
  FW_SELECTED = name;
  renderFwIfaceList();
  const cfg = FW_INTERFACES.find((i) => i.interface === name);
  if (!cfg) return;
  $("#fw-iface-empty").classList.add("hidden");
  $("#fw-iface-detail").classList.remove("hidden");
  $("#fw-detail-name").textContent = name;
  $("#fw-iface-enabled").checked = !!cfg.enabled;
  $("#fw-inbound-policy").value = cfg.inbound_policy || "accept";
  $("#fw-outbound-policy").value = cfg.outbound_policy || "accept";
  resetFwRuleForm();
  await loadFwRules(name);
}

async function loadFwRules(name) {
  try {
    const j = await (await fetch(`/api/firewall/rules?interface=${encodeURIComponent(name)}`)).json();
    FW_RULES = j.ok ? (j.rules || []) : [];
  } catch (_) { FW_RULES = []; }
  renderFwRuleRows();
}

const FW_ACTION_BADGE = {
  accept: "bg-emerald-100 text-emerald-700",
  drop: "bg-slate-200 text-slate-600",
  reject: "bg-red-100 text-red-700",
};

function renderFwRuleRows() {
  const tb = $("#fw-rule-rows");
  if (!tb) return;
  tb.innerHTML = "";
  $("#fw-rule-empty").classList.toggle("hidden", FW_RULES.length > 0);
  FW_RULES.forEach((r) => {
    const tr = document.createElement("tr");
    tr.className = "hover:bg-slate-50 align-top" + (r.enabled === false ? " opacity-50" : "");
    tr.innerHTML = `
      <td class="px-4 py-3 text-xs font-mono text-slate-500">${r.priority}</td>
      <td class="px-4 py-3 text-xs">${r.direction === "in" ? "Inbound" : "Outbound"}</td>
      <td class="px-4 py-3 text-xs uppercase font-mono text-slate-500">${escapeHtml(r.protocol)}</td>
      <td class="px-4 py-3 text-xs font-mono">${r.port_range ? escapeHtml(r.port_range) : "<span class='text-slate-400'>any</span>"}</td>
      <td class="px-4 py-3 text-xs font-mono">${escapeHtml(r.source_cidr) === "0.0.0.0/0" ? "<span class='text-slate-400'>anywhere</span>" : escapeHtml(r.source_cidr)}</td>
      <td class="px-4 py-3"><span class="text-[11px] font-semibold px-2 py-0.5 rounded-full ${FW_ACTION_BADGE[r.action] || ""}">${escapeHtml(r.action)}</span></td>
      <td class="px-6 py-3 text-xs text-slate-500">${escapeHtml(r.description || "")}</td>
      <td class="px-6 py-3 text-right whitespace-nowrap space-x-3">
        <button data-fw-edit="${escapeHtml(r.id)}" class="text-xs font-medium text-slate-600 hover:text-slate-900">Edit</button>
        <button data-fw-del="${escapeHtml(r.id)}" class="text-xs font-medium text-red-600 hover:text-red-800">Delete</button>
      </td>`;
    tb.appendChild(tr);
  });
  tb.querySelectorAll("[data-fw-edit]").forEach((b) =>
    b.addEventListener("click", () => editFwRule(b.dataset.fwEdit)));
  tb.querySelectorAll("[data-fw-del]").forEach((b) =>
    b.addEventListener("click", () => deleteFwRule(b.dataset.fwDel)));
}

// --- Rule form: presets, plain-English preview, risk warnings --------------

// One click fills the whole form. `risky` ones default to "my IP only" as the
// source rather than anywhere, since that's almost always what's wanted.
const FW_PRESETS = [
  { label: "SSH", port: "22", protocol: "tcp", risky: true, description: "Allow SSH" },
  { label: "HTTP", port: "80", protocol: "tcp", description: "Allow HTTP" },
  { label: "HTTPS", port: "443", protocol: "tcp", description: "Allow HTTPS" },
  { label: "RTMP", port: "1935", protocol: "tcp", description: "Allow RTMP ingest" },
  { label: "SRT", port: "9000", protocol: "udp", description: "Allow SRT ingest" },
  { label: "DNS", port: "53", protocol: "udp", description: "Allow DNS" },
  { label: "Ping", port: "", protocol: "icmp", description: "Allow ping" },
];

// Ports that should essentially never be open to the whole internet.
const FW_SENSITIVE_PORTS = {
  22: "SSH", 23: "Telnet", 445: "SMB", 2375: "Docker API", 3306: "MySQL",
  3389: "RDP", 5432: "PostgreSQL", 5900: "VNC", 6379: "Redis",
  9200: "Elasticsearch", 27017: "MongoDB",
};

function renderFwPresets() {
  const box = $("#fw-presets");
  if (!box) return;
  box.innerHTML = "";
  FW_PRESETS.forEach((p, idx) => {
    const port = p.dashboard ? String(FW_DASH_PORT) : p.port;
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.fwPreset = String(idx);
    b.className = "text-xs font-medium px-3 py-1.5 rounded-full border border-slate-200 " +
      "bg-white text-slate-700 hover:border-emerald-400 hover:bg-emerald-50 hover:text-emerald-700 transition";
    b.innerHTML = `${escapeHtml(p.label)}${port ? ` <span class="text-slate-400 font-mono">${escapeHtml(port)}</span>` : ""}`;
    b.addEventListener("click", () => applyFwPreset(idx));
    box.appendChild(b);
  });
}

async function applyFwPreset(idx) {
  const p = FW_PRESETS[idx];
  if (!p) return;
  setVal("fw-rule-direction", "in");
  setVal("fw-rule-protocol", p.protocol);
  setVal("fw-rule-port", p.dashboard ? String(FW_DASH_PORT) : p.port);
  setVal("fw-rule-action", "accept");
  setVal("fw-rule-description", p.description || "");
  // A risky service starts locked to the admin's own IP; they can widen it.
  if (p.risky && !$("#fw-rule-source").value) await fillFwSourceMyIp();
  updateFwRulePreview();
  $("#fw-rule-source").focus();
}

async function fillFwSourceMyIp() {
  try {
    const j = await (await fetch("/api/firewall/whoami")).json();
    if (j.ok) setVal("fw-rule-source", j.cidr);
    else toast(j.error || "Could not detect your IP.", false);
  } catch (_) {
    toast("Could not detect your IP.", false);
  }
  updateFwRulePreview();
}

function fwRuleFromForm() {
  return {
    direction: $("#fw-rule-direction").value,
    protocol: $("#fw-rule-protocol").value,
    port: $("#fw-rule-port").value.trim(),
    action: $("#fw-rule-action").value,
    source: $("#fw-rule-source").value.trim(),
  };
}

function fwPreviewSentence(r) {
  const verb = { accept: "Allow", drop: "Silently drop", reject: "Reject" }[r.action] || "Allow";
  const flow = r.direction === "in" ? "incoming" : "outgoing";
  const proto = { tcp: "TCP", udp: "UDP", icmp: "ICMP (ping)", all: "any-protocol" }[r.protocol] || r.protocol;

  let port = "";
  if (r.protocol === "tcp" || r.protocol === "udp") {
    if (!r.port) port = " on any port";
    else port = r.port.includes("-") ? ` on ports ${r.port}` : ` on port ${r.port}`;
  }
  const iface = FW_SELECTED || "this interface";

  if (r.direction === "in") {
    const from = r.source
      ? `from ${r.source}`
      : "from anyone on the internet (0.0.0.0/0)";
    return `${verb} ${flow} ${proto} traffic${port} ${from}, arriving on ${iface}.`;
  }
  const from = r.source ? ` sent from local source ${r.source},` : "";
  return `${verb} ${flow} ${proto} traffic${port},${from} leaving via ${iface}.`;
}

// Mirrors backend/firewall.py::_rule_args so the form shows the exact command.
function fwPreviewCommand(r) {
  const chain = `${r.direction === "in" ? "SFW-IN-" : "SFW-OUT-"}${FW_SELECTED || "<iface>"}`;
  const args = ["iptables", "-A", chain];
  if (r.protocol !== "all") args.push("-p", r.protocol);
  if ((r.protocol === "tcp" || r.protocol === "udp") && r.port) args.push("--dport", r.port);
  if (r.source && r.source !== "0.0.0.0/0") args.push("-s", r.source);
  args.push("-j", { accept: "ACCEPT", drop: "DROP", reject: "REJECT" }[r.action] || "ACCEPT");
  return args.join(" ");
}

// Admin services in this rule's port range that would become world-reachable.
// The dashboard port is deliberately NOT here: every inbound chain accepts it
// before any user rule (see backend/firewall.py), so no rule opens or closes it.
function fwExposedAdminPorts(r) {
  const open = !r.source || r.source === "0.0.0.0/0";
  if (r.direction !== "in" || r.action !== "accept" || !open) return [];
  return r.port.split("-").map((p) => parseInt(p, 10))
    .filter((n) => FW_SENSITIVE_PORTS[n])
    .map((p) => `${p} (${FW_SENSITIVE_PORTS[p]})`);
}

function fwRuleWarnings(r) {
  const out = [];
  const open = !r.source || r.source === "0.0.0.0/0";
  const ports = r.port.split("-").map((p) => parseInt(p, 10)).filter((n) => !isNaN(n));
  const cfg = FW_INTERFACES.find((i) => i.interface === FW_SELECTED) || {};

  if (r.direction === "in" && r.action === "accept" && open) {
    const named = fwExposedAdminPorts(r);
    if (named.length) {
      out.push(`Port ${named.join(" and ")} would accept connections from <b>every IP on the internet</b>. ` +
               `Unless that is deliberate, use <b>Only my IP</b> or <b>My LAN</b> as the source.`);
    }
    if (!r.port && (r.protocol === "tcp" || r.protocol === "udp" || r.protocol === "all")) {
      out.push("No port set with source <b>anyone</b> — this opens <b>every port</b> on this interface to the internet.");
    }
  }
  // Any rule touching the dashboard port is a no-op, whichever way it points.
  if (r.direction === "in" && ports.includes(FW_DASH_PORT)) {
    out.push(`Every inbound chain accepts port ${FW_DASH_PORT} — this dashboard — before any of your rules, so you ` +
             `can't lock yourself out of the tool that fixes the firewall. <b>Rules on port ${FW_DASH_PORT} therefore ` +
             `have no effect, allow or drop.</b> To limit who can reach the dashboard, bind it to a private address ` +
             `(<span class="font-mono">SPLITTER_HOST</span>), put it behind a VPN, or restrict it upstream — not here.`);
  }
  if (r.direction === "in" && r.action === "accept" && (cfg.inbound_policy || "accept") === "accept") {
    out.push("This interface's default inbound policy is already <b>Accept</b>, so everything is allowed anyway and " +
             "this rule changes nothing. Set the default inbound policy to <b>Drop</b> for allow-rules to mean something.");
  }
  return out;
}

function updateFwRulePreview() {
  const prev = $("#fw-rule-preview");
  if (!prev) return;
  const r = fwRuleFromForm();
  prev.textContent = fwPreviewSentence(r);

  const cmd = $("#fw-rule-cmd");
  cmd.textContent = fwPreviewCommand(r);
  cmd.classList.remove("hidden");

  const warn = $("#fw-rule-warn");
  const msgs = fwRuleWarnings(r);
  warn.classList.toggle("hidden", msgs.length === 0);
  warn.innerHTML = msgs.map((m) => `<p class="flex gap-2"><span>⚠️</span><span>${m}</span></p>`).join("");
}

function resetFwRuleForm() {
  FW_RULE_EDITING = null;
  $("#fw-rule-form").reset();
  setVal("fw-rule-id", "");
  setVal("fw-rule-priority", 100);
  $("#fw-rule-enabled").checked = true;
  $("#fw-rule-form-title").textContent = "New rule";
  $("#fw-rule-cancel-edit").classList.add("hidden");
  $("#fw-rule-save").textContent = "Save rule";
  updateFwRulePreview();
}

function editFwRule(id) {
  const r = FW_RULES.find((x) => x.id === id);
  if (!r) return;
  FW_RULE_EDITING = id;
  setVal("fw-rule-id", id);
  setVal("fw-rule-direction", r.direction);
  setVal("fw-rule-protocol", r.protocol);
  setVal("fw-rule-port", r.port_range || "");
  setVal("fw-rule-action", r.action);
  setVal("fw-rule-source", r.source_cidr === "0.0.0.0/0" ? "" : r.source_cidr);
  setVal("fw-rule-priority", r.priority);
  setVal("fw-rule-description", r.description || "");
  $("#fw-rule-enabled").checked = r.enabled !== false;
  $("#fw-rule-form-title").textContent = `Edit rule`;
  $("#fw-rule-cancel-edit").classList.remove("hidden");
  $("#fw-rule-save").textContent = "Update rule";
  updateFwRulePreview();
  $("#fw-rule-form").scrollIntoView({ behavior: "smooth", block: "center" });
}

async function submitFwRule(e) {
  e.preventDefault();
  if (!FW_SELECTED) return;

  // Last line of defence: make the admin acknowledge a rule that would expose
  // an admin service to the whole internet.
  const r = fwRuleFromForm();
  const exposed = fwExposedAdminPorts(r);
  if (exposed.length) {
    if (!confirm(`Open port ${exposed.join(", ")} to EVERY IP on the internet?\n\n${fwPreviewSentence(r)}\n\n` +
                 `Anyone anywhere could then reach this service. Cancel and set the source to your own IP or LAN if that isn't what you want.`)) return;
  }

  const fd = new FormData();
  fd.append("interface", FW_SELECTED);
  fd.append("direction", $("#fw-rule-direction").value);
  fd.append("protocol", $("#fw-rule-protocol").value);
  fd.append("port_range", $("#fw-rule-port").value);
  fd.append("action", $("#fw-rule-action").value);
  fd.append("source", $("#fw-rule-source").value);
  fd.append("priority", $("#fw-rule-priority").value);
  fd.append("description", $("#fw-rule-description").value);
  fd.append("enabled", $("#fw-rule-enabled").checked ? "1" : "0");

  const editing = FW_RULE_EDITING;
  const btn = $("#fw-rule-save");
  setBtnLoading(btn, true, editing ? "Saving…" : "Creating…");
  try {
    const url = editing ? `/api/firewall/rules/${encodeURIComponent(editing)}` : "/api/firewall/rules";
    const j = await (await fetch(url, { method: "POST", body: fd })).json();
    if (j.ok) {
      toast(editing ? "Rule updated." : "Rule created.");
      const bad = fwStepsSummary(j.steps);
      if (bad) toast(bad, false);
      resetFwRuleForm();
      await loadFwRules(FW_SELECTED);
      await loadFirewall();
    } else {
      toast(j.error || "Could not save rule.", false);
    }
  } catch (err) {
    toast("Failed: " + err.message, false);
  } finally {
    setBtnLoading(btn, false);
    btn.textContent = FW_RULE_EDITING ? "Update rule" : "Save rule";
  }
}

async function deleteFwRule(id) {
  const r = FW_RULES.find((x) => x.id === id);
  if (!confirm(`Delete this rule?${r && r.description ? "\n\n" + r.description : ""}`)) return;
  try {
    const j = await (await fetch(`/api/firewall/rules/${encodeURIComponent(id)}`, { method: "DELETE" })).json();
    if (j.ok) {
      toast("Rule deleted.");
      if (FW_RULE_EDITING === id) resetFwRuleForm();
      await loadFwRules(FW_SELECTED);
      await loadFirewall();
    } else {
      toast(j.error || "Could not delete rule.", false);
    }
  } catch (err) { toast("Failed: " + err.message, false); }
}

async function saveFwIfaceSettings() {
  if (!FW_SELECTED) return;
  const fd = new FormData();
  fd.append("enabled", $("#fw-iface-enabled").checked ? "1" : "0");
  fd.append("inbound_policy", $("#fw-inbound-policy").value);
  fd.append("outbound_policy", $("#fw-outbound-policy").value);
  const msg = $("#fw-iface-msg");
  const btn = $("#fw-iface-save");
  setBtnLoading(btn, true, "Saving…");
  try {
    const j = await (await fetch(`/api/firewall/interfaces/${encodeURIComponent(FW_SELECTED)}`,
      { method: "POST", body: fd })).json();
    if (j.ok) {
      msg.textContent = "Saved.";
      msg.className = "text-xs text-emerald-600";
      const bad = fwStepsSummary(j.steps);
      if (bad) { msg.textContent = bad; msg.className = "text-xs text-red-600"; }
    } else {
      msg.textContent = j.error || "Could not save.";
      msg.className = "text-xs text-red-600";
    }
    msg.classList.remove("hidden");
    await loadFirewall();
  } catch (err) {
    msg.textContent = "Failed: " + err.message;
    msg.className = "text-xs text-red-600";
    msg.classList.remove("hidden");
  } finally {
    setBtnLoading(btn, false);
  }
}

async function toggleFwMaster(enabled) {
  const msg = $("#fw-master-msg");
  if (enabled && !confirm(
    "Turn firewall enforcement on?\n\nEvery interface marked \"enforce\" will get its iptables chain installed. " +
    "Established connections and this dashboard's own port are always allowed, so this can't lock you out of " +
    "Splitter itself — but it can still block other services on an interface set to a Drop default. " +
    "Make sure you've reviewed the rules first.")) {
    $("#fw-master-toggle").checked = FW_ENABLED;
    return;
  }
  try {
    const fd = new FormData();
    fd.append("enabled", enabled ? "1" : "0");
    const j = await (await fetch("/api/firewall/settings", { method: "POST", body: fd })).json();
    if (j.ok) {
      toast(enabled ? "Firewall enforcement is on." : "Firewall enforcement is off.");
      const bad = fwStepsSummary(j.steps);
      if (bad) { msg.textContent = bad; msg.className = "text-xs text-red-600"; msg.classList.remove("hidden"); }
      else msg.classList.add("hidden");
    } else {
      toast(j.error || "Could not update.", false);
      $("#fw-master-toggle").checked = FW_ENABLED;
    }
  } catch (err) {
    toast("Failed: " + err.message, false);
    $("#fw-master-toggle").checked = FW_ENABLED;
  } finally {
    await loadFirewall();
  }
}

async function fwPanic() {
  if (!confirm("Panic — disable the firewall now?\n\nThis immediately tears down every managed iptables chain on every interface and turns the master switch off. Saved rules are kept and can be re-applied later.")) return;
  const btn = $("#fw-panic-btn");
  const msg = $("#fw-panic-msg");
  setBtnLoading(btn, true, "Disabling…");
  try {
    const j = await (await fetch("/api/firewall/panic", { method: "POST" })).json();
    if (j.ok) {
      toast("Firewall disabled.");
      msg.classList.add("hidden");
    } else {
      msg.textContent = j.error || "Could not disable.";
      msg.className = "text-xs mt-3 text-red-600";
      msg.classList.remove("hidden");
    }
  } catch (err) {
    msg.textContent = "Failed: " + err.message;
    msg.className = "text-xs mt-3 text-red-600";
    msg.classList.remove("hidden");
  } finally {
    setBtnLoading(btn, false);
    await loadFirewall();
  }
}

// --- activity / audit log (admin) ------------------------------------------
// action -> [label, tailwind colour classes]
const ACTIONS = {
  "login":            ["Login",        "bg-slate-100 text-slate-600"],
  "login.failed":     ["Login failed", "bg-red-100 text-red-700"],
  "logout":           ["Logout",       "bg-slate-100 text-slate-500"],
  "setup":            ["Setup",        "bg-indigo-100 text-indigo-700"],
  "mapping.create":   ["Added map",    "bg-emerald-100 text-emerald-700"],
  "mapping.update":   ["Edited map",   "bg-amber-100 text-amber-700"],
  "mapping.delete":   ["Deleted map",  "bg-red-100 text-red-700"],
  "mappings.import":  ["Imported",     "bg-sky-100 text-sky-700"],
  "mappings.export":  ["Exported",     "bg-sky-100 text-sky-700"],
  "mappings.reapply": ["Re-applied",   "bg-violet-100 text-violet-700"],
  "user.create":      ["Added user",   "bg-emerald-100 text-emerald-700"],
  "user.update":      ["Edited user",  "bg-amber-100 text-amber-700"],
  "user.delete":      ["Deleted user", "bg-red-100 text-red-700"],
  "password.change":  ["Password",     "bg-slate-100 text-slate-600"],
  "access.create":    ["Added ACL",     "bg-emerald-100 text-emerald-700"],
  "access.update":    ["Edited ACL",    "bg-amber-100 text-amber-700"],
  "access.delete":    ["Deleted ACL",   "bg-red-100 text-red-700"],
  "access.refresh":   ["ACL refresh",   "bg-sky-100 text-sky-700"],
};

function actionBadge(action) {
  const [label, cls] = ACTIONS[action] || [action, "bg-slate-100 text-slate-600"];
  return `<span class="inline-block px-2 py-0.5 rounded-full text-xs font-medium ${cls}">${escapeHtml(label)}</span>`;
}

function fmtWhen(ts) {
  const d = new Date(ts);
  return isNaN(d) ? escapeHtml(ts) : escapeHtml(d.toLocaleString());
}

async function loadActivity() {
  try {
    const j = await (await fetch("/api/activity?limit=500")).json();
    if (!j.ok) return;
    const events = j.events || [];
    const tb = $("#activity-rows");
    tb.innerHTML = "";
    $("#activity-empty").classList.toggle("hidden", events.length > 0);
    events.forEach((e, idx) => {
      const tr = document.createElement("tr");
      tr.className = "hover:bg-slate-50 align-top";
      tr.style.setProperty("--i", Math.min(idx, 12));
      tr.innerHTML = `
        <td class="px-6 py-3 whitespace-nowrap text-slate-500 text-xs">${fmtWhen(e.ts)}</td>
        <td class="px-6 py-3 whitespace-nowrap">
          <span class="font-mono text-slate-700">${escapeHtml(e.user || "—")}</span>
          <div class="text-xs text-slate-400">${escapeHtml(e.role || "—")}</div>
        </td>
        <td class="px-6 py-3 whitespace-nowrap">${actionBadge(e.action)}</td>
        <td class="px-6 py-3 font-mono text-xs text-slate-600">${e.target ? escapeHtml(e.target) : '<span class="text-slate-300">—</span>'}</td>
        <td class="px-6 py-3 text-xs text-slate-500">${e.detail ? escapeHtml(e.detail) : ""}</td>
        <td class="px-6 py-3 font-mono text-xs text-slate-400">${e.ip ? escapeHtml(e.ip) : ""}</td>`;
      tb.appendChild(tr);
    });
  } catch (_) { /* non-fatal */ }
}

// --- backup & restore (admin) ----------------------------------------------
let BACKUPS = [];
const BK_SELECTED = new Set();

async function loadBackups() {
  if (!isAdmin()) return;
  try {
    const j = await (await fetch("/api/backups")).json();
    if (!j.ok) return;
    BACKUPS = j.backups || [];
    renderBackups();
    renderSchedule(j.schedule || {});
  } catch (_) { /* non-fatal */ }
}

function renderSchedule(s) {
  $("#sched-enabled").checked = !!s.enabled;
  if (s.interval_hours) $("#sched-interval").value = String(s.interval_hours);
  if (s.retention) $("#sched-retention").value = s.retention;
  $("#sched-last").textContent = s.last_run
    ? `Last automatic backup: ${fmtWhen(s.last_run)}` : "No automatic backup taken yet.";
}

function backupTypeBadge(b) {
  if (b.auto) return '<span class="inline-block px-2 py-0.5 rounded-full bg-sky-100 text-sky-700 text-xs">automatic</span>';
  if (b.label === "imported") return '<span class="inline-block px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 text-xs">imported</span>';
  return `<span class="inline-block px-2 py-0.5 rounded-full bg-slate-100 text-slate-600 text-xs">manual</span>${b.label ? ' <span class="text-xs text-slate-400">' + escapeHtml(b.label) + "</span>" : ""}`;
}

function renderBackups() {
  const tb = $("#backup-rows");
  tb.innerHTML = "";
  $("#backup-empty").classList.toggle("hidden", BACKUPS.length > 0);
  BACKUPS.forEach((b) => {
    const c = b.counts || {};
    const contents = `${c.mappings || 0} mappings · ${c.users || 0} users · ${c.certs || 0} certs · ${c.subinterfaces || 0} sub-ifaces${c.ssl_files ? " · " + c.ssl_files + " ssl" : ""}`;
    const tr = document.createElement("tr");
    tr.className = "hover:bg-slate-50 align-top";
    tr.innerHTML = `
      <td class="px-4 py-3"><input type="checkbox" class="bk-check rounded border-slate-300 cursor-pointer align-middle" data-name="${escapeHtml(b.name)}" ${BK_SELECTED.has(b.name) ? "checked" : ""} /></td>
      <td class="px-6 py-3 whitespace-nowrap text-slate-700">${fmtWhen(b.created || b.mtime)}<div class="text-xs text-slate-400 font-mono">${escapeHtml(b.name)}</div></td>
      <td class="px-6 py-3 whitespace-nowrap">${backupTypeBadge(b)}</td>
      <td class="px-6 py-3 text-xs text-slate-500">${escapeHtml(contents)}</td>
      <td class="px-6 py-3 whitespace-nowrap text-slate-500 tabular-nums">${fmtBytes(b.size)}</td>
      <td class="px-6 py-3 text-right whitespace-nowrap">
        <button data-name="${escapeHtml(b.name)}" class="bk-download text-xs font-medium text-slate-600 hover:text-slate-900 mr-3">Download</button>
        <button data-name="${escapeHtml(b.name)}" class="bk-restore text-xs font-medium text-emerald-700 hover:text-emerald-900 mr-3">Restore</button>
        <button data-name="${escapeHtml(b.name)}" class="bk-del text-xs font-medium text-red-600 hover:text-red-800">Delete</button>
      </td>`;
    tb.appendChild(tr);
  });
  $$(".bk-download").forEach((b) => b.addEventListener("click", () =>
    window.location.href = `/api/backups/download?name=${encodeURIComponent(b.dataset.name)}`));
  $$(".bk-restore").forEach((b) => b.addEventListener("click", () => restoreBackup(b.dataset.name)));
  $$(".bk-del").forEach((b) => b.addEventListener("click", () => deleteBackup(b.dataset.name)));
  $$(".bk-check").forEach((c) => c.addEventListener("change", () => {
    c.checked ? BK_SELECTED.add(c.dataset.name) : BK_SELECTED.delete(c.dataset.name);
    updateBackupBulkBar();
  }));
  updateBackupBulkBar();
}

function updateBackupBulkBar() {
  const present = new Set(BACKUPS.map((b) => b.name));
  [...BK_SELECTED].forEach((n) => { if (!present.has(n)) BK_SELECTED.delete(n); });
  $("#backup-bulk-count").textContent = BK_SELECTED.size;
  $("#backup-bulk-delete").classList.toggle("hidden", BK_SELECTED.size === 0);
  const names = BACKUPS.map((b) => b.name);
  const all = names.length > 0 && names.every((n) => BK_SELECTED.has(n));
  const head = $("#backup-check-all");
  if (head) { head.checked = all; head.indeterminate = !all && names.some((n) => BK_SELECTED.has(n)); }
}

async function createBackupNow() {
  const fd = new FormData();
  fd.append("label", $("#backup-label").value);
  const btn = $("#backup-now"); btn.disabled = true;
  try {
    const j = await (await fetch("/api/backups", { method: "POST", body: fd })).json();
    if (!j.ok) throw new Error(j.error || "Failed");
    $("#backup-label").value = "";
    toast(`Snapshot saved (${j.backup.name}).`);
    await loadBackups();
  } catch (e) { toast(String(e.message || e), false); }
  finally { btn.disabled = false; }
}

async function restoreBackup(name) {
  if (!confirm(`Restore from ${name}?\n\nThis OVERWRITES current mappings, users, certificates, sub-interfaces and settings with the backup's contents. After restoring, use "Re-apply all" on the Mappings page to push them onto the host.`)) return;
  try {
    const fd = new FormData(); fd.append("name", name);
    const j = await (await fetch("/api/backups/restore", { method: "POST", body: fd })).json();
    if (!j.ok) throw new Error(j.error || "Failed");
    toast(`Restored ${j.restored.data.length} data file(s). Reloading…`);
    setTimeout(() => window.location.reload(), 1200);
  } catch (e) { toast(String(e.message || e), false); }
}

async function importBackupFile(file) {
  if (!file) return;
  if (!confirm(`Restore the system from "${file.name}"?\n\nThis OVERWRITES current data (including users & certificates).`)) return;
  try {
    const fd = new FormData(); fd.append("file", file);
    const j = await (await fetch("/api/backups/restore", { method: "POST", body: fd })).json();
    if (!j.ok) throw new Error(j.error || "Failed");
    toast(`Restored from ${file.name}. Reloading…`);
    setTimeout(() => window.location.reload(), 1200);
  } catch (e) { toast(String(e.message || e), false); }
}

async function deleteBackup(name) {
  if (!confirm(`Delete backup ${name}?`)) return;
  try {
    const j = await (await fetch(`/api/backups/${encodeURIComponent(name)}`, { method: "DELETE" })).json();
    if (!j.ok) throw new Error(j.error || "Failed");
    toast(`Deleted ${name}.`);
    await loadBackups();
  } catch (e) { toast(String(e.message || e), false); }
}

async function deleteSelectedBackups() {
  const names = [...BK_SELECTED];
  if (!names.length) return;
  if (!confirm(`Delete ${names.length} backup(s)?`)) return;
  let ok = 0; const fails = [];
  for (const n of names) {
    try {
      const j = await (await fetch(`/api/backups/${encodeURIComponent(n)}`, { method: "DELETE" })).json();
      j.ok ? ok++ : fails.push(n);
    } catch (_) { fails.push(n); }
  }
  BK_SELECTED.clear();
  await loadBackups();
  toast(fails.length ? `Deleted ${ok}, ${fails.length} failed.` : `Deleted ${ok} backup(s).`, !fails.length);
}

async function saveSchedule() {
  const fd = new FormData();
  fd.append("enabled", $("#sched-enabled").checked ? "1" : "0");
  fd.append("interval_hours", $("#sched-interval").value);
  fd.append("retention", $("#sched-retention").value);
  try {
    const j = await (await fetch("/api/backups/schedule", { method: "POST", body: fd })).json();
    if (!j.ok) throw new Error(j.error || "Failed");
    renderSchedule(j.schedule);
    toast("Backup schedule saved.");
  } catch (e) { toast(String(e.message || e), false); }
}

async function loadMe() {
  try {
    const st = await (await fetch("/api/auth/status")).json();
    ME = st.user || null;
  } catch (_) { ME = null; }
  if (!ME) { window.location.href = "/login"; return; }
  $("#who").textContent = `${ME.username} · ${ME.role}`;
  const av = $("#avatar"); if (av) av.textContent = (ME.username[0] || "?").toUpperCase();
  // Creators can add/edit + export, but not destructive/bulk or user mgmt.
  const admin = isAdmin();
  $("#import-btn").classList.toggle("hidden", !admin);
  $("#reapply-btn").classList.toggle("hidden", !admin);
  $("#users-card").classList.toggle("hidden", !admin);
  $("#nav-users").classList.toggle("hidden", !admin);
  $("#activity-card").classList.toggle("hidden", !admin);
  $("#nav-activity").classList.toggle("hidden", !admin);
  $("#nav-backup").classList.toggle("hidden", !admin);
  $("#nav-waf").classList.toggle("hidden", !admin);
  $("#nav-access").classList.toggle("hidden", !admin);
  $("#nav-firewall").classList.toggle("hidden", !admin);
  // Interfaces page: only admins can change the sub-interface policy / network.
  $("#iface-settings-card").classList.toggle("hidden", !admin);
  $("#iface-network-card").classList.toggle("hidden", !admin);
  $("#iface-system-card").classList.toggle("hidden", !admin);
}

async function logout() {
  await fetch("/api/logout", { method: "POST" });
  window.location.href = "/login";
}

async function changePassword() {
  const cur = prompt("Current password:");
  if (cur === null) return;
  const next = prompt("New password (min 8 chars):");
  if (next === null) return;
  const fd = new FormData();
  fd.append("current_password", cur);
  fd.append("new_password", next);
  const j = await (await fetch("/api/account/password", { method: "POST", body: fd })).json();
  toast(j.ok ? "Password changed." : (j.error || "Could not change password."), j.ok);
}

// --- user management (admin) -----------------------------------------------
let USERS = [];
async function loadUsers() {
  if (!isAdmin()) return;
  const users = (await (await fetch("/api/users")).json()).users || [];
  USERS = users;
  const rows = $("#user-rows");
  rows.innerHTML = "";
  for (const u of users) {
    const self = ME && u.username === ME.username;
    const tr = document.createElement("tr");
    tr.className = "hover:bg-slate-50";
    tr.innerHTML = `
      <td class="px-4 py-3">${self
        ? '<input type="checkbox" disabled title="Cannot delete your own account" class="rounded border-slate-200 align-middle opacity-40" />'
        : `<input type="checkbox" class="user-check rounded border-slate-300 cursor-pointer align-middle" data-user="${escapeHtml(u.username)}" ${USR_SELECTED.has(u.username) ? "checked" : ""} />`}</td>
      <td class="px-6 py-3 font-mono">${escapeHtml(u.username)}${self ? ' <span class="text-xs text-slate-400">(you)</span>' : ""}</td>
      <td class="px-6 py-3"><span class="inline-block px-2 py-0.5 rounded-full text-xs ${u.role === "admin" ? "bg-emerald-100 text-emerald-700" : "bg-slate-100 text-slate-600"}">${escapeHtml(u.role)}</span></td>
      <td class="px-6 py-3 text-right whitespace-nowrap">
        <button type="button" data-user="${escapeHtml(u.username)}" class="edit-user text-xs font-medium text-emerald-700 hover:text-emerald-900 mr-3">Edit</button>
        ${self ? "" : `<button type="button" data-user="${escapeHtml(u.username)}" class="del-user text-xs font-medium text-red-600 hover:text-red-800">Delete</button>`}
      </td>`;
    rows.appendChild(tr);

    // Hidden inline editor: change role and/or reset the password.
    const ed = document.createElement("tr");
    ed.className = "hidden bg-slate-50/70";
    ed.dataset.editor = u.username;
    ed.innerHTML = `
      <td colspan="4" class="px-6 py-4 animate-fadeIn">
        <div class="flex flex-wrap items-end gap-3">
          <label class="text-xs font-medium text-slate-600">Role
            <select class="ed-role mt-1 block rounded-lg border border-slate-300 px-3 py-2 text-sm bg-white outline-none focus:ring-2 focus:ring-emerald-500">
              <option value="creator"${u.role === "creator" ? " selected" : ""}>creator</option>
              <option value="admin"${u.role === "admin" ? " selected" : ""}>admin</option>
            </select>
          </label>
          <label class="text-xs font-medium text-slate-600 flex-1 min-w-[160px]">New password <span class="text-slate-400 font-normal">(blank = keep)</span>
            <input type="password" class="ed-pass mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-emerald-500" placeholder="min 8 chars" autocomplete="new-password" />
          </label>
          <div class="flex gap-2">
            <button type="button" class="ed-save btn-primary bg-gradient-to-br from-emerald-500 to-emerald-600 hover:from-emerald-600 hover:to-emerald-700 text-white text-sm font-semibold rounded-xl px-4 py-2 shadow-lg shadow-emerald-600/25">Save</button>
            <button type="button" class="ed-cancel text-sm font-medium text-slate-500 hover:text-slate-800 rounded-xl px-3 py-2">Cancel</button>
          </div>
        </div>
      </td>`;
    rows.appendChild(ed);
  }
  rows.querySelectorAll(".del-user").forEach((b) =>
    b.addEventListener("click", () => deleteUser(b.dataset.user)));
  rows.querySelectorAll(".edit-user").forEach((b) =>
    b.addEventListener("click", () => toggleUserEditor(b.dataset.user)));
  rows.querySelectorAll("[data-editor]").forEach((ed) => {
    ed.querySelector(".ed-cancel").addEventListener("click", () => ed.classList.add("hidden"));
    ed.querySelector(".ed-save").addEventListener("click", () => saveUserEdit(ed.dataset.editor, ed));
  });
  rows.querySelectorAll(".user-check").forEach((c) => c.addEventListener("change", () => {
    c.checked ? USR_SELECTED.add(c.dataset.user) : USR_SELECTED.delete(c.dataset.user);
    updateUserBulkBar();
  }));
  updateUserBulkBar();
}

// --- bulk selection on the users table (never includes your own account) ----
const USR_SELECTED = new Set();

function deletableUsers() {
  return USERS.map((u) => u.username).filter((n) => !(ME && n === ME.username));
}

function updateUserBulkBar() {
  const present = new Set(deletableUsers());
  [...USR_SELECTED].forEach((n) => { if (!present.has(n)) USR_SELECTED.delete(n); });
  $("#user-bulk-count").textContent = USR_SELECTED.size;
  $("#user-bulk-delete").classList.toggle("hidden", USR_SELECTED.size === 0);
  const names = deletableUsers();
  const all = names.length > 0 && names.every((n) => USR_SELECTED.has(n));
  const head = $("#user-check-all");
  if (head) { head.checked = all; head.indeterminate = !all && names.some((n) => USR_SELECTED.has(n)); }
}

function toggleUserSelectAll(on) {
  deletableUsers().forEach((n) => on ? USR_SELECTED.add(n) : USR_SELECTED.delete(n));
  $$(".user-check").forEach((c) => { c.checked = on; });
  updateUserBulkBar();
}

async function deleteSelectedUsers() {
  const names = [...USR_SELECTED];
  if (!names.length) return;
  if (!confirm(`Delete ${names.length} user(s)?\n\n${names.join(", ")}`)) return;
  let ok = 0; const fails = [];
  for (const n of names) {
    try {
      const j = await (await fetch(`/api/users/${encodeURIComponent(n)}`, { method: "DELETE" })).json();
      j.ok ? ok++ : fails.push(`${n}: ${j.error || "failed"}`);
    } catch (e) { fails.push(`${n}: ${e.message}`); }
  }
  USR_SELECTED.clear();
  await loadUsers();
  toast(fails.length ? `Deleted ${ok}, ${fails.length} failed: ${fails[0]}` : `Deleted ${ok} user(s).`, !fails.length);
}

function toggleUserEditor(username) {
  const ed = document.querySelector(`tr[data-editor="${CSS.escape(username)}"]`);
  if (!ed) return;
  $$("[data-editor]").forEach((x) => { if (x !== ed) x.classList.add("hidden"); });
  ed.classList.toggle("hidden");
  if (!ed.classList.contains("hidden")) ed.querySelector(".ed-pass").focus();
}

async function saveUserEdit(username, ed) {
  const role = ed.querySelector(".ed-role").value;
  const pass = ed.querySelector(".ed-pass").value;
  const fd = new FormData();
  fd.append("role", role);
  if (pass) fd.append("password", pass);
  const btn = ed.querySelector(".ed-save");
  btn.disabled = true;
  try {
    const j = await (await fetch(`/api/users/${encodeURIComponent(username)}`,
      { method: "POST", body: fd })).json();
    if (j.ok) {
      toast(`User ${username} updated.`);
      const editingSelf = ME && username === ME.username;
      await loadUsers();
      if (editingSelf) await loadMe();   // role change may alter my own permissions
    } else {
      toast(j.error || "Could not update user.", false);
    }
  } catch (err) {
    toast("Update failed: " + err.message, false);
  } finally {
    btn.disabled = false;
  }
}

async function createUser(e) {
  e.preventDefault();
  const fd = new FormData($("#user-form"));
  const btn = $("#user-form").querySelector('button[type="submit"]');
  setBtnLoading(btn, true, "Adding…");
  showStatus("loading", "Adding user…", "Creating the account.");
  try {
    const j = await (await fetch("/api/users", { method: "POST", body: fd })).json();
    if (j.ok) {
      showStatus("success", "User added", `${j.user.username} (${j.user.role}) created.`);
      $("#user-form").reset();
      await loadUsers();
    } else {
      showStatus("error", "Couldn't add user", j.error || "The host rejected the request.");
    }
  } catch (err) {
    showStatus("error", "Request failed", err.message || String(err));
  } finally {
    setBtnLoading(btn, false);
  }
}

async function deleteUser(username) {
  if (!confirm(`Delete user "${username}"?`)) return;
  const j = await (await fetch(`/api/users/${encodeURIComponent(username)}`, { method: "DELETE" })).json();
  if (j.ok) { toast(`User ${username} deleted.`); await loadUsers(); }
  else toast(j.error || "Could not delete user.", false);
}

// --- per-mapping Diagnose panel -------------------------------------------
let DIAG_DOMAIN = null;     // domain currently open in the panel, or null
let DIAG_PORT = null;       // listen port of the mapping open in the panel
let DIAG_TIMER = null;      // auto-refresh interval id

function openDiagnose(domain, port) {
  DIAG_DOMAIN = domain;
  DIAG_PORT = port != null ? String(port) : null;
  $("#diag-domain").textContent = DIAG_PORT ? `${domain}:${DIAG_PORT}` : domain;
  $("#diag-listener").textContent = "checking…";
  $("#diag-conns").textContent = "—";
  $("#diag-backends-sum").textContent = "—";
  $("#diag-backends").innerHTML = "";
  $("#diag-log").textContent = "loading…";
  $("#diag-log-path").textContent = "";
  $("#diag-updated").textContent = "—";
  $("#diag-overlay").classList.remove("hidden");
  refreshDiagnose();
  clearInterval(DIAG_TIMER);
  DIAG_TIMER = setInterval(refreshDiagnose, 2500);   // ~live
}

function closeDiagnose() {
  clearInterval(DIAG_TIMER);
  DIAG_TIMER = null;
  DIAG_DOMAIN = null;
  DIAG_PORT = null;
  $("#diag-overlay").classList.add("hidden");
}

async function refreshDiagnose() {
  const domain = DIAG_DOMAIN;
  if (!domain) return;
  const port = DIAG_PORT;
  try {
    const url = `/api/mappings/${encodeURIComponent(domain)}/diagnose${port ? `?port=${encodeURIComponent(port)}` : ""}`;
    const j = await (await fetch(url)).json();
    if (DIAG_DOMAIN !== domain || DIAG_PORT !== port) return;   // panel switched/closed mid-flight
    if (!j.ok) { $("#diag-log").textContent = j.error || "Diagnose failed."; return; }
    renderDiagnose(j);
  } catch (err) {
    if (DIAG_DOMAIN === domain) $("#diag-log").textContent = "Diagnose failed: " + err.message;
  }
}

function renderDiagnose(j) {
  const lis = j.listener || {};
  const dot = (ok) => `<span class="inline-block h-2 w-2 rounded-full mr-1 ${ok ? "bg-emerald-500" : "bg-red-500"}"></span>`;
  if (!lis.ip) {
    $("#diag-listener").innerHTML = `<span class="text-slate-400">no bind IP (DHCP pending?)</span>`;
  } else {
    $("#diag-listener").innerHTML = `${dot(lis.bound)}<span class="${lis.bound ? "text-emerald-700" : "text-red-600"}">`
      + `${escapeHtml(lis.ip)}:${escapeHtml(lis.port)} ${lis.bound ? "bound" : "NOT bound"}</span>`;
  }

  const c = j.connections;
  const udp = (lis.transport || "tcp") === "udp";
  $("#diag-conns").innerHTML = udp
    ? '<span class="text-slate-400">n/a · UDP (connectionless)</span>'
    : (c === null || c === undefined
        ? '<span class="text-slate-400">unavailable</span>' : `<span class="font-mono">${c}</span>`);

  const bks = j.backends || [];
  const up = bks.filter((b) => b.enabled && b.up).length;
  const tot = bks.filter((b) => b.enabled).length;
  $("#diag-backends-sum").innerHTML = tot
    ? `<span class="${up === tot ? "text-emerald-700" : up ? "text-amber-600" : "text-red-600"}">${up}/${tot} reachable</span>`
    : '<span class="text-slate-400">none</span>';

  $("#diag-backends").innerHTML = bks.map((b) => {
    const ok = b.up, off = !b.enabled;
    const meta = off ? "disabled" : ok ? (b.latency_ms != null ? b.latency_ms + " ms" : "up") : (b.error || "unreachable");
    return `<div class="flex items-center justify-between text-xs rounded-md border border-slate-100 px-2.5 py-1.5">`
      + `<span class="font-mono ${off ? "text-slate-400 line-through" : "text-slate-700"}">${dot(ok && !off)}${escapeHtml(beDisplay(b.server))}</span>`
      + `<span class="${ok && !off ? "text-slate-500" : "text-red-600"}">${escapeHtml(meta)}</span></div>`;
  }).join("");

  const logs = j.logs || {};
  $("#diag-log-path").textContent = logs.path || "";
  if (logs.error) {
    $("#diag-log").textContent = "⚠ " + logs.error;
  } else if (!logs.lines || !logs.lines.length) {
    $("#diag-log").textContent = "No matching error-log lines — nothing wrong logged for this mapping.";
  } else {
    const pre = $("#diag-log");
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent = logs.lines.join("\n");
    if (atBottom) pre.scrollTop = pre.scrollHeight;   // keep following the tail
  }

  $("#diag-updated").textContent = (j.simulate ? "simulated · " : "") + new Date().toLocaleTimeString();
}

// --- WAF (ModSecurity + OWASP CRS) -----------------------------------------
let WAF = null;
async function loadWaf() {
  if (!isAdmin()) return;
  try {
    const j = await (await fetch("/api/waf/status")).json();
    WAF = j.status || {};
    renderWaf(WAF, j.events || []);
  } catch (_) { toast("Could not load WAF status.", false); }
  loadWafApps();
}

async function loadWafApps() {
  if (!isAdmin()) return;
  try {
    const j = await (await fetch("/api/waf/apps")).json();
    renderWafApps(j.apps || [], !!j.waf_installed);
  } catch (_) { /* leave the table as-is */ }
}

function renderWafApps(apps, installed) {
  const tb = $("#waf-apps");
  tb.innerHTML = "";
  $("#waf-apps-empty").classList.toggle("hidden", apps.length > 0);
  $("#waf-apps-needinstall").classList.toggle("hidden", installed || apps.length === 0);
  for (const a of apps) {
    const tr = document.createElement("tr");
    const typeBadge = a.transport === "udp"
      ? '<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-slate-100 text-slate-500">UDP/L4</span>'
      : a.has_cert
        ? '<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-sky-100 text-sky-700">HTTPS</span>'
        : a.protocol === "http"
          ? '<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-sky-100 text-sky-700">HTTP</span>'
          : '<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-slate-100 text-slate-500">TCP/L4</span>';

    let action;
    if (!a.eligible) {
      action = `<span class="text-xs text-slate-400">n/a</span>` +
               `<div class="text-[11px] text-slate-400 mt-0.5 max-w-[220px] ml-auto">${escapeHtml(a.reason)}</div>`;
    } else if (!installed) {
      action = `<span class="text-xs text-slate-400">install WAF first</span>`;
    } else if (a.bound) {
      action = `<button data-unbind="${escapeHtml(a.domain)}" class="waf-unbind text-xs font-semibold px-3 py-1.5 rounded-lg bg-emerald-100 text-emerald-700 hover:bg-emerald-200 transition">Protected · Unbind</button>`;
    } else {
      action = `<button data-bind="${escapeHtml(a.domain)}" class="waf-bind text-xs font-semibold px-3 py-1.5 rounded-lg bg-slate-200 text-slate-700 hover:bg-slate-300 transition">Bind WAF</button>`;
    }
    tr.innerHTML =
      `<td class="px-6 py-3"><div class="font-medium text-slate-800">${escapeHtml(a.domain)}</div>` +
      `${a.enabled ? "" : '<div class="text-xs text-amber-600">disabled</div>'}</td>` +
      `<td class="px-4 py-3 font-mono text-xs text-slate-600">${escapeHtml(a.backend)}</td>` +
      `<td class="px-4 py-3">${typeBadge}</td>` +
      `<td class="px-6 py-3 text-right">${action}</td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll(".waf-bind").forEach((b) =>
    b.addEventListener("click", () => bindApp(b.dataset.bind)));
  tb.querySelectorAll(".waf-unbind").forEach((b) =>
    b.addEventListener("click", () => unbindApp(b.dataset.unbind)));
}

async function bindApp(domain) {
  if (!confirm(`Bind ${domain} to the WAF?\n\nIt becomes an HTTPS reverse proxy with ModSecurity in front — nginx terminates TLS to inspect requests. Starts in the WAF's current mode. Tune false positives before enforcing.`)) return;
  const fd = new FormData(); fd.append("domain", domain);
  const j = await (await fetch("/api/waf/bind", { method: "POST", body: fd })).json();
  renderWafSteps(j.steps);
  toast(j.ok ? `${domain} is now behind the WAF.` : (j.error || "Bind failed."), j.ok);
  loadWafApps();
}

async function unbindApp(domain) {
  if (!confirm(`Unbind ${domain}?\n\nIt reverts to a Layer-4 stream proxy (no WAF inspection).`)) return;
  const fd = new FormData(); fd.append("domain", domain);
  const j = await (await fetch("/api/waf/unbind", { method: "POST", body: fd })).json();
  renderWafSteps(j.steps);
  toast(j.ok ? `${domain} reverted to Layer-4.` : (j.error || "Unbind failed."), j.ok);
  loadWafApps();
}

function renderWaf(s, events) {
  const installed = !!s.installed;
  $("#waf-notinstalled").classList.toggle("hidden", installed);

  const mode = s.mode || "—";
  const badge = $("#waf-mode-badge");
  const label = { On: "Enforcing", DetectionOnly: "Detection", Off: "Off" }[mode] || "Unknown";
  const styles = { On: "bg-emerald-100 text-emerald-700", DetectionOnly: "bg-amber-100 text-amber-700", Off: "bg-red-100 text-red-700" };
  badge.textContent = installed ? label : "Not installed";
  badge.className = "text-xs font-semibold px-2.5 py-1 rounded-full " + (installed ? (styles[mode] || "bg-slate-100 text-slate-600") : "bg-slate-100 text-slate-600");

  $$(".waf-mode-btn").forEach((b) => {
    const active = installed && b.dataset.mode === mode;
    b.classList.toggle("ring-2", active);
    b.classList.toggle("ring-emerald-500", active);
    b.classList.toggle("border-emerald-500", active);
    b.disabled = !installed;
    b.classList.toggle("opacity-40", !installed);
  });

  $("#waf-stat-installed").textContent = installed ? (s.simulate ? "Yes (simulated)" : "Yes") : "No";
  $("#waf-stat-port").textContent = s.port ? ("https :" + s.port) : "—";
  $("#waf-stat-upstream").textContent = s.upstream || "—";
  $("#waf-stat-bind").textContent = s.app_host || "—";

  $("#waf-loopback-warn").classList.toggle("hidden", !(installed && !s.app_loopback));
  $("#waf-app-host").textContent = s.app_host || "";

  $("#waf-port").value = s.port || "";
  $("#waf-login-rate").value = s.login_rate || "";
  $("#waf-api-rate").value = s.api_rate || "";
  $("#waf-log-path").textContent = s.audit_log || "audit log";

  // install / repair progress
  const inst = s.install || {};
  const installing = !!inst.running;
  const panel = $("#waf-install-panel");
  if (installing || inst.log) {
    panel.classList.remove("hidden");
    $("#waf-install-log").textContent = inst.log || "";
    $("#waf-install-spin").classList.toggle("hidden", !installing);
    $("#waf-install-title").textContent = installing
      ? "Installing WAF…" : (inst.ok ? "WAF install finished ✓" : "WAF install failed ✗");
  } else {
    panel.classList.add("hidden");
  }
  $("#waf-repair-btn").classList.toggle("hidden", !installed);
  [$("#waf-install-btn"), $("#waf-repair-btn")].forEach((b) => { if (b) b.disabled = installing; });
  if (installing) startWafInstallPoll();   // resume polling if a run is live

  renderWafEvents(events);
}

function renderWafEvents(events) {
  const tb = $("#waf-events");
  const empty = $("#waf-events-empty");
  tb.innerHTML = "";
  if (!events || !events.length) { empty.classList.remove("hidden"); return; }
  empty.classList.add("hidden");
  // libmodsecurity logs severity as a number; CRS docs use the names.
  const SEV = ["EMERGENCY", "ALERT", "CRITICAL", "ERROR", "WARNING", "NOTICE", "INFO", "DEBUG"];
  const sevLabel = (v) => (/^\d$/.test(v) ? (SEV[+v] || v) : v);
  const sevColor = (v) => /emerg|alert|crit/i.test(v) ? "text-red-600" : /warn|error/i.test(v) ? "text-amber-600" : "text-slate-500";
  for (const e of events) {
    const sev = sevLabel(e.severity || "");
    const tr = document.createElement("tr");
    if (e.time) tr.title = e.time;
    tr.innerHTML =
      `<td class="px-4 py-2 font-mono text-xs text-slate-500">${escapeHtml(e.id || "—")}</td>` +
      `<td class="px-4 py-2">${escapeHtml(e.msg || "—")}</td>` +
      `<td class="px-4 py-2 text-xs font-semibold ${sevColor(sev)}">${escapeHtml(sev || "—")}</td>` +
      `<td class="px-4 py-2 font-mono text-xs text-slate-600">${escapeHtml(e.client_ip || "—")}</td>` +
      `<td class="px-4 py-2 font-mono text-xs text-slate-600">${escapeHtml(e.uri || "—")}</td>`;
    tb.appendChild(tr);
  }
}

function renderWafSteps(steps) {
  const ol = $("#waf-steps");
  ol.innerHTML = "";
  if (!steps || !steps.length) { ol.innerHTML = '<li class="text-slate-400">No recent action.</li>'; return; }
  for (const s of steps) {
    const li = document.createElement("li");
    li.className = s.ok ? "text-emerald-700 break-all" : "text-red-600 break-all";
    li.textContent = (s.ok ? "✓ " : "✗ ") + s.name + (s.detail ? " — " + s.detail : "");
    ol.appendChild(li);
  }
}

async function setWafMode(mode) {
  if (mode === "On" && !confirm("Switch the WAF to ENFORCE?\n\nIt will start blocking requests it considers malicious. Review your Detection-mode events first — a false positive can lock legitimate admin actions.")) return;
  const fd = new FormData(); fd.append("mode", mode);
  const j = await (await fetch("/api/waf/mode", { method: "POST", body: fd })).json();
  renderWafSteps(j.steps);
  toast(j.ok ? ("WAF mode → " + mode) : "Could not change WAF mode.", j.ok);
  await loadWaf();
}

async function saveWafSettings() {
  const fd = new FormData();
  fd.append("port", $("#waf-port").value.trim());
  fd.append("login_rate", $("#waf-login-rate").value.trim());
  fd.append("api_rate", $("#waf-api-rate").value.trim());
  const j = await (await fetch("/api/waf/settings", { method: "POST", body: fd })).json();
  renderWafSteps(j.steps);
  toast(j.ok ? "WAF settings saved & nginx reloaded." : (j.error || "Could not save WAF settings."), j.ok);
  await loadWaf();
}

let WAF_POLL = null;
async function installWaf() {
  if (!confirm("Install / repair the WAF now?\n\nThis runs on the server (apt + compiles the ModSecurity module + installs the rules) and can take a few minutes. It starts in Detection mode, so it won't block anything until you enforce.")) return;
  await fetch("/api/waf/install", { method: "POST" });
  toast("WAF install started — watch the progress log.");
  await loadWaf();
  startWafInstallPoll();
}

function startWafInstallPoll() {
  if (WAF_POLL) return;   // already polling
  WAF_POLL = setInterval(async () => {
    await loadWaf();
    if (!(WAF && WAF.install && WAF.install.running)) {
      clearInterval(WAF_POLL); WAF_POLL = null;
      const ok = WAF && WAF.install && WAF.install.ok;
      toast(ok ? "WAF install finished." : "WAF install failed — see the progress log.", ok);
    }
  }, 3000);
}

document.addEventListener("DOMContentLoaded", async () => {
  // Pre-switch to the hash page immediately (pure CSS, no data needed) so the
  // correct section is visible from the first paint instead of flashing mappings.
  const _initHash = location.hash.slice(1);
  if (PAGES.includes(_initHash) && _initHash !== "mappings") {
    PAGES.forEach(p => { const el = $("#page-" + p); if (el) el.classList.toggle("hidden", p !== _initHash); });
    $$(".nav-link").forEach(b => b.classList.toggle("active", b.dataset.page === _initHash));
  }

  await loadMe();
  await loadConfig();
  await loadInterfaces();
  await loadSettings();   // sub-interface policy → adapts the mapping form
  await loadSubinterfaces();   // populate the mapping form's sub-interface dropdown
  addBackendRow();   // empty — placeholder shows the example address
  selectSsl("none");
  onMethodChange();
  onLbChange();
  filterProtocols();   // hide UDP presets until UDP is selected
  reflectProtocol();

  // sidebar nav: switch pages (preserve form state)
  $$(".nav-link").forEach((b) => b.addEventListener("click", () => showPage(b.dataset.page)));
  // "New Mapping" / empty-state jumps start a fresh add
  $$(".nav-jump").forEach((b) => b.addEventListener("click", () => { resetForm(); showPage("form"); }));
  $("#search").addEventListener("input", () => { MAP_PAGE = 1; renderMappings(); });

  $("#map-form").addEventListener("submit", apply);
  $("#preview-btn").addEventListener("click", preview);
  $("#refresh-btn").addEventListener("click", async () => { await loadMappings(); loadHealth(true); });
  $("#export-btn").addEventListener("click", exportBackup);
  $("#check-all").addEventListener("change", (e) => toggleSelectAll(e.target.checked));
  $("#bulk-export").addEventListener("click", exportSelected);
  $("#bulk-delete").addEventListener("click", deleteSelected);
  $("#bulk-clear").addEventListener("click", clearSelection);
  $("#import-btn").addEventListener("click", () => $("#import-file").click());
  $("#reapply-btn").addEventListener("click", reapplyAll);
  $("#import-file").addEventListener("change", (e) => {
    importBackup(e.target.files[0]);
    e.target.value = "";   // allow re-importing the same file
  });
  $("#add-backend").addEventListener("click", () => addBackendRow());
  // Docker page
  const dcf = $("#docker-create-form"); if (dcf) dcf.addEventListener("submit", dockerCreateMapping);
  const dr = $("#docker-refresh"); if (dr) dr.addEventListener("click", loadDocker);
  refreshDockerNav();
  $("#protocol").addEventListener("change", onProtocolChange);
  $("#listen_port").addEventListener("input", onListenPortChange);
  $$('input[name="transport"]').forEach((r) => r.addEventListener("change", onTransportChange));
  $("#cancel-edit").addEventListener("click", resetForm);
  $("#gen-mac").addEventListener("click", generateMac);
  $("#interface").addEventListener("change", updateIfaceInfo);
  $("#lb_method").addEventListener("change", onLbChange);
  $("#lb_enabled").addEventListener("change", () => {
    if (!$("#lb_enabled").checked) { $("#lb_method").value = "round_robin"; onLbChange(); }
    toggleLbSection();
  });
  $("#rate_limit_enabled").addEventListener("change", toggleRateSection);
  $("#failover").addEventListener("change", syncFailoverUI);
  $$('input[name="alloc_method"]').forEach((r) => r.addEventListener("change", onMethodChange));
  $$(".ssl-tab").forEach((b) => b.addEventListener("click", () => selectSsl(b.dataset.ssl)));
  $("#logout-btn").addEventListener("click", logout);
  $("#passwd-btn").addEventListener("click", changePassword);
  $("#user-form").addEventListener("submit", createUser);
  $("#user-check-all").addEventListener("change", (e) => toggleUserSelectAll(e.target.checked));
  $("#user-bulk-delete").addEventListener("click", deleteSelectedUsers);
  $("#activity-refresh").addEventListener("click", loadActivity);
  $("#backup-refresh").addEventListener("click", loadBackups);
  $("#backup-now").addEventListener("click", createBackupNow);
  $("#backup-download").addEventListener("click", () => { window.location.href = "/api/backups/download?now=1"; });
  $("#backup-import-btn").addEventListener("click", () => $("#backup-import-file").click());
  $("#backup-import-file").addEventListener("change", (e) => { importBackupFile(e.target.files[0]); e.target.value = ""; });
  $("#sched-save").addEventListener("click", saveSchedule);
  $("#backup-check-all").addEventListener("change", (e) => {
    BACKUPS.forEach((b) => e.target.checked ? BK_SELECTED.add(b.name) : BK_SELECTED.delete(b.name));
    $$(".bk-check").forEach((c) => { c.checked = e.target.checked; });
    updateBackupBulkBar();
  });
  $("#backup-bulk-delete").addEventListener("click", deleteSelectedBackups);
  $("#waf-refresh").addEventListener("click", loadWaf);
  $("#waf-save").addEventListener("click", saveWafSettings);
  $("#waf-install-btn").addEventListener("click", installWaf);
  $("#waf-repair-btn").addEventListener("click", installWaf);
  $$(".waf-mode-btn").forEach((b) => b.addEventListener("click", () => setWafMode(b.dataset.mode)));
  // routing map = pan + zoom like a map. Wheel zooms toward the cursor…
  const rscroll = $("#route-scroll");
  rscroll.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoomRouteAt(ROUTE_ZOOM + (e.deltaY < 0 ? 0.12 : -0.12), e.clientX, e.clientY);
  }, { passive: false });
  $("#route-zoom-in").addEventListener("click", () => zoomRouteAt(ROUTE_ZOOM + 0.2));
  $("#route-zoom-out").addEventListener("click", () => zoomRouteAt(ROUTE_ZOOM - 0.2));
  $("#route-zoom-reset").addEventListener("click", () => setRouteZoom(1));

  // …and drag to pan (grab cursor). Clicks/drags on a graph node act on the node
  // (expand, collapse, move) rather than panning the canvas.
  let panning = false, pSL = 0, pST = 0, pX = 0, pY = 0;
  rscroll.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.target.closest(".rnode")) return;
    panning = true;
    pX = e.clientX; pY = e.clientY; pSL = rscroll.scrollLeft; pST = rscroll.scrollTop;
    rscroll.classList.add("cursor-grabbing");
    e.preventDefault();   // suppress text selection while dragging
  });
  window.addEventListener("mousemove", (e) => {
    if (!panning) return;
    rscroll.scrollLeft = pSL - (e.clientX - pX);
    rscroll.scrollTop = pST - (e.clientY - pY);
  });
  const endPan = () => { if (!panning) return; panning = false; rscroll.classList.remove("cursor-grabbing"); };
  window.addEventListener("mouseup", endPan);
  window.addEventListener("blur", endPan);
  // n8n-style node dragging (reset to default layout on any refresh/re-render)
  window.addEventListener("mousemove", onRouteNodeMove);
  window.addEventListener("mouseup", onRouteNodeUp);
  $("#mon-refresh").addEventListener("click", () => { loadMetrics(); loadIfaceTraffic(); });
  $("#iface-refresh").addEventListener("click", () => { loadSubinterfaces(); if (isAdmin()) loadNetworkSettings(); });
  $("#subiface-toggle").addEventListener("change", (e) => saveSubifaceSetting(e.target.checked));
  $("#dns-add").addEventListener("click", () => addDnsRow(""));
  $("#dns-save").addEventListener("click", saveDns);
  $("#hosts-save").addEventListener("click", saveHosts);
  $("#reboot-host").addEventListener("click", rebootHost);
  // Sub-interface manager
  $("#subiface-form").addEventListener("submit", submitSubiface);
  $("#si-cancel").addEventListener("click", resetSubifaceForm);
  $("#si-gen-mac").addEventListener("click", async () => {
    try { const j = await (await fetch("/api/random-mac")).json(); if (j.ok) $("#si-mac").value = j.mac; } catch (_) {}
  });
  $("#si-check-all").addEventListener("change", (e) => toggleSiSelectAll(e.target.checked));
  $("#si-bulk-delete").addEventListener("click", deleteSelectedSubifaces);
  $("#subiface-select").addEventListener("change", syncSubifaceBindIp);
  $("#ssl-refresh").addEventListener("click", loadSslCerts);
  $("#ssl-selfsigned-form").addEventListener("submit", (e) => { e.preventDefault(); createCert("selfsigned", e.target); });
  $("#ssl-upload-form").addEventListener("submit", (e) => { e.preventDefault(); createCert("upload", e.target); });

  // Access lists
  $("#acl-form").addEventListener("submit", submitAccessList);
  $("#acl-cancel-edit").addEventListener("click", resetAccessForm);
  $("#acl-refresh-all").addEventListener("click", loadAccessLists);
  $("#acl-default-select").addEventListener("change", (e) => setDefaultAccessList(e.target.value));

  // Firewall
  $("#fw-refresh").addEventListener("click", loadFirewall);
  $("#fw-master-toggle").addEventListener("change", (e) => toggleFwMaster(e.target.checked));
  $("#fw-iface-save").addEventListener("click", saveFwIfaceSettings);
  $("#fw-rule-form").addEventListener("submit", submitFwRule);
  $("#fw-rule-cancel-edit").addEventListener("click", resetFwRuleForm);
  $("#fw-panic-btn").addEventListener("click", fwPanic);
  // Keep the plain-English preview + warnings in step with the form.
  ["fw-rule-direction", "fw-rule-protocol", "fw-rule-port", "fw-rule-action", "fw-rule-source"]
    .forEach((id) => $("#" + id).addEventListener("input", updateFwRulePreview));
  $("#fw-src-myip").addEventListener("click", fillFwSourceMyIp);
  $("#fw-src-private").addEventListener("click", () => {
    setVal("fw-rule-source", "192.168.0.0/16");
    updateFwRulePreview();
  });
  $("#fw-src-any").addEventListener("click", () => {
    setVal("fw-rule-source", "");
    updateFwRulePreview();
  });

  // Diagnose panel: close via the × button, backdrop click, or Escape.
  $("#diag-close").addEventListener("click", closeDiagnose);
  $("#diag-overlay").addEventListener("click", (e) => {
    if (e.target === $("#diag-overlay")) closeDiagnose();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && DIAG_DOMAIN) closeDiagnose();
  });

  await loadAccessLists();   // small — also populates the mapping form's access-list dropdown

  // Lazy: each page's list data loads on first access (showPage), not all up
  // front — so the shell + sidebar paint immediately and only the page you open
  // fetches its rows (mappings/health, users, …).
  const hash = location.hash.slice(1);
  showPage(PAGES.includes(hash) ? hash : "mappings");
});

// ==========================================================================
// Tools page
// ==========================================================================

let _toolsReady = false;

function startTools() {
  if (_toolsReady) return;
  _toolsReady = true;

  // Wire up tab buttons
  $$(".tool-tab").forEach((btn) => {
    btn.addEventListener("click", () => showToolTab(btn.dataset.tool));
  });

  // Populate interface select from known IFACES or live API
  _toolsPopulateInterfaces();

  // Allow Enter key to submit in tool input fields
  ["ping-host", "port-host", "port-port", "dns-host", "traceroute-host", "whois-query"].forEach((id) => {
    const el = $("#" + id);
    if (el) el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const tool = id.split("-")[0];
        runTool(tool);
      }
    });
  });

  // Show first tab
  showToolTab("ping");
}

function showToolTab(name) {
  // Update tab buttons
  $$(".tool-tab").forEach((btn) => {
    const active = btn.dataset.tool === name;
    btn.classList.toggle("bg-white",  active);
    btn.classList.toggle("border-slate-200", active);
    btn.classList.toggle("text-sky-600", active);
    btn.classList.toggle("shadow-sm", active);
    btn.classList.toggle("text-slate-500", !active);
    btn.classList.toggle("border-transparent", !active);
    // number badge colour
    const badge = btn.querySelector("span");
    if (badge) {
      badge.classList.toggle("bg-sky-100",   active);
      badge.classList.toggle("text-sky-600", active);
      badge.classList.toggle("bg-slate-200",   !active);
      badge.classList.toggle("text-slate-500", !active);
    }
  });
  // Show / hide panels
  $$(".tool-panel").forEach((p) => p.classList.add("hidden"));
  const panel = $("#toolpanel-" + name);
  if (panel) panel.classList.remove("hidden");
}

function _toolsPopulateInterfaces() {
  const sel = $("#tcpdump-interface");
  if (!sel) return;
  fetch("/api/interfaces/traffic")
    .then((r) => r.json())
    .then((d) => {
      if (!d.ok || !d.interfaces) return;
      const flatten = (nodes) => nodes.flatMap((n) => [n, ...flatten(n.children || [])]);
      const all = flatten(d.interfaces);
      const existing = new Set(Array.from(sel.options).map((o) => o.value));
      all.forEach((iface) => {
        if (!existing.has(iface.name)) {
          const opt = document.createElement("option");
          opt.value = iface.name;
          opt.textContent = iface.name + (iface.kind ? ` (${iface.kind})` : "");
          sel.appendChild(opt);
        }
      });
    })
    .catch(() => {});
}

function runTool(tool) {
  const out = $("#tool-output-" + tool);
  if (!out) return;

  // Gather form data
  const fd = new FormData();
  if (tool === "ping") {
    const host = ($("#ping-host").value || "").trim();
    if (!host) { out.textContent = "Error: Host is required."; return; }
    fd.set("host", host);
    fd.set("count", $("#ping-count").value || "4");
  } else if (tool === "port") {
    const host = ($("#port-host").value || "").trim();
    const port = ($("#port-port").value || "").trim();
    if (!host) { out.textContent = "Error: Host is required."; return; }
    if (!port)  { out.textContent = "Error: Port is required."; return; }
    fd.set("host", host);
    fd.set("port", port);
    fd.set("timeout", $("#port-timeout").value || "5");
  } else if (tool === "dns") {
    const host = ($("#dns-host").value || "").trim();
    if (!host) { out.textContent = "Error: Hostname is required."; return; }
    fd.set("host", host);
    fd.set("type",   $("#dns-type").value || "A");
    fd.set("server", $("#dns-server").value || "");
  } else if (tool === "traceroute") {
    const host = ($("#traceroute-host").value || "").trim();
    if (!host) { out.textContent = "Error: Host is required."; return; }
    fd.set("host", host);
    fd.set("maxhops", $("#traceroute-maxhops").value || "30");
  } else if (tool === "tcpdump") {
    fd.set("interface", $("#tcpdump-interface").value || "any");
    fd.set("count",     $("#tcpdump-count").value || "50");
    fd.set("filter",    $("#tcpdump-filter").value || "");
  } else if (tool === "whois") {
    const query = ($("#whois-query").value || "").trim();
    if (!query) { out.textContent = "Error: Domain or IP is required."; return; }
    fd.set("query", query);
  }

  // Show running state
  out.textContent = "Running…";
  out.classList.add("opacity-60");
  const btn = document.querySelector(`#toolpanel-${tool} .tool-run-btn`);
  if (btn) btn.disabled = true;

  fetch(`/api/tools/${tool}`, { method: "POST", body: fd })
    .then((r) => r.json())
    .then((d) => {
      out.textContent = d.output || d.error || "(empty response)";
      if (!d.ok) out.classList.add("text-red-400");
      else out.classList.remove("text-red-400");
    })
    .catch((err) => {
      out.textContent = "Request failed: " + err.message;
      out.classList.add("text-red-400");
    })
    .finally(() => {
      out.classList.remove("opacity-60");
      if (btn) btn.disabled = false;
    });
}

function clearToolOutput(tool) {
  const out = $("#tool-output-" + tool);
  if (out) {
    out.textContent = "Enter parameters and click Run.";
    out.classList.remove("text-red-400");
  }
}

function copyToolOutput(tool) {
  const out = $("#tool-output-" + tool);
  if (!out) return;
  navigator.clipboard.writeText(out.textContent).then(() => toast("Copied to clipboard")).catch(() => {});
}
