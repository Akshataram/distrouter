// Inference playground: send one request at a time (or several, without
// reloading) through the REAL router and see exactly what happened to it.

import { chat } from "./api.js";
import { renderDetail } from "./detail.js";
import { clear, fmtMs, h, replicaChip, shortId, uid } from "./util.js";

export function initPlayground(root, ctx) {
  const { store, replicaLabel } = ctx;
  const state = { selectedId: null, inflight: new Map(), history: [] };

  root.append(
    h("div", { class: "split" },
      h("section", { class: "card" },
        h("h2", {}, "Send a request"),
        h("label", { for: "pg-system" }, "System prompt (optional)"),
        h("textarea", { id: "pg-system", rows: 3, placeholder: "e.g. You are a helpful support agent for Acme Bank." }),
        h("label", { for: "pg-prompt" }, "Prompt"),
        h("textarea", { id: "pg-prompt", rows: 5 }, "Explain what a KV cache is in two sentences."),
        h("div", { class: "grid3" },
          h("div", {}, h("label", { for: "pg-max" }, "max_tokens"), h("input", { id: "pg-max", type: "number", min: 1, max: 4096, value: 64 })),
          h("div", {}, h("label", { for: "pg-stream" }, "Streaming (needed for TTFT/ITL)"),
            h("select", { id: "pg-stream" }, h("option", { value: "1" }, "on"), h("option", { value: "0" }, "off")))),
        h("details", { class: "more" }, h("summary", {}, "Advanced"),
          h("label", { for: "pg-model" }, "Model name (filled from the router)"),
          h("input", { id: "pg-model", type: "text", autocomplete: "off" }),
          h("div", { class: "grid3" },
            h("div", {}, h("label", { for: "pg-temp" }, "temperature"), h("input", { id: "pg-temp", type: "number", min: 0, max: 2, step: 0.1, value: 0.7 })),
            h("div", {}, h("label", { for: "pg-top" }, "top_p"), h("input", { id: "pg-top", type: "number", min: 0, max: 1, step: 0.05, value: 1 })),
            h("div", {}, h("label", { for: "pg-sla" }, "SLA ms"), h("input", { id: "pg-sla", type: "number", min: 1, placeholder: "router default" }))),
          h("label", { for: "pg-session" }, "Session id"), h("input", { id: "pg-session", type: "text", placeholder: "random per request" })),
        h("div", { class: "row-actions" },
          h("button", { id: "pg-send", class: "primary" }, "Send"),
          h("button", { id: "pg-stop", class: "ghost", disabled: true }, "Abort in-flight")),
        h("div", { id: "pg-status", class: "status", role: "status" })),
      h("div", { class: "stack" },
        h("section", { class: "card" }, h("h2", {}, "Response"), h("div", { id: "pg-output", class: "output", "aria-live": "polite" }, h("span", { class: "muted" }, "Nothing sent yet."))),
        h("section", { class: "card" }, h("h2", {}, "Result"), h("div", { id: "pg-detail" })),
        h("section", { class: "card" }, h("h2", {}, "Requests"), h("div", { id: "pg-history", class: "tablewrap" })))));

  const el = (id) => root.querySelector("#" + id);
  const setStatus = (msg, bad = false) => { const s = el("pg-status"); s.textContent = msg || ""; s.className = "status" + (bad ? " bad" : ""); };

  store.on(() => { if (!root.hidden) render(); });

  function syncModel() {
    const snap = store.snapshot;
    const m = el("pg-model");
    if (snap && !m.dataset.touched && !m.value) m.value = (snap.router && snap.router.model) || "";
  }
  el("pg-model").addEventListener("input", (e) => { e.target.dataset.touched = "1"; });

  function render() {
    syncModel();
    const req = state.selectedId ? store.requests.get(state.selectedId) : null;
    renderDetail(el("pg-detail"), req, { replicaLabel });
    const rows = state.history.map((id) => store.requests.get(id)).filter(Boolean);
    const body = h("tbody", {}, ...rows.map((r) => {
      const s = r.server, c = r.client;
      return h("tr", { class: r.id === state.selectedId ? "selected" : "", tabindex: 0, onclick: () => select(r.id),
                       onkeydown: (e) => { if (e.key === "Enter") select(r.id); } },
        h("td", {}, s.state === "received" && c && c.status === "pending" ? "pending" : (c && c.aborted ? "aborted" : s.state)),
        h("td", {}, s.served === null ? "–" : replicaChip(s.served, replicaLabel(s.served))),
        h("td", { class: "num" }, fmtMs(s.ttft_ms)), h("td", { class: "num" }, fmtMs(s.total_ms)));
    }));
    clear(el("pg-history")).append(rows.length
      ? h("table", { class: "grid" }, h("thead", {}, h("tr", {}, ...["State", "Served by", "TTFT", "Total"].map((t) => h("th", {}, t)))), body)
      : h("p", { class: "muted" }, "No requests yet."));
  }

  function select(id) {
    state.selectedId = id;
    const req = store.requests.get(id);
    const out = el("pg-output");
    out.textContent = (req && req.client && req.client.text) || "";
    if (req && req.client && req.client.error) out.append(h("span", { class: "bad" }, "\n" + req.client.error));
    render();
  }

  async function send() {
    const prompt = el("pg-prompt").value.trim();
    if (!prompt) { setStatus("Type a prompt first.", true); return; }
    const model = el("pg-model").value.trim() || ((store.snapshot && store.snapshot.router.model) || "");
    const system = el("pg-system").value.trim();
    const messages = (system ? [{ role: "system", content: system }] : []).concat([{ role: "user", content: prompt }]);
    const params = { max_tokens: Number(el("pg-max").value) || 64 };
    const temp = el("pg-temp").value, top = el("pg-top").value;
    if (temp !== "") params.temperature = Number(temp);
    if (top !== "") params.top_p = Number(top);
    const stream = el("pg-stream").value === "1";
    const slaMs = Number(el("pg-sla").value) || null;
    const sessionId = el("pg-session").value.trim() || null;

    const id = uid("pg");
    const controller = new AbortController();
    state.inflight.set(id, controller);
    el("pg-stop").disabled = false;
    store.registerClient(id, { source: "playground", label: "playground", prompt });
    state.history.unshift(id);
    state.history.length = Math.min(state.history.length, 25);
    state.selectedId = id;
    const out = el("pg-output"); out.textContent = "";
    out.append(h("span", { class: "muted" }, "waiting for the first token…"));
    setStatus(`Request ${shortId(id)} sent.`);
    render();

    let first = true;
    const result = await chat({
      requestId: id, model, messages, params, stream, slaMs, sessionId, signal: controller.signal,
      onHeaders: (o) => store.patchClient(id, { status: "streaming", httpStatus: o.status, replicaHeader: o.replicaHeader, predictedHeader: o.predictedHeader, headersAt: o.tHeaders }),
      onDelta: (o) => {
        if (state.selectedId === id) { if (first) { out.textContent = ""; first = false; } out.textContent = o.text; }
        store.patchClient(id, { text: o.text, firstTokenAt: o.tFirst });
      },
    });
    state.inflight.delete(id);
    el("pg-stop").disabled = state.inflight.size === 0;
    store.patchClient(id, { status: result.aborted ? "aborted" : result.error ? "error" : "done", doneAt: result.tDone, firstTokenAt: result.tFirst,
                            text: result.text, usage: result.usage, error: result.error, aborted: result.aborted, httpStatus: result.status,
                            replicaHeader: result.replicaHeader });
    if (state.selectedId === id) {
      out.textContent = result.text || "";
      if (result.error) out.append(h("span", { class: "bad" }, (result.text ? "\n" : "") + result.error));
      if (!result.text && !result.error) out.append(h("span", { class: "muted" }, result.aborted ? "(aborted before any text arrived)" : "(empty response)"));
    }
    setStatus(result.error ? `Request failed: ${result.error}` : result.aborted ? "Aborted in the browser." : `Done (HTTP ${result.status}).`, !!result.error);
  }

  el("pg-send").addEventListener("click", send);
  el("pg-prompt").addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") send(); });
  el("pg-stop").addEventListener("click", () => {
    for (const c of state.inflight.values()) c.abort();
    setStatus("Aborted in the browser. For streaming requests the router closes its upstream connection when it sees the disconnect; the engine's behavior after that is not verified here.");
  });
  return { render };
}
