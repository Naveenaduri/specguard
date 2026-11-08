"""Record the PRProof demo with Playwright (guest mode, public PRs) and note when each narration cue starts,
so scripts/narrate.py can speak the lines in parallel with what is on screen.
Run the server first (python -m specguard serve), then: python scripts/record_demo.py
Output: demo/raw.webm and demo/cues.json"""
import json
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8000"
OUT = Path(__file__).resolve().parent.parent / "demo"
CURSOR = """
(() => { const d = document.createElement('div');
  d.style.cssText = 'position:fixed;z-index:99999;width:18px;height:18px;border-radius:50%;background:rgba(56,225,255,.75);box-shadow:0 0 0 6px rgba(56,225,255,.25);pointer-events:none;left:-40px;top:-40px';
  document.documentElement.appendChild(d);
  addEventListener('mousemove', e => { d.style.left = (e.clientX - 9) + 'px'; d.style.top = (e.clientY - 9) + 'px'; }, true); })();
"""
CUES: list = []
T0 = [0.0]


def cue(text):
    """The line starts being spoken at this moment, alongside whatever the page is doing."""
    CUES.append({"t": round(time.time() - T0[0], 2), "text": text})


def pause(s):
    time.sleep(s)


def type_target(page, target):
    page.click("#tgt", delay=120)
    pause(.6)
    page.type("#tin", target, delay=70)
    pause(.7)
    page.keyboard.press("Enter")


def slow_scroll(page, steps, dy=380, gap=1.6):
    for _ in range(steps):
        page.mouse.wheel(0, dy)
        pause(gap)


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
        T0[0] = time.time()
        page.goto(BASE)
        page.wait_for_selector("text=Review its proof")
        page.mouse.move(640, 360)
        cue("AI writes code faster than any of us can review it. So PRProof doesn't ask you to trust the code. It shows you the proof.")
        pause(6.5)

        cue("Let's try it on a real pull request. I'll just paste the link.")
        type_target(page, "github.com/Naveenaduri/tinyjwt/pull/2")
        pause(.5)
        cue("While that runs, it reads the change in plain English, scores nine review categories with SubQ's decision model, "
            "and then writes its own test and runs it in a sandbox.")
        page.wait_for_selector("text=Needs changes", timeout=240_000)
        page.evaluate("window.scrollTo(0,0)")
        cue("And here's the verdict. Needs changes, risk severe. This pull request quietly removed the audience check.")
        pause(5.5)
        cue("The proof is a test it wrote and ran. It fails. The token is accepted when it should have been rejected.")
        slow_scroll(page, 3)
        cue("It quotes the spec, it explains why it matters, and it tells you the fix.")
        slow_scroll(page, 3)
        pause(1.5)

        page.goto("https://github.com/Naveenaduri/tinyjwt/pull/2")
        cue("Back on GitHub, the same pull request now carries a failing PRProof check, so branch protection can block the merge.")
        pause(2)
        page.evaluate("document.querySelector('.merge-status-list, [data-testid=\"mergebox-partial\"], .branch-action')?.scrollIntoView({block:'center'})")
        pause(7)

        page.goto(BASE)
        page.wait_for_selector("#tgt")
        pause(1)
        cue("Now compare that with a healthy change.")
        type_target(page, "github.com/Naveenaduri/tinyjwt/pull/1")
        page.wait_for_selector("text=Looks good", timeout=240_000)
        page.evaluate("window.scrollTo(0,0)")
        cue("Looks good. Nothing proven wrong, so it says so, and it never claims a problem it couldn't test.")
        pause(6)
        cue("PRProof. Don't review AI code. Review its proof.")
        pause(4)
        ctx.close()
        video = Path(page.video.path())
        browser.close()
    shutil.move(str(video), OUT / "raw.webm")
    shutil.rmtree(tmp, ignore_errors=True)
    (OUT / "cues.json").write_text(json.dumps(CUES, indent=1), encoding="utf-8")
    print("saved", OUT / "raw.webm", len(CUES), "cues")


if __name__ == "__main__":
    sys.exit(main())
