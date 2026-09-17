"""The whole flow for one project: source -> transcript -> moments -> fact check -> rendered clips."""
from __future__ import annotations
import json
import shutil
import time
from pathlib import Path
from . import db, media, download, transcribe, moments, factcheck, render, captions, reframe
from .config import cfg as _cfg, output_size
from .config import PROJECTS, cfg
from .timeline import Timeline


def project_dir(pid: str) -> Path:
    d = PROJECTS / pid
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_project(source: str, options: dict, title: str = "") -> str:
    pid = db.new_id("p_")
    is_url = download.is_url(source)
    db.insert("projects", {
        "id": pid, "title": title or (source if is_url else Path(source).stem), "source_url": source if is_url else "",
        "source_type": "url" if is_url else "upload", "source_path": "" if is_url else source,
        "options": options, "status": "queued", "stage": "Waiting to start",
    })
    return pid


def default_options() -> dict:
    return {"clips": int(cfg.get("moments.default_clips", 5)), "length": "auto",
            "keywords": list(cfg.get("moments.default_keywords", [])), "start": None, "end": None,
            "ratio": "9:16", "layout": "auto", "style": "auto", "emoji": True}


def run_project(pid: str, progress) -> None:
    proj = db.loads(db.row("SELECT * FROM projects WHERE id=?", (pid,)), "options", "info")
    if not proj:
        raise RuntimeError("Project not found")
    opts = {**default_options(), **(proj["options"] or {})}
    pdir = project_dir(pid)

    # 1. source
    progress("Downloading", 1)
    if proj["source_type"] == "url":
        meta = download.download(proj["source_url"], progress)
        src = Path(meta["path"])
        info = meta
    else:
        src = Path(proj["source_path"])
        if not src.exists():
            raise RuntimeError("The uploaded file is missing. Please upload it again.")
        if src.parent != pdir:
            info = download.import_upload(src, pdir, proj["title"])
            src = Path(info["path"])
        else:
            info = {"title": proj["title"], "channel": proj.get("channel", ""), "duration": media.probe(src)["duration"]}
    thumb = pdir / "thumb.jpg"
    if not thumb.exists():
        try:
            media.thumbnail(src, thumb, t=min(5.0, max(0.0, float(info.get("duration") or 2) / 2)))
        except Exception:
            pass
    db.update("projects", pid, {"title": info.get("title") or proj["title"], "channel": info.get("channel") or proj.get("channel") or "",
                                "duration": float(info.get("duration") or 0), "source_path": str(src),
                                "thumbnail": str(thumb) if thumb.exists() else "", "info": info})

    # 2. transcript
    progress("Transcribing", 20)
    tr = transcribe.transcribe(src, pdir, progress, start=opts.get("start"), end=opts.get("end"))
    if not tr["words"]:
        raise RuntimeError("No speech was found in this video.")

    # 3. moments
    progress("Finding best moments", 40)
    picked, method = moments.pick_moments(tr, int(opts.get("clips") or 5), opts.get("length") or "auto",
                                          opts.get("keywords") or [], progress)
    if not picked:
        raise RuntimeError("Could not find any usable moments. Try a longer part of the video.")
    db.update("projects", pid, {"info": {**info, "pick_method": method, "transcript_backend": tr.get("backend")}})

    # remove old clips of this project (a re-run)
    for old in db.rows("SELECT * FROM clips WHERE project_id=?", (pid,)):
        db.execute("DELETE FROM clips WHERE id=?", (old["id"],))

    # 4. per clip: fact check + render
    clip_ids = []
    for i, m in enumerate(picked, start=1):
        cid = db.new_id("c_")
        clip_text = " ".join(w["w"] for w in tr["words"][m["wi"]:m["wj"] + 1])
        progress(f"Checking facts: clip {i} of {len(picked)}", 55 + 3 * i / len(picked))
        fc = factcheck.check_clip(clip_text, m.get("title", ""))
        data = {**m, "text": clip_text, "fact_check": fc, "pick_method": method, "options": opts,
                "honest_title": fc.get("honest_title") or m.get("title")}
        db.insert("clips", {"id": cid, "project_id": pid, "idx": i, "start": m["start"], "end": m["end"],
                            "score": m["score"], "title": data["honest_title"], "status": "pending",
                            "data": data, "settings": {"ratio": opts.get("ratio", "9:16"), "layout": opts.get("layout", "auto"),
                                                       "style": opts.get("style", "auto"),
                                                       "emoji": bool(opts.get("emoji", True))}})
        clip_ids.append(cid)
    for i, cid in enumerate(clip_ids, start=1):
        base = 60 + 40 * (i - 1) / len(clip_ids)
        span = 40 / len(clip_ids)
        progress(f"Rendering clip {i} of {len(clip_ids)}", base)
        render_one(cid, lambda stage, pct=None, status=None: progress(f"Rendering clip {i} of {len(clip_ids)}", base + span * (pct or 0) / 100))
    progress("Done", 100)


def render_one(cid: str, progress=None) -> dict:
    """(Re)render one clip from its stored decisions + settings."""
    clip = db.loads(db.row("SELECT * FROM clips WHERE id=?", (cid,)), "data", "settings")
    proj = db.loads(db.row("SELECT * FROM projects WHERE id=?", (clip["project_id"],)), "options", "info")
    pdir = project_dir(proj["id"])
    src = Path(proj["source_path"])
    settings = clip["settings"] or {}
    data = clip["data"] or {}
    db.update("clips", cid, {"status": "rendering", "stage": "Rendering", "progress": 0, "error": ""})
    try:
        out_dir = pdir / "clips"
        out_dir.mkdir(exist_ok=True)
        out = out_dir / f"clip_{clip['idx']:02d}.mp4"
        tl = Timeline.single(clip["start"], clip["end"])
        work = pdir / "work" / cid
        ratio = settings.get("ratio", "9:16")

        def prog(stage, pct=None, status=None):
            db.update("clips", cid, {"stage": stage, "progress": pct or 0})
            if progress:
                progress(stage, pct)

        work.mkdir(parents=True, exist_ok=True)
        # smart reframe (faces, cuts, layout)
        prog("Finding faces and camera cuts", 0)
        info = media.probe(src)
        ow, oh = output_size(ratio)
        speech_wav = work / "speech16k.wav"
        media.extract_audio(src, speech_wav, sr=16000, mono=True, start=tl.start, dur=tl.end - tl.start)
        analysis = reframe.analyze(src, tl.start, tl.end, pdir / "analysis", audio_wav=speech_wav,
                                   progress=lambda st, pct=None, status=None: prog(st, pct))
        framer = reframe.SmartFramer(analysis, info["width"], info["height"], ow, oh,
                                     forced_layout=settings.get("layout", "auto"))

        # captions
        subtitles, overlay, cap_info = None, None, {}
        if settings.get("captions", True):
            tr = json.loads((pdir / "transcript.json").read_text())
            words = captions.clip_words(tr["words"], tl)
            fc_type = (data.get("fact_check") or {}).get("type", "")
            style_name = settings.get("style") or "auto"
            if style_name == "auto":
                style_name = _cfg.get("captions.faith_preset") if fc_type == "faith" else _cfg.get("captions.default_preset")
            st = captions.preset(style_name)
            use_emoji = settings.get("emoji", bool(_cfg.get("captions.emoji", True)))
            if use_emoji:
                captions.choose_emojis(words, float(_cfg.get("captions.emoji_every_seconds", 10)),
                                       set(data.get("key_words") or []), data.get("emojis"))
            split_share = sum(sh["end"] - sh["start"] for sh in analysis["shots"] if sh["layout"] == "split") / max(0.1, tl.end - tl.start)
            y_frac = 0.5 if (split_share > 0.5 and ratio == "9:16" and settings.get("layout", "auto") in ("auto", "split")) else None
            ass_text, em_overlays = captions.build_ass(words, ow, oh, st, ratio, key_words=data.get("key_words") or [], y_frac=y_frac)
            subtitles = work / "captions.ass"
            subtitles.write_text(ass_text)
            overlay = captions.EmojiOverlay(em_overlays) if em_overlays else None
            cap_info = {"style": st["name"], "words": len(words), "emojis": [o["emoji"] for o in em_overlays],
                        "lines": ass_text.count("Dialogue: 1,")}
            shutil.copy(subtitles, out_dir / f"clip_{clip['idx']:02d}.ass")

        result = render.render_clip(src, tl, out, work, framer=framer, ratio=ratio, subtitles=subtitles, overlays=overlay, progress=prog)
        result["captions"] = cap_info
        result["reframe"] = {"layout_setting": settings.get("layout", "auto"), "shots": reframe.summary(analysis),
                             "keyframes": framer.keyframes, "lip_reader": analysis.get("lip_reader"),
                             "diarization": analysis.get("diarization")}
        thumb = out_dir / f"clip_{clip['idx']:02d}.jpg"
        media.thumbnail(out, thumb, t=min(1.0, tl.duration / 2), width=540)
        record = {
            "id": cid, "project": proj["id"], "index": clip["idx"], "source": str(src), "title": clip["title"],
            "start": clip["start"], "end": clip["end"], "duration": result["duration"], "timeline": tl.as_list(),
            "score": clip["score"], "settings": settings, "render": result, **{k: v for k, v in data.items() if k != "options"},
            "rendered_at": time.time(),
        }
        render.write_json(out_dir / f"clip_{clip['idx']:02d}.json", record)
        db.update("clips", cid, {"status": "done", "stage": "Done", "progress": 100, "path": str(out), "thumbnail": str(thumb),
                                 "data": {**data, "render": result, "timeline": tl.as_list()}})
        shutil.rmtree(work, ignore_errors=True)
        return record
    except Exception as e:  # noqa: BLE001
        db.update("clips", cid, {"status": "error", "error": str(e)[:600]})
        raise


def delete_project(pid: str):
    db.execute("DELETE FROM clips WHERE project_id=?", (pid,))
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
    shutil.rmtree(PROJECTS / pid, ignore_errors=True)


def cleanup_old_sources(days: float | None = None):
    """Delete source videos (not clips) older than N days. Returns bytes freed."""
    days = days if days is not None else float(cfg.get("app.delete_sources_after_days", 7))
    cutoff = time.time() - days * 86400
    freed = 0
    for p in db.rows("SELECT id, source_path, created_at FROM projects"):
        sp = Path(p["source_path"] or "")
        if p["created_at"] and p["created_at"] < cutoff and sp.exists() and sp.is_file():
            freed += sp.stat().st_size
            sp.unlink(missing_ok=True)
            for extra in ("audio16k.wav",):
                (PROJECTS / p["id"] / extra).unlink(missing_ok=True)
    from .download import DL_DIR
    for f in DL_DIR.glob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            freed += f.stat().st_size
            f.unlink(missing_ok=True)
    return freed
