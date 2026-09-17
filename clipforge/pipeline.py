"""The whole flow for one project: source -> transcript -> moments -> fact check -> rendered clips."""
from __future__ import annotations
import json
import shutil
import time
from pathlib import Path
from . import db, media, download, transcribe, moments, factcheck, render, captions, reframe, filler, effects, brand, edits as ed, broll
from .config import cfg as _cfg, output_size, env as _env
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
            "ratio": "9:16", "layout": "auto", "style": "auto", "emoji": True, "filler": cfg.get("filler.level", "light"),
            "hook": True, "zooms": True, "progress_bar": True, "template": "", "credit_name": None}


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
                                                       "emoji": bool(opts.get("emoji", True)),
                                                       "filler": opts.get("filler", _cfg.get("filler.level", "light")),
                                                       "hook": bool(opts.get("hook", True)), "zooms": bool(opts.get("zooms", True)),
                                                       "progress_bar": bool(opts.get("progress_bar", True)),
                                                       "template": opts.get("template") or "",
                                                       "broll": bool(opts.get("broll", True))}})
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
        edits = ed.normalise(settings.get("edits"), clip["start"], clip["end"])
        tl = ed.timeline_for(edits)
        work = pdir / "work" / cid
        ratio = settings.get("ratio", "9:16")
        tr = json.loads((pdir / "transcript.json").read_text())
        tr["words"] = ed.apply_text_fixes([{"i": i, **w} for i, w in enumerate(tr["words"])], edits["text_fixes"])
        template = brand.get(settings.get("template"))
        tdata = template["data"]
        popts = proj["options"] or {}
        credit_name = popts.get("credit_name") if popts.get("credit_name") is not None else (proj.get("channel") or "")

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
        # filler words, false starts and long silences
        level = settings.get("filler", _cfg.get("filler.level", "light"))
        src_words = [w for w in tr["words"] if w["e"] > tl.start and w["s"] < tl.end
                     and not any(a <= w["s"] and w["e"] <= b for a, b in edits["deleted"])]
        cuts = []
        if level in ("light", "aggressive"):
            rms = reframe._audio_rms(speech_wav, tl.start, tl.end)
            cuts = filler.find_cuts(src_words, level, rms, start=tl.start, end=tl.end)
            tl = tl.remove([(c["s"], c["e"]) for c in cuts])
        prog("Planning zooms", 0)
        zoom_fn, zooms = None, []
        if settings.get("zooms", bool(_cfg.get("zooms.enabled", True))):
            times = effects.pick_zoom_times(data, src_words, int(_cfg.get("zooms.max_per_clip", 3)))
            zooms = effects.zoom_windows(times, tl, analysis["shots"], float(_cfg.get("zooms.amount", 1.12)))
            zoom_fn = effects.ZoomFn(zooms) if zooms else None
        framer = reframe.SmartFramer(analysis, info["width"], info["height"], ow, oh,
                                     forced_layout=settings.get("layout", "auto"), zoom_fn=zoom_fn,
                                     shot_layouts=edits["shot_layouts"])
        if tdata.get("intro_card") and settings.get("intro_card", True):
            tl.lead_in = float(_cfg.get("cards.intro_seconds", 0.8))
        if tdata.get("outro_card") and settings.get("outro_card", True):
            tl.lead_out = float(_cfg.get("cards.outro_seconds", 1.6))
        hook_on = settings.get("hook", bool(_cfg.get("hook.enabled", True)))
        hook_text = (settings.get("hook_text") or data.get("hook") or effects.hook_text_from(clip["title"], data.get("text", ""))).strip()
        extra = None
        if hook_on and hook_text:
            hook_secs = float(_cfg.get("hook.seconds", 0)) or tl.speech_duration
            split_share_h = sum(sh["end"] - sh["start"] for sh in analysis["shots"] if sh["layout"] == "split") / max(0.1, tl.end - tl.start)
            hook_extra = {"size": int(_cfg.get("hook.size_split", 80))} if split_share_h > 0.5 else {}
            hstyle, hev = effects.hook_ass(hook_text, ow, oh, min(hook_secs, tl.speech_duration - 0.3),
                                          {**hook_extra, "style": settings.get("hook_style") or tdata.get("hook_style", "card"),
                                           "box_color": _cfg.get("hook.box_color", "#FFFFFF") if (settings.get("hook_style") or tdata.get("hook_style", "card")) == "card" else tdata.get("accent", "#F5A524")},
                                          offset=tl.lead_in)
            extra = ([hstyle], hev)

        # captions
        subtitles, overlay, cap_info = None, None, {}
        if settings.get("captions", True):
            words = captions.clip_words(tr["words"], tl)
            fc_type = (data.get("fact_check") or {}).get("type", "")
            style_name = settings.get("style") or "auto"
            if style_name == "auto" and tdata.get("caption_preset", "auto") != "auto":
                style_name = tdata["caption_preset"]
            if style_name == "auto":
                style_name = _cfg.get("captions.faith_preset") if fc_type == "faith" else _cfg.get("captions.default_preset")
            st = captions.preset(style_name)
            use_emoji = settings.get("emoji", bool(_cfg.get("captions.emoji", True)))
            if use_emoji:
                captions.choose_emojis(words, float(_cfg.get("captions.emoji_every_seconds", 10)),
                                       set(data.get("key_words") or []), data.get("emojis"))
            split_share = sum(sh["end"] - sh["start"] for sh in analysis["shots"] if sh["layout"] == "split") / max(0.1, tl.end - tl.start)
            y_frac = 0.5 if (split_share > 0.5 and ratio == "9:16" and settings.get("layout", "auto") in ("auto", "split")) else None
            ass_text, em_overlays = captions.build_ass(words, ow, oh, st, ratio, key_words=data.get("key_words") or [], y_frac=y_frac,
                                                       extra=extra)
            subtitles = work / "captions.ass"
            subtitles.write_text(ass_text)
            overlay = captions.EmojiOverlay(em_overlays) if em_overlays else None
        elif extra:
            subtitles = work / "captions.ass"
            subtitles.write_text(captions.build_ass([], ow, oh, captions.preset(None), ratio, extra=extra)[0])
        # B-roll: short free stock shots when the clip mentions something visual
        prog("Finding B-roll", 0)
        broll_items = []
        try:
            broll_items = broll.plan(data, src_words, tl, settings, ratio)
        except Exception as e:  # noqa: BLE001
            db.log_error("broll", str(e))
        broll_frames = broll.BrollFrames(broll_items, ow, oh, float(_cfg.get("render.fps", 30))) if broll_items else None
        extras = [broll_frames, overlay]
        if settings.get("progress_bar", bool(tdata.get("progress_bar", True)) and bool(_cfg.get("progress_bar.enabled", True))):
            extras.append(effects.ProgressBar(tl.duration, ow, oh, tdata.get("accent") or None))
        hook_whole = bool(hook_on and hook_text) and not float(_cfg.get("hook.seconds", 0))
        wm_from = tl.lead_in + float(_cfg.get("hook.seconds", 0)) + 0.3 if (hook_on and hook_text and not hook_whole) else tl.lead_in
        extras += effects.brand_overlays(template, ow, oh, tl.duration, credit_name if settings.get("credit", True) else "",
                                         offset=tl.lead_in)
        overlay = effects.Compose(extras)
        intro_img = outro_img = None
        if tl.lead_in > 0:
            intro_img = effects.card_bgr(effects.card_image(template, ow, oh, tdata.get("watermark_text") or proj["title"][:30], ""))
        if tl.lead_out > 0:
            outro_img = effects.card_bgr(effects.card_image(template, ow, oh, tdata.get("outro_text") or "Follow for more",
                                                            tdata.get("watermark_text") or ""))
            cap_info = {"style": st["name"], "words": len(words), "emojis": [o["emoji"] for o in em_overlays],
                        "lines": ass_text.count("Dialogue: 1,")}
            shutil.copy(subtitles, out_dir / f"clip_{clip['idx']:02d}.ass")

        result = render.render_clip(src, tl, out, work, framer=framer, ratio=ratio, subtitles=subtitles, overlays=overlay, progress=prog,
                                    intro_card=intro_img, outro_card=outro_img)
        result["template"] = {"id": template["id"], "name": template["name"], "credit": credit_name}
        result["edits"] = edits
        result["lead_in"] = tl.lead_in
        result["broll"] = {"items": [{k: v for k, v in it.items() if k != "video"} for it in broll_items],
                           "source": "mock" if _env("PEXELS_MOCK_DIR") else ("pexels" if _env("PEXELS_API_KEY") else "off")}
        result["captions"] = cap_info
        result["filler"] = {"level": level, "cuts": cuts, "removed_seconds": round(sum(c["e"] - c["s"] for c in cuts), 2)}
        result["zooms"] = zooms
        result["hook"] = {"on": bool(hook_on and hook_text), "text": hook_text}
        result["reframe"] = {"layout_setting": settings.get("layout", "auto"), "shots": reframe.summary(analysis),
                             "keyframes": framer.keyframes, "lip_reader": analysis.get("lip_reader"),
                             "diarization": analysis.get("diarization")}
        thumb = out_dir / f"clip_{clip['idx']:02d}.jpg"
        big = out_dir / f"clip_{clip['idx']:02d}_thumb.jpg"
        try:
            result["thumbnail"] = effects.pick_thumbnail(out, big, seconds=5.0 + tl.lead_in)
            media.run(["ffmpeg", "-v", "error", "-y", "-i", big, "-vf", "scale=540:-2", "-q:v", "3", thumb])
        except Exception as e:  # noqa: BLE001
            db.log_error("thumbnail", str(e))
            media.thumbnail(out, thumb, t=min(1.0, tl.duration / 2), width=540)
        record = {
            **{k: v for k, v in data.items() if k not in ("options", "render", "timeline")},
            "id": cid, "project": proj["id"], "index": clip["idx"], "source": str(src), "title": clip["title"],
            "start": edits["start"], "end": edits["end"], "original_start": clip["start"], "original_end": clip["end"],
            "duration": result["duration"], "timeline": tl.as_list(),
            "score": clip["score"], "settings": settings, "render": result,
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
        if p["created_at"] is not None and p["created_at"] < cutoff and sp.exists() and sp.is_file():
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
