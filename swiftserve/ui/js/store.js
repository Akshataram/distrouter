// The UI's single source of truth about requests.
//
// Two independent kinds of evidence are merged here, kept in separate fields
// so the UI can always say where a number came from:
//
//   req.server  -- what the ROUTER reported, from its live event stream
//                  (routing decision, dispatch attempts, TTFT/ITL measured at
//                  the router, served replica, ...). This is the authority for
//                  anything about routing.
//   req.client  -- what THIS BROWSER observed for requests it sent itself
//                  (its own timings, the response headers, the text).
//
// Requests are keyed by the X-Request-Id the browser sends, which the router
// echoes into every event, so the two halves join without guessing. Requests
// sent by other clients appear too (source "external") because the router's
// event stream covers everything it handles.
//
// This module is pure logic (no DOM) so it can be unit-tested under Node.

const MAX_REQUESTS = 3000;

export function emptyServer() {
  return {
    state: "received", t_received: null, session_id: null, stream: null, policy: null, sla_ms: null,
    messages: null, prompt_chars: null, max_tokens: null,
    selected: null, path: null, explain: null, candidates: [], predicted: null, half_open: false,
    dispatches: [], retries: [], served: null,
    http_status: null, ok: null, error: null, error_stage: null, reject_reason: null,
    ttfb_ms: null, ttft_ms: null, itl_mean_ms: null, tpot_ms: null, total_ms: null, upstream_ms: null,
    content_events: null, stream_complete: null,
    prompt_tokens: null, completion_tokens: null, cached_tokens: null,
    at: { routed: null, dispatch: null, first_token: null, finished: null },
  };
}

export const TERMINAL = new Set(["completed", "failed", "rejected", "client_disconnected"]);

export class Store {
  constructor() {
    this.requests = new Map();
    this.order = [];
    this.listeners = new Set();
    this.eventListeners = new Set();
    this.replicaEvents = [];
    this.snapshot = null;
    this.history = { rps: [], tps: [] }; // router-reported throughput, sampled on every snapshot
    this.lastSeq = 0;
    this._lastSnapTime = 0;
    this.connection = { state: "connecting", detail: "" };
    this._notifyQueued = false;
  }

  // -- subscriptions ---------------------------------------------------------
  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  onEvent(fn) { this.eventListeners.add(fn); return () => this.eventListeners.delete(fn); }
  notify() {
    if (this._notifyQueued) return;
    this._notifyQueued = true;
    const run = () => { this._notifyQueued = false; for (const fn of this.listeners) fn(this); };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(run); else queueMicrotask(run);
  }

  // -- request records ----------------------------------------------------------
  ensure(id, source = "external", label = "") {
    let req = this.requests.get(id);
    if (!req) {
      req = { id, source, label, runId: null, server: emptyServer(), client: null };
      this.requests.set(id, req);
      this.order.push(id);
      if (this.order.length > MAX_REQUESTS) this.requests.delete(this.order.shift());
    }
    return req;
  }

  // Register a request this browser is about to send (before the first event can arrive).
  registerClient(id, { source, label, runId = null, prompt = "" }) {
    const req = this.ensure(id, source, label);
    req.source = source; req.label = label; req.runId = runId;
    req.client = { status: "pending", startedAt: performance.now(), startedWall: Date.now(), headersAt: null, firstTokenAt: null,
                   doneAt: null, httpStatus: null, replicaHeader: null, predictedHeader: null, text: "", usage: null,
                   error: null, aborted: false, prompt };
    this.notify();
    return req;
  }

  patchClient(id, patch) {
    const req = this.requests.get(id);
    if (!req || !req.client) return;
    Object.assign(req.client, patch);
    this.notify();
  }

  // -- router events -----------------------------------------------------------------
  applyEvent(ev) {
    if (typeof ev.seq === "number") this.lastSeq = Math.max(this.lastSeq, ev.seq);
    for (const fn of this.eventListeners) fn(ev);
    if (ev.type === "replica.state") {
      this.replicaEvents.unshift(ev);
      this.replicaEvents.length = Math.min(this.replicaEvents.length, 50);
      this.notify();
      return;
    }
    if (!ev.request_id) return;
    const req = this.ensure(ev.request_id);
    const s = req.server;
    switch (ev.type) {
      case "request.received":
        Object.assign(s, { state: "received", t_received: ev.ts, session_id: ev.session_id, stream: ev.stream, policy: ev.policy,
                           sla_ms: ev.sla_ms, messages: ev.messages, prompt_chars: ev.prompt_chars, max_tokens: ev.max_tokens });
        break;
      case "routing.decision":
        Object.assign(s, { state: "routed", selected: ev.selected_replica, path: ev.path, explain: ev.explain,
                           candidates: ev.candidates || [], predicted: ev.predicted_cached_tokens, half_open: !!ev.half_open_override });
        s.at.routed = ev.at_ms;
        break;
      case "dispatch":
        s.state = "dispatched";
        s.dispatches.push({ attempt: ev.attempt, replica: ev.replica, at_ms: ev.at_ms, same_as_selected: ev.same_as_selected });
        if (s.at.dispatch === null) s.at.dispatch = ev.at_ms;
        break;
      case "dispatch.retry":
        s.retries.push({ replica: ev.replica, failed_attempt: ev.failed_attempt, error: ev.error, delay_s: ev.delay_s });
        break;
      case "upstream.headers":
        s.http_status = ev.http_status;
        break;
      case "first_token":
        s.state = "streaming"; s.ttft_ms = ev.ttft_ms; s.ttfb_ms = ev.ttfb_ms; s.at.first_token = ev.at_ms;
        break;
      case "request.completed":
      case "request.failed":
        Object.assign(s, {
          state: ev.type === "request.completed" ? "completed" : "failed", served: ev.served_replica, http_status: ev.http_status ?? s.http_status,
          ok: ev.ok, error: ev.error ?? null, error_stage: ev.error_stage ?? null, total_ms: ev.total_ms, upstream_ms: ev.upstream_ms,
          ttfb_ms: ev.ttfb_ms ?? s.ttfb_ms, ttft_ms: ev.ttft_ms ?? s.ttft_ms, itl_mean_ms: ev.itl_mean_ms ?? null, tpot_ms: ev.tpot_ms ?? null,
          content_events: ev.content_events ?? null, stream_complete: ev.stream_complete ?? null,
          prompt_tokens: ev.prompt_tokens ?? null, completion_tokens: ev.completion_tokens ?? null, cached_tokens: ev.cached_tokens ?? null,
        });
        if (ev.predicted_cached_tokens !== undefined && ev.predicted_cached_tokens !== null) s.predicted = ev.predicted_cached_tokens;
        s.at.finished = ev.at_ms;
        break;
      case "request.client_disconnected":
        Object.assign(s, { state: "client_disconnected", upstream_ms: ev.upstream_ms });
        s.at.finished = ev.at_ms;
        break;
      case "request.rejected":
        Object.assign(s, { state: "rejected", reject_reason: ev.reason, http_status: ev.http_status, error: ev.detail || ev.reason });
        break;
      default:
        break;
    }
    this.notify();
  }

  // Seed from /api/requests (oldest-first order is restored here).
  seedFromRecords(records) {
    for (const rec of [...records].reverse()) {
      const req = this.ensure(rec.request_id);
      const s = req.server;
      Object.assign(s, {
        state: rec.state, t_received: rec.received_at, session_id: rec.session_id, stream: rec.stream, policy: rec.policy, sla_ms: rec.sla_ms,
        messages: rec.messages, prompt_chars: rec.prompt_chars, max_tokens: rec.max_tokens, selected: rec.selected_replica, path: rec.route_path,
        explain: rec.explain, candidates: rec.candidates || [], predicted: rec.predicted_cached_tokens, dispatches: rec.dispatches || [],
        retries: rec.retries || [], served: rec.served_replica, http_status: rec.http_status, ok: rec.ok, error: rec.error,
        error_stage: rec.error_stage, reject_reason: rec.reject_reason, ttfb_ms: rec.ttfb_ms, ttft_ms: rec.ttft_ms, itl_mean_ms: rec.itl_mean_ms,
        tpot_ms: rec.tpot_ms, total_ms: rec.total_ms, upstream_ms: rec.upstream_ms, content_events: rec.content_events,
        stream_complete: rec.stream_complete, prompt_tokens: rec.prompt_tokens, completion_tokens: rec.completion_tokens, cached_tokens: rec.cached_tokens,
        at: { routed: rec.timeline_ms.routed, dispatch: rec.timeline_ms.first_dispatch, first_token: rec.timeline_ms.first_token, finished: rec.timeline_ms.finished },
      });
    }
    this.notify();
  }

  setSnapshot(snap) {
    this.snapshot = snap;
    if (snap.server_time !== this._lastSnapTime && snap.throughput) {
      this._lastSnapTime = snap.server_time;
      this.history.rps.push(snap.throughput.requests_per_s); this.history.tps.push(snap.throughput.completion_tokens_per_s);
      for (const k of Object.keys(this.history)) if (this.history[k].length > 60) this.history[k].shift();
    }
    this.notify();
  }
  setConnection(state, detail = "") { this.connection = { state, detail }; this.notify(); }

  // -- derived views ---------------------------------------------------------------------
  all() { return this.order.map((id) => this.requests.get(id)).filter(Boolean); }

  // Which replica a request is currently running on: the replica of its most
  // recent dispatch attempt, while it has not reached a terminal state.
  activeReplica(req) {
    const s = req.server;
    if (TERMINAL.has(s.state) || !s.dispatches.length) return null;
    return s.dispatches[s.dispatches.length - 1].replica;
  }

  activeByReplica() {
    const out = {};
    for (const req of this.requests.values()) {
      const rid = this.activeReplica(req);
      if (rid !== null) out[rid] = (out[rid] || 0) + 1;
    }
    return out;
  }

  // Per-replica tallies over a set of requests (selected = policy's choice,
  // served = replica that actually returned the response).
  tally(reqs) {
    const t = {};
    const slot = (rid) => (t[rid] ??= { selected: 0, dispatched: 0, served: 0, failed: 0, disconnected: 0 });
    for (const r of reqs) {
      const s = r.server;
      if (s.selected !== null) slot(s.selected).selected++;
      if (s.dispatches.length) slot(s.dispatches[s.dispatches.length - 1].replica).dispatched++;
      if (s.state === "completed" && s.served !== null) slot(s.served).served++;
      else if (s.state === "failed") slot(s.dispatches.length ? s.dispatches[s.dispatches.length - 1].replica : (s.selected ?? -1)).failed++;
      else if (s.state === "client_disconnected" && s.dispatches.length) slot(s.dispatches[s.dispatches.length - 1].replica).disconnected++;
    }
    return t;
  }
}
