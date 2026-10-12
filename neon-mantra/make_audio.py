"""NEON MANTRA - Indo-techno track synthesizer.

128 BPM, 32 bars (exactly 60 s, loops seamlessly for Reels/Shorts/TikTok).
Scale: D Bhairav (D Eb F# G A Bb C) - the classic "Indian" sound.
Everything is synthesized from scratch with numpy/scipy:
  techno kick, rolling bass, hats, clap, supersaw lead, riser,
  Karplus-Strong sitar (with jawari buzz), tabla (na / tin / ghe / ka),
  dhol (dagga + tilli) in bhangra chaal, tanpura drone.

Writes out/neon_mantra.wav and out/timeline.json (beat/section map used by the video).
"""
import json
import os

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, sosfilt

SR = 44100
BPM = 128
BEAT = 60 / BPM
BAR = 4 * BEAT
STEP = BEAT / 4
BARS = 32
LEN = int(round(BARS * BAR * SR))
rng = np.random.default_rng(7)

OUT = os.path.join(os.path.dirname(__file__), "out")
os.makedirs(OUT, exist_ok=True)

# Sections (bar ranges) - hook first, drop at 0:30 (bar 16) for max retention
SECTIONS = [
    ("hook", 0, 4),     # sitar hook + tabla, filtered kick: grab in first second
    ("groove", 4, 12),  # full techno groove + dhol chaal
    ("build", 12, 16),  # riser, snare roll, "wait for it"
    ("drop", 16, 28),   # everything + supersaw lead doubling the sitar
    ("outro", 28, 32),  # back to the hook -> seamless loop
]


def section_of(bar):
    for name, a, b in SECTIONS:
        if a <= bar < b:
            return name
    return "outro"


def midi(n):
    return 440.0 * 2 ** ((n - 69) / 12)


# D Bhairav degrees as semitone offsets from D
D4 = 62
SC = {"S": 0, "r": 1, "G": 4, "M": 5, "P": 7, "d": 8, "n": 10, "S'": 12, "r'": 13, "G'": 16}


def lp(x, fc, order=2):
    return sosfilt(butter(order, min(fc, SR / 2 - 100) / (SR / 2), "low", output="sos"), x)


def hp(x, fc, order=2):
    return sosfilt(butter(order, fc / (SR / 2), "high", output="sos"), x)


def bp(x, lo, hi, order=2):
    return sosfilt(butter(order, [lo / (SR / 2), hi / (SR / 2)], "band", output="sos"), x)


def place(buf, sig, t, gain=1.0):
    i = int(round(t * SR))
    if i >= len(buf):
        return
    n = min(len(sig), len(buf) - i)
    buf[i:i + n] += sig[:n] * gain


def env_exp(n, tau):
    return np.exp(-np.arange(n) / (tau * SR))


# ---------------------------------------------------------------- instruments
def kick():
    n = int(0.45 * SR)
    t = np.arange(n) / SR
    f = 45 + 110 * np.exp(-t / 0.035)
    ph = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(ph) * np.exp(-t / 0.22)
    click = hp(rng.standard_normal(n), 3000) * np.exp(-t / 0.004) * 0.35
    return np.tanh(1.8 * (body + click))


def hat(open_=False):
    n = int((0.25 if open_ else 0.06) * SR)
    x = hp(rng.standard_normal(n), 8000, 4)
    return x * env_exp(n, 0.07 if open_ else 0.012) * 0.5


def clap():
    n = int(0.3 * SR)
    x = bp(rng.standard_normal(n), 900, 4000)
    e = np.zeros(n)
    for k, d in enumerate([0, 0.011, 0.022]):
        i = int(d * SR)
        e[i:] += env_exp(n - i, 0.006 if k < 2 else 0.09)
    return x * e * 0.8


def snare():
    n = int(0.18 * SR)
    t = np.arange(n) / SR
    tone = np.sin(2 * np.pi * 190 * t) * np.exp(-t / 0.03)
    noise = bp(rng.standard_normal(n), 1500, 9000) * np.exp(-t / 0.05)
    return 0.5 * tone + 0.7 * noise


def bass_note(f, dur, cutoff=900):
    n = int(dur * SR)
    t = np.arange(n) / SR
    saw = 2 * ((f * t) % 1) - 1
    sub = np.sin(2 * np.pi * f / 2 * t)
    e = np.exp(-t / 0.05)  # filter-envelope "pluck": crossfade bright -> dark
    x = (lp(saw, cutoff) * e + lp(saw, cutoff * 0.4) * (1 - e)) * 0.7 + sub * 0.6
    a = np.minimum(1, t / 0.004)
    return np.tanh(1.5 * x) * a * np.exp(-t / (dur * 0.7))


def supersaw(f, dur, bright=1.0):
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = np.zeros(n)
    for d in (-0.19, -0.11, -0.04, 0, 0.04, 0.11, 0.19):
        ff = f * 2 ** (d / 12)
        x += 2 * (((ff * t) + rng.random()) % 1) - 1
    x /= 7
    x = lp(x, 1500 + 6000 * bright, 2)
    a = np.minimum(1, t / 0.005) * np.minimum(1, (dur - t) / 0.02 + 0.0)
    return x * np.clip(a, 0, 1)


def sitar(f, dur, bend=0.0):
    """Karplus-Strong string + jawari buzz + sympathetic shimmer (+ optional meend)."""
    n = int(dur * SR)
    N = max(2, int(round(SR / f)))
    out = np.zeros(n + N + 1)
    out[:N + 1] = rng.uniform(-1, 1, N + 1) * np.hanning(N + 1) ** 0.3
    damp = 0.4985
    i = N + 1
    while i < len(out):
        j = min(i + N, len(out))
        out[i:j] = damp * (out[i - N:j - N] + out[i - N - 1:j - N - 1])
        i = j
    x = out[N + 1:N + 1 + n]
    if bend:  # meend: glide up by `bend` semitones over the note via resampling
        rate = 2 ** (np.linspace(0, bend, n) / 12)
        pos = np.cumsum(rate)
        pos = pos[pos < n - 1]
        x = np.interp(pos, np.arange(n), x)
        x = np.pad(x, (0, n - len(x)))
    buzz = np.tanh(6 * x) - np.tanh(x)  # jawari: bright nonlinear bridge buzz
    x = x + 0.55 * buzz
    x = x + 0.25 * bp(x, 2500, 7000)
    t = np.arange(n) / SR
    return x * np.exp(-t / 1.2) * 0.6


def tabla(kind):
    n = int(0.5 * SR)
    t = np.arange(n) / SR
    if kind == "na":  # dayan rim, bright ringing with harmonic overtones
        f0 = 520
        x = sum(np.sin(2 * np.pi * f0 * h * t) / h * np.exp(-t / (0.12 / h)) for h in (1, 2, 3, 4, 5))
        x += bp(rng.standard_normal(n), 2000, 8000) * np.exp(-t / 0.004) * 0.5
        return x * 0.5
    if kind == "tin":
        f0 = 520
        x = sum(np.sin(2 * np.pi * f0 * h * t) / h * np.exp(-t / (0.3 / h)) for h in (1, 2, 3))
        return x * 0.35
    if kind == "ghe":  # bayan with upward pitch glide (the iconic "wooo")
        f = 85 + 70 * (1 - np.exp(-t / 0.12))
        x = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.28)
        return x * 0.9
    if kind == "ka":
        return bp(rng.standard_normal(n), 300, 2500) * np.exp(-t / 0.015) * 0.6
    raise ValueError(kind)


def dhol(kind):
    n = int(0.5 * SR)
    t = np.arange(n) / SR
    if kind == "dagga":  # bass side: big boom
        f = 70 + 60 * np.exp(-t / 0.03)
        body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.18)
        slap = bp(rng.standard_normal(n), 200, 1200) * np.exp(-t / 0.02) * 0.5
        return np.tanh(1.6 * (body + slap))
    # tilli: high stick side, sharp crack
    x = bp(rng.standard_normal(n), 900, 6000) * np.exp(-t / 0.03)
    x += np.sin(2 * np.pi * 330 * t) * np.exp(-t / 0.05) * 0.5
    return x * 0.6


def tanpura(f, dur):
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = np.zeros(n)
    for h in range(1, 14):
        x += np.sin(2 * np.pi * f * h * t + rng.random() * 6) / h ** 1.1 * (1 + 0.3 * np.sin(2 * np.pi * 0.3 * h * t))
    return x * np.minimum(1, t / 0.3) * np.exp(-t / (dur * 0.8)) * 0.08


# ------------------------------------------------------------------- patterns
# Hook: 2-bar sitar phrase, 16th grid (None = rest). (degree, length_in_steps, meend)
HOOK = [
    [("S'", 2, 0), None, ("r'", 1, 0), ("S'", 1, 0), ("n", 2, 0), None, ("d", 2, 0), None, ("P", 3, 0), None, None, ("d", 1, 0), ("P", 2, 0), None, ("G", 2, 1), None],
    [("M", 2, 0), None, ("G", 1, 0), ("r", 1, 0), ("S", 4, 0), None, None, None, ("S", 1, 0), ("r", 1, 0), ("G", 1, 0), ("M", 1, 0), ("P", 2, 0), None, ("d", 2, -1), None],
]
BASS_STEPS = [0, 0, 1, 1] * 4  # offbeat rolling: kick on step 0 of each beat, bass on the other 3
BASS_ROOTS = ["S", "S", "d", "n"]  # per bar, D - D - Bb - C
DHOL_CHAAL = {0: "dagga", 3: "tilli", 6: "dagga", 8: "dagga", 11: "tilli", 14: "dagga"}  # bhangra
TABLA = {0: "ghe", 2: "na", 3: "na", 4: "tin", 6: "na", 7: "ka", 8: "ghe", 10: "na", 11: "ghe", 12: "tin", 14: "na", 15: "ka"}

tracks = {k: np.zeros(LEN) for k in ("kick", "bass", "drums", "sitar", "lead", "indian", "drone", "fx")}
events = []  # for the video sync
KICK, CLAP, SNARE = kick(), clap(), snare()

for bar in range(BARS):
    sec = section_of(bar)
    t0 = bar * BAR
    root = D4 - 24 + SC[BASS_ROOTS[bar % 4]]
    for s in range(16):
        t = t0 + s * STEP
        beat_step = s % 4 == 0
        # kick
        if beat_step and sec != "hook":
            if not (sec == "build" and bar == 15 and s >= 8):
                place(tracks["kick"], KICK, t)
                events.append({"t": t, "type": "kick"})
        if sec == "hook" and s in (0, 8) and bar >= 2:
            place(tracks["kick"], lp(KICK, 400), t, 0.7)
            events.append({"t": t, "type": "kick_soft"})
        # bass
        if sec in ("groove", "drop") and not beat_step:
            place(tracks["bass"], bass_note(midi(root), STEP * 0.9, 1400 if sec == "drop" else 800), t, 0.55)
        # hats / clap
        if sec in ("groove", "drop", "outro"):
            if s % 4 == 2:
                place(tracks["drums"], hat(True), t, 0.5)
            elif sec == "drop" or s % 2 == 1:
                place(tracks["drums"], hat(), t, 0.35 + 0.15 * (s % 2))
        if sec in ("groove", "drop") and s in (4, 12):
            place(tracks["drums"], CLAP, t, 0.6)
            events.append({"t": t, "type": "clap"})
        # dhol chaal
        if sec in ("groove", "drop", "build") and s in DHOL_CHAAL:
            k = DHOL_CHAAL[s]
            place(tracks["indian"], dhol(k), t, 0.7 if k == "dagga" else 0.55)
            events.append({"t": t, "type": "dhol"})
        # tabla
        if sec in ("hook", "groove", "outro", "build") and s in TABLA:
            place(tracks["indian"], tabla(TABLA[s]), t, 0.6)
            events.append({"t": t, "type": "tabla"})
        # sitar hook
        if sec != "build" or bar < 14:
            note = HOOK[bar % 2][s]
            if note:
                deg, ln, bend = note
                f = midi(D4 + 12 + SC[deg] - 12)
                place(tracks["sitar"], sitar(f, ln * STEP + 0.6, bend), t, 1.0)
                events.append({"t": t, "type": "sitar", "deg": deg})
                if sec == "drop":
                    place(tracks["lead"], supersaw(midi(D4 + SC[deg]), ln * STEP * 0.95, 1.0), t, 0.32)
                    place(tracks["lead"], supersaw(midi(D4 + 12 + SC[deg]), ln * STEP * 0.95, 0.6), t, 0.12)
    # snare roll in the build (accelerating)
    if sec == "build":
        div = {12: 4, 13: 8, 14: 16, 15: 32}[bar]
        for k in range(div):
            g = 0.15 + 0.5 * ((bar - 12) * div + k) / (4 * 32)
            place(tracks["drums"], SNARE, t0 + k * BAR / div, g)
            events.append({"t": t0 + k * BAR / div, "type": "snare"})
    # tanpura drone pluck pattern (Pa Sa Sa Sa) each bar
    for k, deg in enumerate(["P", "S'", "S'", "S"]):
        place(tracks["drone"], tanpura(midi(D4 - 12 + SC[deg]), BAR * 0.9), t0 + k * BEAT, 1.0)

# riser + impact into the drop
r0, r1 = 12 * BAR, 16 * BAR
n = int((r1 - r0) * SR)
tt = np.linspace(0, 1, n)
noise = rng.standard_normal(n)
riser = np.zeros(n)
for a in range(0, n, 2048):  # sweeping band-pass noise
    fc = 400 + 9000 * tt[a] ** 2
    seg = noise[max(0, a - 4096):a + 2048]
    riser[a:a + 2048] = bp(seg, fc * 0.7, min(fc * 1.4, 20000))[-len(riser[a:a + 2048]):]
riser *= tt ** 2 * 0.5
sweep = np.sin(2 * np.pi * np.cumsum(200 * 2 ** (tt * 4)) / SR) * tt ** 3 * 0.15
place(tracks["fx"], riser + sweep, r0)
imp_n = int(2.5 * SR)
it = np.arange(imp_n) / SR
impact = np.sin(2 * np.pi * np.cumsum(60 * np.exp(-it / 0.5) + 30) / SR) * np.exp(-it / 0.8)
impact += lp(rng.standard_normal(imp_n), 3000) * np.exp(-it / 0.6) * 0.4
place(tracks["fx"], impact, r1, 0.9)
events.append({"t": r1, "type": "impact"})

# filter the hook section kick/drone for the "muffled -> open" effect
hook_end = int(4 * BAR * SR)
tracks["drone"][:hook_end] = lp(tracks["drone"][:hook_end], 1200)

# sidechain pump keyed from the kick
key = np.abs(tracks["kick"])
win = int(0.01 * SR)
key = np.convolve(key, np.ones(win) / win, "same")
duck = 1 - 0.65 * np.clip(key / (key.max() + 1e-9) * 2.5, 0, 1)
duck = np.convolve(duck, np.ones(win * 3) / (win * 3), "same")

mix = (
    tracks["kick"] * 0.95
    + (tracks["bass"] * 0.9 + tracks["lead"] + tracks["drone"] + tracks["fx"] * 0.8) * duck
    + tracks["sitar"] * (0.55 + 0.45 * duck) * 0.75
    + tracks["indian"] * 0.8
    + tracks["drums"] * 0.7
)

# simple stereo: Haas widening on lead/sitar/drone, hats slightly right
wide = tracks["lead"] * duck + tracks["sitar"] * 0.3 + tracks["drone"] * duck
d = int(0.012 * SR)
L = mix + 0.25 * wide
R = mix + 0.25 * np.concatenate([np.zeros(d), wide[:-d]]) + 0.1 * tracks["drums"]
st = np.stack([L, R], 1)
st = hp(st.T, 28).T
st = np.tanh(1.3 * st / np.max(np.abs(st)) * 1.6) / np.tanh(1.3 * 1.6)  # glue + loudness
st *= 0.89 / np.max(np.abs(st))
# tiny fades so the 60 s loop is click-free
f = int(0.005 * SR)
st[:f] *= np.linspace(0, 1, f)[:, None]
st[-f:] *= np.linspace(1, 0, f)[:, None]
wavfile.write(os.path.join(OUT, "neon_mantra.wav"), SR, (st * 32767).astype(np.int16))

# per-video-frame energy (30 fps) for audio-reactive visuals
FPS = 30
hop = SR // FPS
mono = st.mean(1)
nfr = LEN // hop
rms = np.array([np.sqrt(np.mean(mono[i * hop:(i + 1) * hop] ** 2)) for i in range(nfr)])
lowb = lp(mono, 150)
low = np.array([np.sqrt(np.mean(lowb[i * hop:(i + 1) * hop] ** 2)) for i in range(nfr)])
json.dump({
    "bpm": BPM, "bars": BARS, "duration": LEN / SR, "fps": FPS,
    "sections": [{"name": s, "start": a * BAR, "end": b * BAR} for s, a, b in SECTIONS],
    "events": sorted(events, key=lambda e: e["t"]),
    "rms": (rms / rms.max()).round(4).tolist(),
    "low": (low / low.max()).round(4).tolist(),
}, open(os.path.join(OUT, "timeline.json"), "w"))
print("wrote", LEN / SR, "s")
