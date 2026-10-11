// Replica & router dashboard. Every value comes from GET /api/dashboard (the
// router's own state and router-side aggregates). A value the router cannot
// measure is rendered as "unavailable" with the reason it gave -- the page
// never substitutes a stand-in (no CPU-for-GPU, no invented VRAM, no guessed
// queue depth).

import { fmtMs, fmtNum, fmtPct, h, replicaChip } from "./util.js";

const HISTORY = 60;

function meter(frac, label, cls = "") {
  const pct = frac === null || frac === undefined ? null : Math.max(0, Math.min(100, frac * 100));
  return h("div", { class: `meter ${cls}` },
    h("div", { class: "meter-track" }, pct === null ? null : h("i", { style: { width: pct + "%" } })),
    h("span", { class: "meter-label" }, label));
}

function kv(rows) {
  return h("dl", { class: "kvl" }, ...rows.flatMap(([k, v, title]) => [h("dt", { title: title || "" }, k), h("dd", {}, v)]));
}

function spark(values, color) {
  const w = 240, ht = 46, n = values.length;
  const max = Math.max(1e-9, ...values);
  const pts = values.map((v, i) => `${(i / Math.max(HISTORY - 1, 1)) * w + (w - (Math.max(n - 1, 0) / Math.max(HISTORY - 1, 1)) * w)},${ht - 4 - (v / max) * (ht - 10)}`).join(" ");
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${ht}`); svg.setAttribute("class", "spark"); svg.setAttribute("role", "img");
  const line = document.createElementNS(ns, "polyline");
  line.setAttribute("points", pts); line.setAttribute("fill", "none"); line.setAttribute("stroke", color); line.setAttribute("stroke-width", "2");
  svg.append(line);
  return svg;
}

export function initDashboard(root, ctx) {
  const { store } = ctx;

  root.append(h("div", { id: "db-banner" }), h("div", { id: "db-tiles" }), h("div", { id: "db-replicas", class: "replica-grid" }),
    h("div", { class: "split2" },
      h("section", { class: "card" }, h("h2", {}, "Request distribution"), h("div", { id: "db-dist" })),
      h("section", { class: "card" }, h("h2", {}, "Throughput"), h("div", { id: "db-throughput" }))),
    h("div", { class: "split2" },
      h("section", { class: "card" }, h("h2", {}, "Policy and router state"), h("div", { id: "db-policy" })),
      h("section", { class: "card" }, h("h2", {}, "What is and is not measured"), h("div", { id: "db-avail" }))));
  const el = (id) => root.querySelector("#" + id);

  function replicaCard(r) {
    const s = r.stats, g = r.gpu;
    const mapping = r.gpu_mapping;
    const gpuTitle = mapping.known
      ? [mapping.label, mapping.detected ? `${mapping.detected} (reported by the node)` : null].filter(Boolean).join(" · ")
      : "GPU mapping unavailable";
    const health = r.healthy === null ? ["unknown", "info"] : r.healthy ? ["healthy", "ok"] : ["unreachable", "bad"];
    const circ = { closed: "ok", half_open: "warn", open: "bad" }[r.circuit.state] || "info";
    const gpuBlock = g.available
      ? [meter((g.utilization_pct ?? null) === null ? null : g.utilization_pct / 100, `GPU utilization ${g.utilization_pct === null ? "n/a" : g.utilization_pct + "%"}`, "gpu"),
         meter(g.memory_total_mib ? g.memory_used_mib / g.memory_total_mib : null, `VRAM ${fmtNum(g.memory_used_mib)} / ${fmtNum(g.memory_total_mib)} MiB`, "gpu"),
         g.temperature_c !== null && g.temperature_c !== undefined ? h("div", { class: "muted small" }, `${g.temperature_c} °C · ${g.source}`) : h("div", { class: "muted small" }, g.source)]
      : [h("div", { class: "unavail", title: g.reason }, "GPU utilization / VRAM: unavailable"), h("div", { class: "muted small" }, g.reason)];
    const kv_ = r.scraped.kv_cache_usage;
    let host = r.base_url; try { host = new URL(r.base_url).host; } catch { /* keep */ }
    return h("section", { class: "card replica" , style: { borderTopColor: `var(--r${r.replica_id % 8})` } },
      h("div", { class: "card-head" }, h("h3", {}, replicaChip(r.replica_id, `R${r.replica_id}`), " ", gpuTitle),
        h("span", { class: `badge badge-${health[1]}` }, health[0]), h("span", { class: `badge badge-${circ}` }, `circuit ${r.circuit.state}`)),
      h("div", { class: "muted small mono", title: r.base_url }, host),
      ...gpuBlock,
      meter(kv_, kv_ === null ? "KV-cache usage: not exposed by this replica's /metrics" : `KV-cache usage ${(kv_ * 100).toFixed(0)}% (cache occupancy, not GPU load)`),
      h("h4", {}, "Load (router's view)"),
      kv([["in flight", r.in_flight, "requests the router has sent here that have not finished"], ["queue depth", r.queue_depth, "max(scraped running+waiting, in flight) when metrics are fresh, else in flight"],
          ["vLLM running / waiting", r.scraped.running === null ? "not scraped yet" : `${r.scraped.running} / ${r.scraped.waiting}`], ["estimated latency", fmtMs(r.est_latency_ms), "what a new request would be expected to take, from the router's latency model"],
          ["average latency (EWMA)", fmtMs(r.ewma_latency_ms)], ["batch capacity", r.batch_capacity, "how many concurrent requests the latency model assumes the replica batches"], ["tracked sessions", r.tracked_sessions]]),
      h("h4", {}, "Handled by this replica"),
      kv([["selected by policy", s.selected], ["dispatch attempts", `${s.dispatch_attempts}${s.retries ? ` (${s.retries} retried)` : ""}`], ["succeeded", s.completed_ok],
          ["failed", s.completed_error], ["client disconnected", s.client_disconnected]]),
      h("h4", {}, "Latency (measured at the router)"),
      kv([["TTFT p50 / p95", `${fmtMs(s.ttft_ms.p50)} / ${fmtMs(s.ttft_ms.p95)}`, `${s.ttft_ms.n} streaming request(s)`], ["ITL mean", fmtMs(s.itl_mean_ms.mean, 1), `${s.itl_mean_ms.n} request(s) with ≥2 content events`],
          ["total p50 / p95", `${fmtMs(s.total_ms.p50)} / ${fmtMs(s.total_ms.p95)}`, `${s.total_ms.n} completed request(s)`]]),
      h("h4", {}, "Tokens and cache"),
      kv([["prompt / completion", `${fmtNum(s.prompt_tokens)} / ${fmtNum(s.completion_tokens)}`, `from ${s.requests_with_usage} response(s) that reported usage`],
          ["reused from cache", s.requests_with_usage ? `${fmtNum(s.cached_tokens)} (${fmtPct(s.prompt_tokens ? s.cached_tokens / s.prompt_tokens : null)})` : "n/a"],
          ["engine prefix hit rate", r.true_prefix_hit_rate === null ? "not exposed" : fmtPct(r.true_prefix_hit_rate), "vllm:prefix_cache_hits / queries, cumulative"]]));
  }

  function render() {
    const snap = store.snapshot;
    if (!snap) { el("db-banner").replaceChildren(h("p", { class: "muted" }, "Waiting for the first snapshot from the router…")); return; }

    el("db-banner").replaceChildren(
      h("div", { class: `banner ${snap.mode.simulated ? "banner-sim" : "banner-live"}` }, h("strong", {}, snap.mode.simulated ? "SIMULATED" : "LIVE"), " ", snap.mode.note),
      ...snap.warnings.map((w) => h("div", { class: "banner banner-warn" }, w)));

    const t = snap.totals, inflight = snap.replicas.reduce((a, r) => a + r.in_flight, 0);
    const tile = (label, value, cls = "") => h("div", { class: `stat ${cls}` }, h("div", { class: "stat-v" }, value), h("div", { class: "stat-l" }, label));
    el("db-tiles").replaceChildren(h("div", { class: "stats big" },
      tile("received", fmtNum(t.received)), tile("completed", fmtNum(t.completed), "ok"), tile("failed", fmtNum(t.failed), t.failed ? "bad" : ""),
      tile("client disconnected", fmtNum(t.client_disconnected)), tile("in flight now", fmtNum(inflight)),
      tile("req/s", snap.throughput.requests_per_s.toFixed(2)), tile("tokens/s", snap.throughput.completion_tokens_per_s.toFixed(1)),
      tile("policy", snap.router.policy), tile("uptime", Math.round(snap.uptime_s) + " s")));

    el("db-replicas").replaceChildren(...snap.replicas.map(replicaCard));

    const totalSel = snap.replicas.reduce((a, r) => a + r.stats.selected, 0);
    const max = Math.max(1, ...snap.replicas.map((r) => Math.max(r.stats.selected, r.stats.completed_ok)));
    el("db-dist").replaceChildren(totalSel
      ? h("div", {}, ...snap.replicas.map((r) => h("div", { class: "distrow" },
          h("div", { class: "distlabel" }, replicaChip(r.replica_id, `R${r.replica_id}`)),
          h("div", { class: "bars" },
            h("div", { class: "bar sel", style: { width: (r.stats.selected / max) * 100 + "%", background: `color-mix(in srgb, var(--r${r.replica_id % 8}) 45%, transparent)` } }),
            h("div", { class: "bar srv", style: { width: (r.stats.completed_ok / max) * 100 + "%", background: `var(--r${r.replica_id % 8})` } })),
          h("div", { class: "distnum" }, `${fmtPct(r.stats.selected / totalSel)} of decisions (${r.stats.selected}) · served ${r.stats.completed_ok}`))),
        h("p", { class: "muted small" }, "Cumulative since the router started. Pale = selected by the policy, solid = actually served."))
      : h("p", { class: "muted" }, "No requests routed yet."));

    el("db-throughput").replaceChildren(
      h("div", {}, h("div", { class: "muted small" }, `requests/s (completed, last ${snap.throughput.window_s}s window): ${snap.throughput.requests_per_s.toFixed(2)}`), spark(store.history.rps, "var(--accent)")),
      h("div", {}, h("div", { class: "muted small" }, `completion tokens/s (engine-reported usage): ${snap.throughput.completion_tokens_per_s.toFixed(1)}`), spark(store.history.tps, "var(--ok)")),
      h("p", { class: "muted small" }, snap.throughput.note));

    const ps = snap.policy_state;
    el("db-policy").replaceChildren(kv([
      ["policy", ps.name], ["default SLA", fmtMs(snap.router.default_sla_ms)], ["model", snap.router.model || "n/a"], ["block size", snap.router.block_size ?? "n/a"],
      ["admission", snap.router.admission ? `${snap.router.admission.in_flight} in flight / max ${snap.router.admission.max_in_flight}` : "n/a"],
      ...(ps.prefix_index ? [["tokenizer", ps.tokenizer || "n/a"], ["index: distinct blocks", fmtNum(ps.prefix_index.distinct_blocks)], ["index cap per replica", fmtNum(ps.prefix_index.max_blocks_per_replica)],
        ["index blocks per replica", Object.entries(ps.prefix_index.blocks_per_replica).map(([k, v]) => `R${k}:${v}`).join("  ") || "none yet"],
        ["match thresholds", `≥${ps.thresholds.min_ratio * 100}% of prompt or ≥${ps.thresholds.min_tokens} tokens`], ["hash namespace", ps.namespace || "n/a"]] : []),
      ...(ps.prefix_trie ? [["message trie nodes", `${ps.prefix_trie.node_count} (max ${ps.prefix_trie.max_nodes}, depth ${ps.prefix_trie.max_depth})`]] : []),
      ["session-affinity TTL", ps.session_affinity_ttl_s + " s"],
      ["events buffered", `${snap.router.events.buffered} (head #${snap.router.events.head_seq}, ${snap.router.events.subscribers} stream(s) open)`]]));

    const av = snap.metric_availability;
    const rows = [
      ["GPU utilization / VRAM", av.gpu_utilization.available, av.gpu_utilization.available ? `reported by replicas ${av.gpu_utilization.reporting_replicas.join(", ")} via nvidia-smi on the node` : av.gpu_utilization.reason, av.gpu_utilization.reporting_replicas && av.gpu_utilization.reporting_replicas.length < snap.replicas.length],
      ["KV-cache usage", av.kv_cache_usage.available, av.kv_cache_usage.note + " · " + av.kv_cache_usage.source],
      ["TTFT", av.ttft.available, av.ttft.scope], ["ITL", av.itl.available, av.itl.scope], ["Token counts", av.token_counts.available, av.token_counts.scope],
      ["Engine prefix hit rate", av.engine_prefix_hit_rate.available, av.engine_prefix_hit_rate.source]];
    el("db-avail").replaceChildren(h("table", { class: "grid small" }, h("tbody", {}, ...rows.map(([name, ok, why, partial]) => h("tr", {},
      h("th", { scope: "row" }, name), h("td", {}, h("span", { class: `badge badge-${ok ? (partial ? "warn" : "ok") : "bad"}` }, ok ? (partial ? "partial" : "measured") : "unavailable")), h("td", { class: "note" }, why))))));
  }

  store.on(() => { if (!root.hidden) render(); });
  return { render };
}
