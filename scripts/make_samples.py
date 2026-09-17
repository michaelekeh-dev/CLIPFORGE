"""Render sample clips for a release into samples/<release>/.

    python scripts/make_samples.py 1.0 /path/to/video.mp4 --clips 3 [extra CLI args...]

Copies clip_XX.json, a small preview mp4 (720x1280, small file so it fits in git) and a contact sheet
of frames (every 0.5s) so the result can be checked by eye.
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    release, video = sys.argv[1], sys.argv[2]
    extra = sys.argv[3:]
    prefix = ""
    if extra[:1] == ["--prefix"]:
        prefix, extra = extra[1] + "_", extra[2:]
    out_dir = ROOT / "samples" / release
    out_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, "-m", "clipforge", video, *extra], cwd=ROOT, capture_output=True, text=True)
    print(r.stdout[-3000:])
    if r.returncode != 0:
        print(r.stderr[-3000:])
        sys.exit(1)
    pid = [ln.split()[-1] for ln in r.stdout.splitlines() if ln.startswith("project ")][0]
    src_dir = ROOT / "output" / pid
    notes = []
    for j in sorted(src_dir.glob("clip_*.json")):
        d = json.loads(j.read_text())
        mp4 = j.with_suffix(".mp4")
        name = prefix + j.stem
        shutil.copy(j, out_dir / f"{name}.json")
        small = out_dir / f"{name}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", mp4, "-vf", "scale=-2:1280", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "28", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", small], check=True)
        sheet = out_dir / f"{name}_frames.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", mp4, "-vf", "fps=2,scale=180:-1,tile=8x6", "-frames:v", "1", "-q:v", "5", sheet], check=True)
        notes.append(f"- **{name}.mp4** score {d['score']} · {d['duration']}s · {d['start']:.1f}-{d['end']:.1f}s · {d['title']}")
    readme = out_dir / "README.md"
    head = f"# Samples for CLIPFORGE {release}\n\n" if not (prefix and readme.exists()) else readme.read_text() + "\n"
    readme.write_text(head + f"Source: `{Path(video).name}` (project {pid})\n\n" + "\n".join(notes) + "\n")
    print(f"samples in {out_dir}")


if __name__ == "__main__":
    main()
