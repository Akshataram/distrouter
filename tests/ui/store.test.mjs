// Unit tests for the console's pure logic (no DOM): the request store that merges
// the router's event stream with what the browser saw, and the routing-decision
// description. Run with `node tests/ui/store.test.mjs`; tests/test_ui.py runs it
// under pytest when Node is available.
import assert from "node:assert/strict";
import { Store, TERMINAL } from "../../swiftserve/ui/js/store.js";
import { describeDecision, percentile } from "../../swiftserve/ui/js/util.js";

let n = 0;
const test = (name, fn) => { fn(); n++; console.log("ok -", name); };
const ev = (type, request_id, extra = {}) => ({ seq: ++ev.seq, ts: 1700000000, type, request_id, ...extra });
ev.seq = 0;

test("events build a request's server-side story in order", () => {
  const s = new Store();
  for (const e of [
    ev("request.received", "r1", { session_id: "s", stream: true, policy: "prefix_aware", sla_ms: 3000, messages: 2, prompt_chars: 10, max_tokens: 8 }),
    ev("routing.decision", "r1", { selected_replica: 2, path: "prefix_match", explain: { path: "prefix_match" }, candidates: [{ replica_id: 2 }], predicted_cached_tokens: 64, half_open_override: false, at_ms: 1.5 }),
    ev("dispatch", "r1", { replica: 2, attempt: 1, same_as_selected: true, at_ms: 2 }),
    ev("upstream.headers", "r1", { replica: 2, http_status: 200 }),
    ev("first_token", "r1", { replica: 2, ttft_ms: 40, ttfb_ms: 35, at_ms: 40 }),
    ev("request.completed", "r1", { replica: 2, served_replica: 2, selected_replica: 2, http_status: 200, ok: true, total_ms: 90, upstream_ms: 88, ttft_ms: 40,
                                    itl_mean_ms: 12, tpot_ms: 12, prompt_tokens: 20, completion_tokens: 5, cached_tokens: 16, stream_complete: true, at_ms: 90 }),
  ]) s.applyEvent(e);
  const r = s.requests.get("r1").server;
  assert.equal(r.state, "completed"); assert.equal(r.selected, 2); assert.equal(r.served, 2);
  assert.deepEqual(r.dispatches.map((d) => d.replica), [2]);
  assert.equal(r.ttft_ms, 40); assert.equal(r.itl_mean_ms, 12); assert.equal(r.cached_tokens, 16); assert.equal(r.predicted, 64);
  assert.equal(s.requests.get("r1").source, "external");   // nobody registered it: another client sent it
});

test("a browser-registered request joins the router's events by id", () => {
  const s = new Store();
  s.registerClient("ui-1", { source: "playground", label: "playground", prompt: "hi" });
  s.applyEvent(ev("request.received", "ui-1", { stream: true }));
  s.applyEvent(ev("routing.decision", "ui-1", { selected_replica: 0, path: "round_robin", candidates: [] }));
  const r = s.requests.get("ui-1");
  assert.equal(r.source, "playground"); assert.equal(r.client.status, "pending"); assert.equal(r.server.selected, 0);
  assert.equal(s.requests.size, 1);
});

test("selected, dispatched and served stay distinct (retries, failure)", () => {
  const s = new Store();
  s.applyEvent(ev("request.received", "f1", {}));
  s.applyEvent(ev("routing.decision", "f1", { selected_replica: 1, path: "x", candidates: [] }));
  s.applyEvent(ev("dispatch", "f1", { replica: 1, attempt: 1, same_as_selected: true }));
  s.applyEvent(ev("dispatch.retry", "f1", { replica: 1, failed_attempt: 1, error: "ConnectError", delay_s: 0.1 }));
  s.applyEvent(ev("dispatch", "f1", { replica: 1, attempt: 2, same_as_selected: true }));
  s.applyEvent(ev("request.failed", "f1", { replica: 1, served_replica: null, selected_replica: 1, ok: false, error: "ConnectError", error_stage: "connect", total_ms: 5 }));
  const r = s.requests.get("f1").server;
  assert.equal(r.state, "failed"); assert.equal(r.served, null); assert.equal(r.selected, 1);
  assert.equal(r.dispatches.length, 2); assert.equal(r.retries.length, 1); assert.equal(r.error_stage, "connect");
});

test("active-by-replica counts only requests that are dispatched and not yet finished", () => {
  const s = new Store();
  for (const [id, rep] of [["a", 0], ["b", 0], ["c", 1]]) {
    s.applyEvent(ev("request.received", id, {})); s.applyEvent(ev("dispatch", id, { replica: rep, attempt: 1 }));
  }
  s.applyEvent(ev("request.completed", "a", { served_replica: 0, replica: 0, ok: true }));
  s.applyEvent(ev("request.received", "d", {}));            // received but never dispatched: not "on" any replica
  assert.deepEqual(s.activeByReplica(), { 0: 1, 1: 1 });
});

test("tally separates what the policy selected from what actually served", () => {
  const s = new Store();
  const mk = (id, sel, evs) => { s.applyEvent(ev("request.received", id, {})); s.applyEvent(ev("routing.decision", id, { selected_replica: sel, path: "p", candidates: [] })); evs.forEach((e) => s.applyEvent(e)); };
  mk("a", 0, [ev("dispatch", "a", { replica: 0, attempt: 1 }), ev("request.completed", "a", { served_replica: 0, replica: 0, ok: true })]);
  mk("b", 1, [ev("dispatch", "b", { replica: 1, attempt: 1 }), ev("request.failed", "b", { replica: 1, ok: false })]);
  mk("c", 0, [ev("dispatch", "c", { replica: 0, attempt: 1 }), ev("request.client_disconnected", "c", { replica: 0 })]);
  const t = s.tally(s.all());
  assert.deepEqual(t[0], { selected: 2, dispatched: 2, served: 1, failed: 0, disconnected: 1 });
  assert.deepEqual(t[1], { selected: 1, dispatched: 1, served: 0, failed: 1, disconnected: 0 });
});

test("terminal states are the ones the router can finish a request in", () => {
  assert.deepEqual([...TERMINAL].sort(), ["client_disconnected", "completed", "failed", "rejected"]);
});

test("rejections and replica state changes are recorded", () => {
  const s = new Store();
  s.applyEvent(ev("request.received", "x", {})); s.applyEvent(ev("request.rejected", "x", { reason: "admission", http_status: 503, detail: "busy" }));
  assert.equal(s.requests.get("x").server.state, "rejected"); assert.equal(s.requests.get("x").server.reject_reason, "admission");
  s.applyEvent(ev("replica.state", null, { replica: 1, circuit: { from: "closed", to: "open" }, healthy: { from: true, to: false } }));
  assert.equal(s.replicaEvents.length, 1); assert.equal(s.lastSeq, ev.seq);
});

test("the store is bounded", () => {
  const s = new Store();
  for (let i = 0; i < 3100; i++) s.ensure(`r${i}`);
  assert.equal(s.requests.size, 3000); assert.equal(s.requests.has("r0"), false); assert.equal(s.requests.has("r3099"), true);
});

test("seeding from /api/requests restores history oldest-first", () => {
  const s = new Store();
  const rec = (id, replica) => ({ request_id: id, state: "completed", received_at: 1, stream: true, selected_replica: replica, served_replica: replica, dispatches: [{ replica }], retries: [],
    candidates: [], timeline_ms: { routed: 1, first_dispatch: 2, first_token: 3, finished: 4 }, route_path: "p" });
  s.seedFromRecords([rec("newest", 1), rec("older", 0)]);   // API returns newest first
  assert.deepEqual(s.order, ["older", "newest"]); assert.equal(s.requests.get("newest").server.served, 1);
});

test("describeDecision only states what the policy recorded", () => {
  assert.match(describeDecision({ path: "prefix_match", selected: 1, explain: { block_size: 16, total_blocks: 45, sla_ms: 3000, best: { replica_id: 1, matched_blocks: 40, estimated_latency_ms: 800 } } }), /R1 holds 40\/45 prompt blocks \(640 tokens\).*≤ SLA 3000/);
  assert.match(describeDecision({ path: "sla_veto", selected: 2, explain: { total_blocks: 10, sla_ms: 3000, best: { replica_id: 0, matched_blocks: 9, estimated_latency_ms: 4198 } } }), /skipped: estimated 4198 ms > SLA 3000/);
  assert.match(describeDecision({ path: "least_loaded", selected: 3, explain: { trie: { matched_depth: 1, min_depth_required: 2 } } }), /trie depth 1, needs ≥2/);
  assert.match(describeDecision({ path: "round_robin", selected: 0, explain: { slot: 2, rotation_size: 4 } }), /slot 2 of 4/);
  assert.equal(describeDecision({ path: "mystery", selected: 0, explain: null }), "mystery (the policy reported no further detail)");   // never invents a reason
  assert.equal(describeDecision(null), "no decision recorded");
});

test("percentile ignores missing values", () => {
  assert.equal(percentile([null, 10, undefined, 20, 30], 50), 20); assert.equal(percentile([], 50), null);
});

console.log(`\n${n} tests passed`);
