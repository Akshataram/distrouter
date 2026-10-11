// Small DOM and formatting helpers shared by every view.
// Rule for the whole UI: server-supplied strings are only ever inserted with
// textContent / setAttribute, never innerHTML, so nothing a replica or a
// prompt contains can inject markup.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);

export function fmtMs(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "n/a";
  if (v >= 10000) return (v / 1000).toFixed(1) + " s";
  return v.toFixed(digits) + " ms";
}

export function fmtNum(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "n/a";
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function fmtPct(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "n/a";
  return (v * 100).toFixed(digits) + "%";
}

export function shortId(id) {
  if (!id) return "";
  return id.length <= 14 ? id : id.slice(0, 8) + "…" + id.slice(-4);
}

export function percentile(values, p) {
  const xs = values.filter((v) => v !== null && v !== undefined && !Number.isNaN(v)).sort((a, b) => a - b);
  if (!xs.length) return null;
  if (xs.length === 1) return xs[0];
  const k = ((xs.length - 1) * p) / 100;
  const lo = Math.floor(k);
  const hi = Math.min(lo + 1, xs.length - 1);
  return xs[lo] + (xs[hi] - xs[lo]) * (k - lo);
}

export function mean(values) {
  const xs = values.filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
}

// Stable colour per replica id (CSS variables defined in styles.css).
export function replicaColor(rid) {
  if (rid === null || rid === undefined || rid === "") return "var(--muted)";
  const n = Number(rid);
  return `var(--r${Number.isNaN(n) ? 0 : n % 8})`;
}

export function replicaChip(rid, label) {
  if (rid === null || rid === undefined) return h("span", { class: "chip chip-none" }, "none");
  return h("span", { class: "chip", style: { background: replicaColor(rid) } }, label || `R${rid}`);
}

export function sleep(ms, signal) {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms);
    if (signal) signal.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
  });
}

export function uid(prefix = "req") {
  const rnd = (globalThis.crypto && crypto.randomUUID) ? crypto.randomUUID().slice(0, 8) : Math.random().toString(16).slice(2, 10);
  return `${prefix}-${Date.now().toString(36)}-${rnd}`;
}

// One-line, human-readable account of a routing decision, built ONLY from the
// fields the policy actually reported in `explain`. If the policy reported
// nothing, say so; never guess a reason.
export function describeDecision(server) {
  const ex = server && server.explain;
  const path = server && server.path;
  if (!ex) return path ? `${path} (the policy reported no further detail)` : "no decision recorded";
  const r = server.selected;
  switch (path) {
    case "prefix_match": {
      const b = ex.best || {};
      const tokens = (b.matched_blocks || 0) * (ex.block_size || 0);
      return `R${b.replica_id} holds ${b.matched_blocks}/${ex.total_blocks} prompt blocks (${tokens} tokens); estimated ${b.estimated_latency_ms} ms ≤ SLA ${ex.sla_ms} ms`;
    }
    case "sla_veto": {
      const b = ex.best || {};
      return `best match R${b.replica_id} (${b.matched_blocks}/${ex.total_blocks} blocks) skipped: estimated ${b.estimated_latency_ms} ms > SLA ${ex.sla_ms} ms → least loaded (R${r})`;
    }
    case "no_eligible_match":
      return `no replica held an eligible prefix (matched blocks: ${JSON.stringify(ex.matched_blocks)}; need ≥${ex.thresholds ? ex.thresholds.min_ratio * 100 : "?"}% or ≥${ex.thresholds ? ex.thresholds.min_tokens : "?"} tokens) → least loaded (R${r})`;
    case "session_affinity":
      return `session last used R${ex.session_owner.replica_id}; estimated ${ex.session_owner.estimated_latency_ms} ms ≤ SLA ${ex.sla_ms} ms`;
    case "prefix_trie":
      return `message-prefix trie matched ${ex.trie.matched_depth} message(s) on R${ex.trie.matched_replica_id}; estimated ${ex.trie.estimated_latency_ms} ms ≤ SLA ${ex.sla_ms} ms`;
    case "least_loaded": {
      const t = ex.trie || {};
      return `no usable session/trie match (trie depth ${t.matched_depth ?? 0}, needs ≥${t.min_depth_required ?? "?"}) → least loaded (R${r})`;
    }
    case "round_robin":
      return `rotation slot ${ex.slot} of ${ex.rotation_size}`;
    case "least_connections":
      return `queue depths ${JSON.stringify(ex.queue_depths)} → shortest queue`;
    case "half_open_probe":
      return ex.note || "recovering replica probed";
    default:
      return `${path}: ${JSON.stringify(ex).slice(0, 160)}`;
  }
}

// Replace an element's children, skipping null/undefined/false. The DOM's own
// replaceChildren/append would render a bare `null` as the text "null".
export function fill(el, ...kids) {
  el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
}
