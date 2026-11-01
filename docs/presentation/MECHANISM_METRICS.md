# Mechanism metrics (Compare tab, "What the cache saved")

These six rows sit under the main table in the Compare tab and are included in **Export JSON**. They are computed in `swiftserve/ui/js/compare_logic.js` (`mechanismMetrics`) from values the router already measures for every request: `prompt_tokens` and `cached_tokens` (vLLM's own usage report), TTFT (the router's stream timer), and the policy's `predicted_cached_tokens`. Nothing new is instrumented, and none of them is tuned to favour a policy. They report whatever happens, including when prefix_aware does not win.

## Why they exist

The headline metrics (median TTFT, p95, cache reuse) say *what* happened. These say *why*, which is the argument your project makes: **routing to the GPU that holds the prefix means fewer prompt tokens get recomputed, and requests that hit the cache are much faster than requests that miss.**

## The six rows

| Row | Definition | What it proves | Better is |
|---|---|---|---|
| **Prompt tokens computed per request** | average of (prompt tokens − cached tokens) | The GPU work the policy actually caused. The direct measure of prefill saved. | lower |
| **TTFT median, requests that hit the cache** | median TTFT of requests with ≥ 50% of the prompt cached | What a good request costs. Expected to be similar for all policies, because it is the engine's behaviour. | lower |
| **TTFT median, requests that missed it** | median TTFT of requests with < 50% cached | What a bad request costs. Also similar across policies. | lower |
| **Cold requests after each tenant's first** | among requests after the first request of each tenant, how many were cold | The first request of a tenant is cold for every policy, so it is not counted. Any later cold request is a **routing miss**: the prefix existed somewhere, and the policy sent the request elsewhere. | lower |
| **Requests per second (whole run)** | completed requests ÷ wall-clock time from the first launch to the last finish | End-to-end effect, including queueing and load imbalance. Where an uneven load would show up. | higher |
| **Router's cache prediction, average error** | mean of \|predicted − actual cached tokens\|, and the share within 16 tokens (one block) | Whether the router's belief about the cache matches vLLM's report. Only policies that predict (prefix_aware) have a value. | lower |

## How to use them to argue (and what they cannot prove)

1. Hit and miss TTFT are roughly the same for every policy (the engine does the same thing). So **the difference between policies is how many requests land on each side.**
2. "Cold requests after each tenant's first" counts how many requests landed on the miss side when they did not have to.
3. "Prompt tokens computed per request" turns that into work: fewer tokens recomputed means less GPU time on prefill.
4. "Requests per second" and the load balance row show whether that saving survived the cost of uneven load.

What these do **not** prove:
- They do not show prefix_aware is better in every run. Your fourth run lost on median TTFT while having the highest cache reuse. If that happens again, these rows show whether the extra hits were wasted by queueing (hit TTFT high on a busy replica, requests per second lower).
- "Cold after warm-up" can still count early requests that overlap in time (concurrency 8), because a tenant's first request may not have finished when its second starts. That affects every policy, so it is fine for comparing, not for an absolute claim.
- The prediction row only measures agreement with vLLM's `cached_tokens`; it does not show the index is right about what is cached at other times.
- The numbers in a simulated run come from fake replicas and mean nothing about your GPUs.

## What to say

"I added metrics that measure the mechanism, not only the outcome. Hit and miss latency are about the same for every policy, since that is the engine. What differs is how many requests miss a cache that already existed, and how many prompt tokens each policy made the GPUs recompute. Prefix-aware routing should reduce both, and the table shows whether it did in this run, along with what it cost in load balance and throughput."
