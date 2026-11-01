# 10-minute implementation demo

Show the real system: router on your laptop, four Colab T4 nodes behind tunnels, the web console. **Show** is what is on screen, **Say** is what you say in your own words.

## Before you start (20 minutes early)

1. All four Colab nodes running (tunnel cell showing a URL, left running). Copy the four URLs.
2. `git pull`, then run `scripts/dev.py --check-only` with the four URLs. All four must say OK.
3. Start the router (command in `LIVE_DEMO_RUNBOOK.md`, section A4, with `--index-max-blocks 375`). Open `http://127.0.0.1:8000/ui/` and hard-refresh.
4. Send one short request in the Playground (warm-up).
5. **Do one full Compare run beforehand and screenshot the finished table.** This is your backup if a tunnel dies.
6. Browser zoom 110 to 125 percent. Close other tabs.

---

## 0:00 to 1:00 | The point, and the launch

**Show:** the terminal where `dev.py` is running, scrolled to the start-up lines.

**Say:** "Normal load balancers spread requests evenly, but each GPU keeps a cache of the prompt prefixes it already processed. A request sent to the wrong GPU redoes that work, which makes the first word slow. My router reads the prompt, finds the GPU that already holds its beginning, and sends it there unless that GPU is too slow. One command starts it. Before starting it checks each GPU: tunnel alive, model name matches, tokenizer set. It never starts a model itself."

## 1:00 to 2:00 | Dashboard: the real GPUs

**Show:** the Dashboard tab.

**Say:** "Four cards, four real Tesla T4s on Google Colab behind Cloudflare tunnels. The GPU name, memory and temperature come from nvidia-smi on each node through a small sidecar, not from my labels. 'In flight' is requests the router sent that haven't finished. The router also keeps a circuit breaker per GPU: five failures in a row stop traffic to it, then one probe request tests it. The counters are cumulative since the router started; the latency numbers are the last 500 requests per GPU."

## 2:00 to 4:30 | Playground: one request, cold then warm

**Do:** Load test tab, click **Insert sample ~1500-token system prompt**, select all, copy. Playground: paste into **System prompt**, set Prompt to `reset my password`, `max_tokens` 24, **Send**. Then change only the Prompt to `where is my invoice` and **Send** again. Open **All details**.

**Say after the first:** "Cold request. The router had never seen this prompt, so nothing is reused and the time to first token includes reading about 1,500 tokens."

**Say after the second:** "Same system prompt, different question. The router sent it back to the GPU that holds the start of the prompt. 'Reused from cache' is vLLM's own count of prompt tokens it did not recompute. Next to it is what my router predicted before sending."

**Say, pointing at the Why line (about 60 seconds):** "Prefix-aware tokenizes the chat with the model's own template and cuts it into blocks of 16 tokens, the unit vLLM caches. Each block's hash includes the previous block's hash, so one hash stands for the whole prefix so far. The router keeps a map from each hash to the GPUs believed to hold it, finds the GPU with the longest run of matching blocks, and accepts it only if it covers at least half the prompt or 256 tokens. Then it checks that GPU's estimated latency against a response-time target of 3,000 ms. If it is too slow, the match is ignored and the least busy GPU is used. It's a belief, not a read of the real cache, which is why the console shows prediction next to vLLM's number."

## 4:30 to 5:15 | The metrics, and where they come from

**Say:** "All measured by the router with a stopwatch while the answer streams back. TTFT is arrival to first piece of text: reading the prompt plus any queueing, which caching shortens. ITL is the average gap between tokens after that; caching does not change it. Cache reuse is cached tokens divided by prompt tokens from vLLM's usage report. The median is the typical request; the 95th percentile is the slow tail."

## 5:15 to 8:15 | Compare: four policies on identical traffic

**Do:** Compare tab. Set **Requests per policy** to `30` (keep concurrency 8, tenants 6, system prompt 1500, max_tokens 16, seed 1). Click **Run comparison**. While it runs, open **Live routing** and narrate.

**Say before clicking:** "The same seeded set of requests goes through each policy, one after another: six companies, each with its own 1,500-token system prompt. Each policy gets its own prompt namespace, so none can reuse cache another policy created, and the router forgets what it learned about each GPU between policies."

**While it runs (Live routing):** "Each dot is a real request moved by the router's own events. The table shows the path the policy took and why. Round robin rotates and never looks at the prompt. Least connections picks the shortest queue. Swiftserve is my earlier policy: session affinity, then a tree over whole messages, which needs two matching messages, then least loaded. Prefix-aware matches on 16-token blocks."

## 8:15 to 9:15 | Read the table, honestly

**Show:** the finished table. Point at the rows under **What the cache saved**.

**Say:** "Here is what I measured on four real GPUs. In three full runs, prefix-aware had the lowest median first-token time, about 450 ms against 765 for swiftserve and 966 to 993 for the baselines, and the highest cache reuse, about 86 percent against 52 to 66. In a fourth run it lost on median latency while still having the highest cache reuse, and I haven't explained why. So I present this as a controlled demonstration, not a benchmark.

The rows under the main table show the mechanism. Requests that hit the cache are fast for every policy and requests that miss are slow for every policy. What differs is how many requests miss a cache that already existed, and how many prompt tokens each policy made the GPUs recompute. The costs: prefix-aware loads the GPUs less evenly, and I make no claim about its slowest requests." (If the live result differs from your earlier runs, say so and show the backup screenshot.)

## 9:15 to 10:00 | Where it lives, and the limits

**Show:** your editor, one sentence per file.

**Say:** "`policy.py`: each policy is a class with a `select` method; prefix-aware's decision is `select_with_prediction`. `prefix_index.py`: the chained hashes and the hash-to-GPUs map. `state.py`: the learned latency estimate behind the response-time check. `app.py`: the request path, admission, snapshot, policy, proxy.

What I verified: real routing across four real GPUs, the cache effect on the nodes, one request where the prediction matched vLLM's report, and four comparison runs. What I did not: failover to another GPU when one fails, which isn't implemented, and why the fourth run went the other way. The 371 automated tests use fake replicas, so they prove the logic, not GPU performance."

---

## If something goes wrong

| What you see | Do this |
|---|---|
| A node's tunnel dies | Show the backup screenshot and the code; don't retry live |
| Compare says it can't switch policy | Router wasn't started with `scripts/dev.py`; restart it |
| Compare says requests in flight | Wait a few seconds; stop other tabs sending |
| First request slow, then normal | Tunnel warm-up; do the throwaway request beforehand |
| Dashboard shows a card "unreachable" while it serves traffic | Say the badge is the router's health poll; routing uses the circuit breaker. Don't guess a cause. |
| Live numbers differ from earlier runs | Say so. Shorter run, shared GPUs, tunnel noise. Show the backup. |

## Do not say

- That prefix-aware always wins, or improves tail latency.
- That this is a benchmark or production-ready.
- Anything about `swiftserve_cache_hit_total` or the cache-hit header; use vLLM's `cached_tokens`.
- That failover works. It does not.
