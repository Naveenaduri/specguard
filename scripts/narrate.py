"""Voice the recorded demo: speaks each cue at the moment it happened on screen (lines never overlap), plays the boring
waiting stretches faster, and muxes to MP4.
Voice is Piper (offline neural TTS): pip install piper-tts imageio-ffmpeg, then python -m piper.download_voices en_US-ryan-high
in VOICE_DIR. Run after scripts/record_demo.py. Output: demo/prproof-demo.mp4"""
import json
import os
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

OUT = Path(__file__).resolve().parent.parent / "demo"
VOICE_DIR = Path(os.getenv("VOICE_DIR", str(OUT.parent.parent / "voices")))
MODEL = VOICE_DIR / "en_US-ryan-high.onnx"


def speak(i, text):
    f = OUT / f"cue{i}.wav"
    subprocess.run([sys.executable, "-m", "piper", "-m", str(MODEL), "-f", str(f), "--length-scale", "1.08", "--sentence-silence", "0.3"],
                   input=text, text=True, check=True, capture_output=True)
    return f


def duration(ff, f):
    r = subprocess.run([ff, "-i", str(f)], capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "Duration:" in line:
            h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return 3.0


def timeline(fast, total):
    """Split the recording into normal and sped-up stretches: [(start, end, speed)]."""
    segs, t = [], 0.0
    for a, b, sp in sorted(fast):
        if a > t:
            segs.append((t, a, 1.0))
        segs.append((a, b, sp))
        t = b
    if t < total:
        segs.append((t, total, 1.0))
    return segs


def remap(t, segs):
    """Time in the raw recording -> time in the sped-up video."""
    out = 0.0
    for a, b, sp in segs:
        if t <= a:
            break
        out += (min(t, b) - a) / sp
    return out


STILL = OUT / "signed_in_build.png"
STILL_SECS = 12
STILL_LINE = ("Signed in, the same build runs on your own repos, private ones too. It takes an issue or a feature request, "
              "writes failing tests first, implements, inspects, and opens a PR you approve.")


def main():
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    data = json.loads((OUT / "cues.json").read_text(encoding="utf-8"))
    cues, insert_at = list(data["cues"]), None
    if STILL.exists() and not any(c["text"].startswith("Signed in") for c in cues):  # recordings made before the still existed
        k = next((i for i, c in enumerate(cues) if c["text"].startswith("This pull request was opened by a build")), None)
        if k is not None:
            insert_at = cues[k]["t"]
            cues.insert(k, {"t": insert_at, "text": STILL_LINE})
    segs = timeline(data.get("fast", []), duration(ff, OUT / "raw.webm"))
    items, placed = [], insert_at is None
    for a, b, sp in segs:
        if not placed and a >= insert_at:
            items.append("STILL")
            placed = True
        if not placed and a < insert_at < b:
            items += [(a, insert_at, sp), "STILL", (insert_at, b, sp)]
            placed = True
        else:
            items.append((a, b, sp))
    if not placed:
        items.append("STILL")
    files = [speak(i, c["text"]) for i, c in enumerate(cues)]
    starts, free, shift = [], 0.0, 0.0
    for c, f in zip(cues, files):  # a line starts when its moment arrives, or right after the previous line ends
        s = max(remap(c["t"], segs) + shift, free)
        starts.append(s)
        free = s + duration(ff, f) + .35
        if insert_at is not None and c["text"] == STILL_LINE:
            shift = STILL_SECS
    cmd = [ff, "-y", "-i", str(OUT / "raw.webm")]
    base = 1
    if insert_at is not None:
        cmd += ["-loop", "1", "-framerate", "25", "-t", str(STILL_SECS), "-i", str(STILL)]
        base = 2
    for f in files:
        cmd += ["-i", str(f)]
    norm = "fps=25,scale=1280:720,format=yuv420p,setsar=1"
    vparts = []
    for i, it in enumerate(items):
        if it == "STILL":
            vparts.append(f"[1:v]scale=1280:-2,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=0x05070d,"
                          f"zoompan=z='min(zoom+0.0006,1.1)':d={STILL_SECS * 25}:s=1280x720:fps=25,{norm}[v{i}]")
        else:
            a, b, sp = it
            vparts.append(f"[0:v]trim=start={a}:end={b},setpts=(PTS-STARTPTS)/{sp},{norm}[v{i}]")
    vcat = "".join(f"[v{i}]" for i in range(len(items))) + f"concat=n={len(items)}:v=1:a=0,tpad=stop_mode=clone:stop_duration=6[vout]"
    parts = [f"[{i + base}:a]adelay={int(s * 1000)}|{int(s * 1000)}[a{i}]" for i, s in enumerate(starts)]
    mix = "".join(f"[a{i}]" for i in range(len(files))) + f"amix=inputs={len(files)}:normalize=0[aout]"
    cmd += ["-filter_complex", ";".join(vparts + [vcat] + parts + [mix]), "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "160k", "-t", str(round(free + 1.5, 1)),
            str(OUT / "prproof-demo.mp4")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr[-1500:])
    for f in files:
        f.unlink(missing_ok=True)
    print("saved", OUT / "prproof-demo.mp4", f"(narration ends at ~{free:.0f}s)")


if __name__ == "__main__":
    sys.exit(main())
