// Unit tests for the Compare tab's pure logic: identical request sequence for every policy, a separate
// prompt namespace per policy, distinct tenants, and the summary/verdict arithmetic. Run by tests/test_ui.py.
import assert from "node:assert/strict";
import { buildWorkload, tenantSystemPrompt, summarize, verdict, tenantSequence, mechanismMetrics } from "../../swiftserve/ui/js/compare_logic.js";

const base = { n: 60, tenants: 6, tokens: 1500, seed: 1 };
// same sequence for every policy, different namespace
const a = buildWorkload({ ...base, namespace: "run-a-round_robin" }), b = buildWorkload({ ...base, namespace: "run-a-prefix_aware" });
assert.deepEqual(a.map((r) => [r.tenant, r.user]), b.map((r) => [r.tenant, r.user]));
assert.notEqual(a[0].system, b[0].system);
assert.ok(a[0].system.startsWith("[ns:run-a-round_robin] "));
// tenants are distinct from the first line on, and each is long enough
const ps = Array.from({ length: 6 }, (_, t) => tenantSystemPrompt(t, 1500, "x"));
assert.equal(new Set(ps.map((p) => p.split("\n")[0])).size, 6);
assert.ok(ps.every((p) => p.length >= 1500 * 4));
// different seeds differ; sequence uses every tenant
assert.notDeepEqual(tenantSequence({ ...base, namespace: "x" }), tenantSequence({ ...base, seed: 2, namespace: "x" }));
assert.equal(new Set(tenantSequence({ ...base, namespace: "x" })).size, 6);
// same tenant => identical system prompt within a policy (that is what makes the cache reusable)
const w = buildWorkload({ ...base, namespace: "n" });
const t0 = w.filter((r) => r.tenant === w[0].tenant); assert.ok(t0.length > 1 && new Set(t0.map((r) => r.system)).size === 1);

const mk = (state, served, ttft, prompt, cached) => ({ server: { state, served, ttft_ms: ttft, itl_mean_ms: 30, prompt_tokens: prompt, cached_tokens: cached }, client: null });
const s = summarize([mk("completed", 0, 100, 1000, 900), mk("completed", 0, 200, 1000, 700), mk("completed", 1, 300, 1000, 0), mk("failed", null, null, null, null)]);
assert.equal(s.completed, 3); assert.equal(s.failed, 1); assert.equal(s.ttft_p50, 200);
assert.equal(s.cache_reuse, 1600 / 3000); assert.deepEqual(s.per_replica, { 0: 2, 1: 1 });
assert.ok(Math.abs(s.spread - 2 / 1.5) < 1e-9);
const empty = summarize([]); assert.equal(empty.ttft_p50, null); assert.equal(empty.cache_reuse, null); assert.equal(empty.spread, null);

const v = verdict({ round_robin: { ttft_p50: 400, ttft_p95: 900, cache_reuse: 0.3 }, prefix_aware: { ttft_p50: 100, ttft_p95: 500, cache_reuse: 0.8 }, least_connections: { ttft_p50: 300, ttft_p95: 800, cache_reuse: 0.4 } });
assert.equal(v.bestTtft, "prefix_aware"); assert.equal(v.worstTtft, "round_robin"); assert.equal(v.ttftRatio, 4); assert.equal(v.bestCache, "prefix_aware");
assert.equal(verdict({ round_robin: { ttft_p50: 1 } }), null);

// ---- mechanism metrics -------------------------------------------------------------------------
const mk2 = (idx, tenant, ttft, prompt, cached, predicted = null) => ({ label: `p #${idx + 1}`, tenant, idx, server: { state: "completed", served: 0, ttft_ms: ttft, itl_mean_ms: 30, prompt_tokens: prompt, cached_tokens: cached, predicted }, client: null });
const recs = [
  mk2(0, 0, 900, 1000, 0, 0),        // tenant 0, first request: cold but unavoidable
  mk2(1, 1, 950, 1000, 0, 0),        // tenant 1, first request
  mk2(2, 0, 100, 1000, 960, 960),    // tenant 0 again, warm, prediction exact
  mk2(3, 1, 800, 1000, 0, 960),      // tenant 1 again but COLD (cache existed on another replica), prediction off by 960
  mk2(4, 0, 120, 1000, 960, 944),    // warm, prediction off by 16 (one block)
];
const meta = (r) => ({ tenant: r.tenant, index: r.idx });
const m = summarize(recs, meta);
assert.equal(m.recomputed_total, 1000 + 1000 + 40 + 1000 + 40); assert.equal(m.recomputed_avg, 3080 / 5);
assert.equal(m.hit_count, 2); assert.equal(m.miss_count, 3);
assert.equal(m.ttft_hit_p50, 110); assert.equal(m.ttft_miss_p50, 900);
assert.equal(m.cold_after_warmup, 1); assert.equal(m.warmup_denominator, 3);   // requests 2,3,4 count; only #3 was cold
assert.equal(m.pred_count, 5); assert.ok(Math.abs(m.pred_mae_tokens - (0 + 0 + 0 + 960 + 16) / 5) < 1e-9);
assert.equal(m.pred_within_block, 4 / 5);
// no meta => cold-after-warmup is unknown, not zero; policies that do not predict report no prediction error
const noMeta = summarize(recs.map((r) => ({ ...r, server: { ...r.server, predicted: null } })));
assert.equal(noMeta.cold_after_warmup, null); assert.equal(noMeta.warmup_denominator, null);
assert.equal(noMeta.pred_mae_tokens, null); assert.equal(noMeta.pred_within_block, null);
// requests without usage are ignored by the mechanism metrics instead of counted as zero
const mm = mechanismMetrics([]); assert.equal(mm.recomputed_avg, null); assert.equal(mm.ttft_hit_p50, null); assert.equal(mm.recomputed_total, 0);
console.log("compare_logic ok");
