// Page bootstrap: tabs, connection indicator, snapshot polling, event stream.

import { ApiError, auth, getJSON, startEventStream } from "./api.js";
import { initDashboard } from "./dashboard.js";
import { initLoadTest } from "./loadtest.js";
import { initPlayground } from "./playground.js";
import { initRouting } from "./routing.js";
import { Store } from "./store.js";
import { $, h } from "./util.js";

const store = new Store();
const replicaLabel = (rid) => {
  const r = store.snapshot && store.snapshot.replicas[rid];
  if (!r) return `R${rid}`;
  return `R${rid}` + (r.gpu_mapping && r.gpu_mapping.label ? ` · ${r.gpu_mapping.label}` : "");
};
const ctx = { store, replicaLabel };

const TABS = [["playground", "Playground", initPlayground], ["loadtest", "Load test", initLoadTest], ["routing", "Live routing", initRouting], ["dashboard", "Dashboard", initDashboard]];
const views = {};

function buildTabs() {
  const nav = $("#tabs");
  for (const [id, label, init] of TABS) {
    const panel = $(`#panel-${id}`);
    views[id] = init(panel, ctx);
    nav.append(h("button", { role: "tab", id: `tab-${id}`, "aria-controls": `panel-${id}`, "aria-selected": "false", onclick: () => show(id) }, label));
  }
  show(location.hash.slice(1) in views ? location.hash.slice(1) : "playground");
}

function show(id) {
  for (const [tid] of TABS) {
    const on = tid === id;
    $(`#panel-${tid}`).hidden = !on;
    $(`#tab-${tid}`).setAttribute("aria-selected", String(on));
  }
  history.replaceState(null, "", "#" + id);
  if (views[id] && views[id].render) views[id].render();
}

function renderHeader() {
  const snap = store.snapshot, c = store.connection;
  const mode = $("#mode");
  if (snap) {
    mode.textContent = snap.mode.simulated ? "SIMULATED replicas" : "live replicas";
    mode.className = "badge " + (snap.mode.simulated ? "badge-warn" : "badge-ok");
    $("#meta").textContent = `${snap.replicas.length} replicas · policy ${snap.router.policy} · model ${snap.router.model || "n/a"}`;
  }
  const conn = $("#conn");
  conn.textContent = { live: "events: live", connecting: "events: connecting…", reconnecting: "events: reconnecting…", unauthorized: "events: unauthorized", error: "events: error" }[c.state] || c.state;
  conn.className = "badge " + ({ live: "badge-ok", connecting: "badge-info", reconnecting: "badge-warn" }[c.state] || "badge-bad");
  conn.title = c.detail || "";
}

async function refreshSnapshot() {
  try {
    store.setSnapshot(await getJSON("/api/dashboard"));
    $("#auth-box").hidden = true;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) { $("#auth-box").hidden = false; store.setConnection("unauthorized", "router requires an API token"); }
    else store.setConnection("error", String(e.message || e));
  }
}

async function start() {
  buildTabs();
  store.on(renderHeader);

  $("#auth-save").addEventListener("click", async () => { auth.token = $("#auth-token").value.trim(); await begin(); });
  await begin();
}

let streaming = null;
async function begin() {
  await refreshSnapshot();
  if (!store.snapshot) return;
  if (streaming) streaming.stop();
  // Start the live stream from "now": history comes from the records endpoint,
  // so a fresh page does not replay thousands of old events (or animations).
  store.lastSeq = store.snapshot.router.events.head_seq;
  try { store.seedFromRecords((await getJSON("/api/requests?limit=300")).requests); } catch { /* history is optional */ }
  streaming = startEventStream(store, {
    onGap: async () => { await refreshSnapshot(); try { store.seedFromRecords((await getJSON("/api/requests?limit=300")).requests); } catch { /* ignore */ } },
  });
  if (!begin.timer) begin.timer = setInterval(() => { if (!document.hidden) refreshSnapshot(); }, 2000);
}

start();
