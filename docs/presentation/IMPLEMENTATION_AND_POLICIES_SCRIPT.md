# Speaker script: implementation and routing policies

For the deck `SwiftServe_Implementation_and_Policies.pptx` (16 slides, about 14 to 16 minutes at a calm pace; each slide also carries short speaker notes). Slide numbers below match that file.

Code pointers are line numbers in the current repository. Open the file at the pointer if someone asks to see the code.

---

## Slide 1: Title (10 s)
"I'll cover how each of the four routing policies chooses a replica, and then how the router around them is implemented."

## Slide 2: Four policies, each adding one idea (45 s)
"The four policies form a ladder. Round robin knows nothing. Least connections adds load awareness. Swiftserve adds memory of sessions and of whole-message prefixes. Prefix-aware moves that memory to the unit vLLM actually caches, which is blocks of 16 tokens. Each policy is a class in `swiftserve/policy.py` with one method, `select`, so they are interchangeable and can be compared on identical traffic."

Code: `policy.py` lines 57 (round robin), 78 (least connections), 91 (swiftserve), 181 (prefix-aware).

## Slide 3: round_robin (45 s)
"A single counter decides the replica: request one goes to A, two to B, three to C, four to D, five back to A. It spreads load perfectly evenly. Its busiest-to-average ratio was exactly 1.00 in all three runs. But it never looks at the prompt, so whether a request meets a warm cache is luck, and with several tenants every replica ends up holding a bit of every tenant."

## Slide 4: least_connections (45 s)
"It picks the shortest queue and breaks ties with the lowest replica number. Queue depth blends two sources: the replica's own running plus waiting requests, scraped from vLLM, and the requests the router has in flight there. If the scrape is older than five seconds it uses the router's own count. It adapts to uneven load, but it still does not know which replica holds the cache. The bars are illustrative numbers."

Code: `LeastConnectionsPolicy.select` and `ReplicaState.queue_depth` (`state.py` line 131).

## Slide 5: swiftserve (60 s)
"Swiftserve tries three things in order. First: if a replica served this session recently and its estimated latency is within the response-time target, send it back there. Second: consult a trie over whole messages. If an earlier request on some replica shared at least two messages with this one, and that replica is within target, route there. Third: fall back to the least-loaded replica, using queue depth first and estimated latency plus an optional cold-start penalty second. Every call records the messages into the trie, so it keeps learning."

Be ready for: "What is the response-time target?" It is the request's SLA, from the `X-SLA-Ms` header or the router default of 3,000 ms.

## Slide 6: why messages are the wrong unit (60 s)
"The weakness is the unit it matches on. It counts messages, but the engine reuses blocks of tokens. A 2,000-token system prompt shared by two sessions is a depth of one and is rejected. Two trivial shared messages are a depth of two and are accepted, although almost nothing can be reused. That is the finding that motivated prefix-aware.

In our experiment the same tenant and question pair does repeat, so depth-two matches do happen. That is a likely reason swiftserve reused more cache than the baselines, 66 percent against about 52 to 53. I want to be clear that this follows from reading the code. I did not check it request by request."

## Slide 7: prefix_aware, the decision (75 s)
"Step one: the prompt is tokenized with the model's own chat template and cut into 16-token blocks whose hashes are chained. Step two: the index tells us, for each replica, how many leading blocks it holds. Step three: a replica is eligible only if the match is big enough to matter, at least half the prompt or at least 256 tokens. Step four: the longest match wins, ties go to the shorter queue. Step five: if the winner's latency estimate is within the target, route there, otherwise use the least-loaded replica. Step six: record the prompt's hashes against the chosen replica straight away. That is optimistic, and it is why concurrent requests with the same prefix gather on one replica."

Code: `PrefixAwarePolicy.select_with_prediction` (`policy.py` line 253), `_eligible` (line 246).

## Slide 8: chained block hashes (60 s)
"Each block's hash is computed from the previous hash and the block's own tokens, so one hash stands for the whole prefix before it. That gives prefix-only matching for free: change the first block and every later hash changes. Only full blocks count, as in vLLM, so a 100-token prompt gives 6 blocks, not 7. The model name seeds the chain so two models never share credit. A stable hash function, blake2b, is used so a router restart keeps the same hashes. The index stores, for each hash, the set of replicas believed to hold it, and caps blocks per replica to imitate the engine's eviction."

Code: `block_hashes` (`prefix_index.py` line 57), `PrefixIndex` (line 76).

## Slide 9: the response-time check (60 s)
"Prefix-aware does not blindly follow the cache. Before routing to the best match it asks the replica's latency estimate whether it would stay within the target. The estimate treats the replica as a queue with as many servers as it can batch. Below that capacity a new request runs alongside the others and the estimate is the recent latency at that occupancy, using three occupancy buckets. Above capacity requests queue, and the estimate is service time times queue depth plus one, divided by capacity. Latencies are learned from completed requests with an exponential average. If the check fails, the decision is recorded as an SLA veto and the least-loaded replica is used. The console shows which of the three paths each request took."

Code: `ReplicaState.estimate_latency_ms` (`state.py` line 177), `record_completion_latency` (line 195).

## Slide 10: what each policy knows (30 s)
"This table summarizes the four by what each can see. Only prefix-aware looks at token blocks, and it is the only one that needs the model's tokenizer. The tokenizer has to match the model, or block boundaries will not line up with the engine's and predictions fail. Swiftserve and prefix-aware both check the response-time target."

## Slide 11: what the measurements show (45 s)
"Averaged over three real runs on four T4 GPUs, cache reuse follows how much each policy knows about the cache: about 52 to 53 percent for the two cache-blind policies, about two thirds for swiftserve and about 86 percent for prefix-aware. Prefix-aware had the lowest median time to first token in every run. The tail was not consistently better, so I make no claim about it. The full per-run table is in the results section."

## Slide 12: module map (45 s)
"The router is a FastAPI application in `app.py`. The policies are in `policy.py`, independent of the web layer. `prefix_index.py` and `tokenization.py` support the prefix-aware policy. `state.py` holds what the router knows about each replica. `proxy.py` and `resilience.py` handle streaming, retries, the circuit breaker and admission control. Telemetry, events and the dashboard API power the console, and `ui/` holds the console."

## Slide 13: the path of one request (75 s)
"The handler, `chat_completions`, reads the request and opens a trace. Admission control rejects it if too many requests are in flight. The state of every replica is read before the policy runs, so the console shows what the policy saw. The policy returns a replica, with one special case: a recovering replica can be given a probe request. The proxy sends the request, retrying connection errors on the same replica, then relays the response while a timer measures time to first token and inter-token latency. Finally the outcome is recorded exactly once, whether it completed, failed, or the client disconnected."

Code: `chat_completions` (`app.py` line 312), `forward_chat_completion` (`proxy.py` line 95), `_send_with_retry` (line 67).

## Slide 14: protection and state (45 s)
"The circuit breaker stops sending traffic to a failing replica: five consecutive failures open it, and after a cooldown that starts at 10 seconds and doubles up to 120, one probe request tests the replica. Admission control caps concurrency at 256. Retries stay on the same replica, which is a known limitation, since there is no failover. The metrics scraper reads each replica's load every two seconds, and the sidecar on each node adds GPU memory and temperature."

## Slide 15: events and the console (60 s)
"Every step of every request is published to an in-process event bus, and a trace object keeps the selected, dispatched and served replica separate, because retries can make them differ. The console subscribes over server-sent events and can resume after a reconnect. The Compare tab depends on one extra endpoint that swaps the active policy. It is off by default, the launcher turns it on only for localhost, and it refuses to switch while requests are in flight. On a switch it also clears the router's learned latency data so every policy starts from the same state."

Code: `switch_policy` (`app.py` line 219), `reset_learned_state` (`state.py` line 116).

## Slide 16: testing and verification (45 s)
"Be clear about what each kind of evidence means. The 371 automated tests use mocked or fake replicas, so they prove the router's logic, not GPU performance. The real-GPU evidence is a short list: a matching cache prediction on a test request, and three comparison runs of all four policies. Failover is not implemented, and I have not explained why prefix-aware's tail latency varied between runs."

---

## Questions to be ready for

**Why is prefix_aware better here?**
It keeps a tenant on the replica that already holds its blocks, so more of each prompt comes from cache (86% against 52 to 53% for the baselines), and a cache hit removes prefill work, which is what time to first token mostly measures. The measurements support the median and cache reuse; they do not support a claim about the tail.

**Doesn't it overload one replica?**
It can. Its busiest-to-average load was 1.5 to 1.7 across runs, against 1.0 for round robin. The response-time check exists to stop that, but it did not remove the imbalance. The tail varied across runs, which fits queueing on a busy replica, but I have not confirmed that.

**What if the router's index is wrong?**
It is a belief, not the engine's truth. Blocks are recorded optimistically when a request is routed, and vLLM may have evicted them. A failed request also leaves a stale entry. The error is measurable: the console shows the router's predicted cached tokens next to the engine's reported cached tokens. On the test request they were 16 and 16.

**How do you keep the comparison fair?**
Same seeded request sequence for every policy, a different prompt namespace per policy so no policy reuses another's cache, learned latency data cleared on each switch, and repeated runs with different seeds, one in reverse order.

**Why does swiftserve do better than the baselines on cache reuse?**
Probably because the same tenant and question pair repeats, giving it depth-two trie matches. That is an explanation from the code, not a verified one.

**What happens when a replica fails?**
Connection errors are retried on the same replica; five consecutive failures open the circuit breaker, and later a probe request tests it. There is no failover to another replica yet.

**Why not use the engine's real cache state?**
vLLM can emit cache events, which would replace the router's guess with a report from the replica. That is a next step, not implemented.
