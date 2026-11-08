"""Record the PRProof demo with Playwright (guest mode, public repos) and note when each narration cue starts,
so scripts/narrate.py can speak the lines in parallel with what is on screen.
Run the server first (python -m specguard serve), then: python scripts/record_demo.py
Output: demo/raw.webm and demo/cues.json (cues + the stretches that narrate.py plays faster)"""
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
FAST: list = []
T0 = [0.0]


def cue(text):
    """The line starts being spoken at this moment, alongside whatever the page is doing."""
    CUES.append({"t": round(time.time() - T0[0], 2), "text": text})


class fast:
    """Waiting that is real on screen but boring to watch: narrate.py plays this stretch at `speed`."""

    def __init__(self, speed):
        self.speed = speed

    def __enter__(self):
        self.t = time.time() - T0[0]

    def __exit__(self, *a):
        FAST.append([round(self.t, 2), round(time.time() - T0[0], 2), self.speed])


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

        # 1. the owner's repos as a map
        cue("Start with any GitHub owner. I'll type mine, and you get every repo as a living map.")
        type_target(page, "github.com/Naveenaduri")
        with fast(3):
            page.wait_for_selector("#load.hide", state="attached", timeout=180_000)
        pause(2)
        cue("The categories aren't hard-coded. They're proposed from this owner's own repos, and SubQ's decision model places every repo in one, with a confidence score.")
        for x, y in [(500, 420), (700, 380), (880, 450), (640, 520), (760, 400)]:
            page.mouse.move(x, y, steps=25)
            pause(.9)
        pause(2)
        page.click(".fc >> nth=3", delay=150)
        pause(2.5)
        page.click(".fc >> nth=0", delay=150)
        pause(1.2)
        page.evaluate("pick('Naveenaduri/tinyjwt')")
        cue("Click a repo and you get its card: what it is, how mature it is, how far the model trusts that category, and one click to its pull requests, its report, or a build.")
        pause(5)
        page.mouse.move(1100, 400, steps=15)
        page.mouse.wheel(0, 380)
        pause(3.5)
        page.keyboard.press("Escape")
        pause(.8)

        # 2. every open PR of that owner, grouped by repo
        type_target(page, "github.com/Naveenaduri")
        pause(.3)
        page.click("#nav button[data-v=prs]", delay=150)
        cue("The pull requests page shows every open PR across that owner, grouped by repository. Each one is typed and ranked by how much review it needs, again decided by SubQ.")
        page.wait_for_selector(".prow", timeout=180_000)
        pause(4.5)
        page.mouse.wheel(0, 300)
        pause(2.5)
        page.evaluate("window.scrollTo(0,0)")
        pause(1)

        # 3. review a PR
        cue("Now I'll review the riskiest one. It's a refactor that claims to simplify audience validation.")
        page.locator(".prow", has_text="#2").locator("button", has_text="Review").first.click(delay=150)
        pause(.5)
        cue("While that runs, it reads the change in plain English, scores nine review categories with SubQ, and then writes its own test and runs it in a sandbox.")
        with fast(2):
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

        # 4. a clean PR
        page.goto(BASE)
        page.wait_for_selector("#tgt")
        pause(1)
        cue("Now compare that with a healthy change.")
        type_target(page, "github.com/Naveenaduri/tinyjwt/pull/1")
        with fast(2):
            page.wait_for_selector("text=Looks good", timeout=240_000)
        page.evaluate("window.scrollTo(0,0)")
        cue("Looks good. Nothing proven wrong, so it says so, and it never claims a problem it couldn't test.")
        pause(6)

        # 5. build: the form, then a PR it opened earlier
        page.click("#nav button[data-v=build]", delay=150)
        page.wait_for_selector("#bsel", timeout=60_000)
        pause(1)
        page.select_option("#bsel", "Naveenaduri/tinyjwt")
        cue("It can also build. You pick a repo, describe the feature, or start from one of its open issues, and no sign-in is needed in demo mode.")
        pause(6)
        page.goto("https://github.com/Naveenaduri/tinyjwt/pull/3")
        cue("This pull request was opened by a build. Tests were written first and failed, then the implementation made them pass, and the evidence is attached to the PR.")
        pause(3)
        page.mouse.wheel(0, 500)
        pause(4)
        page.mouse.wheel(0, 600)
        pause(5)

        cue("PRProof. Don't review AI code. Review its proof.")
        pause(4)
        ctx.close()
        video = Path(page.video.path())
        browser.close()
    shutil.move(str(video), OUT / "raw.webm")
    shutil.rmtree(tmp, ignore_errors=True)
    (OUT / "cues.json").write_text(json.dumps({"cues": CUES, "fast": FAST}, indent=1), encoding="utf-8")
    print("saved", OUT / "raw.webm", len(CUES), "cues,", len(FAST), "fast stretches")


if __name__ == "__main__":
    sys.exit(main())
