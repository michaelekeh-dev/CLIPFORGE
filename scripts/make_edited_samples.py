"""Samples for the editor release: run the pipeline, then apply real edits to each clip and re-render.

    python scripts/make_edited_samples.py 1.5 video.mp4 [extra CLI args]
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from clipforge import db, pipeline, edits as ed  # noqa: E402
from clipforge.config import PROJECTS  # noqa: E402


def main():
    release, video, extra = sys.argv[1], sys.argv[2], sys.argv[3:]
    out_dir = ROOT / "samples" / release
    out_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, "-m", "clipforge", video, *extra], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:])
        sys.exit(1)
    pid = [ln.split()[-1] for ln in r.stdout.splitlines() if ln.startswith("project ")][0]
    db.init_db()
    tr = json.loads((PROJECTS / pid / "transcript.json").read_text())
    words = [{"i": i, **w} for i, w in enumerate(tr["words"])]
    notes = []
    for k, clip in enumerate(db.rows("SELECT * FROM clips WHERE project_id=? ORDER BY idx", (pid,))):
        c = db.loads(clip, "data", "settings")
        inside = [w for w in words if w["s"] >= c["start"] and w["e"] <= c["end"]]
        e = {"start": c["start"], "end": c["end"], "deleted": [], "text_fixes": {}, "shot_layouts": {}}
        what = []
        if k == 0 and len(inside) > 12:
            # cut two words in the middle and fix one word's caption text
            mid = inside[len(inside) // 2]
            e["deleted"] = ed.word_ranges(words, [mid["i"], mid["i"] + 1])
            e["text_fixes"] = {str(inside[3]["i"]): inside[3]["w"].upper()}
            what.append(f"cut out '{mid['w']} {words[mid['i'] + 1]['w']}', caption word '{inside[3]['w']}' fixed to upper case")
        if k == 1:
            # extend the clip 4 seconds past its original end
            e["end"] = min(c["end"] + 4.0, tr["words"][-1]["e"])
            what.append("end extended by 4s past the original range")
        if k == 2:
            shots = ((c["data"].get("render") or {}).get("reframe") or {}).get("shots") or []
            if shots:
                e["shot_layouts"] = {str(shots[0]["start"]): "wide"}
                what.append(f"first shot forced to 'wide'")
            e["start"] = max(0.0, c["start"] - 2.0)
            what.append("start moved 2s earlier")
        settings = {**(c["settings"] or {}), "edits": e, "hook_text": "Edited in the CLIPFORGE editor"}
        db.update("clips", c["id"], {"settings": settings})
        pipeline.render_one(c["id"])
        src = PROJECTS / pid / "clips"
        name = f"clip_{c['idx']:02d}"
        d = json.loads((src / f"{name}.json").read_text())
        shutil.copy(src / f"{name}.json", out_dir / f"{name}.json")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src / f"{name}.mp4", "-vf", "scale=-2:1280", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "28", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out_dir / f"{name}.mp4"], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src / f"{name}.mp4", "-vf", "fps=2,scale=180:-1,tile=8x6", "-frames:v", "1", "-q:v", "5",
                        out_dir / f"{name}_frames.jpg"], check=True)
        notes.append(f"- **{name}.mp4** score {int(d['score'])} · {d['duration']}s · {d['start']:.1f}-{d['end']:.1f}s · {d['title']}\n  - edits: {'; '.join(what) or 'none'}")
    (out_dir / "README.md").write_text(f"# Samples for CLIPFORGE {release}\n\nSource: `{Path(video).name}` (project {pid}). Each clip was re-rendered after edits made through the editor API.\n\n" + "\n".join(notes) + "\n")
    print(f"samples in {out_dir}")


if __name__ == "__main__":
    main()
