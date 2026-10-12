// Request detail panel, shared by the playground, load test and routing views.
//
// Every value is labelled with where it came from:
//   "router"  -- measured/recorded by the router and delivered over its event stream
//   "browser" -- measured by this page for a request it sent itself
// and a value nobody measured is shown as "n/a" with the reason, never as zero.

import { TERMINAL } from "./store.js";
import { describeDecision, fmtMs, fmtNum, h, replicaChip } from "./util.js";

function row(label, value, source, note) {
  return h("tr", {},
    h("th", { scope: "row" }, label),
    h("td", {}, value),
    h("td", { class: "src" }, source ? h("span", { class: `src-tag src-${source}` }, source) : ""),
    h("td", { class: "note" }, note || ""));
}

function stateChip(state) {
  const cls = { completed: "ok", failed: "bad", rejected: "bad", client_disconnected: "warn", streaming: "info", dispatched: "info", routed: "info", received: "info" }[state] || "info";
  return h("span", { class: `badge badge-${cls}` }, state);
}

function renderFull(container, req, { replicaLabel = (rid) => `R${rid}` } = {}) {
  container.textContent = "";
  if (!req) { container.append(h("p", { class: "muted" }, "Select a request to see exactly what happened to it.")); return; }
  const s = req.server, c = req.client;

  // ---- header ----
  container.append(h("div", { class: "detail-head" },
    h("code", { class: "rid", title: "X-Request-Id; also the key in the router's events" }, req.id),
    stateChip(c && c.aborted ? "aborted by browser" : s.state),
    h("span", { class: "muted" }, req.source === "external" ? "sent by another client" : `sent from this page (${req.source})`)));

  // ---- replica attribution: selected vs dispatched vs served ----
  const dispatched = s.dispatches.length
    ? s.dispatches.map((d) => h("span", { class: "attempt" }, replicaChip(d.replica, replicaLabel(d.replica)), h("small", {}, ` attempt ${d.attempt}`)))
    : [h("span", { class: "muted" }, "never dispatched")];
  const differs = s.state === "completed" && s.served !== null && s.selected !== null && s.served !== s.selected;
  container.append(h("table", { class: "kv" }, h("tbody", {},
    row("Selected by policy", s.selected === null ? "n/a" : replicaChip(s.selected, replicaLabel(s.selected)), "router",
      "the replica the routing decision chose (or a half-open probe override)"),
    row("Dispatched to", h("span", { class: "attempts" }, ...dispatched), "router",
      "every replica an HTTP attempt was sent to; the proxy retries the same replica on connect errors"),
    row("Served by", s.served !== null ? replicaChip(s.served, replicaLabel(s.served))
        : TERMINAL.has(s.state) ? (s.state === "completed" ? "n/a" : "none") : h("span", { class: "muted" }, "in progress"), "router",
      differs ? "differs from the selected replica" : "the replica that produced the response"),
    c && c.replicaHeader !== null && c.replicaHeader !== undefined
      ? row("X-SwiftServe-Replica header", replicaChip(c.replicaHeader, replicaLabel(c.replicaHeader)), "browser", "what the HTTP response told this page") : null,
    s.retries.length ? row("Retries", `${s.retries.length} connect failure(s) retried`, "router", s.retries.map((r) => r.error).join(" · ")) : null)));

  // ---- routing decision ----
  container.append(h("h4", {}, "Routing decision"));
  container.append(h("p", { class: "decision" },
    h("span", { class: "badge badge-info" }, s.path || "no decision"), " ", describeDecision(s)));
  if (s.candidates && s.candidates.length) {
    container.append(h("table", { class: "grid small" },
      h("thead", {}, h("tr", {}, ...["Replica", "Usable", "Circuit", "Health", "In flight", "Queue", "Est. latency", "Capacity"].map((t) => h("th", {}, t)))),
      h("tbody", {}, ...s.candidates.map((cd) => h("tr", { class: cd.replica_id === s.selected ? "selected" : "" },
        h("td", {}, replicaChip(cd.replica_id, replicaLabel(cd.replica_id))),
        h("td", {}, cd.available ? "yes" : "no"), h("td", {}, cd.circuit),
        h("td", {}, cd.healthy === null ? "unknown" : cd.healthy ? "up" : "down"),
        h("td", { class: "num" }, cd.in_flight), h("td", { class: "num" }, cd.queue_depth),
        h("td", { class: "num" }, fmtMs(cd.est_latency_ms)), h("td", { class: "num" }, cd.batch_capacity))))));
    container.append(h("p", { class: "muted small" }, "State of every replica at the instant the decision was made (read before the policy ran)."));
  }

  // ---- timings ----
  const noStream = s.stream === false;
  const browserTtft = c && c.firstTokenAt !== null && c.firstTokenAt !== undefined ? c.firstTokenAt - c.startedAt : null;
  const browserTotal = c && c.doneAt !== null && c.doneAt !== undefined ? c.doneAt - c.startedAt : null;
  container.append(h("h4", {}, "Latency"));
  container.append(h("table", { class: "kv" }, h("tbody", {},
    row("TTFT (time to first token)", noStream ? "n/a" : fmtMs(s.ttft_ms, 1), "router",
      noStream ? "non-streaming response: the router sees the whole body at once, so no first token is observable"
               : "request received → first non-empty content event at the router"),
    browserTtft !== null ? row("TTFT (browser view)", fmtMs(browserTtft, 1), "browser", "includes browser↔router network time") : null,
    row("ITL (mean inter-token latency)", noStream ? "n/a" : fmtMs(s.itl_mean_ms, 2), "router",
      s.itl_mean_ms === null && !noStream ? "needs ≥2 content events" : "mean gap between content events"),
    row("TPOT (per output token)", noStream ? "n/a" : fmtMs(s.tpot_ms, 2), "router", "uses the engine's completion_tokens; equals ITL when each event is one token"),
    row("Total latency", fmtMs(s.total_ms, 1), "router", "request received → response finished, as the router saw it"),
    browserTotal !== null ? row("Total latency (browser view)", fmtMs(browserTotal, 1), "browser", "") : null,
    row("Upstream time", fmtMs(s.upstream_ms, 1), "router", "first dispatch → finished (the value that feeds the replica's latency average)"),
    s.t_received ? row("Received at", new Date(s.t_received * 1000).toLocaleTimeString() + "." + String(Math.floor((s.t_received % 1) * 1000)).padStart(3, "0"), "router", "") : null)));

  // ---- tokens ----
  const predicted = s.predicted;
  const cached = s.cached_tokens;
  container.append(h("h4", {}, "Tokens"));
  container.append(h("table", { class: "kv" }, h("tbody", {},
    row("Prompt tokens", fmtNum(s.prompt_tokens), "router", s.prompt_tokens === null ? "the engine did not report usage" : "from the engine's usage report"),
    row("Completion tokens", fmtNum(s.completion_tokens), "router", ""),
    row("Reused from engine cache", fmtNum(cached), "router", cached === null ? "not reported (needs vLLM --enable-prompt-tokens-details)" : "usage.prompt_tokens_details.cached_tokens"),
    row("Router's predicted reuse", predicted === null ? "n/a (this policy makes no prediction)" : fmtNum(predicted), "router",
      predicted !== null && cached !== null ? `prediction error ${fmtNum(Math.abs(predicted - cached))} tokens` : ""))));

  // ---- error ----
  const err = (c && c.error) || s.error;
  if (err) container.append(h("div", { class: "errbox" }, h("strong", {}, s.error_stage ? `Failed (${s.error_stage}): ` : "Error: "), String(err),
    s.http_status ? h("span", { class: "muted" }, `  · HTTP ${s.http_status}`) : null));
  else if (s.http_status && s.http_status >= 400) container.append(h("div", { class: "errbox" }, `Upstream answered HTTP ${s.http_status}.`));
}

// What most people need: who served it, why, how fast, and whether the cache helped.
// Everything else (selected vs dispatched, candidate table, timing sources) is one click away.
export function renderDetail(container, req, opts = {}) {
  const replicaLabel = opts.replicaLabel || ((rid) => `R${rid}`);
  container.textContent = "";
  if (!req) { container.append(h("p", { class: "muted" }, "Send a request or select one to see what happened to it.")); return; }
  const s = req.server, c = req.client;
  const noStream = s.stream === false;
  const err = (c && c.error) || s.error;
  const cached = s.cached_tokens, predicted = s.predicted;
  const reuse = s.prompt_tokens === null ? "n/a" : cached === null ? "not reported by the engine"
    : `${fmtNum(cached)} of ${fmtNum(s.prompt_tokens)} prompt tokens`;
  const rows = [
    row("Served by", s.served !== null ? replicaChip(s.served, replicaLabel(s.served)) : (TERMINAL.has(s.state) ? "none" : "in progress")),
    row("Why", s.path ? describeDecision(s) : "n/a"),
    row("TTFT", noStream ? "n/a (non-streaming)" : fmtMs(s.ttft_ms, 0)),
    row("ITL (mean)", noStream ? "n/a (non-streaming)" : fmtMs(s.itl_mean_ms, 1)),
    row("Total", fmtMs(s.total_ms, 0)),
    row("Reused from cache", reuse),
    predicted === null || predicted === undefined ? null : row("Router predicted", `${fmtNum(predicted)} tokens`)];
  container.append(h("div", { class: "detail-head" }, stateChip(c && c.aborted ? "aborted by browser" : s.state),
    s.retries.length ? h("span", { class: "muted small" }, `${s.retries.length} retried`) : null),
    h("table", { class: "kv" }, h("tbody", {}, ...rows)));
  if (err) container.append(h("div", { class: "errbox" }, h("strong", {}, s.error_stage ? `Failed (${s.error_stage}): ` : "Error: "), String(err)));
  const more = h("details", { class: "more" }, h("summary", {}, "All details"));
  const inner = h("div", {});
  more.append(inner);
  container.append(more);
  renderFull(inner, req, opts);
}
