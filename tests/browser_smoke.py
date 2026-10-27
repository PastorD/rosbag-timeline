"""Optional real-browser integration check; needs Playwright and Chromium.

CHROMIUM_PATH=/path/to/chromium python tests/browser_smoke.py
Normally omit CHROMIUM_PATH after `python -m playwright install chromium`.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from rosbag_timeline.demo import generate

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    bag = generate(root / "demo.mcap", 3000)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen([sys.executable, "-m", "rosbag_timeline", "view", str(bag), "--nodes", str(bag.with_suffix('.nodes.json')),
                               "--no-browser", "--port", str(port), "--cache-dir", str(root/'cache')], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/api/meta", timeout=1).close()
                break
            except OSError:
                time.sleep(.05)
        else:
            raise RuntimeError("viewer did not start")
        with sync_playwright() as p:
            options = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]}
            if os.environ.get("CHROMIUM_PATH"):
                options["executable_path"] = os.environ["CHROMIUM_PATH"]
            browser = p.chromium.launch(**options)
            page = browser.new_page(viewport={"width": 1600, "height": 1080}, device_scale_factor=1)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(url)
            expect(page.locator("#status")).to_contain_text("Ready", timeout=20000)
            assert "3,000 messages" in page.locator("#count").inner_text()
            page.locator("#compute").click()
            expect(page.locator("#status")).to_contain_text("Statistics complete", timeout=20000)
            info = page.evaluate("(()=>{const h=currentStat().metrics[S.metric].histogram;return {count:h.counts.length,bin:h.counts.findIndex(v=>v>0)}})()")
            box = page.locator("#histogram").bounding_box()
            page.mouse.click(box["x"]+10+(info["bin"]+.5)*(box["width"]-20)/info["count"],box["y"]+75)
            expect(page.locator("#status")).to_contain_text("highlighted", timeout=20000)
            assert page.evaluate("S.data.highlighted") > 0
            if os.environ.get("SCREENSHOT_DIR"):
                page.screenshot(path=str(Path(os.environ["SCREENSHOT_DIR"])/"viewer-topics.png"), full_page=True)
            page.locator("#nodeMode").click()
            assert "bag recorder" in page.locator("#viewTitle").inner_text()
            if os.environ.get("SCREENSHOT_DIR"):
                page.screenshot(path=str(Path(os.environ["SCREENSHOT_DIR"])/"viewer-nodes.png"), full_page=True)
            page.locator("#start").fill("0.1")
            page.locator("#end").fill("0.5")
            page.locator("#applyRange").click()
            expect(page.locator("#count")).to_contain_text("400 messages", timeout=20000)
            page.locator("#search").fill("PointCloud2")
            page.locator("#none").click()
            page.locator("#all").click()
            expect(page.locator("#count")).to_contain_text("133 messages", timeout=20000)
            assert not errors, errors
            print("Browser checks passed: initial timeline, exact histogram highlight, node mode, range filtering, fuzzy topic selection; no JS errors.")
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
