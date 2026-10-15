"""NEON MANTRA - procedural cyberpunk x Indian-classical music video.

Reads out/neon_mantra.wav + out/timeline.json (from make_audio.py) and renders
a 9:16 vertical video (1080x1920, 30 fps, as long as the track) where every light, camera move and
dance step is locked to the track:
  neon gopuram + cyberpunk skyline, rotating neon mandala, synthwave floor grid,
  three Bharatanatyam/Kathak-style neon dancers (spins on the drop), jumping crowd
  with phone lights, laser sweeps, marigold particles, strobes on kicks,
  chromatic aberration, kick-pulsed zoom/shake/roll camera and on-screen hooks.

Usage: python3 make_video.py [--preview SECONDS]   (preview renders a few stills)
"""
import json
import math
import os
import subprocess
import sys
from multiprocessing import Pool

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
W, H = 720, 1280
TL = json.load(open(os.path.join(OUT, "timeline.json")))
FPS = TL["fps"]
BEAT = 60 / TL["bpm"]
BAR = 4 * BEAT
DUR = TL["duration"]
NFR = int(DUR * FPS)
KICKS = np.array([e["t"] for e in TL["events"] if e["type"] == "kick"])
HITS = np.array([e["t"] for e in TL["events"] if e["type"] in ("dhol", "clap", "snare", "tabla")])
SITAR = np.array([e["t"] for e in TL["events"] if e["type"] == "sitar"])
KEYS = np.array([e["t"] for e in TL["events"] if e["type"] == "keys"])
ACID = np.array([e["t"] for e in TL["events"] if e["type"] == "acid"])
RMS, LOW = np.array(TL["rms"]), np.array(TL["low"])
DROPS = [s["start"] for s in TL["sections"] if s["name"] == "drop"]
DROP = DROPS[0]
FIRST_BUILD = next(s["start"] for s in TL["sections"] if s["name"] == "build")

FONT_B = "/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf"
FONT_X = "/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf"
FONT_DEV = "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"
_fonts = {}


def font(path, size):
    if (path, size) not in _fonts:
        _fonts[(path, size)] = ImageFont.truetype(path, size)
    return _fonts[(path, size)]


# neon palette (RGB 0..1)
MAG = np.array([1.0, 0.15, 0.75])
CYA = np.array([0.1, 0.95, 1.0])
SAF = np.array([1.0, 0.55, 0.05])  # saffron / marigold
GOLD = np.array([1.0, 0.82, 0.3])
VIO = np.array([0.55, 0.2, 1.0])


def c255(c, k=1.0):
    return tuple(int(max(0, min(255, v * 255 * k))) for v in c)


def section(t):
    for s in TL["sections"]:
        if s["start"] <= t < s["end"]:
            return s["name"], (t - s["start"]) / (s["end"] - s["start"])
    return "outro", 1.0


def since(arr, t):
    i = np.searchsorted(arr, t, "right") - 1
    return t - arr[i] if i >= 0 else 99.0


def ease(x):
    return 0.5 - 0.5 * math.cos(math.pi * max(0, min(1, x)))


# ------------------------------------------------------------- static layers
def make_static():
    rng = np.random.default_rng(3)
    yy = np.linspace(0, 1, H)[:, None, None]
    bg = (1 - yy) * np.array([0.05, 0.01, 0.12]) + yy * np.array([0.12, 0.0, 0.16])
    bg = np.broadcast_to(bg, (H, W, 3)).astype(np.float32).copy()

    # skyline: 2 parallax layers, wider than the frame so the camera can pan
    sky = []
    for layer in range(2):
        SW = W + 200
        im = Image.new("RGBA", (SW, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        base = 840 if layer == 0 else 870
        x = -10
        col = (14, 6, 30, 255) if layer == 0 else (8, 3, 18, 255)
        while x < SW:
            bw = int(rng.integers(40, 95))
            bh = int(rng.integers(120, 380 if layer == 0 else 260))
            if abs(x + bw / 2 - SW / 2) < 140:  # leave room for the gopuram
                bh = int(bh * 0.5)
            d.rectangle([x, base - bh, x + bw, base], fill=col)
            for wy in range(base - bh + 10, base - 8, 14):
                for wx in range(x + 6, x + bw - 6, 11):
                    if rng.random() < 0.28:
                        wc = [(255, 40, 200), (40, 240, 255), (255, 160, 40), (150, 80, 255)][rng.integers(4)]
                        a = int(rng.integers(90, 230))
                        d.rectangle([wx, wy, wx + 4, wy + 6], fill=wc + (a,))
            if rng.random() < 0.3:
                d.line([x + bw // 2, base - bh, x + bw // 2, base - bh - 40], fill=(255, 40, 80, 255), width=2)
            x += bw + int(rng.integers(0, 12))
        if layer == 0:  # neon gopuram (temple tower) - India x cyberpunk
            cx = SW // 2
            tiers = 7
            for k in range(tiers):
                w0 = 230 - k * 26
                y1 = base - k * 52
                y0 = y1 - 52
                d.polygon([(cx - w0 // 2, y1), (cx + w0 // 2, y1), (cx + w0 // 2 - 12, y0), (cx - w0 // 2 + 12, y0)],
                          fill=(20, 6, 34, 255), outline=(255, 140, 30, 255))
                d.line([cx - w0 // 2 + 6, y1 - 26, cx + w0 // 2 - 6, y1 - 26], fill=(255, 40, 200, 200), width=2)
            top = base - tiers * 52
            d.ellipse([cx - 22, top - 34, cx + 22, top + 4], fill=(20, 6, 34, 255), outline=(255, 200, 60, 255), width=3)
            d.line([cx, top - 34, cx, top - 70], fill=(255, 200, 60, 255), width=3)
        sky.append(np.asarray(im).astype(np.float32) / 255)

    # mandala (grayscale, colorized + rotated per frame)
    M = 900
    mim = Image.new("L", (M, M), 0)
    d = ImageDraw.Draw(mim)
    c = M / 2
    for r, w in [(430, 4), (400, 2), (300, 3), (180, 3), (90, 3)]:
        d.ellipse([c - r, c - r, c + r, c + r], outline=255, width=w)
    for ring, (n, r0, r1, wid) in enumerate([(16, 300, 400, 0.18), (12, 180, 295, 0.25), (8, 90, 178, 0.35)]):
        for k in range(n):
            a = 2 * math.pi * k / n + (math.pi / n if ring % 2 else 0)
            pts = []
            for s in np.linspace(0, 1, 24):  # petal
                rr = r0 + (r1 - r0) * s
                aa = a + wid * math.sin(math.pi * s) * (1 - 0.3 * s)
                pts.append((c + rr * math.cos(aa), c + rr * math.sin(aa)))
            for s in np.linspace(1, 0, 24):
                rr = r0 + (r1 - r0) * s
                aa = a - wid * math.sin(math.pi * s) * (1 - 0.3 * s)
                pts.append((c + rr * math.cos(aa), c + rr * math.sin(aa)))
            d.line(pts + [pts[0]], fill=255, width=3)
    for k in range(48):
        a = 2 * math.pi * k / 48
        x, y = c + 415 * math.cos(a), c + 415 * math.sin(a)
        d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=255)
    for k in range(6):  # inner star
        a = 2 * math.pi * k / 6
        b = a + 2 * math.pi / 3
        d.line([c + 85 * math.cos(a), c + 85 * math.sin(a), c + 85 * math.cos(b), c + 85 * math.sin(b)], fill=255, width=3)
    d.ellipse([c - 18, c - 18, c + 18, c + 18], fill=255)

    # vignette + scanlines
    ys, xs = np.mgrid[0:H, 0:W]
    v = 1 - 0.55 * (((xs - W / 2) / (W * 0.75)) ** 2 + ((ys - H / 2) / (H * 0.7)) ** 2)
    v = np.clip(v, 0.2, 1) * (1 - 0.07 * (ys % 3 == 0))
    return bg, sky, mim, v[..., None].astype(np.float32)


BG, SKY, MANDALA, VIGN = make_static()
rngp = np.random.default_rng(11)
PETALS = [(rngp.random() * W, rngp.random() * H, 0.5 + rngp.random(), rngp.random() * 6.28, rngp.integers(3)) for _ in range(70)]
CROWD = sorted([(int(x), int(rngp.integers(1145, 1240)), 0.8 + 0.5 * rngp.random(), rngp.random() * 6.28, rngp.random() < 0.35)
                for x in np.linspace(-20, W + 20, 17) + rngp.normal(0, 14, 17)], key=lambda p: p[1])


# ------------------------------------------------------------------ dancers
# arm keyframes: (upper, fore) angles in degrees for (right arm, left arm); 0=right, 90=up
POSES = [
    ((5, 85), (175, 95)),     # both out, forearms up (classic natya pose)
    ((60, 130), (200, 260)),  # right arc overhead, left low
    ((-30, 150), (120, 50)),  # mirrored arc
    ((45, 60), (135, 120)),   # V hype pose
]


def limb(p, ang, ln):
    a = math.radians(ang)
    return (p[0] + ln * math.cos(a), p[1] - ln * math.sin(a))


def draw_dancer(dg, df, cx, feet_y, s, t, col, col2, phase_off, intensity):
    beat = t / BEAT + phase_off
    sec, _ = section(t)
    hop = abs(math.sin(math.pi * beat)) * 10 * s * intensity
    sway = math.sin(math.pi * beat) * 14 * s
    # spin on every 2nd bar of the drop / hook outro
    spin = 0.0
    bar_pos = (t / BAR) % 2
    if sec == "drop" and bar_pos > 1.5:
        spin = ease((bar_pos - 1.5) / 0.5) * 2 * math.pi
    cs = math.cos(spin)
    flare = abs(math.sin(spin)) if spin else 0.0
    kx = lambda dx: cx + sway + dx * (0.35 + 0.65 * abs(cs)) * (1 if cs >= 0 else -1)

    hip = (kx(0), feet_y - 150 * s - hop + 18 * s)  # aramandi: half-sitting
    chest = (kx(0), hip[1] - 78 * s)
    neck = (kx(0), chest[1] - 18 * s)
    head = (kx(0), neck[1] - 22 * s)
    lw = max(2, int(5 * s))

    # skirt (lehenga) - flares out while spinning
    sw0, sw1 = 22 * s, (62 + 70 * flare) * s
    sk_y = hip[1] + (80 - 25 * flare) * s
    skirt = [(hip[0] - sw0, hip[1]), (hip[0] + sw0, hip[1]), (hip[0] + sw1, sk_y), (hip[0] - sw1, sk_y)]
    df.polygon(skirt, fill=c255(col2, 0.35))
    dg.line(skirt + [skirt[0]], fill=c255(col2), width=lw)
    for k in range(7):  # pleats rotate with the spin
        u = ((k / 7) + spin / (2 * math.pi) + t * 0.2) % 1
        xx = -1 + 2 * u
        dg.line([(hip[0] + xx * sw0, hip[1]), (hip[0] + xx * sw1, sk_y)], fill=c255(GOLD, 0.8), width=max(1, lw // 2))
    # legs: knees out, feet stamping alternately on the beat (tatkar)
    stamp = int(beat) % 2
    lift = (1 - (beat % 1)) ** 3 * 18 * s * intensity
    for side in (-1, 1):
        knee = (kx(side * 48 * s), sk_y + 18 * s)
        fy = feet_y - (lift if (side == 1) == stamp else 0) - hop
        foot = (kx(side * 34 * s), fy)
        dg.line([knee, foot], fill=c255(col), width=lw)
        dg.line([foot, (foot[0] + side * 16 * s, fy)], fill=c255(col), width=lw)
        dg.ellipse([foot[0] - 4 * s, fy - 10 * s, foot[0] + 4 * s, fy - 2 * s], outline=c255(GOLD), width=2)  # ghungroo
    # torso
    torso = [(hip[0] - 20 * s, hip[1]), (chest[0] - 30 * s, chest[1]), (chest[0] + 30 * s, chest[1]), (hip[0] + 20 * s, hip[1])]
    df.polygon(torso, fill=c255(col, 0.25))
    dg.line(torso + [torso[0]], fill=c255(col), width=lw)
    dg.line([chest, neck], fill=c255(col), width=lw)
    # arms: interpolate between pose keyframes, faster in the drop
    rate = 1 if sec in ("drop", "groove") else 0.5
    pb = beat * rate
    p0, p1 = POSES[int(pb) % 4], POSES[(int(pb) + 1) % 4]
    f = ease((pb % 1) * 1.6)
    for side, sh in ((0, (chest[0] + 30 * s * cs, chest[1] + 4 * s)), (1, (chest[0] - 30 * s * cs, chest[1] + 4 * s))):
        ua = p0[side][0] + (p1[side][0] - p0[side][0]) * f
        fa = p0[side][1] + (p1[side][1] - p0[side][1]) * f
        if cs < 0:
            ua, fa = 180 - ua, 180 - fa
        el = limb(sh, ua, 52 * s)
        hand = limb(el, fa, 46 * s)
        dg.line([sh, el, hand], fill=c255(col), width=lw, joint="curve")
        dg.ellipse([hand[0] - 6 * s, hand[1] - 6 * s, hand[0] + 6 * s, hand[1] + 6 * s], outline=c255(SAF), width=2)  # mudra
    # head, bun, bindi, jhumkas
    r = 19 * s
    dg.ellipse([head[0] - r, head[1] - r, head[0] + r, head[1] + r], outline=c255(col), width=lw)
    bx = head[0] - 16 * s * cs
    dg.ellipse([bx - 9 * s, head[1] - 22 * s, bx + 9 * s, head[1] - 6 * s], outline=c255(col2), width=max(1, lw - 1))
    if cs > 0:
        dg.ellipse([head[0] - 3 * s, head[1] - 8 * s, head[0] + 3 * s, head[1] - 2 * s], fill=c255(np.array([1, 0.1, 0.2])))
    for side in (-1, 1):
        ex = head[0] + side * r * abs(cs)
        dg.ellipse([ex - 4 * s, head[1] + 8 * s, ex + 4 * s, head[1] + 16 * s], fill=c255(GOLD))


# -------------------------------------------------------------------- frame
def render(i):
    t = i / FPS
    sec, sp = section(t)
    fr = min(i, len(RMS) - 1)
    rms, low = RMS[fr], LOW[fr]
    dk = since(KICKS, t)
    kick = math.exp(-dk / 0.12) if dk < 1 else 0.0
    hit = math.exp(-since(HITS, t) / 0.08)
    sit = math.exp(-since(SITAR, t) / 0.15)
    keys = math.exp(-since(KEYS, t) / 0.2)
    acid = math.exp(-since(ACID, t) / 0.06)
    drop = sec == "drop"
    last_drop = max([d for d in DROPS if d <= t], default=-99.0)
    impact = math.exp(-(t - last_drop) / 0.6) if t >= last_drop else 0.0
    beat = t / BEAT
    hype = {"hook": 0.35, "groove": 0.75, "build": 0.6 + 0.4 * sp, "drop": 1.0, "break": 0.3, "outro": 0.6}[sec]

    # section color mood
    hue_cycle = [MAG, CYA, SAF, VIO]
    ca = hue_cycle[int(t / BAR) % 4] if drop else (SAF if sec == "hook" else MAG)
    cb = hue_cycle[(int(t / BAR) + 1) % 4] if drop else CYA

    img = BG * (0.8 + 0.4 * low)
    img = img + (ca * 0.06 * kick * hype)[None, None, :]
    # skyline (parallax pan)
    pan = math.sin(t * 0.25) * 60
    for layer, sk in enumerate(SKY):
        off = int(100 + pan * (0.4 if layer == 0 else 0.8))
        sl = sk[:, off:off + W]
        a = sl[..., 3:4]
        img = img * (1 - a) + sl[..., :3] * a * (0.85 + 0.4 * low)

    glow = Image.new("RGB", (W, H))
    dg = ImageDraw.Draw(glow)
    fill = Image.new("RGB", (W, H))
    df = ImageDraw.Draw(fill)

    # lasers
    if sec != "hook" or sp > 0.5:
        n = 8 if drop else 4
        for k in range(n):
            ox = W * (k + 0.5) / n
            ang = math.pi / 2 + math.sin(t * (1.3 if drop else 0.6) + k * 1.7) * 0.6
            ex, ey = ox + math.cos(ang) * 1600, -20 + math.sin(ang) * 1600
            col = (ca if k % 2 else cb)
            br = (0.35 + 0.65 * max(kick, 0.6 * acid)) * hype * (1.0 if drop else 0.6)
            dg.line([(ox, -20), (ex, ey)], fill=c255(col, br), width=3)

    # mandala
    mz = 0.72 + 0.08 * kick * hype + 0.25 * impact
    msz = int(900 * mz)
    rot = t * (24 if drop else 9)
    m = MANDALA.rotate(rot, resample=Image.BILINEAR).resize((msz, msz), Image.BILINEAR)
    mc = (ca * 0.6 + SAF * 0.4) * (0.35 + 0.5 * kick * hype + 0.3 * sit + 0.25 * keys)
    mcol = Image.merge("RGB", [m.point(lambda v, k=k: int(v * mc[k])) for k in range(3)])
    glow.paste(mcol, (W // 2 - msz // 2, 500 - msz // 2), m)

    # synthwave floor grid
    HZ = 860
    speed = beat * (1.0 if drop else 0.5)
    gcol = cb
    for k in range(14):
        z = 1 + k - (speed % 1)
        y = HZ + (H - HZ) * (1.0 / z) ** 1.1 * 1.4 - 30
        if HZ < y < H:
            dg.line([(0, y), (W, y)], fill=c255(gcol, (0.25 + 0.5 * kick) * min(1, (y - HZ) / 80)), width=2)
    for k in range(-12, 13):
        dg.line([(W / 2 + k * 18, HZ), (W / 2 + k * 160, H + 40)], fill=c255(gcol, 0.22 + 0.3 * kick), width=2)
    dg.line([(0, HZ), (W, HZ)], fill=c255(ca, 0.9), width=3)

    # dancers
    inten = 0.4 + 0.6 * hype
    draw_dancer(dg, df, 175, 1065, 0.82, t, cb, ca, 0.5, inten)
    draw_dancer(dg, df, 545, 1065, 0.82, t, cb, ca, 0.5, inten)
    draw_dancer(dg, df, 360, 1105, 1.12, t, SAF if not drop else ca, MAG if not drop else cb, 0.0, inten)

    # marigold petals / sparks
    for (px, py, sz, ph, ci) in PETALS:
        yy = (py + t * 90 * sz * (1.6 if drop else 1)) % (H + 40) - 20
        xx = px + math.sin(t * 1.5 + ph) * 20
        if impact > 0.01:  # burst outward at the drop
            dx, dy = xx - W / 2, yy - 600
            dd = math.hypot(dx, dy) + 1
            xx += dx / dd * impact * 260
            yy += dy / dd * impact * 260
        r = 3 + 3 * sz
        col = [SAF, GOLD, MAG][ci]
        df.ellipse([xx - r, yy - r * 0.6, xx + r, yy + r * 0.6], fill=c255(col, 0.6 + 0.4 * hit))

    g = np.asarray(glow).astype(np.float32) / 255
    fl = np.asarray(fill).astype(np.float32) / 255
    small = glow.resize((W // 4, H // 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(5))
    bloom = np.asarray(small.resize((W, H), Image.BILINEAR)).astype(np.float32) / 255
    small2 = glow.resize((W // 8, H // 8), Image.BILINEAR).filter(ImageFilter.GaussianBlur(6))
    bloom2 = np.asarray(small2.resize((W, H), Image.BILINEAR)).astype(np.float32) / 255
    img = img + fl + g * 1.1 + bloom * (1.6 + kick * hype) + bloom2 * 1.4

    # crowd (foreground silhouettes, rim-lit, phones up)
    crowd = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dc = ImageDraw.Draw(crowd)
    rim = Image.new("RGB", (W, H))
    dr = ImageDraw.Draw(rim)
    for (x, y, s, ph, phone) in CROWD:
        jump = abs(math.sin(math.pi * (beat + ph * 0.05))) * 22 * s * hype
        y = y - jump
        body = [x - 46 * s, y + 10 * s, x + 46 * s, y + 260 * s]
        dc.rounded_rectangle(body, radius=int(30 * s), fill=(6, 3, 12, 255))
        dc.ellipse([x - 22 * s, y - 40 * s, x + 22 * s, y + 8 * s], fill=(6, 3, 12, 255))
        dr.arc([x - 22 * s, y - 40 * s, x + 22 * s, y + 8 * s], 200, 340, fill=c255(ca, 0.7 + 0.3 * kick), width=2)
        if hype > 0.55:  # hands up
            for side in (-1, 1):
                wave = math.sin(math.pi * beat + ph + side) * 12 * s
                hx, hy = x + side * (55 * s + wave), y - (95 + 30 * hype) * s
                dc.line([(x + side * 34 * s, y + 25 * s), (hx, hy)], fill=(6, 3, 12, 255), width=int(15 * s))
                dc.ellipse([hx - 9 * s, hy - 9 * s, hx + 9 * s, hy + 9 * s], fill=(6, 3, 12, 255))
                if phone and side == 1:
                    dr.ellipse([hx - 5, hy - 22 * s - 5, hx + 5, hy - 22 * s + 5], fill=(255, 255, 240))
    ca_ = np.asarray(crowd).astype(np.float32) / 255
    a = ca_[..., 3:4]
    rimn = np.asarray(rim).astype(np.float32) / 255
    rimb = np.asarray(rim.resize((W // 4, H // 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(3)).resize((W, H))).astype(np.float32) / 255
    img = img * (1 - a) + ca_[..., :3] * a + rimn + rimb * 1.5

    # strobe on kicks in the drop, white-out on impact
    if drop:
        img = img + 0.22 * kick ** 3
    img = img + impact ** 2 * 0.9

    img = np.clip(img * VIGN, 0, 1)
    frame = Image.fromarray((img * 255).astype(np.uint8))

    # on-screen hooks (glow text)
    txt = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dt = ImageDraw.Draw(txt)

    def caption(text, y, size, col, fpath=FONT_X, wob=0.0):
        f = font(fpath, size)
        bb = dt.textbbox((0, 0), text, font=f, stroke_width=4)
        x = (W - (bb[2] - bb[0])) / 2 + wob
        dt.text((x, y), text, font=f, fill=(255, 255, 255, 255), stroke_width=4, stroke_fill=c255(col) + (255,))

    if t < 3.75:
        caption("POV: Indian classical", 150, 46, MAG)
        caption("meets TECHNO", 210, 64, SAF, wob=math.sin(t * 20) * 2 * kick)
    elif t < 7.5 or (sec == "outro" and sp < 0.75):
        glitch = (int(t * 12) % 7 == 0) * 6
        caption("NEON MANTRA", 150, 82, ca, wob=glitch)
        caption("नियॉन मंत्र", 250, 54, SAF, fpath=FONT_DEV)
    elif sec == "build":
        caption("WAIT FOR THE DROP", 150, 52, MAG, wob=math.sin(t * 30) * sp * 4)
        n = 4 - int(sp * 4)
        f = (sp * 4) % 1
        caption(str(n), 240, int(150 + 60 * (1 - f)), SAF)
    elif drop and t - last_drop < 1.2:
        caption("DROP", 200, int(200 - 60 * min(1, (t - last_drop) * 3)), ca)
    elif drop:
        caption("NEON MANTRA", 120, 54, ca)
    elif sec == "groove" and t < FIRST_BUILD:
        caption("sitar + tabla + dhol", 150, 44, CYA)
        caption("x 128 BPM techno", 210, 44, MAG)
    elif sec == "groove":
        caption("acid synth + wah-wah", 150, 44, CYA)
        caption("+ clipping", 210, 44, MAG)
    elif sec == "break":
        caption("keys + phaser + echo", 150, 44, VIO)
        caption("feel the mantra", 210, 40, SAF, fpath=FONT_B)
    if sec == "outro" and sp >= 0.75:
        caption("LOOP IT", 170, 90, SAF, wob=math.sin(t * 25) * 3)
        caption("save + share", 290, 40, CYA)
    tglow = txt.resize((W // 4, H // 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(4)).resize((W, H))
    frame.paste(Image.new("RGB", (W, H), c255(ca)), (0, 0), tglow.split()[3].point(lambda v: int(v * 0.8)))
    frame.paste(txt, (0, 0), txt)

    # camera: kick zoom pulse, drop whip-zoom, roll, shake
    zoom = 1.04 + 0.035 * kick * hype + 0.2 * impact + 0.02 * math.sin(t * 0.4)
    roll = math.radians((2.0 if drop else 0.8) * math.sin(t * 0.7) + (3 * impact * math.sin(t * 40)))
    shake = 7 * kick * (1 if drop else 0.3)
    srng = np.random.default_rng(i)
    sx, sy = srng.normal(0, shake), srng.normal(0, shake)
    cosr, sinr = math.cos(roll) / zoom, math.sin(roll) / zoom
    cx, cy = W / 2 + sx + math.sin(t * 0.3) * 10, H / 2 + sy
    coeffs = (cosr, sinr, cx - cosr * W / 2 - sinr * H / 2, -sinr, cosr, cy + sinr * W / 2 - cosr * H / 2)
    frame = frame.transform((W, H), Image.AFFINE, coeffs, resample=Image.BILINEAR)

    # chromatic aberration on drop kicks
    arr = np.asarray(frame).copy()
    ab = int(round((5 * kick if drop else 2 * kick) + 10 * impact))
    if ab:
        arr[..., 0] = np.roll(arr[..., 0], ab, 1)
        arr[..., 2] = np.roll(arr[..., 2], -ab, 1)
    return arr.tobytes()


def main():
    if "--preview" in sys.argv:
        ts = [float(x) for x in sys.argv[sys.argv.index("--preview") + 1].split(",")]
        for t in ts:
            Image.frombytes("RGB", (W, H), render(int(t * FPS))).save(os.path.join(OUT, f"preview_{t:05.1f}.png"))
        return
    final = os.path.join(OUT, "NEON_MANTRA_9x16.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-", "-i", os.path.join(OUT, "neon_mantra.wav"),
           "-vf", "scale=1080:1920:flags=lanczos", "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-maxrate", "12M", "-bufsize", "24M",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", "-shortest",
           "-metadata", "title=NEON MANTRA - Indian Techno (Sitar x Tabla x Dhol)",
           "-metadata", "artist=Neon Mantra", "-metadata", "genre=Indo Techno", final]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    with Pool(os.cpu_count()) as pool:
        for k, b in enumerate(pool.imap(render, range(NFR), chunksize=4)):
            p.stdin.write(b)
            if k % 150 == 0:
                print(f"frame {k}/{NFR}", flush=True)
    p.stdin.close()
    p.wait()
    # cover / thumbnail from the drop
    Image.frombytes("RGB", (W, H), render(int((DROP + 1.6) * FPS))).resize((1080, 1920), Image.LANCZOS).save(
        os.path.join(OUT, "cover_9x16.jpg"), quality=92)
    print("done", final)


if __name__ == "__main__":
    main()
