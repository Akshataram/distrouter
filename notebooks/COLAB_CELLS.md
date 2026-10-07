# Copy-paste cells for a blank Colab notebook (4 accounts)

Self-contained — you do **not** need to open `gpu_node.ipynb`. Make a new
Colab notebook in each account, set the runtime to a **T4 GPU**
(`Runtime → Change runtime type → T4 GPU`), and paste these five cells.

**The only thing that differs between accounts is `REPLICA_TAG` in Cell 1.**

Total time per account: ~10–15 minutes, almost all of it Cell 2 (pip) and
Cell 3 (downloading Qwen weights).

---

## Cell 1 — config (EDIT ONE LINE)

```python
import secrets

# ======== CHANGE THIS PER ACCOUNT: a / b / c / d ========
REPLICA_TAG = "replica-a"
# ========================================================

MODEL  = "Qwen/Qwen2.5-3B-Instruct"
BRANCH = "claude/friendly-turing-v9y7vj"

# THE flag that decides whether the demo shows anything at all.
# A T4 at 0.90 utilization holds ~200k tokens of KV cache, i.e. ~100
# distinct 2000-token system prompts. The demo workload has 6. So without
# this cap nothing ever evicts, every policy gets a cache hit, and every
# routing policy ties -- you would be demoing nothing.
# 375 blocks x 16 tokens = 6000 tokens = 0.5x the 6-app x 2000-token working set.
KV_BLOCKS = 375

VLLM_PORT, SIDECAR_PORT = 8000, 9000
ADMIN_TOKEN = secrets.token_hex(8)

!nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

print(f"\nREPLICA_TAG = {REPLICA_TAG}")
print(f"MODEL       = {MODEL}")
print(f"KV_BLOCKS   = {KV_BLOCKS}")
print(f"ADMIN_TOKEN = {ADMIN_TOKEN}   <-- COPY THIS")
```

Check the `nvidia-smi` line says **Tesla T4**. If you got no GPU, fix the
runtime type before continuing.

---

## Cell 2 — get the code and install (~4 min)

```python
!git clone --depth 1 --branch {BRANCH} https://github.com/Akshataram/distrouter.git
%cd distrouter
!git log --oneline -1

!pip install -q vllm
!pip install -q -r requirements.txt
# Colab's preinstalled torchaudio is often built against a different CUDA
# than vLLM wants, and vLLM does not need it. Without this, vLLM can die at
# startup with a CUDA symbol error that looks nothing like "wrong torchaudio".
!pip uninstall -y -q torchaudio || true
print("done")
```

The `--branch` matters: the block-level prefix index and the `prefix_aware`
policy are not on `main`. Confirm the `git log` line shows a recent commit.

---

## Cell 3 — launch vLLM behind the sidecar (~5–10 min, downloads weights)

```python
import time
import os, subprocess, urllib.request, urllib.error

# Idempotent: re-running this cell must not collide with a previous attempt.
# Without this you get "[Errno 98] address already in use" on SIDECAR_PORT,
# because the old sidecar is still alive holding the port and the GPU.
!pkill -9 -f replica_sidecar || true
!pkill -9 -f vllm.entrypoints || true
time.sleep(8)
print("GPU memory after cleanup (want ~0 MiB used):")
!nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
print()

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
sidecar_log = open(f"{LOG_DIR}/sidecar.log", "w")

vllm_cmd = (
    f"python -m vllm.entrypoints.openai.api_server "
    f"--model {MODEL} --port {VLLM_PORT} "
    f"--enable-prefix-caching "          # the engine feature this project exploits
    f"--enable-prompt-tokens-details "   # MANDATORY: without it usage.cached_tokens is never reported
    f"--block-size 16 "                  # must equal the router's SWIFTSERVE_BLOCK_SIZE
    f"--max-num-seqs 32 --max-model-len 8192 --dtype half "
    f"--num-gpu-blocks-override {KV_BLOCKS} "
    f"--gpu-memory-utilization 0.90"
)

sidecar = subprocess.Popen(
    ["python", "-m", "swiftserve.replica_sidecar",
     "--upstream-url", f"http://127.0.0.1:{VLLM_PORT}",
     "--port", str(SIDECAR_PORT),
     "--admin-token", ADMIN_TOKEN,
     "--supervise", vllm_cmd],
    stdout=sidecar_log, stderr=subprocess.STDOUT,
)
print(f"sidecar pid={sidecar.pid}\nsupervising: {vllm_cmd}\n")

def tail_log(path=f"{LOG_DIR}/sidecar.log", n=40):
    with open(path) as f:
        return "".join(f.readlines()[-n:])

healthy, deadline = False, time.monotonic() + 900
while time.monotonic() < deadline:
    if sidecar.poll() is not None:
        print("!! sidecar died. Log tail:\n" + tail_log()); break
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{SIDECAR_PORT}/health", timeout=3) as r:
            if r.status == 200:
                healthy = True; break
    except Exception:
        pass
    time.sleep(5)

print(f"HEALTHY: real vLLM serving {MODEL} behind the sidecar on :{SIDECAR_PORT}"
      if healthy else "!! not healthy in time. Log tail:\n" + tail_log())
```

`--supervise` means the sidecar **owns** the vLLM process and can kill and
relaunch it on command — that is what makes the failover demo possible over
the tunnel, without touching this notebook mid-presentation.

---

## Cell 4 — prove prefix caching works on THIS node

Run this before involving the router. Debugging it later through the router
is much harder.

```python
import json, time, re, urllib.request

# --- check 1: did --num-gpu-blocks-override actually take effect? ---------
log = tail_log(n=600)
found = re.findall(r"GPU (?:KV cache size|blocks)[^0-9]*([0-9,]+)", log)
print(f"KV_BLOCKS requested : {KV_BLOCKS}")
print(f"vLLM reported       : {found if found else 'NOT FOUND -- search the log manually'}")
print("  (if these disagree, there is no cache pressure and all policies will tie)\n")

# --- check 2: is a repeated prefix really reused? ------------------------
BIG_SYSTEM = "You are a meticulous customer support agent. " * 200   # ~2000 tokens

def call(user, max_tokens=16):
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": BIG_SYSTEM},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens, "stream": True,
        "stream_options": {"include_usage": True},
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{SIDECAR_PORT}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    t0, ttft, usage = time.monotonic(), None, None
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            ch = chunk.get("choices") or []
            if ch and (ch[0].get("delta") or {}).get("content") and ttft is None:
                ttft = (time.monotonic() - t0) * 1000
            if chunk.get("usage"):
                usage = chunk["usage"]
    return ttft, usage

print(f"{'call':<28}{'TTFT':>10}{'prompt_tok':>12}{'cached_tok':>12}")
rows = []
for label, q in [("1st (cold prefix)", "reset my password"),
                 ("2nd (same system prompt)", "where is my invoice"),
                 ("3rd (same system prompt)", "cancel my plan")]:
    ttft, usage = call(q)
    cached = (usage or {}).get("prompt_tokens_details", {}).get("cached_tokens")
    prompt = (usage or {}).get("prompt_tokens")
    rows.append((label, ttft, prompt, cached))
    print(f"{label:<28}{ttft:>8.0f}ms{str(prompt):>12}{str(cached):>12}")

warm = [r for r in rows[1:] if r[3]]
if not warm:
    print("\n!! FAIL: cached_tokens is 0/None on repeat calls.")
    print("   --enable-prompt-tokens-details or --enable-prefix-caching is not in effect.")
    print("   Fix this before running the demo -- true cache ratio will read 0%.")
elif rows[0][1] and warm[0][1] and rows[0][1] / warm[0][1] > 1.5:
    print(f"\nOK: prefix caching is live on this node "
          f"({rows[0][1] / warm[0][1]:.1f}x lower TTFT on the warm call).")
else:
    print("\n?? cached_tokens looks right but TTFT barely moved -- prompt may be too")
    print("   short for prefill to dominate. Lengthen BIG_SYSTEM.")
```

Expected shape (numbers vary by node):

```
call                              TTFT  prompt_tok  cached_tok
1st (cold prefix)                612ms        2048           0
2nd (same system prompt)          78ms        2048        2048
3rd (same system prompt)          74ms        2048        2048

OK: prefix caching is live on this node (7.8x lower TTFT on the warm call).
```

---

## Cell 5 — open the tunnel, then leave it running

```python
import re, subprocess, time

!wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O cloudflared
!chmod +x cloudflared

tunnel_log = open(f"{LOG_DIR}/cloudflared.log", "w")
tunnel = subprocess.Popen(
    ["./cloudflared", "tunnel", "--url", f"http://127.0.0.1:{SIDECAR_PORT}"],
    stdout=tunnel_log, stderr=subprocess.STDOUT,
)

public_url, deadline = None, time.monotonic() + 90
while time.monotonic() < deadline:
    with open(f"{LOG_DIR}/cloudflared.log") as f:
        m = re.search(r"https://[a-zA-Z0-9.-]+\.trycloudflare\.com", f.read())
    if m:
        public_url = m.group(0); break
    time.sleep(2)

if public_url:
    print("=" * 64)
    print(f"  {REPLICA_TAG}")
    print(f"  URL   : {public_url}      <-- COPY THIS")
    print(f"  TOKEN : {ADMIN_TOKEN}")
    print("=" * 64)
else:
    print("!! no tunnel URL. Log tail:\n" + tail_log(f"{LOG_DIR}/cloudflared.log"))

# Keep the runtime busy. Leave this cell running for the whole demo, and keep
# the browser tab visibly active -- Colab disconnects after ~90 minutes idle,
# and background compute does NOT count as interaction.
print("\nkeep-alive running; do not stop this cell")
while True:
    time.sleep(60)
```

---

## After all 4 accounts are up

Collect the four URLs. Then **on your laptop** (not in Colab):

```bash
cd distrouter
git checkout claude/friendly-turing-v9y7vj
pip install -r requirements.txt transformers

export SWIFTSERVE_REPLICAS="https://aaa.trycloudflare.com,https://bbb.trycloudflare.com,https://ccc.trycloudflare.com,https://ddd.trycloudflare.com"

python scripts/demo.py \
  --replicas "$SWIFTSERVE_REPLICAS" \
  --model Qwen/Qwen2.5-3B-Instruct \
  --tokenizer Qwen/Qwen2.5-3B-Instruct \
  --policies round_robin,least_connections,swiftserve,prefix_aware \
  --num-apps 6 --sessions 24 --turns 1 \
  --system-prompt-tokens 2000 --max-tokens 16
```

`transformers` is required: without `--tokenizer` the router falls back to a
byte-chunk tokenizer whose block boundaries do not match the engine's, so
every cache prediction is systematically wrong. The router prints a loud
warning in that mode — if you see it during the real demo, stop.

### Optional: the failover demo

While the demo is running, from your laptop:

```bash
curl -X POST https://aaa.trycloudflare.com/chaos/kill -H 'X-Chaos-Token: <that node ADMIN_TOKEN>'
```

The router circuit-breaks that replica and reroutes. `scripts/chaos_runner.py`
automates the full scenario with measured recovery time.

---

## Quick failure table

| Symptom | Fix |
|---|---|
| `nvidia-smi` shows no GPU | Runtime → Change runtime type → T4 GPU |
| `cached_tok` is 0 or None in Cell 4 | `--enable-prompt-tokens-details` missing |
| Reported blocks ≠ `KV_BLOCKS` | The override did not take; all policies will tie |
| CUDA OOM in Cell 3 | Use `Qwen/Qwen2.5-1.5B-Instruct`, or utilization `0.85` |
| vLLM dies with a CUDA symbol error | The torchaudio uninstall in Cell 2 did not run |
| Node disconnects mid-demo | 90-min idle cap — keep the tab visibly active |
| Tunnel URL changed | Quick tunnels are ephemeral; re-copy and re-export |

**Before touching Colab at all**, rehearse with no GPU on your laptop:

```bash
python scripts/demo.py      # ~2 min, spawns fake replicas
```

Save that output as insurance.
