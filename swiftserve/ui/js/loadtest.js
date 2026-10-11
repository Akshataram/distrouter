// Multi-request load test: N requests at a chosen concurrency, every one sent
// through the real router, every one tracked individually.
//
// Concurrency here is real concurrency: up to `concurrency` fetches are in
// flight at once, each streaming independently, so the UI stays responsive and
// the router sees genuinely overlapping requests. Nothing is simulated.
//
// Stop semantics (stated honestly because "cancel" is easy to over-claim):
//   - no further requests are launched;
//   - every in-flight request is aborted IN THE BROWSER (fetch AbortController);
//   - for streaming requests the router notices the disconnect, closes its
//     upstream connection and records a `client_disconnected` event, which this
//     page counts separately as "router saw disconnect";
//   - the router does not cancel non-streaming requests it already forwarded,
//     and whether an engine stops generating after its connection closes is
//     engine behaviour this page cannot verify.

import { chat } from "./api.js";
import { fill, fmtMs, fmtNum, h, mean, percentile, replicaChip, shortId, sleep, uid } from "./util.js";

function sampleSystemPrompt(tokens) {
  const topics = ["card disputes", "wire transfers", "overdraft fees", "mortgage questions", "fraud alerts", "account closure"];
  const lines = ["You are a careful banking support assistant. Follow every rule below exactly."];
  let chars = lines[0].length;
  for (let n = 1; chars < tokens * 4; n++) {
    const l = `Rule ${n}: when a customer asks about ${topics[n % topics.length]}, answer in at most three short sentences and cite the policy number.`;
    lines.push(l); chars += l.length + 1;
  }
  return lines.join("\n");
}

function throttle(fn, ms) {
  let t = null, pending = false;
  return () => { if (t) { pending = true; return; } fn(); t = setTimeout(() => { t = null; if (pending) { pending = false; fn(); } }, ms); };
}

export function initLoadTest(root, ctx) {
  const { store, replicaLabel } = ctx;
  const run = { id: null, cfg: null, total: 0, launched: 0, settled: 0, ok: 0, failed: 0, aborted: 0, inflight: new Map(),
                stopping: false, running: false, startedAt: 0, endedAt: 0, ids: [], stopSignal: null };

  root.append(
    h("div", { class: "split" },
      h("section", { class: "card" },
        h("h2", {}, "Load test"),
        h("p", { class: "muted small" }, "Every request is sent through the router's real routing path. Nothing here is simulated by the page."),
        h("div", { class: "grid3" },
          h("div", {}, h("label", { for: "lt-n" }, "Requests"), h("input", { id: "lt-n", type: "number", min: 1, max: 5000, value: 60 })),
          h("div", {}, h("label", { for: "lt-c" }, "Concurrency"), h("input", { id: "lt-c", type: "number", min: 1, max: 128, value: 8 })),
          h("div", {}, h("label", { for: "lt-d" }, "Delay between launches (ms)"), h("input", { id: "lt-d", type: "number", min: 0, value: 0 }))),
        h("label", { for: "lt-source" }, "Prompt source"),
        h("select", { id: "lt-source" }, h("option", { value: "common" }, "One common prompt for every request"), h("option", { value: "list" }, "List of different prompts (one per line, cycled)")),
        h("label", { for: "lt-prompt", id: "lt-prompt-label" }, "Prompt"),
        h("textarea", { id: "lt-prompt", rows: 4 }, "Summarize the benefits of caching in one sentence."),
        h("label", { for: "lt-system" }, "Shared system prompt (optional)"),
        h("textarea", { id: "lt-system", rows: 3, placeholder: "Requests that share a system prompt share a cache prefix; this is what a cache-aware policy can exploit." }),
        h("div", { class: "row-actions" },
          h("button", { id: "lt-sys-gen", class: "ghost" }, "Insert sample ~1500-token system prompt"),
          h("button", { id: "lt-sys-clear", class: "ghost" }, "Clear")),
        h("div", { class: "grid3" },
          h("div", {}, h("label", { for: "lt-max" }, "max_tokens"), h("input", { id: "lt-max", type: "number", min: 1, value: 24 })),
          h("div", {}, h("label", { for: "lt-temp" }, "temperature"), h("input", { id: "lt-temp", type: "number", step: 0.1, min: 0, max: 2, value: 0.7 })),
          h("div", {}, h("label", { for: "lt-top" }, "top_p"), h("input", { id: "lt-top", type: "number", step: 0.05, min: 0, max: 1, value: 1 }))),
        h("div", { class: "grid3" },
          h("div", {}, h("label", { for: "lt-stream" }, "Streaming"), h("select", { id: "lt-stream" }, h("option", { value: "1" }, "on"), h("option", { value: "0" }, "off"))),
          h("div", {}, h("label", { for: "lt-sla" }, "SLA ms"), h("input", { id: "lt-sla", type: "number", min: 1, placeholder: "router default" })),
          h("div", {}, h("label", { for: "lt-model" }, "Model name"), h("input", { id: "lt-model", type: "text" }))),
        h("div", { class: "row-actions" },
          h("button", { id: "lt-start", class: "primary" }, "Start"),
          h("button", { id: "lt-stop", class: "ghost", disabled: true }, "Stop"),
          h("button", { id: "lt-clear", class: "ghost" }, "Clear results")),
        h("div", { id: "lt-status", class: "status", role: "status" })),
      h("div", { class: "stack" },
        h("section", { class: "card" }, h("h2", {}, "Progress"), h("div", { id: "lt-progress" })),
        h("section", { class: "card" }, h("h2", {}, "Where the requests went"), h("div", { id: "lt-dist" })),
        h("section", { class: "card" }, h("h2", {}, "Summary"), h("div", { id: "lt-summary" })),
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", {}, "Every request"),
            h("label", { class: "inline" }, h("input", { id: "lt-onlyfail", type: "checkbox" }), " only failures"),
            h("button", { id: "lt-export-json", class: "ghost small" }, "Export JSON"), h("button", { id: "lt-export-csv", class: "ghost small" }, "Export CSV")),
          h("div", { id: "lt-table", class: "tablewrap" })))));

  const el = (id) => root.querySelector("#" + id);
  const setStatus = (m, bad = false) => { const s = el("lt-status"); s.textContent = m || ""; s.className = "status" + (bad ? " bad" : ""); };
  const runRequests = () => run.ids.map((id) => store.requests.get(id)).filter(Boolean);

  el("lt-source").addEventListener("change", () => {
    const list = el("lt-source").value === "list";
    el("lt-prompt-label").textContent = list ? "Prompts (one per line)" : "Prompt";
    if (list && el("lt-prompt").value.indexOf("\n") < 0) el("lt-prompt").value = ["What is a KV cache?", "Why does prefill dominate TTFT?", "Explain continuous batching.", "What is a circuit breaker?"].join("\n");
  });
  el("lt-sys-gen").addEventListener("click", () => { el("lt-system").value = sampleSystemPrompt(1500); });
  el("lt-sys-clear").addEventListener("click", () => { el("lt-system").value = ""; });

  function syncModel() {
    const m = el("lt-model");
    if (!m.value && store.snapshot && store.snapshot.router.model) m.value = store.snapshot.router.model;
  }

  // ---- the runner -----------------------------------------------------------------
  async function start() {
    if (run.running) return;
    const n = Math.max(1, Math.min(5000, Number(el("lt-n").value) || 1));
    const conc = Math.max(1, Math.min(128, Number(el("lt-c").value) || 1));
    const delay = Math.max(0, Number(el("lt-d").value) || 0);
    const raw = el("lt-prompt").value;
    const prompts = (el("lt-source").value === "list" ? raw.split("\n").map((l) => l.trim()).filter(Boolean) : [raw.trim()]).filter(Boolean);
    if (!prompts.length) { setStatus("Enter at least one prompt.", true); return; }
    const model = el("lt-model").value.trim() || (store.snapshot && store.snapshot.router.model) || "";
    const system = el("lt-system").value.trim();
    const params = { max_tokens: Number(el("lt-max").value) || 24 };
    if (el("lt-temp").value !== "") params.temperature = Number(el("lt-temp").value);
    if (el("lt-top").value !== "") params.top_p = Number(el("lt-top").value);
    const stream = el("lt-stream").value === "1";
    const slaMs = Number(el("lt-sla").value) || null;

    Object.assign(run, { id: uid("run").slice(0, 12), total: n, launched: 0, settled: 0, ok: 0, failed: 0, aborted: 0, stopping: false, running: true,
                         startedAt: performance.now(), endedAt: 0, ids: [], inflight: new Map(), cfg: { n, conc, delay, stream, params, slaMs, model, system: !!system } });
    run.stopSignal = new AbortController();
    el("lt-start").disabled = true; el("lt-stop").disabled = false;
    setStatus(`Running ${n} requests at concurrency ${conc}…`);
    render();

    const pending = new Set();
    let next = 0;
    while (!run.stopping && next < n) {
      while (run.inflight.size >= conc && !run.stopping) await Promise.race([...pending]);
      if (run.stopping) break;
      const i = next++;
      const p = launch(i, prompts[i % prompts.length], { model, system, params, stream, slaMs });
      pending.add(p); p.finally(() => pending.delete(p));
      if (delay && next < n) await sleep(delay, run.stopSignal.signal);
    }
    await Promise.all([...pending]);
    run.running = false; run.endedAt = performance.now();
    el("lt-start").disabled = false; el("lt-stop").disabled = true;
    setStatus(run.stopping ? `Stopped: ${run.launched} of ${n} launched, in-flight requests aborted in the browser.` : `Finished ${n} requests.`);
    render();
  }

  async function launch(i, prompt, { model, system, params, stream, slaMs }) {
    const id = uid(`lt${run.id.slice(-4)}`);
    const controller = new AbortController();
    run.inflight.set(id, controller);
    run.ids.push(id);
    run.launched++;
    store.registerClient(id, { source: "loadtest", label: `#${i + 1}`, runId: run.id, prompt });
    const messages = (system ? [{ role: "system", content: system }] : []).concat([{ role: "user", content: prompt }]);
    const res = await chat({
      requestId: id, model, messages, params, stream, slaMs, sessionId: `${run.id}-${i}`, signal: controller.signal,
      onHeaders: (o) => store.patchClient(id, { status: "streaming", httpStatus: o.status, replicaHeader: o.replicaHeader, predictedHeader: o.predictedHeader }),
    });
    run.inflight.delete(id);
    run.settled++;
    const failed = !!res.error || (res.status !== null && res.status >= 400);
    if (res.aborted) run.aborted++; else if (failed) run.failed++; else run.ok++;
    store.patchClient(id, { status: res.aborted ? "aborted" : failed ? "error" : "done", doneAt: res.tDone, firstTokenAt: res.tFirst, text: res.text,
                            usage: res.usage, error: res.error, aborted: res.aborted, httpStatus: res.status, replicaHeader: res.replicaHeader });
  }

  function stop() {
    if (!run.running) return;
    run.stopping = true;
    run.stopSignal.abort();
    for (const c of run.inflight.values()) c.abort();
    setStatus("Stopping: no more requests will launch; aborting in-flight requests in the browser…");
  }

  // ---- rendering ------------------------------------------------------------------------
  function stat(label, value, cls = "") { return h("div", { class: `stat ${cls}` }, h("div", { class: "stat-v" }, value), h("div", { class: "stat-l" }, label)); }

  const render = throttle(() => {
    syncModel();
    const reqs = runRequests();
    const elapsedS = ((run.running ? performance.now() : run.endedAt || performance.now()) - run.startedAt) / 1000;

    // progress
    const pct = run.total ? Math.round((run.settled / run.total) * 100) : 0;
    const seenDisconnect = reqs.filter((r) => r.server.state === "client_disconnected").length;
    fill(el("lt-progress"), 
      run.total ? h("div", { class: "progress", role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100 }, h("i", { style: { width: pct + "%" } })) : h("p", { class: "muted" }, "No run yet."),
      run.total ? h("div", { class: "stats" },
        stat("launched", `${run.launched}/${run.total}`), stat("in flight", run.inflight.size), stat("succeeded", run.ok, "ok"), stat("failed", run.failed, run.failed ? "bad" : ""),
        stat("aborted", run.aborted), stat("router saw disconnect", seenDisconnect), stat("elapsed", elapsedS.toFixed(1) + " s"),
        stat("completion rate", elapsedS > 0 ? (run.settled / elapsedS).toFixed(1) + " req/s" : "n/a")) : null);

    // distribution
    const tally = store.tally(reqs);
    const ids = Object.keys(tally).map(Number).sort((a, b) => a - b);
    const max = Math.max(1, ...ids.map((r) => Math.max(tally[r].selected, tally[r].served)));
    fill(el("lt-dist"), ids.length
      ? h("div", {}, ...ids.map((rid) => h("div", { class: "distrow" },
          h("div", { class: "distlabel" }, replicaChip(rid, replicaLabel(rid))),
          h("div", { class: "bars" },
            h("div", { class: "bar sel", style: { width: (tally[rid].selected / max) * 100 + "%", background: `color-mix(in srgb, var(--r${rid % 8}) 45%, transparent)` } }),
            h("div", { class: "bar srv", style: { width: (tally[rid].served / max) * 100 + "%", background: `var(--r${rid % 8})` } })),
          h("div", { class: "distnum" }, `selected ${tally[rid].selected} · served ${tally[rid].served}` + (tally[rid].failed ? ` · failed ${tally[rid].failed}` : "")))),
        h("p", { class: "muted small" }, "Pale bar: replicas the policy selected. Solid bar: replicas that actually returned a response. They differ only when requests fail or are aborted."))
      : h("p", { class: "muted" }, "Routing decisions appear here as the router reports them."));

    // summary (router-measured)
    const done = reqs.filter((r) => r.server.state === "completed");
    const ttft = done.map((r) => r.server.ttft_ms), total = done.map((r) => r.server.total_ms), itl = done.map((r) => r.server.itl_mean_ms);
    const toks = done.reduce((a, r) => a + (r.server.completion_tokens || 0), 0);
    fill(el("lt-summary"), done.length
      ? h("div", { class: "stats" },
          stat("TTFT p50 (router)", fmtMs(percentile(ttft, 50))), stat("TTFT p95", fmtMs(percentile(ttft, 95))),
          stat("total p50 (router)", fmtMs(percentile(total, 50))), stat("total p95", fmtMs(percentile(total, 95))),
          stat("ITL mean", fmtMs(mean(itl), 1)),
          stat("completion tokens", fmtNum(toks)), stat("tokens/s (engine-reported)", elapsedS > 0 ? fmtNum(toks / elapsedS, 1) : "n/a"))
      : h("p", { class: "muted" }, "Summary appears after the first request completes. TTFT/ITL exist only for streaming requests."));

    // table
    const only = el("lt-onlyfail").checked;
    const rows = reqs.slice().reverse().filter((r) => !only || r.server.state === "failed" || r.server.state === "rejected" || (r.client && (r.client.status === "error"))).slice(0, 200);
    fill(el("lt-table"), rows.length
      ? h("table", { class: "grid" },
          h("thead", {}, h("tr", {}, ...["#", "Request", "State", "Selected", "Served", "HTTP", "TTFT", "Total", "Prompt / compl. / cached", "Error"].map((t) => h("th", {}, t)))),
          h("tbody", {}, ...rows.map((r) => {
            const s = r.server, c = r.client;
            const state = c && c.aborted ? "aborted" : s.state === "received" && c && c.status === "pending" ? "pending" : s.state;
            return h("tr", { class: s.state === "failed" ? "rowbad" : "" },
              h("td", {}, r.label), h("td", {}, h("code", {}, shortId(r.id))), h("td", {}, state),
              h("td", {}, s.selected === null ? "–" : replicaChip(s.selected, replicaLabel(s.selected))),
              h("td", {}, s.served === null ? "–" : replicaChip(s.served, replicaLabel(s.served))),
              h("td", { class: "num" }, s.http_status ?? (c && c.httpStatus) ?? "–"),
              h("td", { class: "num" }, fmtMs(s.ttft_ms)), h("td", { class: "num" }, fmtMs(s.total_ms)),
              h("td", { class: "num" }, `${fmtNum(s.prompt_tokens)} / ${fmtNum(s.completion_tokens)} / ${fmtNum(s.cached_tokens)}`),
              h("td", { class: "ans", title: s.error || (c && c.error) || "" }, s.error || (c && c.error) || ""));
          })))
      : h("p", { class: "muted" }, run.total ? "No rows to show." : "Start a run to see every request here."));
  }, 150);

  // ---- export ----------------------------------------------------------------------------------
  function download(name, text, type) {
    const a = h("a", { href: URL.createObjectURL(new Blob([text], { type })), download: name });
    document.body.append(a); a.click(); a.remove();
  }
  const exportRows = () => runRequests().map((r) => ({ id: r.id, label: r.label, state: r.server.state, selected: r.server.selected, served: r.server.served,
    http_status: r.server.http_status, ttft_ms: r.server.ttft_ms, itl_mean_ms: r.server.itl_mean_ms, total_ms: r.server.total_ms, prompt_tokens: r.server.prompt_tokens,
    completion_tokens: r.server.completion_tokens, cached_tokens: r.server.cached_tokens, predicted_cached_tokens: r.server.predicted, path: r.server.path,
    error: r.server.error || (r.client && r.client.error) || null, aborted: !!(r.client && r.client.aborted) }));
  el("lt-export-json").addEventListener("click", () => download(`loadtest-${run.id || "none"}.json`, JSON.stringify({ config: run.cfg, rows: exportRows() }, null, 2), "application/json"));
  el("lt-export-csv").addEventListener("click", () => {
    const rows = exportRows(); if (!rows.length) return;
    const cols = Object.keys(rows[0]);
    const esc = (v) => (v === null || v === undefined ? "" : `"${String(v).replace(/"/g, '""')}"`);
    download(`loadtest-${run.id || "none"}.csv`, [cols.join(","), ...rows.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n"), "text/csv");
  });

  el("lt-start").addEventListener("click", start);
  el("lt-stop").addEventListener("click", stop);
  el("lt-onlyfail").addEventListener("change", () => render());
  el("lt-clear").addEventListener("click", () => {
    if (run.running) { setStatus("Stop the run before clearing.", true); return; }
    for (const id of run.ids) { store.requests.delete(id); }
    store.order = store.order.filter((id) => store.requests.has(id));
    Object.assign(run, { id: null, total: 0, launched: 0, settled: 0, ok: 0, failed: 0, aborted: 0, ids: [] });
    setStatus(""); render();
  });

  store.on(() => { if (!root.hidden) render(); });
  render();
  return { render, state: run };
}
