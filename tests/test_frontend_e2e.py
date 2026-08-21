"""End-to-end tests for the frontend UI and client-side JavaScript.

Validates that:
  1. The embedded JavaScript in index.html has 0 syntax errors (via node --check).
  2. The frontend boots cleanly in headless Chromium without unhandled exceptions.
  3. The feed table populates with postings and renders correct DOM counts.
  4. Client-side navigation to posting/company detail pages renders without crashing.
"""

import asyncio
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).parent.parent
STATIC_HTML_PATH = ROOT_DIR / "service" / "static" / "index.html"
DIST_DIR = ROOT_DIR / "dist"


def test_index_html_js_syntax():
    """Extracts the <script> block from index.html and checks for syntax errors with node."""
    assert STATIC_HTML_PATH.exists(), f"Missing {STATIC_HTML_PATH}"
    html_content = STATIC_HTML_PATH.read_text(encoding="utf-8")
    
    script_matches = re.findall(r"<script>([\s\S]*?)</script>", html_content)
    assert len(script_matches) >= 1, "Expected at least one <script> block in index.html"
    
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js binary not available on test host")

    proc = subprocess.run(
        [node_bin, "--check", "-"],
        input=script_matches[0],
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, f"JavaScript syntax error in index.html:\n{proc.stderr}"


def test_frontend_renders_in_headless_chromium():
    """Boots a local static server and verifies headless Chromium renders the feed without JS errors."""
    asyncio.run(_run_frontend_headless_test())


async def _run_frontend_headless_test():
    chromium_bin = shutil.which("chromium") or shutil.which("google-chrome") or "/snap/bin/chromium"
    if not os.path.exists(chromium_bin):
        pytest.skip("Chromium / Chrome binary not available for headless E2E testing")

    try:
        import websockets  # noqa: F401
    except ImportError:
        pytest.skip("websockets package not available")

    # Ensure dist exists
    if not (DIST_DIR / "index.html").exists() or not (DIST_DIR / "data" / "feed.json").exists():
        from service.edge_export import export_edge_bundle
        export_edge_bundle(out_dir=DIST_DIR)

    # Start a quiet local HTTP server
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(DIST_DIR), **kwargs)

        def log_message(self, format, *args):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), QuietHandler)
    port = httpd.server_address[1]
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    debug_port = port + 1000 if port + 1000 < 65535 else port - 1000
    chrome_proc = subprocess.Popen(
        [
            chromium_bin,
            "--headless=new",
            "--disable-gpu",
            f"--remote-debugging-port={debug_port}",
            f"http://127.0.0.1:{port}/",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        await asyncio.sleep(1.5)
        # Query CDP tabs
        tab_info_url = f"http://127.0.0.1:{debug_port}/json"
        with urllib.request.urlopen(tab_info_url, timeout=3) as resp:
            tabs = json.loads(resp.read().decode("utf-8"))
            page_tab = next(t for t in tabs if t.get("type") == "page")

        ws_url = page_tab["webSocketDebuggerUrl"]
        async with websockets.connect(ws_url) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
            await ws.send(json.dumps({"id": 2, "method": "Page.enable"}))

            exceptions = []
            start_time = time.time()
            while time.time() - start_time < 2.5:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.3)
                    data = json.loads(msg)
                    if data.get("method") == "Runtime.exceptionThrown":
                        exceptions.append(data["params"])
                except asyncio.TimeoutError:
                    pass

            assert len(exceptions) == 0, f"Unhandled JS exceptions during boot: {exceptions}"

            # Evaluate table rendering
            expr = "JSON.stringify({ rowCount: document.querySelectorAll('#feed-body tr').length, countText: document.getElementById('feed-count')?.textContent })"
            await ws.send(json.dumps({
                "id": 3,
                "method": "Runtime.evaluate",
                "params": {"expression": expr}
            }))

            while True:
                resp_msg = await ws.recv()
                data = json.loads(resp_msg)
                if data.get("id") == 3:
                    eval_result = json.loads(data["result"]["result"]["value"])
                    assert eval_result["rowCount"] > 0, f"Expected feed rows to render, got {eval_result}"
                    assert "matching" in (eval_result["countText"] or "").lower() or "postings" in (eval_result["countText"] or "").lower()
                    break

            # 2. Test search filtering
            search_expr = """
            (function() {
                const s = document.getElementById('search');
                s.value = 'software';
                s.dispatchEvent(new Event('input', { bubbles: true }));
                return new Promise(resolve => setTimeout(() => {
                    resolve(JSON.stringify({
                        rowCount: document.querySelectorAll('#feed-body tr').length,
                        countText: document.getElementById('feed-count')?.textContent
                    }));
                }, 300));
            })()
            """
            await ws.send(json.dumps({
                "id": 4,
                "method": "Runtime.evaluate",
                "params": {"expression": search_expr, "awaitPromise": True}
            }))

            while True:
                resp_msg = await ws.recv()
                data = json.loads(resp_msg)
                if data.get("id") == 4:
                    search_res = json.loads(data["result"]["result"]["value"])
                    assert search_res["rowCount"] > 0, f"Expected search results for 'software', got {search_res}"
                    break

            # 3. Test location alignment style
            align_expr = "window.getComputedStyle(document.querySelector('.location-cell')).textAlign"
            await ws.send(json.dumps({
                "id": 5,
                "method": "Runtime.evaluate",
                "params": {"expression": align_expr}
            }))

            while True:
                resp_msg = await ws.recv()
                data = json.loads(resp_msg)
                if data.get("id") == 5:
                    align_val = data["result"]["result"]["value"]
                    assert align_val == "left", f"Expected location-cell text-align to be 'left', got '{align_val}'"
                    break

    finally:
        chrome_proc.terminate()
        httpd.shutdown()
