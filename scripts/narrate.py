"""Voice the recorded demo: speaks each cue at the moment it happened on screen (lines never overlap), then muxes to MP4.
Voice is Piper (offline neural TTS): pip install piper-tts imageio-ffmpeg, then python -m piper.download_voices en_US-ryan-high in VOICE_DIR. Run after scripts/record_demo.py. Output: demo/prproof-demo.mp4"""
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


def main():
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    cues = json.loads((OUT / "cues.json").read_text(encoding="utf-8"))
    files = [speak(i, c["text"]) for i, c in enumerate(cues)]
    starts, free = [], 0.0
    for c, f in zip(cues, files):  # a line starts when its moment arrives, or right after the previous line ends
        s = max(c["t"], free)
        starts.append(s)
        free = s + duration(ff, f) + .35
    cmd = [ff, "-y", "-i", str(OUT / "raw.webm")]
    for f in files:
        cmd += ["-i", str(f)]
    parts = [f"[{i + 1}:a]adelay={int(s * 1000)}|{int(s * 1000)}[a{i}]" for i, s in enumerate(starts)]
    mix = "".join(f"[a{i}]" for i in range(len(files))) + f"amix=inputs={len(files)}:normalize=0[aout]"
    cmd += ["-filter_complex", ";".join(parts + [mix]), "-vf", "tpad=stop_mode=clone:stop_duration=6", "-map", "0:v", "-map", "[aout]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "160k", "-t", str(round(max(free, 0) + 1.5, 1)), str(OUT / "prproof-demo.mp4")]
    subprocess.run(cmd, check=True, capture_output=True)
    for f in files:
        f.unlink(missing_ok=True)
    print("saved", OUT / "prproof-demo.mp4", f"(narration ends at ~{free:.0f}s)")


if __name__ == "__main__":
    sys.exit(main())
