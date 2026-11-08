"""Record the PRProof demo with Playwright (guest mode, public PRs).
Run the server first (python -m specguard serve), then: python scripts/record_demo.py
Output: demo/prproof-demo.webm"""
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8000"
OUT = Path(__file__).resolve().parent.parent / "demo"
CURSOR = """
(() => { const d = document.createElement('div');
  d.style.cssText = 'position:fixed;z-index:99999;width:18px;height:18px;border-radius:50%;background:rgba(56,225,255,.75);box-shadow:0 0 0 6px rgba(56,225,255,.25);pointer-events:none;left:-40px;top:-40px;transition:transform .08s';
  document.documentElement.appendChild(d);
  addEventListener('mousemove', e => { d.style.left = (e.clientX - 9) + 'px'; d.style.top = (e.clientY - 9) + 'px'; }, true); })();
"""


def pause(s):
    time.sleep(s)


def review(page, target, expect):
    page.click("#tgt", delay=120)
    pause(.7)
    page.type("#tin", target, delay=55)
    pause(.9)
    page.keyboard.press("Enter")
    page.wait_for_selector(f"text={expect}", timeout=240_000)
    pause(2.5)


def slow_scroll(page, steps=5, dy=420):
    for _ in range(steps):
        page.mouse.wheel(0, dy)
        pause(1.1)


def main():
    OUT.mkdir(exist_ok=True)
    tmp = OUT / "_raw"
    shutil.rmtree(tmp, ignore_errors=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 720}, record_video_dir=str(tmp),
                                  record_video_size={"width": 1280, "height": 720}, device_scale_factor=1)
        ctx.add_init_script(CURSOR)
        page = ctx.new_page()
        page.goto(BASE)
        page.wait_for_selector("text=Review its proof")
        page.mouse.move(640, 360)
        pause(4)

        # 1. a PR that removes a security check
        review(page, "github.com/Naveenaduri/tinyjwt/pull/2", "Needs changes")
        page.evaluate("window.scrollTo(0,0)")
        pause(3)
        slow_scroll(page, 6)
        pause(2)

        # 2. the merge check on GitHub
        page.goto("https://github.com/Naveenaduri/tinyjwt/pull/2")
        pause(2)
        page.evaluate("document.querySelector('.merge-status-list, [data-testid=\"mergebox-partial\"], .branch-action')?.scrollIntoView({block:'center'})")
        pause(4)

        # 3. a clean PR for contrast
        page.goto(BASE)
        page.wait_for_selector("#tgt")
        pause(1.2)
        review(page, "github.com/Naveenaduri/tinyjwt/pull/1", "Looks good")
        pause(3)
        ctx.close()
        video = Path(page.video.path())
        browser.close()
    final = OUT / "prproof-demo.webm"
    shutil.move(str(video), final)
    shutil.rmtree(tmp, ignore_errors=True)
    print("saved", final)


if __name__ == "__main__":
    sys.exit(main())
