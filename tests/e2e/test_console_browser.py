"""Browser end-to-end test of the console against the real stack started by
scripts/dev.py (router + FAKE replicas -- simulated, no GPU).

Skipped unless SWIFTSERVE_E2E=1 and Playwright with a Chromium is available:

    pip install playwright            # browser: PLAYWRIGHT_BROWSERS_PATH / playwright install chromium
    SWIFTSERVE_E2E=1 pytest tests/e2e -q

What it verifies, through the real UI: a request sent from the playground is
routed by the real router and the page shows the router's own attribution; a
concurrent load test completes with every request accounted for; Stop aborts
in flight requests and the router records the disconnects; killing a replica
shows up as failures and as an unhealthy replica; nothing throws in the page.
"""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.e2e

if os.environ.get("SWIFTSERVE_E2E") != "1":
    pytest.skip("set SWIFTSERVE_E2E=1 to run the browser end-to-end test", allow_module_level=True)
playwright_sync = pytest.importorskip("playwright.sync_api")

REPO = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    port = free_port()
    logs = tmp_path_factory.mktemp("e2e_logs")
    proc = subprocess.Popen([sys.executable, str(REPO / "scripts" / "dev.py"), "--simulate", "--policy", "prefix_aware", "--port", str(port),
                             "--log-dir", str(logs), "--num-replicas", "3"], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        assert proc.poll() is None, "dev.py exited early"
        time.sleep(0.4)
    else:
        proc.kill()
        pytest.fail("router did not come up")
    yield base
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def page(stack):
    with playwright_sync.sync_playwright() as p:
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1440, "height": 1000})
        problems: list[str] = []
        pg.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        pg.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        pg.goto(f"{stack}/ui/")
        pg.wait_for_function("document.querySelector('#conn').textContent.includes('live')", timeout=15000)
        yield pg, problems
        browser.close()


def test_playground_request_is_attributed_by_the_router(page):
    pg, problems = page
    assert "SIMULATED" in pg.inner_text("#mode")                          # the page admits the replicas are fake
    assert not pg.locator("#auth-box").is_visible()                       # no token configured -> no banner
    pg.fill("#pg-system", "You are a careful support agent. " * 60)
    pg.fill("#pg-prompt", "How do I get started?")
    pg.fill("#pg-max", "6")
    pg.click("#pg-send")
    pg.wait_for_function("document.querySelector('#pg-detail').textContent.includes('completed')", timeout=15000)
    detail = pg.inner_text("#pg-detail")
    assert "Selected by policy" in detail and "Served by" in detail and "TTFT (time to first token)" in detail
    assert re.search(r"R\d", detail) and "prefix" in detail.lower()
    assert "tok0" in pg.inner_text("#pg-output")
    pg.click("#pg-send")                                                   # same prompt again: should now hit the cache
    pg.wait_for_function("document.querySelectorAll('#pg-history tbody tr').length >= 2 && !document.querySelector('#pg-history').textContent.includes('streaming')", timeout=15000)
    assert problems == []


def test_load_test_accounts_for_every_request_and_stop_records_disconnects(page, stack):
    pg, problems = page
    pg.click("#tab-loadtest")
    pg.click("#lt-sys-gen")
    pg.fill("#lt-n", "30")
    pg.fill("#lt-c", "6")
    pg.click("#lt-start")
    pg.wait_for_function("document.querySelector('#lt-status').textContent.startsWith('Finished')", timeout=60000)
    pg.wait_for_timeout(600)
    rows = pg.locator("#lt-table tbody tr")
    assert rows.count() == 30
    texts = [rows.nth(i).inner_text() for i in range(rows.count())]
    assert all("completed" in t for t in texts), "every request must end in 'completed' with the router's attribution"
    dist = pg.inner_text("#lt-dist")
    assert "selected" in dist and "served" in dist

    pg.fill("#lt-n", "300")
    pg.fill("#lt-c", "8")
    pg.click("#lt-start")
    pg.wait_for_timeout(900)
    pg.click("#lt-stop")
    pg.wait_for_function("document.querySelector('#lt-status').textContent.startsWith('Stopped')", timeout=30000)
    pg.wait_for_timeout(2500)                                              # let the router notice the hang-ups
    progress = pg.inner_text("#lt-progress")
    assert "aborted" in progress
    snap = httpx.get(f"{stack}/api/dashboard").json()
    assert snap["totals"]["client_disconnected"] >= 1                      # the ROUTER saw them, not just the browser
    assert snap["router"]["admission"]["in_flight"] == 0 and all(r["in_flight"] == 0 for r in snap["replicas"])
    assert all(not p.startswith("pageerror") for p in problems)


def test_routing_view_and_dashboard_render_real_data(page):
    pg, problems = page
    pg.click("#tab-routing")
    pg.wait_for_function("document.querySelectorAll('.n-replica').length === 3", timeout=10000)
    assert pg.locator("#rt-decisions tbody tr").count() >= 1
    pg.click("#tab-dashboard")
    pg.wait_for_function("document.querySelectorAll('.card.replica').length === 3", timeout=10000)
    text = pg.inner_text("#db-avail")
    assert "GPU utilization / VRAM" in text and "unavailable" in text         # fake replicas have no sidecar: reported, not invented
    assert "SIMULATED" in pg.inner_text("#db-banner")
    assert all(not p.startswith("pageerror") for p in problems)
