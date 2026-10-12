# Live demo runbook: the implementation, running

What you show is the real system: the router running on your laptop, four Colab T4 nodes behind tunnels, and the web console. Total about 12 minutes. Explain the policies as they run; the policy lines below say when.

---

## A. Before the session (do this 20 to 30 minutes early)

### A1. Start the four Colab nodes
Run each node's notebook cells in order (clone, vLLM, sidecar, tunnel). Each ends with a keep-alive cell that prints a URL. **Leave all four running.** Copy the four URLs.

Quick tunnel URLs change every time a node restarts, so always copy fresh ones.

### A2. Update the code on your laptop
```bash
cd distrouter
git pull
pip3 install -r requirements.txt -r requirements-dev.txt transformers
```

### A3. Check the four URLs before you start anything
Replace A, B, C, D with your four URLs (no trailing slash):
```bash
python3 scripts/dev.py --check-only \
  --replicas "https://A.trycloudflare.com,https://B.trycloudflare.com,https://C.trycloudflare.com,https://D.trycloudflare.com" \
  --model Qwen/Qwen2.5-3B-Instruct
```
All four lines must say OK. If one fails, it names the reason (tunnel gone means copy that node's new URL; tunnel up but nothing behind it means that node's vLLM cell is not running).

### A4. Run the router (this is the command you will run live)
```bash
python3 scripts/dev.py \
  --replicas "https://A.trycloudflare.com,https://B.trycloudflare.com,https://C.trycloudflare.com,https://D.trycloudflare.com" \
  --policy prefix_aware \
  --model Qwen/Qwen2.5-3B-Instruct --tokenizer Qwen/Qwen2.5-3B-Instruct \
  --max-num-seqs 4 \
  --labels "colab-a T4,colab-b T4,colab-c T4,colab-d T4"
```
Leave it running. Open `http://127.0.0.1:8000/ui/` and hard-refresh (Cmd+Shift+R).

### A5. Warm-up and backup (important)
1. In the **Playground**, send one short request so the first-connection delay is out of the way.
2. Do **one full run** in the **Compare** tab (defaults) before the session, and take a screenshot of the finished table. This is your backup if a tunnel dies on the day.
3. Optional: in a second terminal, `tail -f dev_logs/router.log` (the router's log, which shows policy switches).
4. Close other tabs. Zoom the browser to 110 to 125 percent so the back row can read it.

### A6. Optional proof the code is tested (30 seconds, can be done live)
In a second terminal:
```bash
python3 -m pytest -q
```
Expect `371 passed, 1 skipped`. The tests use mocked or fake replicas; say so.

---

## B. The demo, step by step (about 12 minutes)

### Step 1: The launch and what it validated (1 min)
**Show:** the terminal where you started `scripts/dev.py` (scroll up to the start-up lines).
**Say:** "One command starts the router and the console. Before it starts anything it checks each replica: whether the tunnel is alive, whether the model name matches, and it warns about settings that would make predictions meaningless, such as a missing tokenizer. It never starts a model itself. The router is a FastAPI application, and the replicas are four Colab T4 GPUs running vLLM behind Cloudflare tunnels."

### Step 2: Dashboard, the real GPUs (1.5 min)
**Show:** the **Dashboard** tab.
**Point at:** the four replica cards, each with GPU name ("Tesla T4, reported by the node"), VRAM, temperature, healthy badge and circuit state.
**Say:** "These numbers come from `nvidia-smi` on each node, through a small sidecar, not from my labels. The router also tracks each replica's in-flight requests and a learned latency average, and a circuit breaker per replica: five consecutive failures open it, and after a cooldown a single probe request tests it."

### Step 3: Playground, one request twice (3 min)
**Do:**
1. Open the **Load test** tab, click **Insert sample ~1500-token system prompt**, select all in the box (Cmd+A) and copy (Cmd+C).
2. Open the **Playground**. Paste into **System prompt**. Set **Prompt** to `reset my password`, `max_tokens` 24. Click **Send**.
3. When it finishes, change **only** the Prompt to `where is my invoice`. Click **Send** again.

**Show after each request:** the **Result** panel: **Served by**, **Why**, **TTFT**, **ITL**, **Reused from cache**, **Router predicted**. Click **All details** once to show selected, dispatched and served.

**Say after the first request:** "This is a cold request. The reused-from-cache number is near zero and time to first token includes reading the whole 1,500-token prompt."

**Say after the second request:** "Same system prompt, different question. The router hashed the prompt into 16-token blocks, found which replica holds the leading blocks, and sent it back there. The Why line says which path it took. Reused-from-cache is what vLLM itself reported, and next to it is what the router predicted before sending, so you can see how close its belief was."

**POLICY EXPLANATION (prefix_aware), 1 min:** "Prefix-aware tokenizes with the model's chat template, cuts the prompt into 16-token blocks, and chains the hashes so one hash stands for the whole prefix. It looks up which replica holds the longest run of leading blocks, requires the match to be at least half the prompt or 256 tokens, then checks that replica's latency estimate against the response-time target. If it passes, the request goes there. Otherwise the least-loaded replica is used."

### Step 4: Live routing (1 min)
**Do:** this happens during Step 5. Start the Compare run, then click the **Live routing** tab. The run keeps going while you look at other tabs. (Or start a **Load test** of 40 requests with the sample system prompt and watch the same tab.)
**Show:** the diagram with dots moving from client to router to replicas, and the **Recent routing decisions** table.
**Say:** "Each dot is a real request, moved by the router's own events. The table shows, per request, the policy path and which replica."

### Step 5: Compare, the four policies on identical traffic (5 min)
**Do:**
1. Open the **Compare** tab. For a shorter live run set **Requests per policy** to `30` (keep the other defaults: concurrency 8, tenants 6, system prompt tokens 1500, max_tokens 16, seed 1).
2. Click **Run comparison**.
3. While it runs, explain the next four lines, one per policy as the status line shows each one running.

**Say before you start:** "This sends the same seeded workload through each policy in turn: six different companies, each with its own 1,500-token system prompt, many questions. Each policy gets its own prompt namespace, so none can reuse blocks another policy warmed, and the router's learned latency data is cleared on every switch so every policy starts from the same knowledge."

**While round_robin runs:** "Round robin takes replicas in turn. It spreads load perfectly evenly but never looks at the prompt, so whether a request meets a warm cache is luck."

**While least_connections runs:** "Least connections picks the shortest queue. It adapts to load, but it still does not know which replica holds the cache."

**While swiftserve runs:** "Swiftserve is the earlier policy. It prefers the replica that served this session, then consults a trie over whole messages, then falls back to least loaded. Its weakness is that it matches on messages, not on the token blocks the engine caches."

**While prefix_aware runs:** "Prefix-aware matches on 16-token blocks and checks the response-time target before following the cache."

**When it says "Finished.":** read the table.
- **TTFT median** and **95th percentile**
- **Prompt reused from cache**
- **Load balance (busiest ÷ average)**: 1.00 is perfectly even

**Say:** "In the three full runs I did, prefix-aware had the lowest median time to first token every time, about 450 ms against 770 to 990 for the others, and the highest cache reuse, about 86 percent against 52 to 66. It also had the most uneven load, because it sends a tenant back to the replica holding its cache. Its tail latency was not consistently better, so I make no claim about the tail. These live numbers come from a shorter run, so they will differ in detail." *(If the live result differs from this, say so and show your backup screenshot.)*

### Step 6: Show where it lives in the code (1.5 min)
Open the repository in your editor and jump to these, saying one sentence each:
- `swiftserve/policy.py`, `class PrefixAwarePolicy` (line 181), `select_with_prediction` (line 253): "This is the decision I described."
- `swiftserve/prefix_index.py`, `block_hashes` (line 57): "The chained hashes."
- `swiftserve/state.py`, `estimate_latency_ms` (line 177): "The response-time check."
- `swiftserve/app.py`, `chat_completions` (line 312): "The request path: admission, snapshot, policy, proxy."
- `swiftserve/app.py`, `switch_policy` (line 219): "How Compare swaps policies: off by default, refused while requests are in flight."

### Step 7: Close (30 s)
**Say:** "Everything you saw was real: four T4 GPUs, one router, measured by the router itself. What I did not verify: failover between replicas, which is not implemented, and why prefix-aware's tail latency varied between runs."

---

## C. If something goes wrong

| What you see | Cause | Do this |
|---|---|---|
| `check-only` says tunnel gone / 530 | That node restarted and its URL changed | Copy the node's new URL and re-run |
| `check-only` says tunnel up but nothing behind it | That node's vLLM cell stopped | Re-run the node's vLLM and sidecar cells, wait for health |
| Page stuck on "events: connecting" or "reconnecting" | Router stopped, or page open from before a restart | Check the terminal, restart the router, hard-refresh |
| Compare shows a yellow banner about switching | Router was not started with `scripts/dev.py` | Restart it with the command in A4 |
| Compare stops with a "requests in flight" message | Another tab is still sending requests | Wait a few seconds and run again |
| Playground request fails with a model error | Model name does not match what the nodes serve | Use `Qwen/Qwen2.5-3B-Instruct` exactly |
| First request is slow, then normal | Tunnel and connection warm-up | Send a throwaway request in A5 |
| The live Compare result does not match your earlier runs | Shorter run, shared GPUs, tunnel noise | Say so, and show the backup screenshot from A5 |
| A node dies in the middle of the demo | Colab session ended | Show the backup screenshot and the code; do not retry live |

## D. What to say about the numbers if asked

- The measurements are from three runs (seeds 1, 2, 3; one in reverse policy order) on four Colab T4 GPUs, 60 requests per policy, 6 tenants, 1,500-token system prompts, concurrency 8.
- Run 1 values were read from a screenshot (rounded); runs 2 and 3 are exported files kept in `docs/presentation/runs/`.
- The policies run one after another on shared GPUs, so this is a controlled demonstration, not a publishable benchmark.
