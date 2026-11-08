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


def main():
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    data = json.loads((OUT / "cues.json").read_text(encoding="utf-8"))
    cues = data["cues"]
    segs = timeline(data.get("fast", []), duration(ff, OUT / "raw.webm"))
    files = [speak(i, c["text"]) for i, c in enumerate(cues)]
    starts, free = [], 0.0
    for c, f in zip(cues, files):  # a line starts when its moment arrives, or right after the previous line ends
        s = max(remap(c["t"], segs), free)
        starts.append(s)
        free = s + duration(ff, f) + .35
    cmd = [ff, "-y", "-i", str(OUT / "raw.webm")]
    for f in files:
        cmd += ["-i", str(f)]
    vparts = [f"[0:v]trim=start={a}:end={b},setpts=(PTS-STARTPTS)/{sp}[v{i}]" for i, (a, b, sp) in enumerate(segs)]
    vcat = "".join(f"[v{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0,tpad=stop_mode=clone:stop_duration=6[vout]"
    parts = [f"[{i + 1}:a]adelay={int(s * 1000)}|{int(s * 1000)}[a{i}]" for i, s in enumerate(starts)]
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
