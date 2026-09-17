"""Hook title, punch-in zooms and the progress bar."""
from __future__ import annotations
import math
import re
import numpy as np
from .config import cfg
from .captions import Measurer, hex_to_ass, ass_time, FONT_FILES
from pathlib import Path
from .timeline import Timeline
from .reframe import hex_bgr

H = cfg.get("hook")
Z = cfg.get("zooms")


# ----------------------------------------------------------------------------- hook
def hook_text_from(title: str, text: str, max_words: int | None = None) -> str:
    """Heuristic hook: the honest title trimmed to 6-8 words."""
    max_words = max_words or int(H.get("max_words", 8))
    t = re.sub(r"\s+", " ", title or "").strip().rstrip(".")
    words = t.split()
    if len(words) > max_words:
        t = " ".join(words[:max_words]).rstrip(",;:") + "..."
    if len(words) < 3 and text:
        t = " ".join(text.split()[:max_words])
    return t


def hook_ass(text: str, out_w: int, out_h: int, seconds: float, style: dict | None = None, offset: float = 0.0) -> tuple[str, list[str]]:
    """Returns (style line, event lines). Box slides in from the top, fades out."""
    st = {**H, **(style or {})}
    scale = out_w / 1080.0
    size = int(st["size"] * scale)
    m = Measurer(st["font"], size)
    max_w = out_w * 0.84
    # wrap into up to 2 lines
    words = text.split()
    lines, cur = [], []
    for w in words:
        if cur and m.width(" ".join(cur + [w])) > max_w:
            lines.append(" ".join(cur))
            cur = []
        cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    lines = lines[:2]
    if len(lines) == 2 and m.width(lines[1]) > max_w:
        lines[1] = lines[1][: int(len(lines[1]) * max_w / m.width(lines[1]))].rsplit(" ", 1)[0] + "..."
    txt = "\\N".join(l.replace("{", "(").replace("}", ")") for l in lines)
    y = int(out_h * float(st.get("y_frac", 0.10)))
    kind = st.get("style", "box")
    text_c = hex_to_ass(st.get("text_color", "#111111"))
    box_c = hex_to_ass(st.get("box_color", "#F5A524"))
    pad = int(18 * scale)
    if kind == "box":
        style_line = (f"Style: Hook,{st['font']},{size},{text_c},{text_c},{box_c},{box_c},0,0,0,0,100,100,0,0,3,{pad},0,8,40,40,0,1")
        tags = ""
    elif kind == "bar":
        # full-width bar: text on a wide box
        style_line = (f"Style: Hook,{st['font']},{size},{text_c},{text_c},{box_c},{box_c},0,0,0,0,100,100,0,0,3,{pad},0,8,0,0,0,1")
        tags = f"\\xbord{out_w}"
    else:
        style_line = (f"Style: Hook,{st['font']},{size},{hex_to_ass(st.get('box_color', '#FFFFFF'))},{text_c},{hex_to_ass('#000000')},{hex_to_ass('#000000', 0x60)},0,0,0,0,100,100,0,0,1,{int(6 * scale)},{int(3 * scale)},8,40,40,0,1")
        tags = ""
    ev = (f"Dialogue: 3,{ass_time(offset)},{ass_time(offset + seconds)},Hook,,0,0,0,,"
          f"{{\\an8\\move({out_w // 2},{y - int(60 * scale)},{out_w // 2},{y},0,260)\\fad(180,260)\\fscx96\\fscy96\\t(0,260,\\fscx100\\fscy100){tags}}}{txt}")
    return style_line, [ev]


# ----------------------------------------------------------------------------- zooms
def pick_zoom_times(data: dict, words: list[dict], max_n: int) -> list[float]:
    """Source-time moments worth a punch-in: Claude's picks, else key words and '?!' sentence ends."""
    picks = []
    for z in data.get("zooms") or []:
        try:
            picks.append(float(z))
        except (TypeError, ValueError):
            pass
    if not picks:
        keys = {k.lower().strip(".,!?") for k in (data.get("key_words") or [])}
        for w in words:
            base = re.sub(r"[^a-zA-Z']", "", w["w"]).lower()
            if base in keys or w["w"].endswith(("!", "?")):
                picks.append(w["s"])
    picks = sorted(set(round(p, 2) for p in picks))
    out: list[float] = []
    for p in picks:
        if out and p - out[-1] < 4.0:
            continue
        out.append(p)
    # spread across the clip: keep the earliest max_n but not in the first 1.5s
    if words:
        out = [p for p in out if p > words[0]["s"] + 1.5]
    return out[:max_n]


def zoom_windows(times_src: list[float], tl: Timeline, shots: list[dict], amount: float) -> list[dict]:
    """Output-time windows [{s, peak_s, peak_e, e, amount}] that never cross a camera cut."""
    ein, hold, eout = float(Z.get("ease_in", 0.35)), float(Z.get("hold", 1.1)), float(Z.get("ease_out", 0.5))
    margin = float(Z.get("cut_margin", 0.3))
    cuts = sorted({sh["start"] for sh in shots[1:]} | {sh["end"] for sh in shots[:-1]})
    out = []
    for t in times_src:
        s_src = t - 0.15
        e_src = s_src + ein + hold + eout
        # shrink to stay inside the shot
        for c in cuts:
            if s_src - margin < c < e_src + margin:
                if c <= t:
                    s_src = c + margin
                else:
                    e_src = c - margin
        if e_src - s_src < ein + eout + 0.2:
            continue
        s_out, e_out = tl.to_output(s_src), tl.to_output(e_src)
        if s_out is None or e_out is None:
            continue
        # cuts from filler removal inside the window? keep the window if it maps monotonically
        if e_out - s_out < ein + eout + 0.1:
            continue
        hold_here = min(hold, (e_out - s_out) - ein - eout)
        out.append({"s": round(s_out, 3), "peak_s": round(s_out + ein, 3), "peak_e": round(s_out + ein + hold_here, 3),
                    "e": round(s_out + ein + hold_here + eout, 3), "amount": amount, "src": round(t, 3)})
    return out


def _ease(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)  # smoothstep


class ZoomFn:
    def __init__(self, windows: list[dict]):
        self.w = windows

    def __call__(self, t: float) -> float:
        for z in self.w:
            if z["s"] <= t <= z["e"]:
                a = z["amount"] - 1.0
                if t < z["peak_s"]:
                    return 1.0 + a * _ease((t - z["s"]) / max(1e-6, z["peak_s"] - z["s"]))
                if t <= z["peak_e"]:
                    return z["amount"]
                return 1.0 + a * (1 - _ease((t - z["peak_e"]) / max(1e-6, z["e"] - z["peak_e"])))
        return 1.0


# ----------------------------------------------------------------------------- overlays
class ProgressBar:
    def __init__(self, duration: float, out_w: int, out_h: int, color: str | None = None, height: int | None = None):
        c = cfg.get("progress_bar")
        self.d = max(0.1, duration)
        self.h = int(height or c.get("height", 8))
        self.col = hex_bgr(color or c.get("color", "#F5A524"))
        self.w = out_w
        self.track = tuple(int(v * 0.25) for v in self.col)

    def __call__(self, frame: np.ndarray, t_src: float, t_out: float) -> np.ndarray:
        frame[0:self.h, :] = self.track
        x = int(self.w * min(1.0, t_out / self.d))
        frame[0:self.h, :x] = self.col
        return frame


class Compose:
    def __init__(self, fns):
        self.fns = [f for f in fns if f]

    def __call__(self, frame, t_src, t_out):
        for f in self.fns:
            frame = f(frame, t_src, t_out)
        return frame


# ----------------------------------------------------------------------------- image overlays (watermark, credit, cards)
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from .captions import FONT_FILES, FONTS_DIR  # noqa: E402


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS_DIR / FONT_FILES.get(name, "Montserrat-Bold.ttf")), size)


def text_image(text: str, size: int, color: str = "#FFFFFF", font: str = "Montserrat Bold", emoji_font: str | None = None,
               shadow: bool = True) -> Image.Image:
    """Render a short text (may contain one leading emoji) to an RGBA image."""
    emoji = ""
    if text and ord(text[0]) > 0x2000:
        # leading emoji drawn with the colour emoji font
        emoji, text = text[0], text[1:].lstrip()
        if text and text[0] == "️":
            text = text[1:].lstrip()
    f = _font(font, size)
    tw = int(f.getlength(text)) + 4
    th = int(size * 1.4)
    ew = int(size * 1.15) if emoji else 0
    im = Image.new("RGBA", (tw + ew + 8, th + 8), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x = 4
    if emoji:
        try:
            ef = ImageFont.truetype(emoji_font or cfg.get("captions.emoji_font"), 109)
            e = Image.new("RGBA", (140, 140), (0, 0, 0, 0))
            ImageDraw.Draw(e).text((8, 4), emoji, font=ef, embedded_color=True)
            bb = e.getbbox()
            if bb:
                e = e.crop(bb).resize((int(size * 1.05), int(size * 1.05)), Image.LANCZOS)
                im.alpha_composite(e, (x, (th + 8 - e.height) // 2))
        except Exception:
            pass
        x += ew
    if shadow:
        d.text((x + 2, 6), text, font=f, fill=(0, 0, 0, 160))
    d.text((x, 4), text, font=f, fill=color)
    return im


class ImageOverlay:
    """Draw an RGBA image at (x, y) between s and e (output seconds) with fades."""

    def __init__(self, img: Image.Image, x: int, y: int, s: float = 0.0, e: float = 1e9, fade: float = 0.3, alpha: float = 1.0):
        self.img = np.array(img.convert("RGBA"))
        self.x, self.y, self.s, self.e, self.fade, self.alpha = int(x), int(y), s, e, fade, alpha

    def __call__(self, frame: np.ndarray, t_src: float, t_out: float) -> np.ndarray:
        if not (self.s <= t_out < self.e):
            return frame
        a = self.alpha
        if self.fade > 0:
            a *= min(1.0, (t_out - self.s) / self.fade, (self.e - t_out) / self.fade)
        if a <= 0:
            return frame
        from .captions import _blend
        img = self.img
        if a < 1.0:
            img = img.copy()
            img[:, :, 3] = (img[:, :, 3] * a).astype(np.uint8)
        _blend(frame, img, self.x, self.y)
        return frame


def load_logo(path: str, height: int) -> Image.Image | None:
    try:
        im = Image.open(path).convert("RGBA")
        w = max(1, int(im.width * height / im.height))
        return im.resize((w, height), Image.LANCZOS)
    except Exception:
        return None


def brand_overlays(template: dict, out_w: int, out_h: int, duration: float, credit_name: str = "", offset: float = 0.0,
                   watermark_from: float = 0.0) -> list:
    """Watermark + logo (whole clip, top right) and the source credit (bottom, first 3s)."""
    d = template.get("data", template)
    scale = out_w / 1080.0
    out = []
    y = int(out_h * 0.075)
    x_right = int(out_w * 0.94)
    if d.get("watermark_text"):
        im = text_image(d["watermark_text"], int(30 * scale), "#FFFFFF", "Montserrat SemiBold")
        logo = load_logo(d["logo"], int(40 * scale)) if d.get("logo") else None
        x = x_right - im.width - (logo.width + int(10 * scale) if logo else 0)
        out.append(ImageOverlay(im, x, y, watermark_from, duration + 1, fade=0.3, alpha=0.85))
        if logo:
            out.append(ImageOverlay(logo, x_right - logo.width, y - (logo.height - im.height) // 2, watermark_from, duration + 1, fade=0.3, alpha=0.95))
    elif d.get("logo"):
        logo = load_logo(d["logo"], int(44 * scale))
        if logo:
            out.append(ImageOverlay(logo, x_right - logo.width, y, watermark_from, duration + 1, fade=0.3, alpha=0.95))
    if d.get("credit", True) and credit_name:
        name = credit_name if credit_name.startswith("@") else "@" + credit_name.replace(" ", "")
        im = text_image("🎙 " + name, int(30 * scale), "#FFFFFF", "Montserrat SemiBold")
        out.append(ImageOverlay(im, (out_w - im.width) // 2, int(out_h * 0.775), offset + 0.2, offset + 3.2, fade=0.3, alpha=0.9))
    return out


def card_bgr(im: Image.Image) -> np.ndarray:
    return np.ascontiguousarray(np.array(im.convert("RGB"))[:, :, ::-1])


def card_image(template: dict, out_w: int, out_h: int, text: str, sub: str = "") -> Image.Image:
    """Intro/outro card: dark background, accent line, logo, text."""
    d = template.get("data", template)
    im = Image.new("RGBA", (out_w, out_h), (15, 17, 21, 255))
    dr = ImageDraw.Draw(im)
    scale = out_w / 1080.0
    acc = d.get("accent", "#F5A524")
    y = int(out_h * 0.42)
    logo = load_logo(d["logo"], int(120 * scale)) if d.get("logo") else None
    if logo:
        im.alpha_composite(logo, ((out_w - logo.width) // 2, y - logo.height - int(30 * scale)))
    f = _font("Montserrat ExtraBold", int(64 * scale))
    tw = f.getlength(text)
    dr.text(((out_w - tw) / 2, y), text, font=f, fill="#FFFFFF")
    dr.rectangle([(out_w // 2 - int(60 * scale), y + int(95 * scale)), (out_w // 2 + int(60 * scale), y + int(101 * scale))], fill=acc)
    if sub:
        f2 = _font("Montserrat SemiBold", int(34 * scale))
        dr.text(((out_w - f2.getlength(sub)) / 2, y + int(125 * scale)), sub, font=f2, fill="#9AA1B4")
    return im


# ----------------------------------------------------------------------------- auto thumbnail
def pick_thumbnail(video: Path, out: Path, seconds: float = 5.0, step: float = 0.25) -> dict:
    """Sharpest frame with a clear face in the first `seconds` of the rendered clip."""
    import subprocess
    import cv2
    from .reframe import YUNET
    from . import media
    info = media.probe(video)
    w, h = info["width"], info["height"]
    det = cv2.FaceDetectorYN.create(str(YUNET), "", (w, h), 0.6, 0.3, 20)
    best, best_score, best_t = None, -1.0, 0.0
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-t", f"{seconds:.2f}", "-i", str(video), "-vf", f"fps={1 / step}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10 ** 7)
    i = 0
    try:
        while True:
            buf = p.stdout.read(w * h * 3)
            if len(buf) < w * h * 3:
                break
            img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            sharp = cv2.Laplacian(gray, cv2.CV_64F).var()
            n, faces = det.detect(img)
            face_score = 0.0
            if faces is not None and len(faces):
                f = max(faces, key=lambda r: r[3])
                face_score = float(f[14]) * min(1.0, f[3] / (0.12 * h))
                # penalise faces cut off at the top
                if f[1] < 0.02 * h:
                    face_score *= 0.5
            score = (0.3 + face_score) * math.log1p(sharp)
            if score > best_score:
                best, best_score, best_t = img.copy(), score, i * step
            i += 1
    finally:
        p.stdout.close()
        p.wait()
    if best is None:
        media.thumbnail(video, out, t=0.5, width=w)
        return {"t": 0.5, "score": 0}
    cv2.imwrite(str(out), best, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return {"t": round(best_t, 2), "score": round(float(best_score), 2)}
