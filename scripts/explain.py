"""Teaching demo: watch the router think, one step at a time.

This runs the REAL routing code (the real block hashing, the real prefix
index, the real PrefixAwarePolicy and the real baseline policies) on inputs
made small enough to read on one screen. It needs no GPU, no network and no
model download.

Only two things are toys, and the output says so:
  * a "word tokenizer" (one word = one token) instead of the real Qwen
    tokenizer, so you can SEE the tokens;
  * a block size of 4 instead of 16, so a short sentence makes several blocks.

Run:
    python3 scripts/explain.py            # prints everything at once
    python3 scripts/explain.py --step     # waits for Enter between steps (use this to present)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swiftserve.policy import LeastConnectionsPolicy, PrefixAwarePolicy, RoundRobinPolicy  # noqa: E402
from swiftserve.prefix_index import PrefixIndex, block_hashes  # noqa: E402
from swiftserve.state import ReplicaState  # noqa: E402

BLOCK = 4          # real system: 16
NUM_REPLICAS = 4   # real system: 4 GPUs

# Two "companies", each with its own long instruction text (16 words each).
COMPANY_X = "you are a helpful bank assistant always be polite and never share account numbers with anyone"
COMPANY_Y = "act as a travel agent who plans trips and suggests hotels flights and cheap local food"

REQUESTS = [
    ("A", "company X, first user",  COMPANY_X, "how do I reset my password"),
    ("B", "company X, second user", COMPANY_X, "where can I see my balance"),
    ("C", "company Y, a user",      COMPANY_Y, "find me a cheap flight to Goa"),
    ("D", "company X, third user",  COMPANY_X, "can I change my phone number"),
    ("E", "company X text, but ONE extra word added at the very START",
     "please " + COMPANY_X, "how do I close my account"),
]


class WordTokenizer:
    """Toy stand-in for the real tokenizer: one word = one token."""

    name = "toy-word-tokenizer"

    def __init__(self) -> None:
        self._ids: dict[str, int] = {}

    def words(self, messages: list[dict]) -> list[str]:
        out: list[str] = []
        for m in messages:
            out.append(m["role"].upper() + ":")
            out.extend(str(m["content"]).split())
        out.append("ASSISTANT:")
        return out

    def encode_chat(self, messages: list[dict]) -> list[int]:
        return [self._ids.setdefault(w, len(self._ids)) for w in self.words(messages)]


def short(h: bytes) -> str:
    return h.hex()[:6]


def banner(text: str) -> None:
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def pause(step: bool) -> None:
    if step:
        input("\n   [press Enter for the next step] ")


def make_replicas() -> list[ReplicaState]:
    # assumed_max_batch_size=32 mirrors the real replicas' --max-num-seqs 32.
    return [
        ReplicaState(replica_id=i, base_url=f"http://gpu{i}", cache_ttl_s=600, assumed_max_batch_size=32)
        for i in range(NUM_REPLICAS)
    ]


def run(step: bool = False, verbose: bool = True) -> dict[str, int]:
    """Returns {policy name: tokens of prompt reused across the 5 requests}."""
    say = print if verbose else (lambda *a, **k: None)
    tok = WordTokenizer()

    if verbose:
        banner("STEP 1 - What a request looks like to the engine")
        say("The engine does NOT see 'messages'. It glues them into one long list of tokens,")
        say(f"then cuts that list into fixed-size BLOCKS ({BLOCK} tokens here, 16 in the real system).")
        say("(toy tokenizer: one word = one token, so you can read it)\n")
        msgs = [{"role": "system", "content": COMPANY_X}, {"role": "user", "content": REQUESTS[0][3]}]
        words = tok.words(msgs)
        hashes = block_hashes(tok.encode_chat(msgs), BLOCK, "")
        say(f"Request A has {len(words)} tokens:")
        say("   " + " ".join(words))
        say(f"\nCut into blocks of {BLOCK} (a leftover partial block at the end is NOT cached):")
        for i in range(len(hashes)):
            chunk = words[i * BLOCK:(i + 1) * BLOCK]
            say(f"   block {i + 1}: {' '.join(chunk):<40} hash {short(hashes[i])}")
        left = words[len(hashes) * BLOCK:]
        say(f"   leftover: {' '.join(left)}   <- not a full block, never cached")
        say("\nEach hash is computed FROM THE PREVIOUS HASH plus this block's tokens.")
        say("So block 3's hash secretly contains blocks 1 and 2. One hash = the whole beginning.")
        pause(step)

    # ---- the real policies, on the real code ----------------------------------------------
    replicas = make_replicas()
    index = PrefixIndex(block_size=BLOCK, max_blocks_per_replica=1000)
    policy = PrefixAwarePolicy(
        tokenizer=tok, index=index, cache_threshold=0.5, min_match_tokens=2 * BLOCK, namespace="toy",
    )

    if verbose:
        banner("STEP 2 - The router sends 5 requests. Watch the table it keeps.")
        say(f"{NUM_REPLICAS} GPUs, all idle, router's table empty. For this toy, each request is still")
        say("running when the next one arrives, so the GPUs stay 'busy' (that is what makes it interesting).")
        pause(step)

    chosen_prefix: list[int] = []
    for name, label, system, question in REQUESTS:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": question}]
        words = tok.words(msgs)
        hashes = block_hashes(tok.encode_chat(msgs), BLOCK, "toy")
        matches = index.match(hashes)

        if verbose:
            banner(f"REQUEST {name}: {label}")
            say(f"   user asks: \"{question}\"")
            say(f"   {len(words)} tokens -> {len(hashes)} full blocks")
            say("\n   a) Router asks its table: 'which GPU holds the START of this prompt?'")
            if matches:
                for rid, blocks in sorted(matches.items()):
                    say(f"      GPU {rid} holds the first {blocks} of {len(hashes)} blocks "
                        f"= {blocks * BLOCK} tokens it can skip")
            else:
                say("      nobody holds even the first block")

        pick, predicted = policy.select_with_prediction(name, 3000.0, replicas, msgs)
        chosen_prefix.append(pick.replica_id)

        if verbose:
            say("\n   b) Decision:")
            if matches and predicted:
                say(f"      -> GPU {pick.replica_id}  (longest match: it can skip {predicted} tokens)")
            else:
                say(f"      -> GPU {pick.replica_id}  (no useful match, so just pick the least busy GPU)")
            say(f"      router PREDICTS {predicted} cached tokens on GPU {pick.replica_id}")
            pause(step)

        pick.in_flight += 1   # toy: this request is still running

    # ---- what the table looks like at the end --------------------------------------------
    if verbose:
        banner("STEP 3 - The router's table after all 5 requests")
        say("hash -> which GPUs are believed to hold that block\n")
        by_gpu: dict[int, int] = {}
        for holders in index._holders.values():  # private on purpose: this is a teaching printout
            for rid in holders:
                by_gpu[rid] = by_gpu.get(rid, 0) + 1
        for rid in range(NUM_REPLICAS):
            say(f"   GPU {rid}: believed to hold {by_gpu.get(rid, 0)} blocks")
        say("\n   Company X's 4 shared blocks live on ONE GPU, so every company-X request that")
        say("   arrives can skip them. That is the whole idea.")
        pause(step)

    # ---- compare with the baselines on the same 5 requests -------------------------------
    def replay(policy_obj) -> int:
        """Same 5 requests, a real simulated cache per GPU, count tokens reused."""
        reps = make_replicas()
        caches: list[set[bytes]] = [set() for _ in range(NUM_REPLICAS)]
        reused = 0
        for name, _label, system, question in REQUESTS:
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": question}]
            pick = policy_obj.select(name, 3000.0, reps, msgs)
            pick.in_flight += 1
            hs = block_hashes(tok.encode_chat(msgs), BLOCK, "toy")
            hit = 0
            for h in hs:
                if h in caches[pick.replica_id]:
                    hit += 1
                else:
                    break
            reused += hit * BLOCK
            caches[pick.replica_id].update(hs)
        return reused

    fresh_prefix = PrefixAwarePolicy(
        tokenizer=tok, index=PrefixIndex(block_size=BLOCK), cache_threshold=0.5,
        min_match_tokens=2 * BLOCK, namespace="toy",
    )
    results = {
        "round_robin": replay(RoundRobinPolicy()),
        "least_connections": replay(LeastConnectionsPolicy()),
        "prefix_aware": replay(fresh_prefix),
    }

    if verbose:
        banner("STEP 4 - Same 5 requests, three strategies. How many tokens did each GPU get to SKIP?")
        for k, v in results.items():
            say(f"   {k:<20} reused {v:>3} tokens   {'#' * (v // 2)}")
        say("\n   round_robin / least_connections spread requests around, so the GPU that gets")
        say("   request D has never seen company X's text. prefix_aware sends B and D to the")
        say("   GPU that already read it.")
        say("\n   Request E added ONE word at the start. That shifts every block, so NOTHING can be")
        say("   reused: caching only works if the text is the same from the very first token.")
        say("\n(Toy sizes: 4-token blocks, word tokens. The real system uses 16-token blocks and")
        say(" the real Qwen tokenizer, but the logic is exactly this code.)")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--step", action="store_true", help="wait for Enter between steps (use this to present)")
    args = parser.parse_args()
    run(step=args.step)


if __name__ == "__main__":
    main()
