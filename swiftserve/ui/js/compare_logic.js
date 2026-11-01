// Pure logic for the Compare tab (no DOM, so it is unit-tested under Node).
//
// A fair policy comparison needs three things the other tabs do not give you:
//   1. SEVERAL distinct long system prompts ("tenants"), more than one replica can keep
//      cached, so that where a request lands decides whether the cache is used;
//   2. the SAME request sequence for every policy;
//   3. a separate prompt namespace per policy, so one policy cannot reuse blocks another
//      policy already warmed on the replicas (the namespace marker goes at the very front,
//      which changes every chained block hash after it).

import { mean, percentile } from "./util.js";

export const POLICY_ORDER = ["round_robin", "least_connections", "swiftserve", "prefix_aware"];

export const QUESTIONS = [
  "How do I reset my password?", "Where can I find my latest invoice?", "How do I cancel my subscription?",
  "Can I change the email address on my account?", "Why was my card declined?", "How do I export my data?",
  "What are your support hours?", "How do I add another user to my plan?",
];

// Small deterministic PRNG so every policy gets the identical sequence.
export function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const TOPICS = ["card disputes", "wire transfers", "overdraft fees", "mortgage questions", "fraud alerts", "account closure", "refunds", "statements"];
const COMPANIES = ["Northwind Bank", "Contoso Telecom", "Fabrikam Energy", "Tailspin Travel", "Litware Health", "Adatum Retail", "Proseware Cloud", "Wingtip Insurance"];

// ~tokens*4 characters. The first line differs per tenant (and carries the namespace), so the very
// first 16-token block already differs between tenants and between policies.
export function tenantSystemPrompt(tenant, tokens, namespace) {
  const company = COMPANIES[tenant % COMPANIES.length] + (tenant >= COMPANIES.length ? ` ${Math.floor(tenant / COMPANIES.length) + 1}` : "");
  const lines = [`[ns:${namespace}] You are the customer support assistant for ${company}. Follow every rule below exactly.`];
  let chars = lines[0].length;
  for (let n = 1; chars < tokens * 4; n++) {
    const l = `Rule ${n}: when a customer of ${company} asks about ${TOPICS[(n + tenant) % TOPICS.length]}, answer in at most three short sentences and cite policy ${tenant + 1}-${n}.`;
    lines.push(l); chars += l.length + 1;
  }
  return lines.join("\n");
}

// The request list for ONE policy: tenant and question are drawn from a seeded PRNG, so the sequence is
// identical for every policy; only the namespace differs.
export function buildWorkload({ n, tenants, tokens, namespace, seed, questions = QUESTIONS }) {
  const rand = mulberry32(seed);
  const prompts = Array.from({ length: tenants }, (_, t) => tenantSystemPrompt(t, tokens, namespace));
  return Array.from({ length: n }, (_, i) => {
    const tenant = Math.floor(rand() * tenants);
    const question = questions[Math.floor(rand() * questions.length)];
    return { index: i, tenant, system: prompts[tenant], user: question };
  });
}

export const tenantSequence = (opts) => buildWorkload(opts).map((r) => r.tenant);

// A request counts as "cold" when less than this share of its prompt came from the engine's cache.
export const COLD_BELOW = 0.5;
const BLOCK = 16;   // vLLM block size: a prediction within one block of the engine's count is "close"

// Metrics that measure the MECHANISM (prefill work skipped), not just the outcome. All of them are derived
// from fields the router already measures per request (prompt_tokens and cached_tokens from vLLM's usage,
// TTFT from the router's stream timer, the policy's predicted_cached_tokens); nothing new is instrumented.
//   `meta(r)` returns {tenant, index} for a request record, or null; it lets us tell an unavoidable first
//   request of a tenant apart from a later request that missed a cache that existed.
export function mechanismMetrics(withUsage, meta = null) {
  const ttftOf = (r) => r.server.ttft_ms;
  const frac = (r) => r.server.cached_tokens / r.server.prompt_tokens;
  const recomputed = withUsage.map((r) => r.server.prompt_tokens - r.server.cached_tokens);
  const hit = withUsage.filter((r) => frac(r) >= COLD_BELOW), miss = withUsage.filter((r) => frac(r) < COLD_BELOW);

  // cold requests AFTER each tenant's first request (the first one is cold for every policy)
  let coldAfterWarmup = null, warmupDenominator = null;
  if (meta) {
    const seen = new Set();
    const ordered = withUsage.map((r) => ({ r, m: meta(r) })).filter((x) => x.m).sort((a, b) => a.m.index - b.m.index);
    let cold = 0, counted = 0;
    for (const { r, m } of ordered) {
      if (!seen.has(m.tenant)) { seen.add(m.tenant); continue; }
      counted++;
      if (frac(r) < COLD_BELOW) cold++;
    }
    coldAfterWarmup = cold; warmupDenominator = counted;
  }

  // how well the router's belief matched the engine's report (only policies that predict)
  const pred = withUsage.filter((r) => r.server.predicted !== null && r.server.predicted !== undefined);
  const errs = pred.map((r) => Math.abs(r.server.predicted - r.server.cached_tokens));
  return {
    recomputed_avg: mean(recomputed), recomputed_total: recomputed.reduce((a, b) => a + b, 0),
    hit_count: hit.length, miss_count: miss.length,
    ttft_hit_p50: percentile(hit.map(ttftOf), 50), ttft_miss_p50: percentile(miss.map(ttftOf), 50),
    cold_after_warmup: coldAfterWarmup, warmup_denominator: warmupDenominator,
    pred_count: pred.length,
    pred_mae_tokens: errs.length ? mean(errs) : null,
    pred_within_block: errs.length ? errs.filter((e) => e <= BLOCK).length / errs.length : null,
  };
}

// Per-policy summary from the store's request records (router-measured values).
export function summarize(reqs, meta = null) {
  const done = reqs.filter((r) => r.server.state === "completed");
  const failed = reqs.filter((r) => r.server.state === "failed" || r.server.state === "rejected" || (r.client && r.client.status === "error")).length;
  const ttft = done.map((r) => r.server.ttft_ms);
  const withUsage = done.filter((r) => r.server.cached_tokens !== null && r.server.prompt_tokens);
  const promptTok = withUsage.reduce((a, r) => a + r.server.prompt_tokens, 0);
  const cachedTok = withUsage.reduce((a, r) => a + r.server.cached_tokens, 0);
  const perReplica = {};
  for (const r of done) if (r.server.served !== null) perReplica[r.server.served] = (perReplica[r.server.served] || 0) + 1;
  const counts = Object.values(perReplica);
  return {
    requests: reqs.length, completed: done.length, failed,
    ttft_p50: percentile(ttft, 50), ttft_p95: percentile(ttft, 95), itl_mean: mean(done.map((r) => r.server.itl_mean_ms)),
    cache_reuse: promptTok ? cachedTok / promptTok : null,
    avg_prompt_tokens: withUsage.length ? promptTok / withUsage.length : null,
    per_replica: perReplica,
    spread: counts.length ? Math.max(...counts) / (counts.reduce((a, b) => a + b, 0) / counts.length) : null,
    ...mechanismMetrics(withUsage, meta),
  };
}

// A plain-language reading built ONLY from the measured numbers. It states a ranking, never a reason the data cannot show.
export function verdict(results) {
  const names = POLICY_ORDER.filter((p) => results[p] && results[p].ttft_p50 !== null);
  if (names.length < 2) return null;
  const by = (k, dir) => names.slice().sort((a, b) => dir * (results[a][k] - results[b][k]));
  const bestTtft = by("ttft_p50", 1)[0], worstTtft = by("ttft_p50", -1)[0];
  const withCache = names.filter((p) => results[p].cache_reuse !== null);
  const bestCache = withCache.length ? withCache.slice().sort((a, b) => results[b].cache_reuse - results[a].cache_reuse)[0] : null;
  return {
    bestTtft, worstTtft, ttftRatio: results[worstTtft].ttft_p50 / results[bestTtft].ttft_p50,
    bestCache, bestCacheValue: bestCache ? results[bestCache].cache_reuse : null,
    p95Best: by("ttft_p95", 1)[0],
  };
}
