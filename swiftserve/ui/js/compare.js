// Compare tab: send the SAME multi-tenant workload through each routing policy in turn and
// show the policies side by side. Every number is measured by the router for requests this
// page sent; nothing is simulated or estimated here.

import { chat, postJSON } from "./api.js";
import { POLICY_ORDER, buildWorkload, summarize, verdict } from "./compare_logic.js";
import { fill, fmtMs, fmtPct, h, sleep, uid } from "./util.js";

const LABEL = { round_robin: "round_robin", least_connections: "least_connections", swiftserve: "swiftserve", prefix_aware: "prefix_aware" };

export function initCompare(root, ctx) {
  const { store } = ctx;
  const run = { running: false, stop: null, results: {}, rows: {}, order: [], done: 0, total: 0, current: null, config: null };

  root.append(h("div", { class: "split" },
    h("section", { class: "card" },
      h("h2", {}, "Compare policies"),
      h("div", { id: "cp-warn" }),
      h("label", {}, "Policies, run in this order"),
      h("div", { id: "cp-policies", class: "checks" }, ...POLICY_ORDER.map((p) =>
        h("label", { class: "check" }, h("input", { type: "checkbox", value: p, checked: true }), " " + p))),
      h("div", { class: "grid3" },
        h("div", {}, h("label", { for: "cp-n" }, "Requests per policy"), h("input", { id: "cp-n", type: "number", min: 4, max: 500, value: 60 })),
        h("div", {}, h("label", { for: "cp-c" }, "Concurrency"), h("input", { id: "cp-c", type: "number", min: 1, max: 64, value: 8 })),
        h("div", {}, h("label", { for: "cp-t" }, "Tenants"), h("input", { id: "cp-t", type: "number", min: 1, max: 24, value: 6 }))),
      h("div", { class: "grid3" },
        h("div", {}, h("label", { for: "cp-tok" }, "System prompt tokens"), h("input", { id: "cp-tok", type: "number", min: 200, max: 3500, value: 1500 })),
        h("div", {}, h("label", { for: "cp-max" }, "max_tokens"), h("input", { id: "cp-max", type: "number", min: 1, value: 16 })),
        h("div", {}, h("label", { for: "cp-seed" }, "Seed"), h("input", { id: "cp-seed", type: "number", value: 1 }))),
      h("details", { class: "more" }, h("summary", {}, "Advanced"),
        h("label", { class: "check" }, h("input", { id: "cp-reverse", type: "checkbox" }), " Reverse the policy order"),
        h("label", { for: "cp-model" }, "Model name"), h("input", { id: "cp-model", type: "text" })),
      h("div", { class: "row-actions" },
        h("button", { id: "cp-start", class: "primary" }, "Run comparison"),
        h("button", { id: "cp-stop", class: "ghost", disabled: true }, "Stop")),
      h("div", { id: "cp-status", class: "status", role: "status" })),
    h("div", { class: "stack" },
      h("section", { class: "card" }, h("h2", {}, "Result"), h("div", { id: "cp-result" })),
      h("section", { class: "card" }, h("h2", {}, "Requests per replica"), h("div", { id: "cp-dist" })))));

  const el = (id) => root.querySelector("#" + id);
  const setStatus = (m, bad = false) => { const s = el("cp-status"); s.textContent = m || ""; s.className = "status" + (bad ? " bad" : ""); };

  function syncMode() {
    const snap = store.snapshot;
    const model = el("cp-model");
    if (snap && !model.value && snap.router.model) model.value = snap.router.model;
    const enabled = snap && snap.mode.policy_switch && snap.mode.policy_switch.enabled;
    fill(el("cp-warn"), snap && !enabled
      ? h("div", { class: "banner banner-warn" }, "This router does not allow switching policy while running. Start it with scripts/dev.py (or set SWIFTSERVE_ALLOW_POLICY_SWITCH=1).") : null);
    el("cp-start").disabled = run.running || !enabled;
  }

  const selected = () => [...root.querySelectorAll("#cp-policies input:checked")].map((i) => i.value);
  const requestsOf = (key) => store.all().filter((r) => r.runId === key);

  async function switchPolicy(name, stop) {
    for (let attempt = 0; attempt < 40 && !stop.aborted; attempt++) {
      try { await postJSON("/api/policy", { name }); return; }
      catch (e) { if (e.status !== 409) throw e; await sleep(500, stop.signal).catch(() => {}); }  // requests still finishing
    }
    throw new Error("the router still had requests in flight; try again when it is idle");
  }

  async function runPolicy(policy, cfg, runId, stop) {
    await switchPolicy(policy, stop);
    if (stop.aborted) return;
    const key = `${runId}|${policy}`;
    const work = buildWorkload({ n: cfg.n, tenants: cfg.tenants, tokens: cfg.tokens, namespace: `${runId}-${policy}`, seed: cfg.seed });
    const inflight = new Set();
    let next = 0;
    run.current = { policy, launched: 0, settled: 0, total: work.length };
    const launch = async (item) => {
      const id = uid("cmp");
      store.registerClient(id, { source: "compare", label: `${policy} #${item.index + 1}`, runId: key, prompt: item.user });
      run.current.launched++;
      const res = await chat({
        requestId: id, model: cfg.model, signal: stop.signal, sessionId: `${key}-${item.index}`,
        messages: [{ role: "system", content: item.system }, { role: "user", content: item.user }], params: { max_tokens: cfg.maxTokens },
        onHeaders: (o) => store.patchClient(id, { status: "streaming", httpStatus: o.status, replicaHeader: o.replicaHeader }),
      });
      run.current.settled++;
      const failed = !!res.error || (res.status !== null && res.status >= 400);
      store.patchClient(id, { status: res.aborted ? "aborted" : failed ? "error" : "done", doneAt: res.tDone, firstTokenAt: res.tFirst, text: res.text, usage: res.usage,
                              error: res.error, aborted: res.aborted, httpStatus: res.status, replicaHeader: res.replicaHeader });
      render();
    };
    while (next < work.length && !stop.aborted) {
      while (inflight.size >= cfg.conc && !stop.aborted) await Promise.race([...inflight]);
      if (stop.aborted) break;
      const p = launch(work[next++]); inflight.add(p); p.finally(() => inflight.delete(p));
    }
    await Promise.all([...inflight]);
    if (stop.aborted) return;
    await sleep(900);                                   // let the router's final events arrive
    const reqs = requestsOf(key);
    run.results[policy] = summarize(reqs);
    // one row per request, so a slow tail can be traced to the replica and routing path that caused it
    run.rows[policy] = reqs.map((r) => ({ request: r.label, state: r.server.state, selected: r.server.selected, served: r.server.served, path: r.server.path,
      ttft_ms: r.server.ttft_ms, total_ms: r.server.total_ms, prompt_tokens: r.server.prompt_tokens, cached_tokens: r.server.cached_tokens, predicted_cached_tokens: r.server.predicted }));
    run.done++;
    run.current = null;
    render();
  }

  async function start() {
    if (run.running) return;
    const cfg = { n: Number(el("cp-n").value) || 60, conc: Number(el("cp-c").value) || 8, tenants: Number(el("cp-t").value) || 6, tokens: Number(el("cp-tok").value) || 1500,
                  maxTokens: Number(el("cp-max").value) || 16, seed: Number(el("cp-seed").value) || 1, model: el("cp-model").value.trim() || (store.snapshot && store.snapshot.router.model) || "" };
    let order = selected();
    if (el("cp-reverse").checked) order = order.slice().reverse();
    if (order.length < 2) { setStatus("Select at least two policies.", true); return; }
    const original = store.snapshot && store.snapshot.router.policy;
    const stop = { aborted: false, signal: null };
    const ctl = new AbortController(); stop.signal = ctl.signal;
    Object.assign(run, { running: true, stop: () => { stop.aborted = true; ctl.abort(); }, results: {}, rows: {}, order, done: 0, total: order.length, config: cfg, current: null });
    el("cp-stop").disabled = false; syncMode();
    const runId = uid("c").slice(0, 10);
    try {
      for (const policy of order) {
        if (stop.aborted) break;
        setStatus(`Running ${policy} (${order.indexOf(policy) + 1} of ${order.length})…`);
        await runPolicy(policy, cfg, runId, stop);
      }
      setStatus(stop.aborted ? "Stopped. Policies that finished are shown; the one that was running is discarded." : "Finished.");
    } catch (e) {
      setStatus(`Comparison failed: ${e.message || e}`, true);
    } finally {
      if (original) { try { await switchPolicy(original, { aborted: false, signal: null }); } catch { /* best effort */ } }
      run.running = false; run.current = null;
      el("cp-stop").disabled = true; syncMode(); render();
    }
  }

  el("cp-start").addEventListener("click", start);
  el("cp-stop").addEventListener("click", () => run.stop && run.stop());

  // ---- rendering ---------------------------------------------------------------------------
  function bar(frac, color) { return h("div", { class: "cmpbar" }, h("i", { style: { width: Math.max(0, Math.min(1, frac)) * 100 + "%", background: color } })); }

  function render() {
    syncMode();
    const names = run.order.filter((p) => run.results[p]);
    const progress = run.running && run.current
      ? h("p", { class: "muted" }, `${run.current.policy}: ${run.current.settled} of ${run.current.total} requests finished`) : null;
    if (!names.length) {
      fill(el("cp-result"), progress, run.running ? null : h("p", { class: "muted" }, "Run the comparison to fill this table."));
      fill(el("cp-dist"), h("p", { class: "muted" }, "None yet."));
      return;
    }
    const R = run.results;
    const best = (k, dir) => { const xs = names.filter((n) => R[n][k] !== null && R[n][k] !== undefined); if (xs.length < 2) return null; return xs.slice().sort((a, b) => dir * (R[a][k] - R[b][k]))[0]; };
    const bests = { ttft_p50: best("ttft_p50", 1), ttft_p95: best("ttft_p95", 1), cache_reuse: best("cache_reuse", -1), spread: best("spread", 1) };
    const rows = [
      ["TTFT median", "ttft_p50", (v) => fmtMs(v)], ["TTFT 95th percentile", "ttft_p95", (v) => fmtMs(v)], ["Prompt reused from cache", "cache_reuse", (v) => fmtPct(v)],
      ["Load balance (busiest ÷ average)", "spread", (v) => (v === null ? "n/a" : v.toFixed(2))], ["ITL mean", "itl_mean", (v) => fmtMs(v, 1)],
    ];
    const v = verdict(R);
    const maxTtft = Math.max(...names.map((n) => R[n].ttft_p50 || 0), 1);
    fill(el("cp-result"), progress,
      h("table", { class: "grid" }, h("thead", {}, h("tr", {}, h("th", {}, ""), ...names.map((n) => h("th", { class: "num" }, LABEL[n])))),
        h("tbody", {},
          ...rows.map(([label, key, f]) => h("tr", {}, h("th", { scope: "row" }, label),
            ...names.map((n) => h("td", { class: "num" + (bests[key] === n ? " best" : "") }, f(R[n][key]))))),
          h("tr", {}, h("th", { scope: "row" }, "Completed / failed"), ...names.map((n) => h("td", { class: "num" }, `${R[n].completed} / ${R[n].failed}`))))),
      h("div", { class: "cmpbars" }, h("div", { class: "muted small" }, "Median time to first token"),
        ...names.map((n) => h("div", { class: "cmprow" }, h("span", { class: "cmplabel" }, LABEL[n]), bar((R[n].ttft_p50 || 0) / maxTtft, n === "prefix_aware" ? "var(--accent)" : "var(--muted)"), h("span", { class: "cmpval" }, fmtMs(R[n].ttft_p50))))),
      v ? h("p", { class: "verdict" },
        `${LABEL[v.bestTtft]} had the lowest median TTFT, ${v.ttftRatio.toFixed(1)}× lower than ${LABEL[v.worstTtft]}.`
        + (v.bestCache ? ` Highest cache reuse: ${LABEL[v.bestCache]} (${fmtPct(v.bestCacheValue)}).` : "")
        + ` Lowest 95th percentile: ${LABEL[v.p95Best]}.`) : null,
      h("p", { class: "muted small" }, `Same ${run.config.n} requests for every policy: ${run.config.tenants} tenants with different ~${run.config.tokens}-token system prompts, concurrency ${run.config.conc}, seed ${run.config.seed}. `
        + "Policies ran one after another; each used its own prompt namespace so none reused another's cache."),
      h("div", { class: "row-actions" }, h("button", { id: "cp-export", class: "ghost", onclick: exportJSON }, "Export JSON")));

    const allIds = [...new Set(names.flatMap((n) => Object.keys(R[n].per_replica)))].map(Number).sort((a, b) => a - b);
    const maxCount = Math.max(1, ...names.flatMap((n) => Object.values(R[n].per_replica)));
    fill(el("cp-dist"), h("table", { class: "grid small" }, h("thead", {}, h("tr", {}, h("th", {}, ""), ...allIds.map((id) => h("th", {}, `R${id}`)))),
      h("tbody", {}, ...names.map((n) => h("tr", {}, h("th", { scope: "row" }, LABEL[n]), ...allIds.map((id) => h("td", { class: "num" }, R[n].per_replica[id] || 0)))))));
  }

  function exportJSON() {
    const blob = new Blob([JSON.stringify({ config: run.config, order: run.order, results: run.results, requests: run.rows }, null, 2)], { type: "application/json" });
    const a = h("a", { href: URL.createObjectURL(blob), download: "policy-comparison.json" });
    document.body.append(a); a.click(); a.remove();
  }

  store.on(() => { if (!root.hidden) render(); });
  render();
  return { render, state: run };
}
