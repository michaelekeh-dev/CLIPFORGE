"""Is this raw footage, or somebody's finished edit?

A video that already has subtitles burned into the picture has been edited by someone else. Clipping
it puts our captions on top of theirs — two sets of words on screen at once — and the result is the
giveaway that the source was never raw footage. Length does not catch this: a long compilation of
edited clips is still edited.

So the picture itself is read. Burned-in captions are bright, high-contrast, roughly horizontal bands
of text that sit in the same part of the frame and CHANGE as the words change — which is what tells
them apart from a logo, a watermark or a lower-third that never moves.
"""
from __future__ import annotations
from .config import cfg

SAMPLES = 14
# captions live in the middle and lower part of the frame, never right at the very top
BAND = (0.25, 0.95)


def _text_mask(gray, w: int, h: int):
    """White blobs where wide, short, bright, text-shaped things are.

    Burned-in captions are near-white letters with a dark outline, so brightness finds them where a
    gradient-and-Otsu pass does not: on a pale wall the gradient of the whole scene drowns the text."""
    import cv2
    import numpy as np
    thr = max(200, int(np.percentile(gray, 99.3)))
    bw = (gray > thr).astype(np.uint8) * 255
    # join the letters of a word, and neighbouring words, into one run
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (25, 3)))
    out = np.zeros_like(bw)
    cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in cnts:
        x, y, cw, ch = cv2.boundingRect(c)
        if ch < h * 0.015 or ch > h * 0.14:     # too thin to read, or too tall to be a caption line
            continue
        if cw < w * 0.12 or cw > w * 0.98:      # a word or two at least, but not a full-width band
            continue
        if cw / max(1, ch) < 2.5:               # caption lines are much wider than they are tall
            continue
        if cv2.contourArea(c) / max(1.0, cw * ch) < 0.25:   # a hollow outline is not a line of text
            continue
        boxes.append((x, y, cw, ch))
        cv2.rectangle(out, (x, y), (x + cw, y + ch), 255, -1)
    return out, boxes


def burned_in_captions(path: str, samples: int = SAMPLES) -> dict:
    """{captioned, share, changing, detail} for a video file."""
    try:
        import cv2
        import numpy as np
    except Exception as e:  # noqa: BLE001
        return {"captioned": False, "share": 0.0, "changing": 0.0, "detail": f"could not look: {e}"}
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return {"captioned": False, "share": 0.0, "changing": 0.0, "detail": "could not open the video"}
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if not total or not w or not h:
        cap.release()
        return {"captioned": False, "share": 0.0, "changing": 0.0, "detail": "the video has no frames"}
    y0, y1 = int(h * BAND[0]), int(h * BAND[1])
    first, last = int(total * 0.05), int(total * 0.95)
    masks, hits, checked = [], 0, 0
    for i in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(first + i * max(1, last - first) / max(1, samples - 1)))
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        checked += 1
        gray = cv2.cvtColor(frame[y0:y1], cv2.COLOR_BGR2GRAY)
        mask, boxes = _text_mask(gray, w, y1 - y0)
        if boxes:
            hits += 1
        masks.append(mask)
    cap.release()
    if not checked:
        return {"captioned": False, "share": 0.0, "changing": 0.0, "detail": "no frames could be read"}
    share = hits / checked
    # a logo or a fixed lower-third looks the same every time; words change as they are spoken
    changing = 0.0
    if len(masks) > 1:
        diffs = []
        for a, b in zip(masks, masks[1:]):
            union = float(np.count_nonzero(a | b))
            if union > 0:
                diffs.append(float(np.count_nonzero(a ^ b)) / union)
        changing = sum(diffs) / len(diffs) if diffs else 0.0
    need_share = float(cfg.get("source.caption_share", 0.5))
    need_change = float(cfg.get("source.caption_change", 0.35))
    captioned = share >= need_share and changing >= need_change
    return {"captioned": captioned, "share": round(share, 2), "changing": round(changing, 2),
            "detail": (f"text on screen in {share:.0%} of the frames looked at, changing between them "
                       f"({changing:.0%} different) — subtitles are already burned into this video"
                       if captioned else
                       f"text in {share:.0%} of frames, {changing:.0%} changing")}


def problem(path: str) -> str:
    """Why this source is not raw footage, or '' when it is fine to clip."""
    if not bool(cfg.get("source.refuse_edited", True)):
        return ""
    out = burned_in_captions(path)
    return out["detail"] if out["captioned"] else ""
