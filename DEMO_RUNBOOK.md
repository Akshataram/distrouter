# Demo runbook: running SwiftServe on 4 free Colab T4s

Two ways to run the demo. **Do the dry run first** — it needs no GPU, takes
two minutes, and proves your laptop side works before you start burning
Colab session time.

Architecture reminder, because it drives everything below: **the router is
CPU-only and runs on your laptop.** Only the vLLM replicas need GPUs. The
laptop reaches them over public tunnel URLs.

```
your laptop                     internet              4 × Colab (one per Google account)
┌──────────────────┐                              ┌──────────────────────────────┐
│ SwiftServe       │ ──── https tunnel ────────▶  │ sidecar → vLLM → Qwen on T4  │
│ scripts/demo.py  │                              └──────────────────────────────┘
└──────────────────┘                              (×4)
```

---

## 0. Dry run (no GPU, 2 minutes) — always do this first

```bash
pip install -r requirements.txt -r requirements-dev.txt
python scripts/demo.py
```

This spawns 3 fake vLLM replicas locally and compares `least_connections`
against `prefix_aware` on a shared-system-prompt workload. Expected shape
of the output:

```
metric                           least_connections            prefix_aware
TTFT p50                                     289ms                   193ms
true cache ratio                             52.6%                   76.9%
prediction error                             52.6%                    0.0%
goodput (TTFT+TPOT SLO)                      66.7%                   83.3%
max:mean per replica                          1.50                    2.25
```

**What to notice, including the part that is not flattering:** TTFT and
cache ratio improve, and `max:mean per replica` gets *worse* (2.25 vs 1.50).
That is the real cost of cache-aware routing — it deliberately concentrates
load to reuse caches. Say this out loud in the presentation; it is the
tradeoff the whole literature is about, and showing you measured it is
stronger than hiding it.

The fake replicas implement **real block-level prefix caching** (same
chained-hash construction as vLLM) but have **no model and a linear timing
model**. The output labels itself SIMULATED. Use it as a rehearsal and as a
backup if Colab dies mid-presentation — never as a result.

---

## 1. Per-Colab-account setup (repeat on all 4)

Open `notebooks/gpu_node.ipynb` in each account. Before running, set in
cell 2:

```python
MODEL = "Qwen/Qwen2.5-3B-Instruct"
REPLICA_TAG = "replica-a"     # a / b / c / d — DIFFERENT PER ACCOUNT
```

### The one flag that decides whether your demo shows anything

A T4 at `--gpu-memory-utilization 0.90` holds roughly **200k tokens of KV
cache ≈ 100 distinct 2000-token system prompts**. The demo workload has
**6**. So by default *nothing ever evicts*, every policy gets a cache hit,
and **all policies tie** — you would be demoing nothing.

Fix it by capping the cache with `--num-gpu-blocks-override`. With
`--block-size 16`, a workload of `num_apps × system_prompt_tokens` tokens
needs `tokens / 16` blocks to hold its whole working set:

| Workload | Working set | 0.25× (heavy pressure) | 0.5× (recommended) | 1.0× (no pressure) |
|---|---|---|---|---|
| 6 apps × 2000 tok | 750 blocks | `187` | **`375`** | `750` |
| 8 apps × 2000 tok | 1000 blocks | `250` | `500` | `1000` |
| 6 apps × 3000 tok | 1125 blocks | `281` | `562` | `1125` |

Start at **0.5× (375)**. Sweeping this ratio is also the strongest figure in
your evaluation — cache-aware routing should show no gain at 1.0×, maximum
gain around 0.25–0.5×, and gain collapsing again under thrashing.

So the vLLM launch line in the notebook becomes:

```
--model Qwen/Qwen2.5-3B-Instruct --port 8000 \
--enable-prefix-caching --enable-prompt-tokens-details \
--block-size 16 --max-num-seqs 32 --max-model-len 8192 --dtype half \
--num-gpu-blocks-override 375 \
--gpu-memory-utilization 0.90
```

- `--enable-prompt-tokens-details` is **mandatory** — without it vLLM never
  reports `usage.prompt_tokens_details.cached_tokens`, so `true cache ratio`
  silently reads 0% and the demo shows nothing.
- `--block-size 16` **must equal** the router's `SWIFTSERVE_BLOCK_SIZE`. If
  they differ, the router's hashes never match the engine's and every
  prediction becomes a miss.

Run every cell. The last one prints a public URL and an admin token. **Copy
both.** Check the startup log for the real block count:

```
# GPU blocks: 375
```

If that number is not what you set, the flag did not take — fix it before
continuing, or your cache-pressure story is wrong.

---

## 2. Point the router at the four nodes (on your laptop)

```bash
export SWIFTSERVE_REPLICAS=https://a.trycloudflare.com,https://b.trycloudflare.com,https://c.trycloudflare.com,https://d.trycloudflare.com
export SWIFTSERVE_MODEL=Qwen/Qwen2.5-3B-Instruct
export SWIFTSERVE_TOKENIZER=Qwen/Qwen2.5-3B-Instruct   # needs: pip install transformers
export SWIFTSERVE_BLOCK_SIZE=16
```

`SWIFTSERVE_TOKENIZER` matters more than it looks. Unset, the router falls
back to a byte-chunk tokenizer whose block boundaries **do not match the
engine's**, so its cache predictions are systematically wrong. The router
prints a loud warning when it is in that mode — if you see that warning
during the real demo, stop and install `transformers`.

Smoke-test one replica before the full run:

```bash
curl -sf https://a.trycloudflare.com/health && echo OK
```

---

## 3. Run the real demo

```bash
python scripts/demo.py \
  --replicas "$SWIFTSERVE_REPLICAS" \
  --model Qwen/Qwen2.5-3B-Instruct \
  --tokenizer Qwen/Qwen2.5-3B-Instruct \
  --policies least_connections,prefix_aware \
  --num-apps 6 --sessions 24 --turns 1 \
  --system-prompt-tokens 2000 --max-tokens 16
```

The header will now say **REAL vLLM on real GPUs**. Everything in the table
is measured: TTFT from a real SSE stream, `cached_tokens` from vLLM's own
usage report.

Add `--policies round_robin,least_connections,swiftserve,prefix_aware` for
the full ablation ladder. `swiftserve` is the message-level version — it
should land between the baselines and `prefix_aware`, which is itself a
result worth showing (message granularity cannot express how many *blocks*
are shared).

### Showing the mechanism directly, not just the summary

For a single-request demonstration that lands harder than any table:

```bash
curl -s -D- -o/dev/null http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'X-Session-Id: demo-1' \
  -d '{"model":"Qwen/Qwen2.5-3B-Instruct","messages":[...],"max_tokens":16}' \
  | grep -i swiftserve
```

```
X-SwiftServe-Replica: 0
X-SwiftServe-Predicted-Cached-Tokens: 2048
X-SwiftServe-Cache-Hit: true
```

Send the same prompt from a *different* `X-Session-Id` and it still lands on
replica 0 with a non-zero prediction — that is cross-session prefix routing
working, which per-session affinity alone cannot do.

And `GET /status` shows the live directory:

```json
{ "policy": "prefix_aware",
  "tokenizer": "hf:Qwen/Qwen2.5-3B-Instruct",
  "prefix_index": {"block_size": 16, "distinct_blocks": 743,
                   "blocks_per_replica": {"0": 375, "1": 248, "2": 120}} }
```

---

## 4. Order to present in

1. **The problem** — prefill is expensive, a cache hit skips it, so the win
   shows up in TTFT and is invisible in end-to-end latency.
2. **The mechanism** — messages → chat template → flat tokens → 16-token
   blocks → chained hashes. Prefix-only, full blocks only.
3. **The router's job** — it holds no cache; it predicts *where* the cache
   is and routes there. Show `/status` and the prediction header.
4. **The single-request contrast** — cold vs warm, two curl calls.
5. **The table** — `scripts/demo.py` output, including the worse load spread.
6. **The honesty slide** — what is measured vs predicted, and the
   prediction-error number. This is the strongest slide you have; most
   projects at this level cannot quantify how wrong their own heuristic is.

---

## 5. When Colab breaks (it will)

| Symptom | Cause | Fix |
|---|---|---|
| Node disconnects mid-run | 90-minute idle timeout — background compute does **not** count as interaction | Keep the browser tab visibly active; design runs under 90 min |
| Node dies after hours | 12-hour hard session cap | Nothing to do; restart that account's notebook |
| `true cache ratio` is 0% | `--enable-prompt-tokens-details` missing | Add the flag, relaunch vLLM |
| All policies tie | Cache too big to ever evict | Lower `--num-gpu-blocks-override` (see §1) |
| Predictions always wrong | Router/engine `block-size` mismatch, or `SWIFTSERVE_TOKENIZER` unset | Match both; check the router's startup warning |
| CUDA OOM at launch | 3B + 0.90 util too tight | Drop to `Qwen/Qwen2.5-1.5B-Instruct`, or `--gpu-memory-utilization 0.85` |
| Tunnel URL changes | Quick tunnels are ephemeral | Re-copy the URL; re-export `SWIFTSERVE_REPLICAS` |
| One replica unreachable | Tunnel or node died | The router circuit-breaks it and routes around — **this is worth demoing on purpose** |

**Static membership caveat:** `SWIFTSERVE_REPLICAS` is read once at router
startup, so a node that dies cannot be replaced without restarting the
router. For a short demo that is fine. It is also W4 in
`RESEARCH_PLAN_V2.md` and the first thing to fix for multi-hour runs.

### Presentation insurance

Run the dry run once and **save the output**. If Colab collapses five
minutes before you present, you still have a working demo and a clearly
labelled simulated table. Don't present simulated numbers as real — but
having them beats having nothing.

---

## 6. What you can and cannot claim

**Can:** real vLLM, real Qwen weights, real T4 GPUs, real prefix caching,
real wall-clock TTFT off a real token stream, and `cached_tokens` straight
from the engine's own usage report. The routing decisions, the block-hash
index, and the tokenization are a real implementation, not a mock.

**Cannot:** that the router *knows* what the engine has cached. It holds an
optimistic belief (`insert()` records "I routed it there, so it is probably
warm") — the mode SGLang and Dynamo call *approximate*. vLLM may have
evicted since. You measure that error rather than assuming it away, which
is why `prediction error` is in the table.

Also don't claim scale conclusions from 4 nodes. What this testbed
genuinely supports is the *prefix-locality* and *wide-area/churn* story —
see `RESEARCH_PLAN_V2.md` §2 and §9.
