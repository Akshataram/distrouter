// Live routing view. Three parts, all driven by the router's real event stream:
//
//  1. A flow diagram of the actual request path in this codebase:
//        Client/UI -> Router[admission -> routing policy -> dispatch/proxy] -> replica -> response
//     Each request is a dot that moves when the router EMITS the matching event
//     (received, routing.decision, dispatch, completed/failed). Nothing is
//     scripted: no event, no movement.
//  2. A timeline: one row per request, segments positioned from the router's own
//     timestamps (routing -> waiting for first token -> streaming).
//  3. A decision log: the policy's own recorded explanation for each choice.
//
// Replica boxes show real data: health/circuit and in-flight counts from the
// router's snapshot, the live count of requests currently dispatched there
// (from events), and the operator-supplied GPU label -- or "GPU mapping
// unavailable". A GPU id is never derived from a replica number.

import { describeDecision, h, replicaChip, shortId } from "./util.js";

const NS = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs = {}, ...kids) => {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) e.setAttribute(k, String(v));
  for (const k of kids.flat()) if (k) e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return e;
};

const W = 1000, H = 460;
const CLIENT = { x: 24, y: H / 2 - 46, w: 150, h: 92 };
const ROUTER = { x: 250, y: 24, w: 340, h: H - 48 };
const BOX = { x: 276, w: 288 };
const ADM = { ...BOX, y: 78, h: 70 };
const POL = { ...BOX, y: 178, h: 110 };
const DSP = { ...BOX, y: 318, h: 70 };
const REP_X = 780, REP_W = 200;

function hue(id) { let n = 0; for (let i = 0; i < id.length; i++) n = (n * 31 + id.charCodeAt(i)) >>> 0; return n % 360; }

export function initRouting(root, ctx) {
  const { store, replicaLabel } = ctx;
  const maxParticles = 90;
  const particles = [];
  let replicaBoxes = [];     // [{rid, x, y, w, h, el parts}]
  let replicaCount = 0;
  let lastDecision = null;

  root.append(
    h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "Live request flow"), h("span", { id: "rt-legend", class: "muted small" })),
      h("div", { id: "rt-diagram", class: "diagram" }),
      h("p", { class: "muted small" }, "Each dot is a real request. It moves only when the router emits the corresponding event. Edge thickness is each replica's share of all routing decisions since the router started; the number inside a replica box is how many requests are dispatched to it right now.")),
    h("section", { class: "card" }, h("h2", {}, "Timeline of recent requests"), h("div", { id: "rt-timeline", class: "timeline" })),
    h("section", { class: "card" }, h("h2", {}, "Routing decisions"), h("div", { id: "rt-decisions", class: "tablewrap" })),
    h("section", { class: "card" }, h("h2", {}, "Replica health changes"), h("div", { id: "rt-health" })));

  const el = (id) => root.querySelector("#" + id);

  // ---------------------------------------------------------------------------------------------
  // diagram
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Request flow from client through the router and its routing policy to the replicas" });
  const gEdges = svgEl("g"), gNodes = svgEl("g"), gDots = svgEl("g"), gReplicas = svgEl("g");
  svg.append(gEdges, gNodes, gReplicas, gDots);
  el("rt-diagram").append(svg);

  const nodeBox = (b, title, sub, cls = "") => {
    const g = svgEl("g", { class: `node ${cls}` });
    g.append(svgEl("rect", { x: b.x, y: b.y, width: b.w, height: b.h, rx: 12 }), svgEl("text", { x: b.x + 14, y: b.y + 24, class: "n-title" }, title));
    const subs = [];
    (Array.isArray(sub) ? sub : [sub]).forEach((t, i) => { const tx = svgEl("text", { x: b.x + 14, y: b.y + 44 + i * 17, class: "n-sub" }, t); subs.push(tx); g.append(tx); });
    return { g, subs, rect: g.firstChild };
  };

  const clientNode = nodeBox(CLIENT, "Client / UI", ["this browser", "or any HTTP client"], "n-client");
  const routerFrame = svgEl("rect", { x: ROUTER.x, y: ROUTER.y, width: ROUTER.w, height: ROUTER.h, rx: 16, class: "router-frame" });
  const routerTitle = svgEl("text", { x: ROUTER.x + 16, y: ROUTER.y + 26, class: "n-title" }, "Router  ·  swiftserve/app.py");
  const admNode = nodeBox(ADM, "1 · Admission control", ["—"], "n-adm");
  const polNode = nodeBox(POL, "2 · Routing policy", ["—", "—", "—"], "n-pol");
  const dspNode = nodeBox(DSP, "3 · Dispatch  (proxy.py)", ["retries the same replica on connect errors"], "n-dsp");
  gNodes.append(clientNode.g, routerFrame, routerTitle, admNode.g, polNode.g, dspNode.g);

  const edgeClientAdm = svgEl("path", { id: "e-client", d: `M ${CLIENT.x + CLIENT.w} ${CLIENT.y + CLIENT.h / 2} C 215 ${CLIENT.y + CLIENT.h / 2}, 235 ${ADM.y + ADM.h / 2}, ${ADM.x} ${ADM.y + ADM.h / 2}`, class: "edge" });
  const edgeAdmPol = svgEl("path", { id: "e-ap", d: `M ${ADM.x + ADM.w / 2} ${ADM.y + ADM.h} L ${POL.x + POL.w / 2} ${POL.y}`, class: "edge" });
  const edgePolDsp = svgEl("path", { id: "e-pd", d: `M ${POL.x + POL.w / 2} ${POL.y + POL.h} L ${DSP.x + DSP.w / 2} ${DSP.y}`, class: "edge" });
  gEdges.append(edgeClientAdm, edgeAdmPol, edgePolDsp);

  let edgesToReplica = [];

  function layoutReplicas(n) {
    if (n === replicaCount) return;
    replicaCount = n;
    gReplicas.textContent = ""; for (const e of edgesToReplica) e.remove(); edgesToReplica = []; replicaBoxes = [];
    const gap = 14, top = 24, usable = H - 48;
    const bh = Math.min(96, (usable - gap * (n - 1)) / Math.max(n, 1));
    for (let i = 0; i < n; i++) {
      const y = top + i * (bh + gap);
      const box = { x: REP_X, y, w: REP_W, h: bh };
      const g = svgEl("g", { class: "node n-replica" });
      const rect = svgEl("rect", { x: box.x, y: box.y, width: box.w, height: box.h, rx: 12, stroke: `var(--r${i % 8})` });
      const title = svgEl("text", { x: box.x + 12, y: box.y + 22, class: "n-title" }, `R${i}`);
      const sub1 = svgEl("text", { x: box.x + 12, y: box.y + 40, class: "n-sub" }, "");
      const sub2 = svgEl("text", { x: box.x + 12, y: box.y + 56, class: "n-sub" }, "");
      const sub3 = svgEl("text", { x: box.x + 12, y: box.y + 72, class: "n-sub" }, "");
      const dot = svgEl("circle", { cx: box.x + box.w - 18, cy: box.y + 18, r: 6, class: "health unknown" });
      const count = svgEl("text", { x: box.x + box.w - 18, y: box.y + box.h - 12, class: "n-count", "text-anchor": "end" }, "0");
      const dots = svgEl("g");
      g.append(rect, title, sub1, sub2, sub3, dot, count, dots);
      gReplicas.append(g);
      const d = `M ${DSP.x + DSP.w} ${DSP.y + DSP.h / 2} C 660 ${DSP.y + DSP.h / 2}, 700 ${y + bh / 2}, ${box.x} ${y + bh / 2}`;
      const edge = svgEl("path", { d, class: "edge edge-r", stroke: `var(--r${i % 8})`, id: `e-r${i}` });
      gEdges.append(edge); edgesToReplica.push(edge);
      replicaBoxes.push({ rid: i, ...box, g, rect, title, sub1, sub2, sub3, dot, count, dots, edge });
    }
  }

  const pathFor = {
    client: () => edgeClientAdm, ap: () => edgeAdmPol, pd: () => edgePolDsp,
    replica: (rid) => edgesToReplica[rid],
  };

  // A particle walks a list of {path, reverse} segments.
  function spawn(segments, color, { fail = false, ms = 380 } = {}) {
    if (!document.body.contains(svg) || particles.length >= maxParticles) return;
    const segs = segments.filter((s) => s.path);
    if (!segs.length) return;
    const c = svgEl("circle", { r: 6, class: "particle", fill: color });
    gDots.append(c);
    particles.push({ c, segs, i: 0, t0: performance.now(), ms, fail });
    if (particles.length === 1) requestAnimationFrame(tick);
  }

  function tick(now) {
    for (let k = particles.length - 1; k >= 0; k--) {
      const p = particles[k];
      const seg = p.segs[p.i];
      const len = seg.path.getTotalLength();
      let f = Math.min(1, (now - p.t0) / p.ms);
      const at = seg.reverse ? len * (1 - f) : len * f;
      const pt = seg.path.getPointAtLength(at);
      p.c.setAttribute("cx", pt.x); p.c.setAttribute("cy", pt.y);
      if (f >= 1) {
        if (p.i + 1 < p.segs.length) { p.i++; p.t0 = now; }
        else { if (p.fail) { p.c.setAttribute("r", 9); p.c.setAttribute("opacity", "0.5"); setTimeout(() => p.c.remove(), 450); } else p.c.remove(); particles.splice(k, 1); }
      }
    }
    if (particles.length) requestAnimationFrame(tick);
  }

  function pulse(node) { node.rect.classList.remove("pulse"); void node.rect.getBBox(); node.rect.classList.add("pulse"); }

  store.onEvent((ev) => {
    if (root.hidden || !ev.request_id) return;
    const color = `hsl(${hue(ev.request_id)} 80% 55%)`;
    switch (ev.type) {
      case "request.received": spawn([{ path: pathFor.client() }], color); pulse(admNode); break;
      case "routing.decision":
        lastDecision = ev; pulse(polNode);
        spawn([{ path: pathFor.ap() }], color, { ms: 220 });
        break;
      case "dispatch": spawn([{ path: pathFor.pd() }, { path: pathFor.replica(ev.replica) }], color, { ms: 260 }); break;
      case "dispatch.retry": { const b = replicaBoxes[ev.replica]; if (b) { b.rect.classList.add("retry"); setTimeout(() => b.rect.classList.remove("retry"), 600); } break; }
      case "request.completed":
        spawn([{ path: pathFor.replica(ev.served_replica ?? ev.replica), reverse: true }, { path: pathFor.client(), reverse: true }], "var(--ok)", { ms: 300 });
        break;
      case "request.failed": spawn([{ path: pathFor.replica(ev.replica ?? 0), reverse: false }].filter((s) => ev.replica !== null && ev.replica !== undefined), "var(--bad)", { fail: true, ms: 260 }); break;
      case "request.client_disconnected": spawn([{ path: pathFor.replica(ev.replica), reverse: true }], "var(--muted)", { fail: true, ms: 260 }); break;
      case "request.rejected": spawn([{ path: pathFor.client() }], "var(--bad)", { fail: true, ms: 260 }); break;
      default: break;
    }
  });

  // ---------------------------------------------------------------------------------------------
  // data-driven parts (re-rendered on snapshot / request changes)
  function updateDiagram() {
    const snap = store.snapshot;
    if (!snap) return;
    layoutReplicas(snap.replicas.length);
    const active = store.activeByReplica();
    const totalSel = snap.replicas.reduce((a, r) => a + (r.stats.selected || 0), 0) || 1;

    admNode.subs[0].textContent = snap.router.admission ? `in flight ${snap.router.admission.in_flight ?? "?"} / max ${snap.router.admission.max_in_flight ?? "?"}` : "—";
    const ps = snap.policy_state;
    polNode.g.querySelector(".n-title").textContent = `2 · Routing policy: ${snap.router.policy}`;
    const line1 = ps.prefix_index ? `index: ${ps.prefix_index.distinct_blocks} distinct blocks · tokenizer ${ps.tokenizer || "?"}`
      : ps.prefix_trie ? `message trie: ${ps.prefix_trie.node_count} nodes · session TTL ${ps.session_affinity_ttl_s}s` : "stateless apart from live replica load";
    polNode.subs[0].textContent = line1.slice(0, 46);
    polNode.subs[1].textContent = lastDecision ? `last: ${lastDecision.path} → R${lastDecision.selected_replica}` : "last decision: none yet";
    polNode.subs[2].textContent = `SLA default ${snap.router.default_sla_ms} ms`;

    for (const b of replicaBoxes) {
      const r = snap.replicas[b.rid];
      const mapping = r.gpu_mapping.known ? r.gpu_mapping.label : "GPU mapping unavailable";
      b.title.textContent = `R${r.replica_id} · ${mapping}`.slice(0, 30);
      let host = r.base_url; try { host = new URL(r.base_url).host; } catch { /* keep raw */ }
      b.sub1.textContent = host.slice(0, 32);
      b.sub2.textContent = `circuit ${r.circuit.state} · queue ${r.queue_depth}`;
      b.sub3.textContent = `selected ${r.stats.selected} · served ${r.stats.completed_ok}`;
      b.dot.setAttribute("class", "health " + (r.healthy === null ? "unknown" : r.healthy && r.circuit.state !== "open" ? "up" : "down"));
      b.count.textContent = String(active[b.rid] || 0);
      b.rect.classList.toggle("busy", (active[b.rid] || 0) > 0);
      b.edge.setAttribute("stroke-width", String(2 + 10 * ((r.stats.selected || 0) / totalSel)));
      b.dots.textContent = "";
      const reqs = store.all().filter((q) => store.activeReplica(q) === b.rid).slice(-14);
      reqs.forEach((q, i) => b.dots.append(svgEl("circle", { cx: b.x + 14 + (i % 14) * 13, cy: b.y + b.h - 12, r: 4.5, fill: `hsl(${hue(q.id)} 80% 55%)` })));
    }
    el("rt-legend").textContent = snap.mode.simulated ? "SIMULATED replicas" : "live replicas";
  }

  function updateTimeline() {
    const reqs = store.all().filter((r) => r.server.t_received).slice(-26);
    const box = el("rt-timeline");
    if (!reqs.length) { box.replaceChildren(h("p", { class: "muted" }, "Requests appear here as the router handles them.")); return; }
    const start = Math.min(...reqs.map((r) => r.server.t_received * 1000));
    const end = Math.max(...reqs.map((r) => r.server.t_received * 1000 + (r.server.at.finished ?? r.server.at.first_token ?? r.server.at.dispatch ?? 0) + 30));
    const span = Math.max(end - start, 200);
    const pctx = (t) => Math.max(0, Math.min(100, ((t - start) / span) * 100));
    const rows = reqs.slice().reverse().map((r) => {
      const s = r.server, t0 = s.t_received * 1000;
      const seg = (a, b, cls, title) => (a === null || a === undefined || b === null || b === undefined) ? null
        : h("i", { class: `seg ${cls}`, title, style: { left: pctx(t0 + a) + "%", width: Math.max(0.4, pctx(t0 + b) - pctx(t0 + a)) + "%" } });
      const fin = s.at.finished ?? (s.state === "streaming" || s.state === "dispatched" ? (Date.now() - t0) : null);
      const bar = h("div", { class: "track" },
        seg(0, s.at.dispatch ?? s.at.routed ?? 0, "seg-route", "received → dispatched"),
        seg(s.at.dispatch, s.at.first_token ?? fin, "seg-wait", "dispatched → first token (prefill / queueing)"),
        seg(s.at.first_token, fin, s.state === "failed" ? "seg-fail" : "seg-decode", "first token → finished"),
        s.state === "failed" || s.state === "rejected" ? h("i", { class: "seg seg-fail", style: { left: pctx(t0 + (fin ?? 0)) + "%", width: "0.8%" }, title: s.error || "failed" }) : null,
        ...s.retries.map(() => h("i", { class: "seg seg-retry", style: { left: pctx(t0 + (s.at.dispatch ?? 0)) + "%", width: "0.6%" }, title: "connect retry" })));
      return h("div", { class: "trow" },
        h("div", { class: "tlabel" }, h("code", {}, shortId(r.id)), " ", s.served !== null ? replicaChip(s.served, replicaLabel(s.served)) : s.selected !== null ? replicaChip(s.selected, replicaLabel(s.selected)) : null, h("small", {}, ` ${s.state}`)), bar);
    });
    box.replaceChildren(h("div", { class: "tlegend" }, h("i", { class: "seg seg-route" }), " routing  ", h("i", { class: "seg seg-wait" }), " waiting for first token  ", h("i", { class: "seg seg-decode" }), " streaming  ", h("i", { class: "seg seg-fail" }), " failed"), ...rows);
  }

  function updateDecisions() {
    const reqs = store.all().filter((r) => r.server.path).slice(-18).reverse();
    el("rt-decisions").replaceChildren(reqs.length
      ? h("table", { class: "grid" }, h("thead", {}, h("tr", {}, ...["Time", "Request", "Path", "Selected", "Dispatched", "Served", "Why (from the policy's own record)"].map((t) => h("th", {}, t)))),
          h("tbody", {}, ...reqs.map((r) => {
            const s = r.server;
            return h("tr", {}, h("td", {}, s.t_received ? new Date(s.t_received * 1000).toLocaleTimeString() : "–"), h("td", {}, h("code", {}, shortId(r.id))),
              h("td", {}, h("span", { class: "badge badge-info" }, s.path)), h("td", {}, replicaChip(s.selected, replicaLabel(s.selected))),
              h("td", {}, s.dispatches.length ? s.dispatches.map((d) => replicaChip(d.replica, replicaLabel(d.replica))) : "–"),
              h("td", {}, s.served !== null ? replicaChip(s.served, replicaLabel(s.served)) : (s.state === "completed" ? "n/a" : "–")),
              h("td", { class: "why" }, describeDecision(s)));
          })))
      : h("p", { class: "muted" }, "No routing decisions seen yet."));
  }

  function updateHealth() {
    const evs = store.replicaEvents;
    el("rt-health").replaceChildren(evs.length
      ? h("ul", { class: "plain" }, ...evs.slice(0, 12).map((e) => h("li", {}, new Date(e.ts * 1000).toLocaleTimeString(), " · ", replicaChip(e.replica, replicaLabel(e.replica)),
          e.circuit.from !== e.circuit.to ? ` circuit ${e.circuit.from} → ${e.circuit.to}` : "", e.healthy.from !== e.healthy.to ? ` health ${e.healthy.from ? "up" : "down"} → ${e.healthy.to ? "up" : "down"}` : "")))
      : h("p", { class: "muted" }, "No replica state changes since this page loaded."));
  }

  function render() { updateDiagram(); updateTimeline(); updateDecisions(); updateHealth(); }
  let rafQueued = false;
  store.on(() => { if (root.hidden || rafQueued) return; rafQueued = true; setTimeout(() => { rafQueued = false; render(); }, 120); });
  return { render };
}
