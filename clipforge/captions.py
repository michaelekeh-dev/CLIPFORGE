"""Animated captions: word-timed .ass subtitles (burned by ffmpeg/libass) + emoji overlays drawn per frame."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from .config import cfg, ASSETS
from .timeline import Timeline

FONT_FILES = {
    "Montserrat ExtraBold": "Montserrat-ExtraBold.ttf", "Montserrat Bold": "Montserrat-Bold.ttf",
    "Montserrat SemiBold": "Montserrat-SemiBold.ttf", "Montserrat Black": "Montserrat-Black.ttf",
    "Anton": "Anton-Regular.ttf", "Bebas Neue": "BebasNeue-Regular.ttf",
    "Poppins Bold": "Poppins-Bold.ttf", "Poppins SemiBold": "Poppins-SemiBold.ttf",
}
FONTS_DIR = ASSETS / "fonts"

EMOJI_WORDS = {
    "fire": "🔥", "hot": "🔥", "burn": "🔥", "money": "💰", "cash": "💰", "rich": "💰", "dollars": "💵", "pay": "💸",
    "god": "🙏", "pray": "🙏", "prayer": "🙏", "jesus": "✝️", "cross": "✝️", "bible": "📖", "scripture": "📖", "book": "📖",
    "heaven": "☁️", "angel": "😇", "angels": "😇", "devil": "😈", "demon": "😈", "hell": "🔥", "church": "⛪",
    "love": "❤️", "heart": "❤️", "crazy": "🤯", "insane": "🤯", "mind": "🧠", "brain": "🧠", "think": "🤔", "idea": "💡",
    "light": "💡", "world": "🌍", "earth": "🌍", "planet": "🪐", "space": "🚀", "rocket": "🚀", "aliens": "👽", "alien": "👽",
    "ufo": "🛸", "stars": "✨", "star": "⭐", "moon": "🌙", "sun": "☀️", "water": "💧", "ocean": "🌊", "sea": "🌊", "rain": "🌧️",
    "storm": "⛈️", "lightning": "⚡", "power": "⚡", "energy": "⚡", "time": "⏳", "clock": "⏰", "history": "📜",
    "ancient": "🏛️", "pyramids": "🔺", "pyramid": "🔺", "egypt": "🐫", "king": "👑", "queen": "👑", "crown": "👑",
    "war": "⚔️", "fight": "🥊", "sword": "🗡️", "death": "💀", "dead": "💀", "skull": "💀", "blood": "🩸", "ghost": "👻",
    "scary": "😱", "scared": "😱", "fear": "😨", "afraid": "😨", "wow": "😮", "shocked": "😮", "laugh": "😂", "funny": "😂",
    "joke": "😂", "cry": "😢", "sad": "😢", "happy": "😊", "smile": "😊", "angry": "😡", "mad": "😡", "eyes": "👀", "look": "👀",
    "see": "👀", "watch": "👀", "listen": "👂", "hear": "👂", "music": "🎵", "song": "🎵", "phone": "📱", "computer": "💻",
    "machine": "🤖", "robot": "🤖", "ai": "🤖", "data": "📊", "numbers": "🔢", "science": "🔬", "doctor": "🩺", "medicine": "💊",
    "food": "🍔", "eat": "🍽️", "coffee": "☕", "car": "🚗", "house": "🏠", "home": "🏠", "city": "🏙️", "school": "🎓",
    "kids": "👶", "baby": "👶", "family": "👨‍👩‍👧", "friends": "🤝", "deal": "🤝", "win": "🏆", "winner": "🏆", "trophy": "🏆",
    "game": "🎮", "ball": "⚽", "goal": "🥅", "gift": "🎁", "party": "🎉", "celebrate": "🎉", "secret": "🤫", "quiet": "🤫",
    "truth": "✅", "true": "✅", "lie": "❌", "lies": "❌", "false": "❌", "stop": "🛑", "warning": "⚠️", "danger": "⚠️",
    "question": "❓", "why": "❓", "key": "🔑", "lock": "🔒", "hidden": "🕵️", "mystery": "🕵️", "clue": "🔍", "search": "🔍",
    "map": "🗺️", "travel": "✈️", "plane": "✈️", "ship": "🚢", "boat": "⛵", "mountain": "⛰️", "tree": "🌳", "forest": "🌲",
    "flower": "🌸", "animal": "🐾", "dog": "🐶", "cat": "🐱", "lion": "🦁", "snake": "🐍", "dragon": "🐉", "bird": "🐦",
    "chimpanzee": "🐵", "monkey": "🐵", "chimpanzees": "🐵", "sleep": "😴", "tired": "😴", "strong": "💪", "muscle": "💪",
    "gym": "🏋️", "run": "🏃", "fast": "💨", "slow": "🐢", "big": "🐘", "giant": "🦣", "giants": "🦣", "small": "🐜",
    "prophecy": "🔮", "future": "🔮", "prophet": "📜", "sign": "🪧", "signs": "🪧", "number": "🔢", "seven": "7️⃣",
    "hundred": "💯", "percent": "💯", "perfect": "💯", "boom": "💥", "explode": "💥", "bomb": "💣", "gun": "🔫",
    "police": "👮", "prison": "⛓️", "jail": "⛓️", "judge": "⚖️", "law": "⚖️", "court": "⚖️", "vote": "🗳️", "flag": "🏁",
    "america": "🇺🇸", "usa": "🇺🇸", "africa": "🌍", "china": "🇨🇳", "israel": "🇮🇱", "rome": "🏛️", "greece": "🏛️",
}


def hex_to_ass(hex_color: str, alpha: int = 0) -> str:
    """'#RRGGBB' or '#RRGGBBAA' -> '&HAABBGGRR&' (ASS uses BGR and alpha 00=opaque)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    if len(h) == 8:
        alpha = 255 - int(h[6:8], 16)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}&"


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def preset(name: str | None) -> dict:
    presets = cfg.get("captions.presets", {})
    name = name if name in presets else cfg.get("captions.default_preset", "bold_pop")
    p = dict(presets[name])
    p["name"] = name
    return p


def preset_names() -> list[dict]:
    return [{"id": k, "label": v.get("label", k)} for k, v in cfg.get("captions.presets", {}).items()]


@dataclass
class Line:
    words: list[dict]            # each: w, s, e (output time), key(bool), emoji(str|None)
    start: float
    end: float
    text_widths: list[float] = field(default_factory=list)
    space_w: float = 0.0
    width: float = 0.0


class Measurer:
    """Measures text the way libass will draw it. libass treats the font size as ascent+descent (VSFilter
    behaviour), so the em size it actually uses is smaller than `size` for most fonts."""

    def __init__(self, font_name: str, size: int):
        fp = FONTS_DIR / FONT_FILES.get(font_name, "Montserrat-ExtraBold.ttf")
        probe = ImageFont.truetype(str(fp), 1000)
        asc, desc = probe.getmetrics()
        ratio = (asc + desc) / 1000.0
        self.em = max(1, int(round(size / ratio)))
        self.font = ImageFont.truetype(str(fp), self.em)

    def width(self, text: str) -> float:
        return float(self.font.getlength(text))


def clean_word(w: str) -> str:
    return w.strip()


def clip_words(words: list[dict], tl: Timeline) -> list[dict]:
    """Words inside the timeline, with times mapped to output time."""
    out = []
    for w in words:
        inside = min(w["e"], tl.end) - max(w["s"], tl.start)
        if inside < 0.5 * max(0.04, w["e"] - w["s"]):
            continue  # less than half of the word is in the clip
        s = tl.to_output(w["s"])
        e = tl.to_output(w["e"])
        if s is None and e is None:
            continue
        if s is None:
            s = tl.to_output_clamped(w["s"])
        if e is None:
            e = tl.to_output_clamped(w["e"])
        if e - s < 0.04:
            e = s + 0.04
        out.append({**w, "s": round(s, 3), "e": round(e, 3)})
    return out


def group_lines(words: list[dict], measure: Measurer, max_w: float, upper: bool, max_words: int, min_words: int,
                gap_split: float, emoji_w: float = 0.0) -> list[Line]:
    lines: list[Line] = []
    cur: list[dict] = []

    def text(w):
        t = clean_word(w["w"])
        return t.upper() if upper else t

    def word_w(w):
        return measure.width(text(w)) + (emoji_w if w.get("emoji") else 0.0)

    def width_of(ws):
        return sum(word_w(w) for w in ws) + measure.width(" ") * max(0, len(ws) - 1)

    def flush():
        nonlocal cur
        if cur:
            lines.append(Line(cur, cur[0]["s"], cur[-1]["e"]))
            cur = []

    for w in words:
        if cur:
            too_many = len(cur) >= max_words
            pause = w["s"] - cur[-1]["e"] > gap_split
            sentence = bool(re.search(r"[.!?]$", clean_word(cur[-1]["w"])))
            wide = width_of(cur + [w]) > max_w
            if too_many or pause or sentence or wide:
                flush()
        cur.append(w)
    flush()
    # merge single-word orphans with the previous line when it fits and there was no pause
    merged: list[Line] = []
    for ln in lines:
        if merged and len(ln.words) < min_words:
            prev = merged[-1]
            if (len(prev.words) + len(ln.words) <= max_words and ln.start - prev.end <= gap_split
                    and width_of(prev.words + ln.words) <= max_w
                    and not re.search(r"[.!?]$", clean_word(prev.words[-1]["w"]))):
                prev.words += ln.words
                prev.end = ln.end
                continue
        merged.append(ln)
    for ln in merged:
        ln.text_widths = [word_w(w) for w in ln.words]
        ln.space_w = measure.width(" ")
        ln.width = sum(ln.text_widths) + ln.space_w * (len(ln.words) - 1)
    return merged


def choose_emojis(words: list[dict], every: float, key_words: set[str], suggestions: list[dict] | None = None) -> None:
    """Attach at most one emoji per `every` seconds to words (in place). Claude suggestions win over the word list."""
    last = -1e9
    sugg = {s.get("word", "").lower().strip(".,!?"): s.get("emoji") for s in (suggestions or []) if s.get("emoji")}
    for w in words:
        w["emoji"] = None
    for w in words:
        base = re.sub(r"[^a-zA-Z']", "", w["w"]).lower()
        if not base or w["s"] - last < every:
            continue
        em = sugg.get(base) or EMOJI_WORDS.get(base)
        if em:
            w["emoji"] = em
            last = w["s"]


def build_ass(words: list[dict], out_w: int, out_h: int, style: dict, ratio: str = "9:16",
              key_words: list[str] | None = None, y_frac: float | None = None) -> tuple[str, list[dict]]:
    """Returns (ass text, emoji overlays [{s, e, x, y, emoji, size}]). Words must already be in output time."""
    c = cfg.get("captions")
    scale = out_w / 1080.0 if ratio != "16:9" else out_h / 1080.0 * 0.85
    size = int(style["size"] * scale)
    upper = bool(style.get("uppercase"))
    measure = Measurer(style["font"], size)
    max_w = out_w * float(c.get("max_width_frac", 0.82))
    yf = c.get("y_frac", {})
    y_c = int(out_h * float(y_frac if y_frac else (yf.get(ratio, 0.66) if isinstance(yf, dict) else yf)))
    keys = {k.lower().strip(".,!?") for k in (key_words or [])}
    for w in words:
        w["key"] = re.sub(r"[^a-zA-Z']", "", w["w"]).lower() in keys
    em_size = int(size * 1.0)
    space_w = max(1.0, measure.width(" "))
    n_spacer = int(em_size / space_w + 0.999) + 1
    spacer = "\\h" * n_spacer
    emoji_w = n_spacer * space_w
    lines = group_lines(words, measure, max_w, upper, int(c.get("words_per_line_max", 4)),
                        int(c.get("words_per_line_min", 2)), float(c.get("gap_split", 0.6)), emoji_w=emoji_w)
    hold = float(c.get("hold_after", 0.25))
    for i, ln in enumerate(lines):
        nxt = lines[i + 1].start if i + 1 < len(lines) else ln.end + hold
        ln.end = min(ln.end + hold, max(ln.end, nxt - 0.02))

    primary = hex_to_ass(style["color"])
    highlight = hex_to_ass(style["highlight"])
    keyword = hex_to_ass(style.get("keyword", style["highlight"]))
    outline = float(style.get("outline", 0)) * scale
    shadow = float(style.get("shadow", 0)) * scale
    outline_c = hex_to_ass(style.get("outline_color", "#000000"))
    shadow_c = hex_to_ass(style.get("shadow_color", "#000000"), alpha=0x60)
    pop = int(style.get("pop", 108))
    box = style.get("box_color")
    backdrop = style.get("backdrop")
    glow = style.get("glow")
    dim = style.get("highlight_alpha_dim")

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {out_w}
PlayResY: {out_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{style['font']},{size},{primary},{primary},{outline_c},{shadow_c},0,0,0,0,100,100,0,0,1,{outline:.1f},{shadow:.1f},5,20,20,20,1
Style: CapBox,{style['font']},{size},{primary},{primary},{hex_to_ass(style.get('other_box_color', '#000000A0'))},{shadow_c},0,0,0,0,100,100,0,0,3,{int(12 * scale)},0,5,20,20,20,1
Style: Box,Arial,20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    overlays: list[dict] = []
    glow_tag = f"\\blur{6 * scale:.1f}\\bord{max(outline, 4 * scale):.1f}\\3c{hex_to_ass(style['highlight'])}" if glow else ""

    for ln in lines:
        x0 = out_w / 2 - ln.width / 2
        # word x ranges (for boxes and emoji)
        xs = []
        x = x0
        for tw in ln.text_widths:
            xs.append((x, x + tw))
            x += tw + ln.space_w
        # backdrop for the whole line (minimal preset)
        if backdrop:
            pad = 18 * scale
            events.append(_box_event(0, ln.start, ln.end, x0 - pad, y_c - size * 0.62, ln.width + 2 * pad, size * 1.3,
                                     backdrop, radius=14 * scale))
        for k, w in enumerate(ln.words):
            ws = w["s"] if k else ln.start
            we = ln.words[k + 1]["s"] if k + 1 < len(ln.words) else ln.end
            if we - ws < 0.02:
                continue
            first = k == 0
            parts = []
            for j, wj in enumerate(ln.words):
                t = clean_word(wj["w"])
                t = t.upper() if upper else t
                t = t.replace("{", "(").replace("}", ")")
                if wj.get("emoji"):
                    t = t + spacer
                if j == k:
                    col = keyword if wj["key"] and not box else highlight
                    if box:
                        col = highlight
                    anim = "" if first else f"\\fscx100\\fscy100\\t(0,80,\\fscx{pop}\\fscy{pop})"
                    boxtag = ""
                    if box:
                        bcol = hex_to_ass(style.get("keyword_box_color", box) if wj["key"] else box)
                        boxtag = f"\\3c{bcol}\\3a&H00&"
                    parts.append(f"{{\\c{col}{anim}{glow_tag}{boxtag}}}{t}{{\\r}}")
                else:
                    col = keyword if wj["key"] and not box else primary
                    if dim and j > k:
                        parts.append(f"{{\\c{col}\\alpha&H50&}}{t}{{\\r}}")
                    else:
                        parts.append(f"{{\\c{col}}}{t}{{\\r}}")
            intro = "\\fad(40,0)\\fscx92\\fscy92\\t(0,70,\\fscx100\\fscy100)" if first else ""
            text = " ".join(parts)
            sty = "CapBox" if box else "Cap"
            events.append(f"Dialogue: 1,{ass_time(ws)},{ass_time(we)},{sty},,0,0,0,,{{\\an5\\pos({out_w / 2:.0f},{y_c}){intro}}}{text}")
            if w.get("emoji"):
                # the word's measured width includes the spacer; the emoji sits in that gap
                gx = xs[k][1] - emoji_w + space_w * 0.5
                overlays.append({"s": ws, "e": ln.end, "x": int(gx), "y": int(y_c - em_size / 2),
                                 "emoji": w["emoji"], "size": em_size})
    return head + "\n".join(events) + "\n", overlays


def _box_event(layer: int, s: float, e: float, x: float, y: float, w: float, h: float, color_hex: str, radius: float = 10) -> str:
    col = hex_to_ass(color_hex)
    r = min(radius, w / 2, h / 2)
    # rounded rectangle as ASS drawing (bezier corners)
    d = (f"m {r:.0f} 0 l {w - r:.0f} 0 b {w:.0f} 0 {w:.0f} 0 {w:.0f} {r:.0f} l {w:.0f} {h - r:.0f} "
         f"b {w:.0f} {h:.0f} {w:.0f} {h:.0f} {w - r:.0f} {h:.0f} l {r:.0f} {h:.0f} b 0 {h:.0f} 0 {h:.0f} 0 {h - r:.0f} "
         f"l 0 {r:.0f} b 0 0 0 0 {r:.0f} 0")
    alpha = col[2:4]
    return (f"Dialogue: {layer},{ass_time(s)},{ass_time(e)},Box,,0,0,0,,{{\\an7\\pos({x:.0f},{y:.0f})\\c{col}\\1a&H{alpha}&"
            f"\\bord0\\shad0\\p1}}{d}{{\\p0}}")


# ----------------------------------------------------------------------------- emoji overlays (drawn per frame)
class EmojiOverlay:
    def __init__(self, overlays: list[dict]):
        self.items = []
        font_path = cfg.get("captions.emoji_font")
        try:
            font = ImageFont.truetype(font_path, 109)
        except Exception:
            font = None
        cache: dict[tuple[str, int], np.ndarray] = {}
        for o in overlays:
            if font is None:
                break
            key = (o["emoji"], o["size"])
            if key not in cache:
                im = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
                d = ImageDraw.Draw(im)
                try:
                    d.text((10, 10), o["emoji"], font=font, embedded_color=True)
                except Exception:
                    continue
                bb = im.getbbox()
                if not bb:
                    continue
                im = im.crop(bb).resize((o["size"], o["size"]), Image.LANCZOS)
                cache[key] = np.array(im)
            self.items.append({**o, "img": cache[key]})

    def __call__(self, frame: np.ndarray, t_src: float, t_out: float) -> np.ndarray:
        for it in self.items:
            if it["s"] <= t_out < it["e"]:
                # pop-in scale for the first 120 ms
                prog = min(1.0, (t_out - it["s"]) / 0.12)
                sc = 0.6 + 0.4 * prog
                img = it["img"]
                if sc < 1.0:
                    n = max(8, int(it["size"] * sc))
                    img = np.array(Image.fromarray(img).resize((n, n), Image.BILINEAR))
                _blend(frame, img, it["x"], it["y"] + (it["size"] - img.shape[0]) // 2)
        return frame


def _blend(frame: np.ndarray, rgba: np.ndarray, x: int, y: int):
    h, w = rgba.shape[:2]
    H, W = frame.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    src = rgba[y0 - y:y1 - y, x0 - x:x1 - x].astype(np.float32)
    a = src[:, :, 3:4] / 255.0
    rgb = src[:, :, :3][:, :, ::-1]  # RGBA -> BGR
    dst = frame[y0:y1, x0:x1].astype(np.float32)
    frame[y0:y1, x0:x1] = (dst * (1 - a) + rgb * a).astype(np.uint8)


def render_preview(style_name: str, out: Path, ratio: str = "9:16"):
    """A 540x960 PNG showing the preset on a dark gradient, used by the app's style picker."""
    from . import media
    w, h = 1080, 1920
    st = preset(style_name)
    words = [{"w": "This", "s": 0.0, "e": 0.3}, {"w": "is", "s": 0.3, "e": 0.5}, {"w": "how", "s": 0.5, "e": 0.8},
             {"w": "captions", "s": 0.8, "e": 1.3}, {"w": "look", "s": 1.3, "e": 1.6}]
    ass_text, _ = build_ass(words, w, h, st, ratio, key_words=["captions"])
    ass_path = out.with_suffix(".ass")
    ass_path.write_text(ass_text)
    media.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x1b1f2a:s={w}x{h}:d=1", "-ss", "0.85",
               "-vf", f"ass={ass_path}:fontsdir={FONTS_DIR}", "-frames:v", "1", str(out)])
    ass_path.unlink(missing_ok=True)
    # keep just the caption band so the style is readable at thumbnail size
    yc = int(h * float(cfg.get("captions.y_frac", {}).get("9:16", 0.66)))
    im = Image.open(out)
    im.crop((0, yc - 150, w, yc + 150)).resize((540, 150), Image.LANCZOS).save(out)
