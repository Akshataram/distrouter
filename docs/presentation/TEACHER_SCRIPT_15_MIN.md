# 15-minute implementation script (for the teacher)

About 2,100 spoken words. **Show** is what is on screen. **Say** is what you say, in your own voice. Times are cumulative.

Before you start: all four Colab nodes running, router started with `scripts/dev.py` (see `LIVE_DEMO_RUNBOOK.md`), console open at `http://127.0.0.1:8000/ui/` on the Dashboard tab, one warm-up request already sent, and a screenshot of a finished Compare run saved as a backup.

---

## 0:00 to 1:00: What I built and why

**Show:** the Dashboard tab with the four healthy GPU cards.

**Say:**
"My project is a request router for language-model serving. When several GPUs run the same model, each GPU keeps its own cache of work it has already done on a prompt. A normal load balancer ignores that cache. So a request that starts with a long prompt the cluster has already seen can land on a GPU that has to read the whole thing again, and that raises time to first token.

My router reads the prompt, works out which GPU already holds the beginning of it, and sends the request there, unless that GPU is too busy. I implemented four routing policies and compared them on four real Tesla T4 GPUs running Qwen2.5-3B through vLLM. What you see here is the live console I built to show what the router does."

---

## 1:00 to 2:30: The architecture

**Show:** the architecture diagram slide, or say it while pointing at the console.

**Say:**
"Here is how a request travels. My browser sends the request to the router, which runs on my laptop and is written in Python with FastAPI. The router chooses one of the four GPUs using the active policy. Each GPU is a Google Colab machine with no public address, so I reach it through a Cloudflare tunnel. On each machine, a small helper program called a sidecar receives the request and passes it to vLLM, the inference engine, which runs the model on the T4 and streams the answer back one word at a time, along the same path.

While the answer streams back, the router times each step. It also publishes an event for every step of every request, and the console you see draws from those events. So the console shows what the router actually did, not an animation."

---

## 2:30 to 4:00: How each GPU is set up

**Show:** the Colab notebook, scrolled to the vLLM cell.

**Say:**
"Each node is made by one notebook. It installs vLLM, starts it with prefix caching turned on, and a block size of 16 tokens, which is the unit vLLM caches in. I also turn on a flag that makes vLLM report, in every reply, how many prompt tokens it reused from cache. That number is how I measure cache reuse.

One deliberate choice: I limited each GPU's cache to 375 blocks, which is about 6,000 tokens. That is small on purpose. With six different long prompts in my experiment, one GPU cannot hold them all, so where the router sends a request genuinely matters. Then the notebook starts the sidecar and the Cloudflare tunnel, and prints the public address I give to the router.

Before using a node, the notebook runs a quick self-test. Sending the same long prompt three times, the first request took about 2,061 milliseconds to produce its first word, and the next ones about 74 milliseconds, with 1,600 of 1,617 prompt tokens reused. That is the effect my router tries to exploit."

---

## 4:00 to 5:00: Starting the router, and the Dashboard

**Show:** the terminal where the router was started, then the Dashboard.

**Say:**
"I start everything with one command. Before it starts the router it checks each GPU: whether the tunnel is alive, whether the model name matches, and it warns about settings that would make the results meaningless. Then it starts the router and the console.

On the Dashboard, each card is one GPU. The name, memory and temperature come from nvidia-smi running on that machine, not from my labels. 'In flight' is how many requests the router has sent there that haven't finished. The router also keeps a circuit breaker for each GPU: five failures in a row stop traffic to it, and after a cooldown one test request checks whether it has recovered."

---

## 5:00 to 7:30: One request, cold then cached (Playground)

**Do:** in Load test click *Insert sample ~1500-token system prompt* and copy it. In the Playground paste it as the system prompt, set the prompt to `reset my password`, set `max_tokens` to 24, click Send. Then change only the prompt to `where is my invoice` and send again. Open *All details*.

**Say, after the first request:**
"This is a cold request. The router had never seen this prompt, so nothing is reused, and the time to first token includes reading all 1,500 tokens."

**Say, after the second:**
"Same system prompt, a different question. This time the router sent it back to the GPU that already holds the prompt's beginning. 'Reused from cache' is vLLM's own count of tokens it did not recompute, and next to it is what the router predicted before sending. They should be close."

**Say, pointing at the Why line (the policy explanation, about 60 seconds):**
"This is how the policy I built, prefix-aware, decides. First it turns the chat into tokens using the model's own chat template and cuts them into blocks of 16 tokens, the same unit vLLM caches. It chains the hashes, so each block's hash includes the one before it, and a single hash stands for the whole prefix up to that block. The router keeps an index of which GPU holds which hashes. For a new request it finds the GPU holding the longest run of leading blocks. A match only counts if it covers at least half the prompt or at least 256 tokens. Then it checks that GPU's estimated latency against a response-time target of 3,000 milliseconds, which I call the SLA. If the GPU would be too slow, it ignores the match and uses the least busy GPU instead. After choosing, it records the prompt's blocks against that GPU straight away, so a burst of similar requests follows the first one."

---

## 7:30 to 8:30: The metrics, and how I get them

**Say:**
"Four numbers matter, and all of them are measured, not estimated.

Time to first token, TTFT, is how long you wait before the first word appears. The router starts a stopwatch when the request arrives and stops it at the first piece of text in the stream. It contains reading the prompt, which is what caching shortens.

Inter-token latency, ITL, is the pause between words after that. The router takes the time between the first and last word and divides by the number of gaps. I report the average, because the network delivers words in bursts, which distorts single gaps. Caching does not change ITL, because it removes work before the first word.

Total latency is arrival to end of response, on the router's clock.

Cache reuse comes from vLLM's reply. The last chunk of the stream has a usage object with the number of prompt tokens and how many were cached. Cache reuse is cached divided by prompt tokens."

---

## 8:30 to 12:30: Comparing the four policies (Compare tab)

**Do:** open Compare. Set *Requests per policy* to 30 (leave the rest at the defaults) and click *Run comparison*. While it runs, explain the policies as each one's name appears in the status line. If it is running long, stop narrating and let it finish; if a node fails, switch to the backup screenshot.

**Say, before you click:**
"This sends exactly the same set of requests through each policy, one after another: six different companies, each with its own 1,500-token system prompt. Each policy runs in its own namespace, by putting a different marker at the start of every prompt, so no policy can benefit from cache that another one created. And the router forgets what it learned about each GPU between policies, so every policy starts from the same knowledge."

**While round robin runs:**
"Round robin takes the GPUs in turn. It spreads load perfectly evenly, but it never looks at the prompt, so a cache hit is luck."

**While least connections runs:**
"Least connections picks the GPU with the shortest queue. It adapts to load, but it does not know which GPU holds the cache."

**While swiftserve runs:**
"Swiftserve is the earlier policy I built. It prefers the GPU that served the same conversation, then looks for a GPU that saw the same whole messages, then falls back to the least busy. It matches on messages, not on the token blocks the engine caches, and that is the weakness prefix-aware fixes."

**While prefix-aware runs:**
"Prefix-aware is the one I described: block hashes, longest match, response-time check."

**When it finishes, read the table:**
"In three full runs of this experiment, on four real GPUs, prefix-aware had the lowest median time to first token every time. Averaged, about 450 milliseconds against 765 for swiftserve and 966 and 993 for the two baselines. It also had the highest cache reuse, about 86 percent, against about 66 for swiftserve and 52 to 53 for the baselines. Cache reuse and latency move together, which fits the mechanism.

There are costs, and I want to be honest about them. Prefix-aware had the most uneven load, because it sends a company back to the GPU that holds its cache: the busiest GPU got about 1.6 times its fair share, against exactly 1.0 for round robin. And its tail latency, the 95th percentile, was not consistently better: it ranked fourth in one run and second in the other two, so I do not claim anything about the tail. These policies ran one after another on shared Colab GPUs, so this is a controlled demonstration, not a formal benchmark."

---

## 12:30 to 13:30: Seeing the routing live (Live routing)

**Show:** the Live routing tab. If a run is still going, you will see the dots; otherwise send a few requests.

**Say:**
"This is the router's real path. Client, then admission control, then the policy, then dispatch, then the four GPUs. Each dot is a real request. It moves only when the router publishes the matching event: received, decision made, sent to a GPU, completed. Nothing here is simulated by the page. The table below shows, for each request, which policy path it took and why, for example 'GPU 2 holds 30 of 37 prompt blocks, estimated latency within the target'. Those reasons are recorded by the policy itself at the moment it decides."

---

## 13:30 to 14:30: Where it lives in the code

**Show:** your editor, one file at a time, one sentence each.

**Say:**
"The four policies are in `policy.py`. Each is a small class with one method, `select`. Prefix-aware's decision is in `select_with_prediction`. The chained hashes and the index are in `prefix_index.py`: a dictionary from each hash to the set of GPUs holding it, plus a least-recently-used list per GPU to imitate the engine's own eviction. The latency estimate is in `state.py`: below its capacity a GPU's latency is its recent average, and above it requests queue, so the estimate grows with the queue. The request path is in `app.py`: admission, the snapshot of every GPU's state, the policy, then the proxy. Everything the console shows is built from events published by the router."

---

## 14:30 to 15:00: Limits and close

**Say:**
"What I verified: real routing across four real GPUs, the cache effect on the nodes, the router's cache prediction matching vLLM's report on a test request, and three comparison runs of all four policies. What I tested only with mocked or fake servers: the 371 automated tests, which prove the router's logic, not GPU performance. What I have not done: failover to a different GPU when one fails, which is not implemented, and I have not explained why prefix-aware's tail latency varied between runs.

So my claim is narrow: in this setup, matching prompts in blocks of tokens, and checking a response-time target before following the cache, gave the lowest median time to first token and the highest cache reuse, at the cost of a more uneven load. Thank you. I'm happy to take questions."

---

## If the teacher asks

**"Why is prefix-aware better than swiftserve?"**
"Swiftserve matches whole messages, but the engine reuses 16-token blocks. A long system prompt shared by two requests counts as one message, below swiftserve's minimum of two, so it is ignored. Prefix-aware matches in the engine's own unit, can tell how many tokens a match saves, and can pick among several GPUs that hold the same blocks."

**"How do you know the router's cache prediction is right?"**
"Next to each request the console shows what the router predicted and what vLLM reported. On my test request both were 16 tokens. The index is a belief, though: it records blocks when it routes a request, and vLLM may have evicted them since."

**"What if a GPU fails?"**
"Connection errors are retried up to two more times on the same GPU. After five failures in a row the circuit opens, and later one test request checks recovery. There is no failover of a running request to another GPU yet."

**"Why does ITL stay the same across policies?"**
"ITL is the pace of writing after the first word. Caching only removes reading work before the first word. In my runs ITL stayed in the same range for all four."

**"Is the data real?"**
"The comparison runs are on real GPUs. Run 1 values were read from a screenshot, so they are rounded; runs 2 and 3 are exported files. The automated tests use fake servers, and I say so. One more detail: clearing the router's learned GPU state between policies was added after my first run, so I only claim identical starting state for runs made with that version."
