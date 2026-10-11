// Network layer: everything that talks to the router.
//
// The browser calls the same process that routes requests (same origin), using
// its existing endpoints. No credentials are baked into the page: if the router
// was started with SWIFTSERVE_API_TOKEN, the user types the token into the UI
// and it is kept in memory only.

export const auth = { token: "" };

function authHeaders(extra = {}) {
  const h = { ...extra };
  if (auth.token) h["Authorization"] = `Bearer ${auth.token}`;
  return h;
}

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export async function getJSON(path, { signal } = {}) {
  const res = await fetch(path, { headers: authHeaders(), signal });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const b = await res.json(); msg = b.error || b.detail || msg; } catch { /* body was not JSON */ }
    throw new ApiError(res.status, msg);
  }
  return res.json();
}

// -- Server-Sent Events parsing (used for both the router's event stream and the
//    OpenAI-style chat stream). Yields {id, event, data} per complete event.
export async function* parseSSE(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.search(/\r?\n\r?\n/)) >= 0) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx).replace(/^\r?\n\r?\n/, "");
        const ev = { id: null, event: "message", data: "" };
        for (const line of raw.split(/\r?\n/)) {
          if (!line || line.startsWith(":")) continue;
          const c = line.indexOf(":");
          const field = c < 0 ? line : line.slice(0, c);
          const val = c < 0 ? "" : line.slice(c + 1).replace(/^ /, "");
          if (field === "id") ev.id = val;
          else if (field === "event") ev.event = val;
          else if (field === "data") ev.data += (ev.data ? "\n" : "") + val;
        }
        if (ev.data || ev.event !== "message") yield ev;
      }
    }
  } finally {
    try { reader.releaseLock(); } catch { /* already released */ }
  }
}

// Long-lived connection to /api/events/stream that reconnects (the router caps
// each connection's lifetime) and resumes from the last sequence number it saw,
// so no event is lost across reconnects. If the router says events fell out of
// its buffer ("gap"), `onGap` lets the caller resynchronise from a snapshot.
export function startEventStream(store, { onGap } = {}) {
  let stopped = false;
  let controller = null;
  let backoff = 500;

  async function loop() {
    while (!stopped) {
      controller = new AbortController();
      try {
        const res = await fetch(`/api/events/stream?since=${store.lastSeq}`, { headers: authHeaders(), signal: controller.signal });
        if (!res.ok) {
          store.setConnection(res.status === 401 ? "unauthorized" : "error", `HTTP ${res.status}`);
          throw new ApiError(res.status, `event stream HTTP ${res.status}`);
        }
        store.setConnection("live");
        backoff = 500;
        for await (const ev of parseSSE(res.body)) {
          if (ev.event === "gap") { if (onGap) await onGap(JSON.parse(ev.data)); continue; }
          try { store.applyEvent(JSON.parse(ev.data)); } catch { /* ignore one malformed event */ }
        }
        // Stream ended normally (server-side lifetime cap): reconnect immediately.
      } catch (e) {
        if (stopped) return;
        if (!(e instanceof ApiError) || e.status !== 401) store.setConnection("reconnecting", String(e.message || e));
        await new Promise((r) => setTimeout(r, backoff));
        backoff = Math.min(backoff * 2, 8000);
      }
    }
  }
  loop();
  return { stop() { stopped = true; if (controller) controller.abort(); } };
}

// -- Chat completion through the real router -----------------------------------------------
//
// Sends one request to POST /v1/chat/completions with a caller-chosen
// X-Request-Id (so the router's events can be matched to it) and returns what
// the browser observed. Timings here are CLIENT-side (performance.now) and are
// labelled as such in the UI; the authoritative TTFT/ITL are the router's.
export async function chat({ requestId, model, messages, params = {}, stream = true, slaMs = null, sessionId = null, signal, onHeaders, onDelta }) {
  const body = { model, messages, stream, ...params };
  if (stream) body.stream_options = { include_usage: true };
  const headers = authHeaders({ "Content-Type": "application/json", "X-Request-Id": requestId });
  if (slaMs) headers["X-SLA-Ms"] = String(slaMs);
  if (sessionId) headers["X-Session-Id"] = sessionId;

  const out = { status: null, replicaHeader: null, predictedHeader: null, requestIdEcho: null, text: "", usage: null,
                t0: performance.now(), tHeaders: null, tFirst: null, tDone: null, error: null, aborted: false };
  try {
    const res = await fetch("/v1/chat/completions", { method: "POST", headers, body: JSON.stringify(body), signal });
    out.status = res.status;
    out.tHeaders = performance.now();
    out.replicaHeader = res.headers.get("x-swiftserve-replica");
    out.predictedHeader = res.headers.get("x-swiftserve-predicted-cached-tokens");
    out.requestIdEcho = res.headers.get("x-request-id");
    if (onHeaders) onHeaders(out);

    if (!res.ok) {
      const text = await res.text();
      try { const j = JSON.parse(text); out.error = (j.error && j.error.message) || j.error || j.detail || text; } catch { out.error = text.slice(0, 500); }
      if (typeof out.error !== "string") out.error = JSON.stringify(out.error);
    } else if (stream) {
      for await (const ev of parseSSE(res.body)) {
        if (ev.data === "[DONE]") break;
        let j; try { j = JSON.parse(ev.data); } catch { continue; }
        const delta = j.choices && j.choices[0] && j.choices[0].delta && j.choices[0].delta.content;
        if (delta) {
          if (out.tFirst === null) out.tFirst = performance.now();
          out.text += delta;
          if (onDelta) onDelta(out, delta);
        }
        if (j.usage) out.usage = j.usage;
      }
    } else {
      const j = await res.json();
      out.text = (j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content) || "";
      out.usage = j.usage || null;
    }
  } catch (e) {
    if (e && e.name === "AbortError") out.aborted = true;
    else out.error = `${e && e.name ? e.name + ": " : ""}${e && e.message ? e.message : e}`;
  }
  out.tDone = performance.now();
  return out;
}
