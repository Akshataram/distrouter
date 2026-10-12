// Replica & router dashboard. Every value comes from GET /api/dashboard (the
// router's own state and router-side aggregates). A value the router cannot
// measure is rendered as "unavailable" with the reason it gave -- the page
// never substitutes a stand-in (no CPU-for-GPU, no invented VRAM, no guessed
// queue depth).

import { fill, fmtMs, fmtNum, fmtPct, h, replicaChip } from "./util.js";

function meter(frac, label, cls = "") {
  const pct = frac === null || frac === undefined ? null : Math.max(0, Math.min(100, frac * 100));
  return h("div", { class: `meter ${cls}` },
    h("div", { class: "meter-track" }, pct === null ? null : h("i", { style: { width: pct + "%" } })),
    h("span", { class: "meter-label" }, label));
}

function kv(rows) {
  return h("dl", { class: "kvl" }, ...rows.flatMap(([k, v, title]) => [h("dt", { title: title || "" }, k), h("dd", {}, v)]));
}

export function initDashboard(root, ctx) {
  const { store } = ctx;

  root.append(h("div", { id: "db-banner" }), h("div", { id: "db-tiles" }), h("div", { id: "db-replicas", class: "replica-grid" }),
    h("details", { class: "card more" }, h("summary", {}, "Router settings and what is measured"),
      h("div", { class: "split2" },
        h("section", {}, h("h2", {}, "Policy and router state"), h("div", { id: "db-policy" })),
        h("section", {}, h("h2", {}, "What is and is not measured"), h("div", { id: "db-avail" })))));
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
    let host = r.base_url; try { host = new URL(r.base_url).host; } catch { /* keep */ }
    return h("section", { class: "card replica" , style: { borderTopColor: `var(--r${r.replica_id % 8})` } },
      h("div", { class: "card-head" }, h("h3", {}, replicaChip(r.replica_id, `R${r.replica_id}`), " ", gpuTitle),
        h("span", { class: `badge badge-${health[1]}` }, health[0]), r.circuit.state === "closed" ? null : h("span", { class: `badge badge-${circ}` }, `circuit ${r.circuit.state}`)),
      h("div", { class: "muted small mono", title: r.base_url }, host),
      ...gpuBlock,
      kv([["in flight", r.in_flight, "requests the router has sent here that have not finished"],
          ["served", s.completed_ok, "requests this replica completed (router's count)"],
          ["failed", s.completed_error],
          ["TTFT p50", fmtMs(s.ttft_ms.p50), `${s.ttft_ms.n} streaming request(s), measured at the router`],
          ["ITL mean", fmtMs(s.itl_mean_ms.mean, 1), `${s.itl_mean_ms.n} request(s) with at least 2 content events`],
          ["reused from cache", s.requests_with_usage ? fmtPct(s.prompt_tokens ? s.cached_tokens / s.prompt_tokens : null) : "n/a", "share of prompt tokens the engine reported as cached"]]));
  }

  function render() {
    const snap = store.snapshot;
    if (!snap) { fill(el("db-banner"), h("p", { class: "muted" }, "Waiting for the first snapshot from the router…")); return; }

    fill(el("db-banner"), 
      snap.mode.simulated ? h("div", { class: "banner banner-sim" }, h("strong", {}, "SIMULATED"), " ", snap.mode.note) : null,
      ...snap.warnings.map((w) => h("div", { class: "banner banner-warn" }, w)));

    const t = snap.totals, inflight = snap.replicas.reduce((a, r) => a + r.in_flight, 0);
    const tile = (label, value, cls = "") => h("div", { class: `stat ${cls}` }, h("div", { class: "stat-v" }, value), h("div", { class: "stat-l" }, label));
    fill(el("db-tiles"), h("div", { class: "stats big" },
      tile("completed", fmtNum(t.completed), "ok"), tile("failed", fmtNum(t.failed), t.failed ? "bad" : ""), tile("in flight now", fmtNum(inflight)),
      tile("req/s", snap.throughput.requests_per_s.toFixed(2)), tile("tokens/s", snap.throughput.completion_tokens_per_s.toFixed(1))));

    fill(el("db-replicas"), ...snap.replicas.map(replicaCard));

    const ps = snap.policy_state;
    fill(el("db-policy"), kv([
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
    fill(el("db-avail"), h("table", { class: "grid small" }, h("tbody", {}, ...rows.map(([name, ok, why, partial]) => h("tr", {},
      h("th", { scope: "row" }, name), h("td", {}, h("span", { class: `badge badge-${ok ? (partial ? "warn" : "ok") : "bad"}` }, ok ? (partial ? "partial" : "measured") : "unavailable")), h("td", { class: "note" }, why))))));
  }

  store.on(() => { if (!root.hidden) render(); });
  return { render };
}
