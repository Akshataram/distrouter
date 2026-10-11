# SwiftServe v2: Cross-Domain Literature Survey, Problem Reframing, and Build Plan

**Supersedes `SWIFTSERVE_UPGRADE_PLAN.md`.** That plan was correct but narrow: it
surveyed the LLM-serving literature and prescribed catching up to it. This one
surveys *distributed systems, caching theory, queueing theory, online algorithms,
networking, and evaluation methodology* as well, and argues you should stop trying
to catch up to the LLM-serving literature at all — because on your hardware, that
literature's core assumptions are false, and the right answers come from older,
better-tested work that nobody has applied here.

Hard constraint assumed throughout: **4 free Google Colab accounts, 1× T4 (16GB)
each, connected to the router over public-internet tunnels, with 12-hour maximum
session lifetime and ~90-minute idle disconnect.**

---

## 0. The thesis, in one paragraph

Every prefix-cache-aware LLM router in the literature — SGLang Router, Preble,
NVIDIA Dynamo, llm-d, and the 2026 crop (CacheRoute, PEEK) — assumes a datacenter:
sub-millisecond RTT between router and replicas, stable non-preemptible workers,
homogeneous GPUs, and a control plane that observes replica state faster than the
data plane changes it. CacheRoute's headline result is on 60 H100s. **Your setting
violates all four assumptions simultaneously, and that is the interesting part.**
When RTT to a replica is 50–150 ms and your telemetry is 2 seconds stale, but a
routing decision must be made in microseconds and its consequences last for a
600 ms prefill, *the control plane is slower than the data plane*. That regime has
a rich literature — it is exactly the regime of wide-area web cache sharing, of
adaptive replica selection under stale load information, of failure detection over
flaky links — and none of it has been brought to LLM prefix routing. That is your
paper: **cache-aware LLM request routing when the control plane is slower than the
data plane.** Your weird free-tier hardware stops being a threat to validity and
becomes the only honest way to evaluate the claim.

---

## 1. Your problem statement, sharpened

The repo currently states the problem informally ("cache- and SLA-aware request
routing"). Here is a precise formulation worth putting at the top of the paper,
because the constraints are what make it non-trivial:

> **Given** a set of replicas $R$ that are ephemeral (each may vanish or restart at
> any time), wide-area-attached (RTT $\rho_i$ of the same order as service time),
> and effectively heterogeneous (nominally identical T4s, actually differing in
> throughput by contention and throttling), each running an **unmodified** inference
> engine with a bounded KV cache under the engine's own eviction policy;
>
> **and** a request stream $r_1, r_2, \dots$ whose prompts have substantial shared
> prefix structure, each carrying a TTFT SLO and a TPOT SLO;
>
> **choose** an online assignment $\pi: r_t \mapsto R$
>
> **using only** (a) black-box HTTP responses and (b) telemetry whose age is at
> least one RTT,
>
> **to maximize goodput** (fraction of requests meeting both SLOs),
>
> **subject to** never modifying the inference engine.

Four properties make this genuinely hard, and all four are absent from the
datacenter papers:

1. **Cache state is unobservable, only inferable.** You never learn what a replica
   holds; you infer it, and your inference decays.
2. **Telemetry is stale by construction.** Not incidentally — structurally, by at
   least one wide-area RTT. Routing on stale load is a known pathology
   ([herding](https://brooker.co.za/blog/2012/01/17/two-random.html)), not a minor
   error term.
3. **Membership churns.** Colab kills a node at 12h, or at 90 min idle, or whenever
   it wants. A restarted replica has a *cold cache but a warm-looking history*.
4. **The router's own actions create the state it later exploits.** Routing to a
   replica warms it. This is a closed feedback loop with self-induced state, which
   is why optimistic insertion works *and* why herding happens. The datacenter
   literature mostly elides this because their cache state comes from engine events.

**Naming:** consider retitling the project around the regime rather than the
mechanism. "SwiftServe" describes a router; *"Routing under a slow control plane"*
describes a contribution.

---

## 2. Why your constraint is the contribution (stop apologizing for it)

`ARCHITECTURE.md` currently lists your hardware defensively, under "Honest threats
to validity": *T4s are small, tunnel RTT varies, only 3 replicas.* Every one of
those is currently framed as a weakness. Reframe all of them:

| Current framing (defensive) | Reframe (the contribution) |
|---|---|
| "T4s are small, prefill costs are lower than on big models" | Prefill/RTT ratio ≈ 4:1 on a 3B model with 2k-token prompts — the *only* published regime where wide-area RTT is a first-class term in the routing cost model |
| "Tunnel RTT varies" | Non-stationary RTT is the independent variable; measuring policy robustness to it is a result nobody else can produce |
| "Only 3 replicas" | 4 nodes with real churn beats 60 H100s with no churn *for this question*; you can induce and observe preemption, they cannot |
| "Synthetic prompts are not real user text" | True; fix with ShareGPT (already planned) |
| Colab's 12h/90min limits are an annoyance | They are a **free, realistic, reproducible churn generator**. Nobody else has one. Preemption in SpotServe/ShuntServe is *simulated or expensive*; yours is free and real |

The ARCHITECTURE.md rewrite is itself a task (see Phase F): move these from
"limitations" to "experimental setting," and keep only the genuine limitations
(no engine modification, small model, synthetic text).

---

## 3. Literature survey

Organized by domain, with what you actually take from each. Papers already in
`SWIFTSERVE_UPGRADE_PLAN.md` §2 are not repeated except where my reading differs.

### 3.1 The direct competition (LLM prefix routing) — and where it stops

| Work | Setting | What it does | Why it doesn't cover you |
|---|---|---|---|
| **SGLang Router / RadixAttention** | Datacenter | Approximate radix tree per worker, `cache_threshold` + abs/rel balance thresholds | Assumes cheap, fresh worker state; thresholds are hand-tuned constants |
| **Preble** (ICLR 2025) | Datacenter | E2 exploit/explore, hot-prefix replication | Replication is heuristic; assumes stable workers |
| **Mooncake** (FAST 2025) | Datacenter | TTFT = queue + prefill-of-uncached; early rejection | Requires engine integration; no RTT term |
| **NVIDIA Dynamo KV Router** | Datacenter | `cost = w·prefill_blocks + decode_blocks` | Same |
| **llm-d precise prefix routing** | Kubernetes, LAN | vLLM publishes `BlockStored`/`BlockRemoved` over ZMQ; scheduler keeps exact index | **Event streams over a 12h-lifetime Cloudflare tunnel are the wrong mechanism** — see §5.4 |
| **[CacheRoute](https://arxiv.org/abs/2608.19677)** (Aug 2026) | 60× H100, fp8 70B | *Periodic routing plan*: admits high-rate keys to a stable warm set, places assignments by expected load. 176 QPS @ 3.5s p99, 2.3× best baseline; hit rate 64%→93% | **Closest competitor. Read it carefully.** Planning periodically assumes membership and capacity are stable across a planning epoch — precisely what Colab denies you. They even state the failure mode: *"when affinity recovers too little KV work, residual load skew can reduce or erase the improvement."* Your regime makes epochs invalid |
| **PEEK** (2026) | Datacenter | Incremental prefix trie over the pending queue, co-optimized with eviction | Requires queue visibility inside the engine |
| **ShuntServe** (2026) | AWS heterogeneous **spot** (L4/A10G/L40S) | Placement + weighted RR + output-preserving migration across spot interruptions | **Closest on churn.** But still LAN, still same-cloud, and it migrates by moving model/tensor state — impossible over your tunnels |
| **Helix** (ASPLOS 2025) | Heterogeneous GPUs + network | Max-flow / MILP joint placement + scheduling, network capacity as edge weights | Offline-ish optimization; your membership changes faster than you can re-solve. But the max-flow *formulation* is a good offline-bound tool — see §6.3 |

**The gap, stated precisely:** no published prefix-aware router treats *staleness of
its own cache belief* as a first-class, modeled quantity, and none targets a setting
where control-plane latency ≥ service-time variance. CacheRoute plans; you must
react. That is the axis.

### 3.2 Load balancing under stale information — *the most important section for you*

This is the literature that actually matches your problem, and none of it has been
applied to LLM prefix routing.

- **[C3: Cutting Tail Latency via Adaptive Replica Selection](https://www.usenix.org/system/files/conference/nsdi15/nsdi15-paper-suresh.pdf)** (Suresh et al., NSDI 2015) — **steal this wholesale.** Cassandra replica selection under server-side performance variability. Two mechanisms: (a) a ranking weight $W = \frac{1}{\bar{L}}(q+1)^3$ where $q$ includes *the client's own outstanding-but-unacknowledged requests*, and the cubic term deliberately over-penalizes queueing to damp herding; (b) distributed rate control with backpressure. Result: up to 3× better p99.9. Your router is structurally a C3 client and currently has neither mechanism.
- **[Tars](https://arxiv.org/abs/1702.08172)** (2017) — the follow-up showing C3's remaining weakness is *timeliness* of feedback. Directly relevant: cite it for why you age-discount telemetry.
- **[The Power of Two Choices](https://www.eecs.harvard.edu/~michaelm/postscripts/tpds2001.pdf)** (Mitzenmacher, TPDS 2001) + the stale-information result: with cached load data, d-choices **herds** — everyone piles onto the host that looked idle until the next refresh. Your 2s scrape interval + 4 replicas + bursty arrivals is a textbook herding configuration. Currently unmitigated.
- **[Join-Idle-Queue](https://www.microsoft.com/en-us/research/wp-content/uploads/2011/10/idleq.pdf)** (Lu et al., Performance Evaluation 2011) — **this is the right late-binding primitive for you, not Sparrow.** JIQ decouples discovery of idle servers from job assignment: servers announce *themselves* when they go idle; the dispatcher pops from a local idle list. **Zero communication on the request path.** Over a 100 ms RTT, Sparrow's probe-then-bind costs 2 RTTs before dispatch; JIQ costs zero. Your sidecar already exists and can push idle notifications.
- **[Sparrow](https://sigops.org/s/conferences/sosp/2013/papers/p69-ousterhout.pdf)** (SOSP 2013) — batch sampling + late binding. Cite as the canonical late-binding reference, then explain why JIQ dominates it at your RTT. *Showing you know why you didn't use the obvious one is a strength.*
- **[The Tail at Scale](https://www.barroso.org/publications/TheTailAtScale.pdf)** (Dean & Barroso, CACM 2013) — hedged requests (fire a duplicate after p95 expected latency, ~5% extra load) and tied requests (duplicates cancel each other on start). See §5.6 for the LLM-specific twist that makes this novel.

### 3.3 Distributed caching & cache-state summarization

- **[Summary Cache](https://www.researchgate.net/publication/221164155_Summary_Cache_A_Scalable_Wide-Area_Web_Cache_Sharing_Protocol)** (Fan, Cao, Almeida, Broder, SIGCOMM 1998) — **the single best idea in this document.** Wide-area web proxies share *Bloom filters* of their cache contents instead of exact contents: 25–60× fewer inter-cache messages, >50% less bandwidth. This is the 1998 answer to exactly the problem llm-d solves in 2025 with ZMQ event streams — and over a wide area, the 1998 answer is *better*. See §5.4.
- **[Network Applications of Bloom Filters](https://www.eecs.harvard.edu/~michaelm/postscripts/im2005b.pdf)** (Broder & Mitzenmacher) — counting Bloom filters for deletions; false-positive math you'll need for the error analysis.
- **[False Negative Awareness in Indicator-Based Caching](https://arxiv.org/abs/2203.09119)** — what happens when the digest is *stale* (the cache evicted something the filter still claims). Exactly your failure mode; gives you the vocabulary and the correction.
- **[RobinHood](https://www.usenix.org/system/files/osdi18-berger.pdf)** (Berger et al., OSDI 2018) — tail-latency-aware cache partitioning: reallocate cache from "cache-rich" to "cache-poor" backends to hit a p99 goal. The analogue: don't maximize aggregate hit rate, allocate *warm-prefix residency* to whichever tenant/app is currently missing its SLO. Directly answers the fairness question (§3.7) with a mechanism rather than just a metric.
- **[Learning Relaxed Belady](https://www.usenix.org/system/files/nsdi20-song.pdf)** (Song et al., NSDI 2020) — approximating Belady's MIN with ML for CDN caching, and crucially **reporting the gap to offline optimal**. LRU is 25–40% off MIN. You should report *your* optimality gap. See §6.3.
- **Cache coherence directories** (textbook: sparse directories, coarse-vector, limited-pointer schemes) — your router *is* a directory with imprecise state. A Bloom digest is literally a coarse-vector directory. Good related-work framing; shows breadth for ~one paragraph of cost.

### 3.4 Online algorithms & learning-augmented algorithms — *your theory contribution lives here*

- **Ski rental / rent-or-buy** — deterministic 2-competitive, randomized $e/(e-1) \approx 1.582$-competitive. **This is the correct formalization of prefix replication and nobody has noticed.** See §5.2. This is your headline theoretical result and it costs ~40 lines of code.
- **[Competitive Caching with Machine Learned Advice](https://www.semanticscholar.org/paper/318e577644abb60feb8263e9083b71893b045eaf)** (Lykouris & Vassilvitskii, ICML 2018) and the follow-ups ([Better and Simpler](https://arxiv.org/abs/2005.13716), [Optimal Robustness-Consistency Trade-offs](https://arxiv.org/abs/2010.11443)) — the **consistency/robustness** framework: an algorithm is *consistent* if it does well when predictions are perfect and *robust* if it retains a worst-case bound when predictions are adversarial. **Your cache-hit prediction is precisely "untrusted advice."** This gives you (a) the right language, (b) a provable degradation guarantee, and (c) an experiment: you already measure `prediction_error` from Phase 0 — now plot goodput vs prediction error and show the consistency/robustness curve. That turns a plumbing metric into a theory-backed figure.
- **[Consistent Hashing with Bounded Loads](https://arxiv.org/abs/1608.01350)** (Mirrokni, Thorup, Zadimoghaddam, SODA 2018) — already in your plan; keep. Gives the provable load cap.
- **[Rendezvous/HRW hashing](https://en.wikipedia.org/wiki/Rendezvous_hashing)** (Thaler & Ravishankar, 1998) — already in your plan; the right cold-prefix placement primitive because "next best server" is trivial, which matters when nodes churn.
- **Online facility location with predictions** ([improved bounds](https://arxiv.org/abs/2107.08277)) — if you want a second theory angle: deciding *which* replicas should host a prefix is online facility location (opening a "facility" = paying prefill).

### 3.5 Overload control & admission — replacing your fixed `max_in_flight=256`

- **[Breakwater](https://www.usenix.org/system/files/osdi20-cho.pdf)** (Cho et al., OSDI 2020) — server-driven **credit-based** admission keyed on *server-side queueing delay*, with demand speculation. Note the architectural match: credits flow to clients, so the admission decision costs no extra round trip — again the right shape for high RTT.
- **[Netflix concurrency-limits](https://github.com/Netflix/concurrency-limits)** — TCP-congestion-control-derived adaptive concurrency: `Limit = avg RPS × avg latency` (Little's Law), with `GradientLimit` using `min_RTT / current_RTT` as the congestion signal and `VegasLimit` estimating queue size from RTT. **Per-replica adaptive limits, learned online,** replacing your global constant. Philosophically identical to your existing "learn batch capacity from telemetry" — just applied to admission.
- **CoDel** (Nichols & Jacobson; deployed by Meta for request queues) — bound *sojourn time* in the router queue rather than queue length. The right control for Phase-6 late binding.

### 3.6 Failure detection & membership under churn — *mandatory for Colab*

- **[The φ Accrual Failure Detector](https://classes.cs.uchicago.edu/archive/2026/spring/23380-1/papers/hayashibara_phi.pdf)** (Hayashibara et al.) — outputs a *continuous suspicion level* $\varphi$ from the observed distribution of heartbeat inter-arrivals, instead of a binary up/down with a fixed threshold. Used in Cassandra and Akka. Your circuit breaker counts 5 consecutive failures — a fixed threshold is exactly wrong for a link whose latency distribution drifts hourly.
- **[Lifeguard](https://arxiv.org/abs/1707.00788)** (HashiCorp) — local health awareness: when *my own* process is slow, don't blame peers. Your router on a laptop with a flaky uplink will otherwise blame all four replicas at once.
- **SWIM** — gossip-based membership; cite for the dynamic-membership design.
- **[SpotServe](https://arxiv.org/abs/2311.15566)** (ASPLOS 2024) and **ShuntServe** (2026) — preemptible-instance LLM serving. Take the *framing* (preemption as a first-class event, grace-period exploitation) and explicitly reject the *mechanism* (KV/tensor migration) as infeasible over your tunnels. Rejecting it with a reason is a contribution.

### 3.7 Fairness, heterogeneity, and scheduling theory

- **VTC** (Sheng et al., OSDI 2024) and **DLPM** (2025) — fairness in LLM serving; cache-aware routing starves rare prefixes. Already in your plan.
- **[Gavel](https://arxiv.org/abs/2008.09213)** (OSDI 2020) — heterogeneity-aware scheduling via a *throughput matrix* (job × GPU-type). Your four T4s are nominally identical but empirically differ; a measured per-replica throughput scalar is the same idea and makes "heterogeneity" real rather than assumed.
- **[Pollux](https://www.usenix.org/system/files/osdi21-qiao.pdf)** (OSDI 2021) — *goodput* as the co-optimization objective. Good precedent for goodput-as-objective outside DistServe.
- **[Oort](https://www.usenix.org/system/files/osdi21-lai.pdf)** (OSDI 2021) — federated-learning client selection with a **straggler penalty factor** under unreliable, heterogeneous, disappearing clients. Your Colab nodes *are* FL clients. The utility-with-penalty formulation ports directly.
- **Harchol-Balter**, *Performance Modeling and Design of Computer Systems* — M/M/c (already used), plus SRPT/SITA size-aware scheduling: prompt length is a known size proxy, so size-aware routing (route long prefills away from the replica serving short interactive turns) is available to you and unexplored in this space.
- **[Taiji](https://tianyin.github.io/pub/taiji.pdf)** (Meta, SOSP 2019) — global user-traffic routing balancing datacenter utilization against latency, with **connection-aware routing** (route socially-connected users to the same DC to improve backend cache hit rate, −17% query load) and *stable segment assignment* to preserve locality across reassignments. This is the closest production analogue to what you're building, one layer up, and it's a strong citation because it shows the locality-vs-balance tradeoff is a real production concern, not a toy.

### 3.8 Networking

- **[Vivaldi](https://pdos.csail.mit.edu/papers/vivaldi:sigcomm/paper.pdf)** (Dabek et al., SIGCOMM 2004) — decentralized synthetic network coordinates predicting RTT with ~14% median error. If you ever run >1 router or want to predict RTT to a *newly joined* node before probing it, this is the tool. Probably optional at 4 nodes; cite in related work.
- **BBR / TCP Vegas** — the congestion-control lineage behind adaptive concurrency (§3.5).

### 3.9 Evaluation methodology — *the section that rescues your entire results chapter*

- **Interleaving / within-subject designs** ([Airbnb](https://airbnb.tech/data/beyond-a-b-test-speeding-up-airbnb-search-ranking-experimentation-through-interleaving/), [Interleaved Online Testing](https://dl.acm.org/doi/fullHtml/10.1145/3543873.3587572)): a paired test where each *unit* is its own control, reported as **10–100× more efficient** than A/B, needing ~0.5% of the runtime and 4% of the traffic to reach the same conclusion. Team-draft interleaving from IR is the canonical form.
- **[Nonstationary A/B tests](https://pubsonline.informs.org/doi/10.1287/mnsc.2022.01205)** (Management Science) — ignoring nonstationarity gives estimators with suboptimal variance *and non-vanishing bias*. Time-stratified estimators fix it.
- **Common random numbers** — classic variance reduction: same seeded workload across policies (you already have deterministic workloads from Phase 0; you're one step away).

**Why this matters enormously for you:** your current design runs policy A for 20 minutes, then policy B for 20 minutes. On Colab, node throughput drifts (contention, thermal, throttling) and nodes *disappear* on timescales shorter than your experiment. That confounds policy with time. **Bootstrap CIs do not fix confounding — they only quantify noise around a biased estimate.** A reviewer will kill the paper on this. Interleaving fixes it structurally. See §6.2.

---

## 4. Analysis of the current repo

Read: `swiftserve/{app,policy,state,prefix_trie,resilience,metrics_scraper,proxy,config}.py`,
`scripts/{benchmark,workloads,load_test,chaos_runner}.py`, 140 tests.

### 4.1 Genuinely good — keep and lean on

- **The statistics harness** (bootstrap CI + permutation test, percentile reliability flagging). Rare and correct. Needs pairing (§6.2), not replacement.
- **Phase 0 work just landed**: streaming TTFT/TPOT, `true_cache_ratio` from real `usage.prompt_tokens_details.cached_tokens`, deterministic shared-prefix workloads, `vllm:prefix_cache_hits` scraping. This was the right prerequisite and it's done.
- **The chaos sidecar + real multi-node tunnel deployment.** Under the reframe in §2 this stops being a side feature and becomes core apparatus — it's your churn generator.
- **The honesty discipline** in docstrings (distinguishing predicted from measured). Keep this; it's why the `prediction_error` metric exists at all, and it's the foundation of the consistency/robustness experiment.

### 4.2 Wrong for your setting (not merely incomplete)

| # | Issue | Where | Why it's wrong *here* specifically |
|---|---|---|---|
| W1 | **No RTT term anywhere** | `state.py`, `policy.py` | At 100 ms RTT vs ~400 ms prefill, RTT is ~20% of TTFT and *varies per replica*. A cost model without it is mis-ranking replicas |
| W2 | **Telemetry staleness unmodeled** | `state.py:queue_depth()` | `_METRICS_FRESHNESS_S = 5.0` is a binary cliff: data is "fresh" or ignored. Should be a continuous age-discount, and `in_flight` should be trusted *more* as scrape age grows |
| W3 | **No anti-herding damping** | `policy.py` | `min(queue_depth, ...)` with 2 s-stale data across 4 replicas is the textbook herding setup. C3's cubic penalty exists for this |
| W4 | **Static membership** | `config.py:replica_urls` frozen at boot | **This alone makes multi-hour Colab experiments impossible.** A node dying at the 12h cap or 90-min idle can never be replaced, and a fresh tunnel URL can never be added |
| W5 | **Restarted replica keeps warm-looking affinity** | `state.py` | Already logged as P9. On Colab this isn't an edge case, it's the common case |
| W6 | **Fixed-threshold circuit breaker** | `resilience.py` | 5 consecutive failures; wrong shape for a link with drifting latency distribution (→ φ-accrual) |
| W7 | **Global `max_in_flight=256` constant** | `config.py` | Arbitrary; should be per-replica and learned (Little's Law / gradient) |
| W8 | **`assumed_max_batch_size=1` default** | `config.py` | Logged as P8. Set floor from `--max-num-seqs 32` |
| W9 | **Stale-owner bug** | `policy.py:88` `next(...)` | Logged as P4. `next()` takes list order, not most recent |
| W10 | **Trie keyed on message text, not tokens/blocks** | `prefix_trie.py` | vLLM caches *blocks of tokens* after the chat template. Character-level message matching cannot see tokenization or block boundaries — [vLLM Router RFC #294](https://github.com/vllm-project/vllm) says this explicitly. Also `max_depth=6` messages is an arbitrary unit |
| W11 | **Trie overflow = full reset** | `prefix_trie.py:record` | Dropping all cross-session state at a size threshold; should be LRU per replica |
| W12 | **Sequential A/B benchmarking** | `scripts/benchmark.py` | Confounds policy with node drift (§3.9). The most damaging methodological issue in the repo |
| W13 | **No offline/optimal baseline** | — | "Beats round-robin" is weak. "Achieves 87% of offline optimal" is strong |
| **W14** | **The trie is dead on your own `shared_system` workload** | `policy.py:14` `_MIN_SHARED_PREFIX_MESSAGES = 2` | **Verified empirically.** Two sessions of the same app share exactly one message (the 2000-token system prompt); their user turns differ, so `longest_match` returns `depth=1`. The gate requires `>= 2`, so **cross-session routing never fires** — on the exact workload built to exercise it. The gate counts *messages*; the thing worth gating on is *shared tokens* |
| **W15** | **A prefix can only be remembered on one replica** | `prefix_trie.py:record` sets `node.last_replica = replica_id` | **Verified:** recording the same prefix on replicas 0 then 1 leaves only replica 1. Replication is literally unrepresentable, so there is no "which holders exist, pick the least loaded" choice — and §5.2's ski-rental replication has nowhere to store its result. Needs `set[replica_id]`, i.e. a directory entry |

### 4.3 The trap nobody has flagged yet: your cache may be too big to matter

Rough arithmetic for **Qwen2.5-3B-Instruct on a 16GB T4** (GQA: 2 KV heads,
head_dim 128, 36 layers, fp16):

```
KV bytes/token ≈ 2 (K,V) × 2 kv_heads × 128 head_dim × 2 bytes × 36 layers ≈ 36 KB
weights ≈ 6.2 GB  →  at --gpu-memory-utilization 0.90, KV pool ≈ 8 GB
capacity ≈ 8 GB / 36 KB ≈ 220,000 tokens ≈ 110 distinct 2k-token system prompts
```

**Verify this on your actual node** (vLLM logs `# GPU blocks:` at startup; blocks ×
`block_size` = token capacity). Computed across utilization settings:

| `--gpu-memory-utilization` | KV pool | Token capacity | ≈ 2k-token prompts held | ≈ 3k-token docs held |
|---|---|---|---|---|
| 0.90 (Phase 0 default) | 7.4 GB | 201k | **101** | 67 |
| 0.75 | 5.0 GB | 137k | 68 | 46 |
| 0.60 | 2.6 GB | 71k | **36** | 24 |
| 0.50 | 1.4 GB | 39k | 19 | 13 |
| 0.40 | — | does not fit (6.2 GB weights) | — | — |

So at the current default, with `--workload-num-apps 6` and 2k-token prompts,
**every prefix fits on every replica simultaneously and there is never an
eviction.** Cache-aware routing then wins only on *the first* request per prefix per
replica, and all policies converge again — you will have rebuilt the exact
"everything ties" problem Phase 0 was meant to fix, one level up.

This is the most likely way your next round of experiments fails, and it is
invisible until you look for it.

**Fix — and it's also a better experiment.** Make cache pressure an explicit
independent variable via the standard caching-paper figure, *hit ratio vs
cache-size/working-set ratio*:

- Control cache size directly with vLLM's `--num-gpu-blocks-override` (preferred —
  it's exact and decoupled from weight size, where `--gpu-memory-utilization` is
  not), sweeping the KV pool across e.g. 0.1×, 0.25×, 0.5×, 1×, 2× the workload's
  distinct-prefix working set. With `block_size 16`, working set $W$ tokens needs
  $W/16$ blocks — so for 6 apps × 2k tokens ($W$ = 12k, 750 blocks) the sweep is
  roughly 75 / 190 / 375 / 750 / 1500 blocks. Alternatively hold cache fixed and
  scale `--workload-num-apps` up instead; do whichever keeps the other variable
  pinned, and say which in the paper.
- Report every policy result **as a function of that ratio.** Cache-aware routing
  should show zero gain at 2× (nothing to evict), maximum gain somewhere near
  0.25–0.5×, and gain collapsing again at 0.1× (thrashing). *Producing that curve
  is a more interesting result than any single number*, and it inoculates you
  against "your gains are workload-specific."

---

## 5. The proposed design: SwiftServe-WAN

Nine changes. Each is independently ablatable (your plan's rule: every new idea is a
new policy name or a flag). Ordered roughly by value-to-effort.

### 5.1 Staleness-aware, RTT-aware replica scoring (from C3 + Tars)

Replace the current `queue_depth()` / `estimate_latency_ms()` ranking with a score
that knows how old its own information is.

```python
# swiftserve/scoring.py  (sketch)
def score(replica, ctx, now):
    age   = now - replica.metrics.last_scraped_monotonic
    # Continuous age-discount, not a 5s cliff (W2): as telemetry ages, fall back
    # to the locally-known in-flight count, which is never stale.
    trust = math.exp(-age / TAU)                       # TAU ≈ one scrape interval
    q_obs = replica.metrics.running + replica.metrics.waiting
    q     = trust * max(q_obs, replica.in_flight) + (1 - trust) * replica.in_flight
    # C3's cubic over-penalty damps herding onto whichever replica last looked idle.
    congestion = (q + 1) ** 3 / max(replica.observed_service_rate, EPS)
    u = ctx.prompt_tokens - matched_blocks(replica, ctx) * BLOCK_SIZE   # uncached
    return replica.rtt_ewma_ms + congestion * SLOT_MS + W_OVERLAP * prefill_ms(replica, u)
```

Three things here are new relative to every cited LLM router: the **RTT term**, the
**age-discounted trust blend**, and the **cubic anti-herding penalty**. All three are
forced by your setting; all three are cheap.

*Measure, don't assume:* `rtt_ewma_ms` comes free from the `/health` probes you
already issue — just time them.

### 5.2 Prefix replication as ski rental — **your headline theory result**

The recurring decision: prefix $P$ is warm on replica $A$, but $A$ is congested.

- **Rent:** queue on $A$. Cost per request: the extra queueing delay $d_t$ you accept
  to keep the hit.
- **Buy:** route to idle $B$. One-time cost: prefill of $P$ on $B$, i.e. $c_P$. But
  afterwards $P$ is warm on *both*, so all future requests for $P$ have two choices.

That is **exactly ski rental**, with "buy" = replicate. The classic result gives you
an algorithm and a proof for free:

```python
# Deterministic, 2-competitive against the offline optimal replicate-or-queue cost.
accumulated[P] += queueing_delay_paid_this_request
if accumulated[P] >= prefill_cost_estimate(P):
    replicate P (route to best non-holder), reset accumulated[P]
```

Randomizing the threshold gives $e/(e-1) \approx 1.582$-competitive. The estimates
$c_P$ and $d_t$ come from the learned prefill model (your Phase 3) — and note the
bound degrades gracefully with estimate error, which is itself a nice lemma.

**Why this is publishable:** Preble replicates hot prefixes by heuristic; CacheRoute
plans admissions periodically; Dynamo uses a tunable weight. **Nobody has given
prefix replication a competitive-ratio guarantee**, and the mapping is exact rather
than analogical. It is a small, self-contained, provable result attached to a real
system — the best kind of contribution for a project like yours. Roughly 40 lines
plus a proof sketch.

### 5.3 Prediction as untrusted advice: consistency/robustness (Lykouris–Vassilvitskii)

Frame the cache-hit prediction explicitly as *advice* and make the policy's reliance
on it a tunable $\lambda$:

- $\lambda = 1$: fully trust the prefix index (consistent — optimal when advice is perfect).
- $\lambda = 0$: pure load-balancing (robust — worst case bounded by least-loaded).
- Intermediate: provable interpolation.

Then produce the figure: **goodput vs measured `prediction_error`**, sweeping
$\lambda$. You already emit `prediction_error` (Phase 0 wired it). Add a knob that
*corrupts* predictions deliberately (drop/perturb a fraction of index entries) to
sweep the x-axis on demand rather than waiting for nature. That is a clean,
controlled, theory-matching experiment — and it directly answers "what happens when
your approximate index is wrong," which is the first question any reviewer asks.

### 5.4 Bloom-filter cache digests instead of exact KV event streams — **replaces Phase 5**

Your old plan's Phase 5 pulls vLLM `BlockStored`/`BlockRemoved` events over ZMQ
through the tunnel, with sequence numbers, gap detection, and engine→router hash
mapping. That's the llm-d design, and it's right for Kubernetes. For you it is the
wrong mechanism: unbounded event volume over a metered tunnel, sequence-gap recovery
logic, and total resync every time a Colab node restarts (which is constantly).

**Do Summary Cache instead.** Each sidecar maintains a counting Bloom filter over the
block hashes its vLLM currently holds; every $T$ ms it ships the plain bit-vector
(a few KB) to the router; the router queries it locally at routing time.

| Property | Exact events (llm-d) | **Bloom digest (Summary Cache)** |
|---|---|---|
| Bandwidth | Grows with block churn | **Constant, ~2–8 KB per interval** |
| Recovery after node restart | Sequence gap → detect → clear → rebuild | **Automatic: next digest is simply the new truth** |
| Tunnel failure mode | Missed events → silent divergence | **Self-healing: state is absolute, not incremental** |
| Error mode | Exact (when healthy) | Bounded false-positive rate $\epsilon$, tunable by bits/element |
| Staleness | Sub-ms in LAN | One digest interval — *and you can model it* |

False positives mean "router thinks the prefix is there, it isn't" → a cache miss
you predicted as a hit → exactly the quantity `prediction_error` measures. So the
Bloom parameters give you a **tunable knob on prediction quality**, which feeds §5.3's
consistency/robustness sweep. The two contributions compose.

The [false-negative-awareness](https://arxiv.org/abs/2203.09119) literature covers
staleness-induced errors and gives you the correction vocabulary.

**Three-point ablation, which is a paper-quality experiment on its own:**
approximate trie (optimistic insert, no feedback) → Bloom digest → exact events
(implement for LAN comparison only if time permits), measured by prediction error and
by goodput. *Nobody has compared these three regimes.*

### 5.5 Membership under churn: dynamic registration + φ-accrual (fixes W4, W5, W6)

Required for your hardware to work at all beyond a single short run.

1. **Sidecar self-registration.** On boot, the sidecar POSTs to the router
   (`POST /replicas/register {url, node_id, boot_id, capacity_hint}`). `boot_id` is a
   fresh UUID per process start — **the router uses a changed `boot_id` to detect a
   restart and immediately clear that replica's affinity and index entries** (this
   is W5/P9, solved cleanly and for free). Deregister on graceful shutdown; expire on
   φ-accrual suspicion.
2. **φ-accrual failure detection** replacing the fixed-count circuit breaker: maintain
   the heartbeat-interval distribution per replica, emit continuous $\varphi$, and feed
   $\varphi$ *into the routing score* rather than using a binary open/closed gate. A
   node degrading (Colab throttling) gets progressively less traffic instead of
   nothing-then-everything.
3. **Lifeguard's local health check:** if *all* replicas' $\varphi$ rise together, suspect
   your own uplink and suppress the reaction.
4. Keep the existing circuit breaker as the hard backstop and as an ablation baseline.

This turns Colab's limits from a blocker into the experiment: *"we ran 6 hours across
4 free-tier accounts during which 7 replica restarts occurred; here is goodput
through each event."* That is a figure no datacenter paper can produce.

### 5.6 Hedging = replication: the tail-latency trick that's free here

Standard hedging (Dean & Barroso): if no first token after the p95 expected TTFT,
fire a duplicate at the second-best replica, take the winner, cancel the loser.
Costs ~5% extra load to cut the tail.

**The LLM-specific observation that makes this novel:** in a prefix-caching system,
the "wasted" hedge *is not wasted* — it prefills prefix $P$ on the second replica,
i.e. **it performs exactly the replication action of §5.2.** Hedging and replication
are the same physical act, differing only in intent.

So: use the hedge both to cut the TTFT tail *and* as the ski-rental "buy." The
ski-rental threshold decides *when* to hedge; the hedge pays the "buy" cost you were
going to pay anyway. Two mechanisms, one implementation, and the usual objection to
hedging (wasted work) evaporates. I have not seen this observation anywhere in the
literature, and on a flaky WAN with straggler-prone free-tier nodes it should show a
large p99 TTFT improvement.

Guardrails: cap hedge rate (≤5–10%), never hedge when the cluster is near capacity,
and use *tied* requests (cancel-on-start) if you can get the sidecar to propagate
cancellation — otherwise cancel client-side on first token.

### 5.7 Late binding via Join-Idle-Queue, not Sparrow (replaces Phase 6 mechanism)

When all replicas are at capacity, park requests in the router and dispatch on slot
availability. Sparrow's probe-then-bind costs 2 RTTs — unaffordable at 100 ms. JIQ
inverts it: **the sidecar notifies the router when a slot frees** (it already knows,
from vLLM's `running` count), the router keeps a local idle list, and dispatch costs
zero extra round trips.

Combine with:
- **Deadline ordering** (Scorpio/EDF): priority = `arrival + ttft_slo`.
- **CoDel** on the router queue: bound sojourn time, shed when the minimum sojourn
  over a window exceeds target.
- **Predictive admission** (Mooncake): reject early when best predicted TTFT
  > `ttft_slo × reject_factor`.

Keep behind `SWIFTSERVE_LATE_BINDING=1`, default off, as your plan already says.

### 5.8 Per-replica adaptive concurrency instead of a global constant (fixes W7)

Replace `SWIFTSERVE_MAX_IN_FLIGHT=256` with a Netflix-style gradient limiter per
replica: `gradient = min_RTT / current_RTT`, shrink the limit when latency inflates,
grow it when stable; Little's Law sets the scale. On Colab, where a node's real
capacity silently changes under contention, a learned limit is simply correct where a
constant cannot be. Optionally add Breakwater-style credits so admission costs no
extra RTT.

### 5.9 Tokenizer + block-level index (keep from old Phase 2, but it's a prerequisite)

Your old Phase 2 is right and unchanged: `HFChatTokenizer` via `apply_chat_template`,
chained block hashes with `blake2b(digest_size=8)`, `block_size=16` matching the vLLM
flag, flat `dict[hash → set[replica]]` + per-replica LRU. Fixes W10/W11. **This is a
prerequisite for §5.4** (the Bloom filter is over *block hashes*), so it must come
first.

One addition: include the model name **and `cache_salt`/LoRA id** in the hash
namespace — otherwise you assert affinity the engine cannot serve.

---

## 6. Evaluation design (this is where the paper is won or lost)

### 6.1 The headline figures

1. **Goodput vs offered rate**, one line per policy (DistServe/Mooncake standard).
2. **TTFT p99 vs offered rate.**
3. **Hit-ratio vs cache-size/working-set ratio** (§4.3) — the caching-paper figure nobody in LLM routing draws.
4. **Goodput vs prediction error**, sweeping $\lambda$ — the consistency/robustness curve (§5.3).
5. **Predicted vs actual cached tokens**: trie vs Bloom digest vs exact (§5.4).
6. **Timeline through real churn**: goodput over a 6-hour run, annotated with Colab preemptions/restarts (§5.5). *Your signature figure.*
7. **Sensitivity to RTT**: inject synthetic delay per replica (your chaos sidecar already does latency injection!) and sweep 0→300 ms. Show datacenter-tuned policies degrade and yours doesn't. **This is the experiment that proves the thesis in §0**, and your existing chaos infrastructure makes it nearly free.
8. **Load imbalance** and **fairness** (p99 TTFT of least-popular vs most-popular app).
9. **Optimality gap** (§6.3).

### 6.2 Interleaved evaluation — fix W12 before running anything else

Sequential A/B on drifting hardware is confounded. Instead:

- **Per-request policy assignment.** One router process, policies as a request-level
  treatment: assign each arriving request a policy by a seeded coin, tag results with
  it. Both policies see the same node states, same minute, same network weather.
- **Paired analysis.** Convert your existing permutation test to a **paired**
  permutation test over matched time-blocks. Same code, stronger design.
- **Common random numbers.** You already generate deterministic workloads by seed —
  drive both arms from the identical seeded stream.
- **Blocked/counterbalanced ordering** where interleaving isn't possible (a policy
  affecting global state, e.g. the index): ABBA blocks of ≤5 minutes rather than one
  long A then one long B, which makes drift a within-block effect.

**Important caveat to state honestly in the paper:** interleaving cache-affinity
policies is not perfectly clean, because policies *share* the replicas' physical
caches — policy A's routing warms caches that policy B then benefits from
(interference between arms). Mitigations: (a) run interleaved for the
*load/latency* metrics where interference is small, and counterbalanced ABBA blocks
with cache flushes between for the *cache-hit* metrics; (b) report both and show they
agree; (c) partition prefix namespaces per arm (give each policy its own set of
apps/documents, via distinct `cache_salt`), which makes arms cache-independent while
sharing the same hardware and the same minute. **(c) is the cleanest and I'd lead
with it** — it's a genuinely nice methodological trick worth a paragraph of its own.

### 6.3 An offline optimal bound — the credibility multiplier

Nobody in this space reports one. Report yours:

- Log the full request trace (arrival, prefix block hashes, prompt/completion tokens).
- Offline, compute the best achievable assignment in hindsight: either (a) an ILP/
  min-cost-flow over the trace with per-replica capacity and cache-residency
  constraints (Helix's max-flow formulation is a good template, applied offline to
  routing rather than placement), or (b) a Belady-style relaxation for the
  cache-hit-ratio component alone — much cheaper and still convincing.
- Report **"% of offline optimal"** for each policy. LRB did exactly this for CDN
  caching and it's why that paper is persuasive: LRU is 25–40% off MIN, and knowing
  the gap tells you whether further work is worth it.

Even approximate bounds (b) transform your claims from "better than round-robin"
(who cares) to "captures 87% of the achievable cache benefit; the remaining 13% is
provably unreachable online" (a result).

### 6.4 Practical experiment protocol for 4 Colab accounts

- **Runs must fit in <90 minutes** (idle disconnect) and tolerate a node vanishing.
  Design every experiment as short repeated blocks, not one long run — which
  interleaving wants anyway.
- **Keep sessions alive**: the notebook must produce browser-visible activity;
  background compute alone does not count as interaction.
- **Automate re-registration** (§5.5) so a dead node rejoining requires zero manual
  steps. Without this you cannot run a 6-hour churn experiment at all.
- **Log `boot_id` + node identity with every result row** so churn is analyzable
  post-hoc rather than being unexplained noise.
- **Pin what you can, measure what you can't**: record GPU name, driver, and observed
  tokens/s per node per run; report inter-node throughput spread as data (it justifies
  the heterogeneity claim in §1).
- **Kaggle gives 2× T4 for ~30h/week** — a cheap 5th/6th node and a second *provider*,
  which strengthens the heterogeneity story if you want it.
- Budget: a full figure set is ~10–20 hours of node time; across 4 accounts × 12h/day
  that's comfortably one or two days of wall clock.

---

## 7. Revised phase plan

Ordered by value-to-effort. Phases A–D are the core; E–G are the differentiators;
H is the writeup. **Do one phase at a time, `pytest -q` green before the next**
(your existing rule, which has worked well).

| Phase | Content | Fixes | Effort | Why now |
|---|---|---|---|---|
| **A** | **Bug fixes**: stale-owner (W9), capacity floor from `--max-num-seqs` (W8), honest predicted-vs-true naming, `swiftserve_true_cached_tokens_total` counters | W8, W9 | S | Cheap, unblocks trust in everything else |
| **B** | **Dynamic membership + φ-accrual + boot_id cache reset** (§5.5) | W4, W5, W6 | M | **Without this you cannot run a multi-hour experiment.** Highest urgency for your hardware |
| **C** | **Interleaved evaluation harness + paired stats + namespace partitioning** (§6.2) | W12 | M | Do this *before* generating results you'd have to throw away |
| **D** | **Tokenizer + block-hash prefix index** (old Phase 2) | W10, W11 | M | Prerequisite for E and F |
| **E** | **Staleness/RTT-aware cost model** (§5.1) + **per-replica adaptive concurrency** (§5.8); new policy `swiftserve_wan` | W1, W2, W3, W7 | M | The core systems contribution |
| **F** | **Ski-rental replication** (§5.2) + **hedge-as-replication** (§5.6) | — | S–M | **Best novelty-per-line-of-code in this document** |
| **G** | **Bloom-digest cache state** (§5.4) + three-point ablation | — | M–L | Replaces old Phase 5; the "1998 beats 2025 over WAN" story |
| **H** | **Offline optimal bound** (§6.3) + cache-size sweep (§4.3) + RTT sweep + churn timeline | W13 | M | Turns results into claims |
| **I** | Late binding via JIQ + deadline order + CoDel (§5.7), flag-gated | — | L | Only if time remains; least essential |
| **J** | Rewrite `ARCHITECTURE.md` per §2 reframe; write the paper | — | M | — |

**If time is very short: A → B → C → D → E → F → H.** That is a complete, defensible
paper: a router designed for slow control planes, with a competitive-ratio result, an
optimality gap, and an evaluation methodology honest about ephemeral hardware. G and I
make it stronger but are not load-bearing.

**Reordering note vs. your old plan:** the old plan put evaluation last (Phase 7).
That is backwards — C must come early or every number produced before it is
confounded and has to be regenerated.

---

## 8. What NOT to build (extends the old plan's §5)

| Idea | Why not, *for you* |
|---|---|
| Prefill/decode disaggregation | Needs fast GPU-GPU KV transfer. Over tunnels between Colab accounts, transfer costs strictly more than recompute. Already correctly rejected |
| KV migration on preemption (SpotServe/ShuntServe) | Same reason. You cannot move tensor state between Colab accounts. **Cite it and reject it with the bandwidth arithmetic** — that rejection is itself a finding |
| Exact ZMQ KV event streams (llm-d) | Wrong mechanism over a metered tunnel with constant restarts — §5.4. Implement only as a LAN-only comparison point if time permits |
| Sparrow-style probe-then-bind | 2 RTTs at 100 ms RTT. Use JIQ. Explain the choice |
| A literal pointer-based radix tree | Chained block hashes are equivalent and faster; already correctly rejected |
| Vivaldi network coordinates | Overkill at 4 nodes; direct probing is exact. Related work only |
| Output-length prediction models | `max_tokens` + learned mean is enough. Already rejected |
| More chaos/resilience features | Already strong. But **do** reuse the existing latency injection for the RTT sweep (§6.1 fig. 7) — that's using what you have, not building more |
| Multi-router HA / gossip | Not the research question |

---

## 9. Honest threats to validity (the real ones)

Keep these; drop the ones reframed in §2.

- **No engine modification** means cache state is always inferred. Bloom digests
  narrow but never close the gap. State the residual error explicitly.
- **Small model (3B)**: prefill costs are real but smaller than production. The
  prefill/RTT *ratio* is the quantity that matters and you should report it directly
  so readers can extrapolate.
- **Interleaving interference**: arms share physical caches. Mitigated by namespace
  partitioning (§6.2) but not eliminated; report both analyses.
- **Synthetic prompts** for the generated workloads; mitigated by also running ShareGPT.
- **4 nodes** limits load-balancing dynamics — power-of-d effects need scale. Be
  explicit that the *staleness/churn* claims are what this testbed supports, and the
  *scale* claims are not.
- **Colab is not a controlled environment.** Someone will say this. The answer is
  §6.2: interleaving makes uncontrolled drift a within-pair effect that cancels,
  and you report the drift as measured data. Say this *before* they ask.

---

## 10. Reading order (if you only read six)

1. **C3** (NSDI 2015) — your cost model's real parent.
2. **Summary Cache** (SIGCOMM 1998) — your cache-state mechanism.
3. **CacheRoute** (arXiv 2608.19677) — your closest competitor; know it cold.
4. **Lykouris & Vassilvitskii** (ICML 2018) — the framing for imperfect predictions.
5. **The Tail at Scale** — hedging, and §5.6's twist.
6. **Join-Idle-Queue** — why your late binding differs from everyone's.

---

## 11. The ledger: every paper → exactly what you take → where it lands

Three tiers, marked honestly:

- **[CODE]** — becomes a real mechanism in the repo.
- **[FRAME]** — shapes how you justify/argue/evaluate; no code, but changes the paper.
- **[CITE]** — one sentence of related work, for breadth and to show you know the space.

### A. LLM serving / prefix routing (the field you're in)

| Paper | Its idea | **What you take** | Where | Tier |
|---|---|---|---|---|
| vLLM / PagedAttention (SOSP'23) | Block-paged KV; prefix caching reuses whole blocks via chained hash | Your index must be **blocks of tokens**, `block_size=16` matching the launch flag — not characters | `prefix_index.py`, Phase D | CODE |
| SGLang / RadixAttention (NeurIPS'24) | Radix tree per worker + `cache_threshold`, `balance_abs/rel` | The **`cache_threshold` rule** (route on match only if match_ratio ≥ 0.5) as your `prefix_affinity` ablation; the two imbalance thresholds | `policy.py`, Phase D/E | CODE |
| **Preble** (ICLR'25) | E2 exploit/explore; hot-prefix replication; Zipf shared-prompt workloads | (i) The **workload shape** — already built; (ii) the finding *gains only appear with long shared prompts* — this is why Phase 0 existed at all; (iii) hot-prefix replication, but you **replace their heuristic with ski-rental** | `scripts/workloads.py` ✅ done; §5.2 | CODE |
| **Mooncake** (FAST'25) | TTFT = queue time + prefill of uncached part; early rejection | The **TTFT decomposition equation itself**, as your cost-model skeleton | §5.1, Phase E | CODE |
| **DistServe** (OSDI'24) | Goodput = requests meeting **both** TTFT and TPOT SLOs | Goodput as the headline metric — **already shipped** as `dual_slo_attainment()` | `benchmark.py` ✅ done | CODE |
| Dynamo KV Router | `cost = w·prefill_blocks + decode_blocks` | The tunable **`overlap_weight` w** in the cost function | §5.1 `W_OVERLAP` | CODE |
| llm-d precise routing | Exact KV events; recompute hashes from `token_ids`, never copy engine hashes | The **principle** (compute your own hashes — survives vLLM version changes). You keep this even with Bloom digests. **Reject** the ZMQ transport | Phase D/G | CODE + reject |
| **CacheRoute** (2026) | Periodic routing plan; stable warm set | Your **primary competitor**. Their stated failure mode (residual load skew) motivates you; match their metric set (served KV hit rate, QPS @ p99 SLO) so numbers are comparable | §3.1, evaluation | FRAME |
| PEEK (2026) | Prefix trie over the pending queue, co-optimized with eviction | The observation that routing and eviction *should* share one signal — and that you **can't**, being engine-unmodified. State it as a limit | §9 | FRAME |
| Sarathi-Serve (OSDI'24) | Chunked prefill; long prefills degrade others' decode | The **justification for a TPOT term** in the cost model | §5.1 rationale | FRAME |
| Orca (OSDI'22) | Iteration-level (continuous) batching | Why your M/M/c model is the right shape — already used | `state.py` ✅ done | CITE |

### B. Load balancing under stale information — *biggest code impact*

| Paper | Its idea | **What you take** | Where | Tier |
|---|---|---|---|---|
| **C3** (NSDI'15) | $W=\frac{1}{\bar L}(q+1)^3$ where $q$ includes the client's own outstanding requests; cubic over-penalty damps herding | The **exact scoring formula shape** — this is your cost model's real parent | §5.1, Phase E | CODE |
| Tars (2017) | C3's residual weakness is feedback *timeliness* | Justification for the **age-discount** `trust = exp(-age/TAU)` | §5.1 | CODE |
| Mitzenmacher, Power of Two Choices (TPDS'01) | With cached/stale load, d-choices **herds** | The **diagnosis** that your `min(queue_depth)` over 2s-stale data is a textbook herding config | motivates §5.1 | FRAME |
| **Join-Idle-Queue** (2011) | Servers announce idleness; **zero** request-path communication | The **entire late-binding mechanism**, replacing Sparrow | Phase I, sidecar push | CODE |
| Sparrow (SOSP'13) | Batch sampling + late binding | Canonical citation — and you explain **why you rejected it** (2 RTTs at 100ms). Showing judgment is worth as much as the mechanism | §5.7 | FRAME |
| **Tail at Scale** (CACM'13) | Hedged requests (fire duplicate after p95, ~5% extra load); tied requests cancel on start | The hedging mechanism, trigger rule, and budget — **plus your novel twist**: under prefix caching the hedge *is* the replication | §5.6, Phase F | CODE |
| Taiji (SOSP'19) | Connection-aware routing for backend cache locality (−17% load); stable segment assignment | Production precedent that locality-vs-balance is a real operational tradeoff; "move assignments in coarse chunks" informs re-placement on churn | §3.7, Phase B | FRAME |

### C. Distributed caching

| Paper | Its idea | **What you take** | Where | Tier |
|---|---|---|---|---|
| **Summary Cache** (SIGCOMM'98) | Wide-area proxies exchange **Bloom filters** of cache contents; 25–60× fewer messages | **Your entire cache-state mechanism**, replacing llm-d event streams | Phase G | CODE |
| Broder & Mitzenmacher, Bloom survey | Counting Bloom filters (support deletion); false-positive math | Counting BF in the sidecar; the ε formula to size bits/element **and to report bounded error** | Phase G | CODE |
| False-Negative Awareness (2021) | Stale indicator → says present, actually evicted | Vocabulary + correction for digest staleness | Phase G | CODE (small) |
| RobinHood (OSDI'18) | Reallocate cache from cache-rich to cache-poor backends to hit p99 | The principle: allocate **warm-prefix residency to whoever is missing SLO**, not to maximize aggregate hits. A mechanism for your fairness result | Phase H/J | FRAME → CODE if time |
| **LRB** (NSDI'20) | Approximate Belady with ML; **report the gap to offline optimal** (LRU is 25–40% off MIN) | The **evaluation practice** of reporting an optimality gap. Credibility multiplier | Phase H | CODE |
| Cache-coherence directories (textbook) | Sparse / coarse-vector / limited-pointer imprecise directories | Framing: your router **is** a directory with imprecise state; a Bloom digest **is** a coarse-vector directory | related work | CITE |

### D. Online algorithms & learning-augmented — *your theory*

| Paper | Its idea | **What you take** | Where | Tier |
|---|---|---|---|---|
| **Ski rental** (Karlin et al.) | Rent-or-buy; 2-competitive deterministic, $e/(e-1)$ randomized | **The replication algorithm and its proof.** Your headline theoretical result | §5.2, Phase F | CODE |
| **Lykouris & Vassilvitskii** (ICML'18) | Competitive caching with ML advice; consistency vs robustness | Framing your predictor as **untrusted advice**, the λ trust knob, and the experiment (goodput vs prediction error) | §5.3, Phase E/H | CODE |
| Rohatgi (SODA'20); Wei & Zhang (NeurIPS'20) | Near-optimal / optimal robustness-consistency tradeoffs | The bound to cite for your λ interpolation | §5.3 | CITE |
| Consistent Hashing w/ Bounded Loads (SODA'18) | No server exceeds $c\times$ average; provable | The **load-bound filter**, applied *before* the SLA filter | Phase E | CODE |
| Rendezvous / HRW (1998) | Deterministic ranking of all servers per key; "next best" is trivial | **Cold-prefix placement** — first requests for a new prompt land on one replica, not all four | Phase E | CODE |
| Online facility location w/ predictions | Opening a facility = paying a fixed cost | Optional second theory framing (which replicas host a prefix) | related work | CITE |

### E. Overload control & admission

| Paper | Its idea | **What you take** | Where | Tier |
|---|---|---|---|---|
| **Netflix concurrency-limits** | `Limit = RPS × latency` (Little's Law); Gradient = `minRTT/curRTT`; Vegas | **Per-replica adaptive concurrency**, replacing `max_in_flight=256` | Phase E | CODE |
| Breakwater (OSDI'20) | Credit-based admission keyed on server queueing delay; demand speculation | Credits cost **no extra round trip** — the right shape at high RTT. Optional mechanism | Phase E/I | FRAME → CODE optional |
| CoDel | Bound **sojourn time**, not queue length | Router-queue control under late binding | Phase I | CODE |

### F. Failure detection & churn — *mandatory for Colab*

| Paper | Its idea | **What you take** | Where | Tier |
|---|---|---|---|---|
| **φ-accrual** (SRDS'04) | Continuous suspicion level from the heartbeat-interval distribution | Replaces your fixed 5-consecutive-failure breaker; **feed φ into the routing score** so a degrading node bleeds traffic gradually | Phase B | CODE |
| Lifeguard (2017) | Local health awareness — don't blame peers when *you* are slow | If all replicas' φ rise together, suspect your own uplink and suppress | Phase B | CODE (small) |
| SWIM | Gossip membership | Citation for the dynamic-membership design | Phase B | CITE |
| SpotServe (ASPLOS'24) | Preemption as first-class; grace period; KV migration by bipartite matching | The **framing** (preemption is a scheduling event, not an error). **Reject** migration with bandwidth arithmetic — the rejection is itself a finding | §8 | FRAME + reject |
| ShuntServe (2026) | Heterogeneous **spot** GPUs; output-preserving migration | Closest churn-related work; position against it (same-cloud LAN, movable tensor state) | §3.1 | FRAME |

### G. Heterogeneity, fairness, scheduling theory

| Paper | Its idea | **What you take** | Where | Tier |
|---|---|---|---|---|
| Gavel (OSDI'20) | Throughput matrix per (job, GPU type) | Measure a **per-replica throughput scalar** so "heterogeneity" is empirical, not assumed — and report the spread as data | Phase E | CODE (small) |
| **Oort** (OSDI'21) | Client selection with a **straggler penalty factor** under unreliable, heterogeneous, vanishing clients | The utility-with-penalty formulation for scoring flaky Colab nodes. Your nodes *are* FL clients | Phase B/E | CODE (small) |
| Pollux (OSDI'21) | Goodput as the co-optimization objective | Precedent for goodput-as-objective outside DistServe | related work | CITE |
| VTC (OSDI'24), DLPM (2025) | Cache-aware scheduling starves rare prefixes | The **fairness metric**: p99 TTFT of least-popular vs most-popular app under Zipf | Phase H | CODE |
| Harchol-Balter (2013) | M/M/c; SRPT/SITA size-aware scheduling | M/M/c already used. **New**: prompt length is a known size proxy → size-aware routing (keep long prefills off the replica serving short interactive turns). Unexplored in this space | optional policy | FRAME → CODE optional |

### H. Networking

| Paper | Its idea | **What you take** | Where | Tier |
|---|---|---|---|---|
| Vivaldi (SIGCOMM'04) | Synthetic coordinates predict RTT, ~14% median error | Honestly: **nothing in code** at 4 nodes — direct probing is exact and cheap. Related work only | related work | CITE |
| BBR / TCP Vegas | Congestion signals from RTT inflation | Lineage behind the adaptive concurrency limiter | §3.5 | CITE |

### I. Evaluation methodology — *rescues the results chapter*

| Paper | Its idea | **What you take** | Where | Tier |
|---|---|---|---|---|
| **Interleaving** (Airbnb; WWW'23) | Within-subject paired testing; 10–100× more efficient than A/B | **Per-request policy assignment + prefix-namespace partitioning per arm** | Phase C | CODE |
| Nonstationary A/B (Mgmt. Science) | Ignoring nonstationarity → suboptimal variance **and non-vanishing bias** | Justification + time-stratified/paired analysis | Phase C | CODE |
| Common random numbers (classic) | Same random stream across arms | Same seeded workload per arm — you're already 90% there from Phase 0 determinism | Phase C | CODE |

### Tally

| Tier | Count | Meaning |
|---|---|---|
| **CODE** | ~24 | Real mechanisms. 3 already shipped in Phase 0 |
| **FRAME** | ~12 | Change the argument, the evaluation, or what you deliberately reject |
| **CITE** | ~10 | Breadth in related work |

### Already banked (Phase 0, committed)

Three survey items are **already in the repo**: DistServe's goodput definition
(`dual_slo_attainment`), Preble's Zipf shared-system-prompt workload
(`scripts/workloads.py`), and Mooncake's use of engine-reported `cached_tokens` as
ground truth (`true_cache_ratio`, `vllm:prefix_cache_hits`).

## 12. References

**LLM serving / prefix routing**
1. Kwon et al. *Efficient Memory Management for LLM Serving with PagedAttention (vLLM).* SOSP 2023.
2. Zheng et al. *SGLang: Efficient Execution of Structured Language Model Programs.* NeurIPS 2024.
3. Srivatsa et al. *Preble: Efficient Distributed Prompt Scheduling for LLM Serving.* ICLR 2025. arXiv:2407.00023.
4. Qin et al. *Mooncake: A KVCache-centric Disaggregated Architecture for LLM Serving.* FAST 2025. arXiv:2407.00079.
5. Zhong et al. *DistServe: Disaggregating Prefill and Decoding for Goodput-optimized LLM Serving.* OSDI 2024. arXiv:2401.09670.
6. [*CacheRoute: Planned Prefix-Affinity Routing for Large-Scale LLM Serving.*](https://arxiv.org/abs/2608.19677) arXiv:2608.19677, 2026.
7. *PEEK: Predictive Queue-Informed KV Cache Management for LLM Serving.* arXiv:2607.02525, 2026.
8. [llm-d: Precise Prefix-Cache Aware Routing.](https://llm-d.ai/docs/dev/architecture/advanced/kv-management/prefix-cache-aware-routing) 2025–26.
9. NVIDIA Dynamo KV Router documentation.
10. Agrawal et al. *Sarathi-Serve: Taming the Throughput-Latency Tradeoff.* OSDI 2024.
11. Yu et al. *Orca: A Distributed Serving System for Transformer-Based Generative Models.* OSDI 2022.

**Heterogeneous / preemptible serving**
12. Miao et al. [*SpotServe: Serving Generative LLMs on Preemptible Instances.*](https://arxiv.org/abs/2311.15566) ASPLOS 2024.
13. [*ShuntServe: Cost-Efficient LLM Serving on Heterogeneous Spot GPU Clusters.*](https://arxiv.org/abs/2606.18600) arXiv:2606.18600, 2026.
14. Mei et al. [*Helix: Serving LLMs over Heterogeneous GPUs and Network via Max-Flow.*](https://arxiv.org/abs/2406.01566) ASPLOS 2025.
15. Griggs et al. [*Mélange: Cost Efficient LLM Serving by Exploiting GPU Heterogeneity.*](https://arxiv.org/html/2404.14527) 2024.

**Load balancing under stale information**
16. Suresh et al. [*C3: Cutting Tail Latency in Cloud Data Stores via Adaptive Replica Selection.*](https://www.usenix.org/system/files/conference/nsdi15/nsdi15-paper-suresh.pdf) NSDI 2015.
17. [*Tars: Timeliness-aware Adaptive Replica Selection for Key-Value Stores.*](https://arxiv.org/abs/1702.08172) 2017.
18. Mitzenmacher. [*The Power of Two Choices in Randomized Load Balancing.*](https://www.eecs.harvard.edu/~michaelm/postscripts/tpds2001.pdf) IEEE TPDS 2001.
19. Lu et al. [*Join-Idle-Queue: A Novel Load Balancing Algorithm for Dynamically Scalable Web Services.*](https://www.microsoft.com/en-us/research/wp-content/uploads/2011/10/idleq.pdf) Performance Evaluation 2011.
20. Ousterhout et al. [*Sparrow: Distributed, Low Latency Scheduling.*](https://sigops.org/s/conferences/sosp/2013/papers/p69-ousterhout.pdf) SOSP 2013.
21. Dean & Barroso. [*The Tail at Scale.*](https://www.barroso.org/publications/TheTailAtScale.pdf) CACM 2013.
22. Calder et al. [*Taiji: Managing Global User Traffic for Large-Scale Internet Services at the Edge.*](https://tianyin.github.io/pub/taiji.pdf) SOSP 2019.

**Distributed caching**
23. Fan, Cao, Almeida, Broder. *Summary Cache: A Scalable Wide-Area Web Cache Sharing Protocol.* SIGCOMM 1998 / IEEE-ACM ToN 2000.
24. Broder & Mitzenmacher. [*Network Applications of Bloom Filters: A Survey.*](https://www.eecs.harvard.edu/~michaelm/postscripts/im2005b.pdf) Internet Mathematics 2004.
25. [*On the Power of False Negative Awareness in Indicator-based Caching Systems.*](https://arxiv.org/abs/2102.01724) 2021.
26. Berger et al. [*RobinHood: Tail Latency Aware Caching.*](https://www.usenix.org/system/files/osdi18-berger.pdf) OSDI 2018.
27. Song et al. [*Learning Relaxed Belady for CDN Caching.*](https://www.usenix.org/system/files/nsdi20-song.pdf) NSDI 2020.

**Online algorithms & learning-augmented**
28. Lykouris & Vassilvitskii. *Competitive Caching with Machine Learned Advice.* ICML 2018.
29. Rohatgi. [*Near-Optimal Bounds for Online Caching with Machine Learned Advice.*](https://arxiv.org/abs/2005.13716) SODA 2020.
30. Wei & Zhang. [*Optimal Robustness-Consistency Trade-offs for Learning-Augmented Online Algorithms.*](https://arxiv.org/abs/2010.11443) NeurIPS 2020.
31. Karlin et al. *Competitive Randomized Algorithms for Nonuniform Problems (ski rental).* Algorithmica 1994.
32. Mirrokni, Thorup, Zadimoghaddam. [*Consistent Hashing with Bounded Loads.*](https://arxiv.org/abs/1608.01350) SODA 2018.
33. Thaler & Ravishankar. *Using Name-Based Mappings to Increase Hit Rates (HRW/Rendezvous).* IEEE/ACM ToN 1998.

**Overload control, failure detection, scheduling**
34. Cho et al. [*Overload Control for µs-scale RPCs with Breakwater.*](https://www.usenix.org/system/files/osdi20-cho.pdf) OSDI 2020.
35. [Netflix concurrency-limits](https://github.com/Netflix/concurrency-limits) (Gradient/Vegas adaptive limits).
36. Nichols & Jacobson. *Controlling Queue Delay (CoDel).* ACM Queue 2012.
37. Hayashibara et al. [*The φ Accrual Failure Detector.*](https://classes.cs.uchicago.edu/archive/2026/spring/23380-1/papers/hayashibara_phi.pdf) SRDS 2004.
38. Dadgar et al. [*Lifeguard: Local Health Awareness for More Accurate Failure Detection.*](https://arxiv.org/abs/1707.00788) 2017.
39. Narayanan et al. [*Gavel: Heterogeneity-Aware Cluster Scheduling.*](https://arxiv.org/abs/2008.09213) OSDI 2020.
40. Qiao et al. [*Pollux: Co-adaptive Cluster Scheduling for Goodput-Optimized Deep Learning.*](https://www.usenix.org/system/files/osdi21-qiao.pdf) OSDI 2021.
41. Lai et al. [*Oort: Efficient Federated Learning via Guided Participant Selection.*](https://www.usenix.org/system/files/osdi21-lai.pdf) OSDI 2021.
42. Sheng et al. *Fairness in Serving Large Language Models (VTC).* OSDI 2024.
43. Harchol-Balter. *Performance Modeling and Design of Computer Systems.* Cambridge, 2013.

**Networking & evaluation methodology**
44. Dabek et al. [*Vivaldi: A Decentralized Network Coordinate System.*](https://pdos.csail.mit.edu/papers/vivaldi:sigcomm/paper.pdf) SIGCOMM 2004.
45. [*Interleaved Online Testing in Large-Scale Systems.*](https://dl.acm.org/doi/fullHtml/10.1145/3543873.3587572) WWW 2023.
46. [*Nonstationary A/B Tests: Optimal Variance Reduction, Bias Correction, and Valid Inference.*](https://pubsonline.informs.org/doi/10.1287/mnsc.2022.01205) Management Science.
47. [Airbnb: Beyond A/B Test — Interleaving for Search Ranking.](https://airbnb.tech/data/beyond-a-b-test-speeding-up-airbnb-search-ranking-experimentation-through-interleaving/)

**Datasets**
48. ShareGPT conversations. 49. Zheng et al. *LMSYS-Chat-1M.* ICLR 2024. 50. Patel et al. *Splitwise* (Azure LLM inference traces). ISCA 2024.
